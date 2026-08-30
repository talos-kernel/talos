"""Stehende Freigaben — des Betreibers „immer" für **genau diese eine** Handlung.

Eine stehende Freigabe ist **keine zweite Erlaubnisquelle**. Sie tut ausschliesslich
das, was des Betreibers getipptes „ja" heute tut: sie setzt `human_approved=True` für einen
**neuen** Kernel-Durchlauf. Der Kernel entscheidet danach genauso wie beim ersten Mal —
und `Executor.run` liest `human_approved` erst, nachdem ein DENY längst zurückgekehrt
ist. **DENY bleibt DENY.** Auch der Autonomie-Regler bleibt davor: auf den Stufen 0–2
wird Wirkung zu DENY, und ein DENY kommt an dieser Abkürzung nicht vorbei, weil sie
gar nicht erst erreicht wird.

Was gespart wird, ist die *Frage*, nicht die *Prüfung*.

**Der Abdruck ist die ganze Sicherheit.** Er muss auf genau eine Handlung passen:

- Datei-Werkzeuge: Tool + die **kernel-abgeleiteten** Ziele (`guard_targets`). Was das
  Modell als `targets` deklariert, geht bewusst nicht ein — sonst könnte eine gefälschte
  Deklaration den Abdruck verschieben.
- `run_shell`: der **exakte** Command-String. Kein Muster, kein Prefix, keine
  Normalisierung. „date" deckt „date --utc" nicht ab und „date; rm -rf /tmp/x" erst
  recht nicht — ein Zeichen Unterschied ist eine andere Handlung.

**Was der Abdruck bewusst NICHT enthält:** den zu schreibenden Inhalt. Eine stehende
Freigabe für `write_file ~/notes.md` gilt für jeden künftigen Inhalt dieser Datei. Das
ist die Bindung, die the operator erteilt hat („diese Datei darfst du schreiben"), und `/allowed`
sagt es genau so — verschwiegen wäre es eine Falle.

**Persistenz über den Event-Log**, kein zweites Config-File: `approval.standing` beim
Anlegen, `approval.standing_revoked` beim Widerruf. Beim Start wird der Stand daraus
neu gespielt (`restore`) — dasselbe Muster wie `autonomy.restore_level`. Kommt das Log
nicht, gibt es keine stehenden Freigaben: der Ausfall macht zu, nicht auf.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .eventlog import Event, EventLog
from .policy import ToolRequest, guard_targets

# Wie weit `restore` ins Log zurücksieht. Wer mehr stehende Freigaben angehäuft hat,
# verliert die ältesten beim Neustart — das ist die sichere Richtung.
RESTORE_LIMIT = 500
LABEL_MAX = 160


def action_key(req: ToolRequest) -> str | None:
    """sha256 über (Tool, exaktes Material) — oder None, wenn nichts Exaktes da ist.

    None heisst: diese Handlung lässt sich nicht sauber binden (Shell ohne Kommando,
    Werkzeug ohne ableitbares Ziel). Dann gibt es keine stehende Freigabe. Lieber
    einmal mehr fragen als eine Regel, die auf mehr passt als gemeint.
    """
    if req.tool == "run_shell":
        command = req.args.get("command")
        if not isinstance(command, str) or not command:
            return None
        material: tuple[str, ...] = ("command", command)
    elif req.tool == "remote_exec":
        # Fern wirkt doppelt: WO (host) und WAS (command). Eine Regel, die nur
        # das Kommando bande, gaelte still fuer jede Maschine in der Allowlist —
        # „uptime" auf dem Pi und „uptime" auf dem Produktivserver sind nicht
        # dieselbe Handlung. Beide Felder gehoeren in den Abdruck, exakt.
        host = req.args.get("host")
        command = req.args.get("command")
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(command, str)
            or not command
        ):
            return None
        material = ("host", host, "command", command)
    elif req.tool == "http_request":
        # Methode UND Adresse: ein „GET registriert" sagt nichts ueber POST auf
        # denselben Endpunkt. Der Body gehoert bewusst NICHT in den Abdruck —
        # dieselbe Bindung wie bei write_file: „diese Adresse mit dieser Methode
        # darfst du schreiben", unabhaengig vom jeweiligen Inhalt.
        method = req.args.get("method")
        url = req.args.get("url")
        if not isinstance(url, str) or not url:
            return None
        material = ("method", str(method or "GET").upper(), "url", url)
    elif req.tool == "git":
        # Op, Repo UND Remote: „clone von X" darf nie „push nach X" decken,
        # und derselbe Op auf einem anderen Repo ist eine andere Handlung.
        op = req.args.get("op")
        repo = req.args.get("repo")
        if not isinstance(op, str) or not op or not isinstance(repo, str) or not repo:
            return None
        material = ("op", op, "repo", repo, "url", str(req.args.get("url") or ""))
    else:
        targets = guard_targets(req)
        if not targets:
            return None
        material = ("targets", *targets)
    canonical = json.dumps([req.tool, list(material)], ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def action_label(req: ToolRequest) -> str:
    """Menschenlesbar für `/allowed` — gekürzt, aber nie beschönigt."""
    if req.tool == "run_shell":
        body = str(req.args.get("command", ""))
    elif req.tool == "remote_exec":
        body = f"{req.args.get('host', '')}: {req.args.get('command', '')}"
    elif req.tool == "http_request":
        body = f"{str(req.args.get('method') or 'GET').upper()} {req.args.get('url', '')}"
    elif req.tool == "git":
        teile = f"{req.args.get('op', '')} {req.args.get('repo', '')}"
        url = str(req.args.get("url") or "")
        body = f"{teile} {url}".strip()
    else:
        body = ", ".join(guard_targets(req))
    text = f"{req.tool} {body}".strip()
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1] + "…"


@dataclass(frozen=True)
class Standing:
    """Eine stehende Freigabe. Gebunden an Chat, Person und genau eine Handlung."""

    key: str
    conversation: str
    principal: str
    tool: str
    label: str
    created_at: float

    def payload(self) -> dict:
        return {
            "key": self.key,
            "conversation": self.conversation,
            "principal": self.principal,
            "tool": self.tool,
            "label": self.label,
            "created_at": self.created_at,
        }


def _from_payload(payload: dict) -> Standing | None:
    """Baut eine Regel aus einem Log-Eintrag — oder None, wenn etwas fehlt.

    Streng, weil das Log auch alte oder halbe Einträge enthalten kann: was sich nicht
    vollständig lesen lässt, wird keine Regel.
    """
    try:
        key = str(payload["key"])
        conversation = str(payload["conversation"])
        principal = str(payload["principal"])
        tool = str(payload["tool"])
    except (KeyError, TypeError):
        return None
    if not (key and conversation and principal and tool):
        return None
    return Standing(
        key=key,
        conversation=conversation,
        principal=principal,
        tool=tool,
        label=str(payload.get("label", tool)),
        created_at=float(payload.get("created_at", 0.0) or 0.0),
    )


class StandingStore:
    """Hält die stehenden Freigaben — im Speicher, belegt im Log.

    Derselbe RLock-Grund wie beim `ApprovalStore`: Poll-Thread (Kommandos, „immer")
    und Worker-Thread (Denken, Parken) greifen beide zu.
    """

    def __init__(self, log: EventLog | None = None, clock: Callable[[], float] = time.time) -> None:
        self._rules: dict[tuple[str, str, str], Standing] = {}
        self._log = log
        self._clock = clock
        self._lock = threading.RLock()

    # --- anlegen / nachschlagen ---------------------------------------------------
    def grant(
        self, conversation: str, req: ToolRequest, *, principal: object, run_id: str
    ) -> Standing | None:
        """Legt die Regel an und belegt sie im Log. None = nicht bindbar, nichts angelegt."""
        key = action_key(req)
        if key is None:
            return None
        rule = Standing(
            key=key,
            conversation=conversation,
            principal=str(principal),
            tool=req.tool,
            label=action_label(req),
            created_at=self._clock(),
        )
        self._put(rule)
        if self._log is not None:
            self._log.append(Event(run_id, "human", "approval.standing", rule.payload()))
        return rule

    def find(self, conversation: str, req: ToolRequest, *, principal: object) -> Standing | None:
        """Die Regel für exakt diese Handlung in diesem Chat von dieser Person — oder None."""
        key = action_key(req)
        if key is None:
            return None
        with self._lock:
            return self._rules.get((conversation, str(principal), key))

    def list(self, conversation: str, *, principal: object) -> tuple[Standing, ...]:
        """Alle Regeln dieses Chats/dieser Person, stabil sortiert — `/revoke <nr>` zählt hier."""
        who = str(principal)
        with self._lock:
            found = [
                rule
                for (chat, person, _key), rule in self._rules.items()
                if chat == conversation and person == who
            ]
        return tuple(sorted(found, key=lambda rule: (rule.created_at, rule.key)))

    # --- widerrufen ----------------------------------------------------------------
    def revoke(
        self, conversation: str, index: int, *, principal: object, run_id: str
    ) -> Standing | None:
        """Widerruft die `index`-te Regel (1-basiert) — oder None, wenn es sie nicht gibt."""
        rules = self.list(conversation, principal=principal)
        if index < 1 or index > len(rules):
            return None
        rule = rules[index - 1]
        self._drop(rule)
        if self._log is not None:
            self._log.append(Event(run_id, "human", "approval.standing_revoked", rule.payload()))
        return rule

    # --- intern (auch von `restore` benutzt) ---------------------------------------
    def _put(self, rule: Standing) -> None:
        with self._lock:
            self._rules = {**self._rules, (rule.conversation, rule.principal, rule.key): rule}

    def _drop(self, rule: Standing) -> None:
        gone = (rule.conversation, rule.principal, rule.key)
        with self._lock:
            self._rules = {k: v for k, v in self._rules.items() if k != gone}


def restore(log: EventLog, *, limit: int = RESTORE_LIMIT, clock: Callable[[], float] = time.time) -> StandingStore:
    """Spielt die stehenden Freigaben aus dem Log nach — wie `autonomy.restore_level`.

    Chronologisch, damit „erteilt → widerrufen → wieder erteilt" richtig endet. Fällt
    das Lesen aus, kommt ein leerer Store zurück: ohne Beleg keine stehende Freigabe.
    """
    store = StandingStore(log, clock=clock)
    try:
        rows = log.recent(limit, ("approval.standing", "approval.standing_revoked"))
    except Exception:
        return StandingStore(log, clock=clock)
    for row in rows:
        rule = _from_payload(row.get("payload") or {})
        if rule is None:
            continue
        if row.get("type") == "approval.standing":
            store._put(rule)
        else:
            store._drop(rule)
    return store
