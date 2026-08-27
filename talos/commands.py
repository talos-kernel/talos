"""Deterministische Slash-Kommandos — die Steuerkonsole für the operator.

Kein LLM. Kein Reasoner. Jedes Kommando ist eine reine Funktion über den Zustand
(Event-Log, Approval-Store, Worker, Policy-Kernel). Das ist Absicht: die Knöpfe,
mit denen man einen Agenten *anhält* oder *zurückrollt*, dürfen nicht davon abhängen,
dass ein Modell die Absicht richtig versteht — genau dann versteht es sie nämlich nicht.

Drei Lücken schließen die Kommandos:
  - **Abbruch** (`/stop`): der Reasoner ist ein Subprozess mit 180 s Timeout. Ohne
    Worker-Thread wurde Telegram währenddessen gar nicht gepollt — ein Abbruch kam
    frühestens nach dem Lauf an, den er abbrechen sollte.
  - **Rückwärtsgang** (`/undo`): Snapshots gab es schon, aber keinen Knopf daran.
  - **Nachvollziehen** (`/log`, `/pending`, `/policy`): das Event-Log war belegbar,
    aber nur per SSH lesbar.

`/undo` läuft NICHT am Kernel vorbei: es baut eine `undo_last`-Anfrage, die der
Conductor durch denselben Executor schickt wie jede andere Schreib-Aktion. Ein
Rückrollen auf `~/.bashrc` fragt den Betreiber also genauso wie ein Schreiben dorthin.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from . import autonomy
from .approval import ApprovalStore
from .channel import Principal, StructuredMessage
from .autonomy import AutonomyError, AutonomyGovernor
from .channel import ChannelRegistry
from .eventlog import Event, EventLog, new_run_id
from .identity import agent_name
from .memory import MAX_CHARS, MAX_TURNS, Memory
from .policy import PolicyKernel, ToolRequest, command_risk_paths, guard_targets
from .provider import ModelPicker
from .reasoner import DISALLOWED_TOOLS_ARGV
from .standing import StandingStore
from .usage import UsageMeter

# Ab wie vielen Cache-Token pro Lauf `/usage` den Hinweis auf den geerbten Agenten-Kontext
# zeigt. Darunter ist es normales Prompt-Caching, darueber traegt Talos fremdes Gepaeck.
CACHE_HINT_TOKENS = 20_000
VERSION_PROBE_TIMEOUT_S = 10

LOG_DEFAULT = 10
LOG_MAX = 50
UNDO_LOOKBACK = 50
SUMMARY_MAX = 200

# Was in `/log` auftaucht: alles, was eine Wirkung hatte oder eine Entscheidung war.
# Poll-Rauschen (task.received, reply.sent, done) bleibt draußen — es steht weiter im Log.
AUDIT_TYPES: tuple[str, ...] = (
    "exec.intent",
    "grant.issued",
    "exec.result",
    "approval.parked",
    "approval.granted",
    "approval.denied",
    "approval.stale",
    "approval.standing",
    "approval.standing_revoked",
    "approval.standing_used",
    "task.rejected",
    "snapshot.taken",
    "autonomy.set",
    "command",
)


class Cancellable(Protocol):
    def cancel(self) -> bool: ...


class Queueing(Protocol):
    def pending(self) -> int: ...
    def busy(self) -> bool: ...
    def drain(self) -> int: ...


class Minting(Protocol):
    def stats(self) -> dict[str, int]: ...


@dataclass(frozen=True)
class CommandResult:
    """Ergebnis eines Kommandos.

    `reply` geht direkt raus. `forward_as` lässt den Conductor den Text so behandeln,
    als hätte the operator ihn geschrieben (für /approve -> „ja"). `request` ist eine fertige
    Tool-Anfrage, die der Conductor durch den Executor schickt (für /undo).
    """

    reply: str | None = None
    forward_as: str | None = None
    request: ToolRequest | None = None
    structured: StructuredMessage | None = None
    # `/background <auftrag>`: der Conductor startet daraufhin einen Lauf NEBEN dem
    # laufenden Gespraech. Ein eigenes Feld statt `forward_as`, weil der Unterschied
    # genau der Punkt ist — `forward_as` behandelt den Text, als haette der Betreiber ihn
    # gerade getippt (also im Vordergrund, mit Verlauf, mit Rueckfragen). Hier ist beides
    # ausdruecklich nicht so.
    background: str | None = None


HELP = """{name} — commands

Control
/stop — abort the running thought and clear the queue
/queue — what is running, what is waiting
/status — runtime, queue, pending approval
/new — clear the active context (log and searchable archive stay)
/retry — ask the last question again
/background <task> (also /bg, /btw) — run it beside this conversation; unattended, so anything
  needing approval is refused. The result arrives as its own message.

Approval
/pending — show the pending approval verbatim
/approve — same as "yes"
/deny — same as "no"
/allowed — standing approvals of this chat ("always"), numbered
/revoke <n> — take back one standing approval

Accountability
/log [n] — the last n events that had effect (max 50)
/undo — roll back the last successful file change
/policy <path|command> — dry run: what would the kernel say?
/autonomy [0-5] — how long the leash is; without a number it only shows
/remember <text> — keep something across restarts
/memory — what is kept · /forget <id|all> — drop it
/every <min> or <M H DOM MON DOW> <task> — recurring job · /schedules · /unschedule <id>
/blueprints — installable automations with plain-language schedules
  (/blueprint install|remove|enable|disable|status <name>)
/skills — what is loaded, what was refused
/tools — which tools exist and how they are gated
/whoami — your ID and whether it is allowed
/version — git state of the running code

Inside view
/usage — runs, tokens, thinking time, computed cost
/model — show the provider/model or select one safely
/reasoning — how thinking happens, and what deliberately does not exist here
/debug — state worth looking at: paths, permissions, counters
/health — the traffic light: what runs, what stumbled lately, what is quiet

Shell runs inside a sandbox (workspace-only writes, no network). The command floor still
decides first: catastrophic is refused outright, risky asks you. Shell runs have no undo."""


def is_command(text: str) -> bool:
    return text.strip().startswith("/")


def parse(text: str) -> tuple[str, str]:
    """„/log@Talos_bot 5" -> („log", „5"). Unbekanntes gibt („", "") back."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", ""
    head, _, rest = stripped[1:].partition(" ")
    name, _, _bot = head.partition("@")  # Gruppen hängen @Botname an
    return name.strip().lower(), rest.strip()


@dataclass(frozen=True)
class CommandCenter:
    """Führt Slash-Kommandos aus. Alle Abhängigkeiten injiziert (testbar ohne Telegram)."""

    log: EventLog
    approvals: ApprovalStore
    policy: PolicyKernel
    started_at: float
    bot_username: str
    reasoner: Cancellable
    worker: Queueing
    repo_dir: Path
    mint: Minting | None = None
    governor: AutonomyGovernor | None = None
    memory: Memory | None = None
    usage: UsageMeter | None = None
    # Schreibender Zugang zum Langzeitgedaechtnis — ausschliesslich hier, im
    # Kommando-Pfad, an die Identitaet des Betreibers gebunden und im Log belegt.
    recall: object | None = None
    channels: ChannelRegistry | None = None
    claude_bin: str = ""
    reasoner_timeout_s: int = 0
    eventlog_db: Path | None = None
    snapshot_dir: Path | None = None
    # Das Gespraechsarchiv — nur fuer `/debug` (Pfad, Groesse, Zeilenzahl). Kein
    # Loesch-Hebel hier: es ist ein automatisches, unkuratiertes Archiv wie der
    # Event-Log, nicht das kuratierte Recall mit seiner Loeschtaste.
    transcript: object | None = None
    transcript_db: Path | None = None
    # Zeitplaene. Angelegt/gelesen NUR hier im Kommando-Pfad, an die Identitaet des
    # Betreibers gebunden — es gibt bewusst kein Werkzeug dafuer: ein Modell, das sich
    # selbst einen wiederkehrenden Auftrag anlegen kann, verlaengert seine eigene Leine.
    schedules: object | None = None
    # Blueprints (talos/blueprints.py) — installierbare Automatisierungen mit
    # menschenlesbarer Zeitangabe. Dasselbe Argument wie bei den Zeitplaenen: NUR der
    # Kommando-Pfad installiert, und ein installierter Blueprint laeuft ueber denselben
    # ScheduleStore wie `/every` — Kernel und UnattendedCeiling inklusive.
    blueprints: object | None = None
    # Woher die Skills kommen. Leer heisst: keine geladen — dann sagt `/skills` das auch,
    # statt eine leere Liste zu zeigen, die nach „kaputt" aussieht.
    skills_dirs: tuple[Path, ...] = ()
    standing: StandingStore | None = None
    start_status: Callable[[], Mapping[str, object]] | None = None
    model_picker: ModelPicker | None = None

    def dispatch(self, name: str, rest: str, *, principal: Principal, conversation: str) -> CommandResult:
        if name == "start":
            return CommandResult(reply=self._start())
        if name in ("help", "commands"):
            return CommandResult(reply=HELP.format(name=agent_name()))
        if name == "status":
            return CommandResult(reply=self._status(conversation))
        if name == "queue":
            return CommandResult(reply=self._queue())
        if name in ("new", "reset") or (name == "forget" and not rest.strip()):
            # `/forget` ohne Argument bleibt das Synonym fuer `/new` (Verlaufs-Reset).
            # MIT Argument faellt es unten zum Recall-Loeschen durch — vorher fing
            # dieser Zweig JEDES `/forget` ab und machte `/forget <id|all>` (das der
            # HELP-Text seit jeher verspricht) zu totem Code.
            return CommandResult(reply=self._new(conversation))
        if name == "retry":
            return self._retry(conversation)
        if name in ("background", "bg", "btw"):
            # Nur weitergereicht, nicht ausgefuehrt: der Lauf gehoert dem Conductor, weil
            # nur dort Kernel, Decke und Zustellweg zusammenkommen. Ein Kommando, das
            # selbst einen Agenten startet, waere ein zweiter Ausfuehrungspfad.
            from .background import EMPTY

            auftrag = rest.strip()
            return (CommandResult(background=auftrag) if auftrag
                    else CommandResult(reply=EMPTY))
        if name in ("stop", "cancel"):
            return CommandResult(reply=self._stop())
        if name == "approve":
            return CommandResult(forward_as="yes")
        if name == "deny":
            return CommandResult(forward_as="no")
        if name == "pending":
            return CommandResult(reply=self._pending(conversation))
        if name == "allowed":
            return CommandResult(reply=self._allowed(conversation, principal=principal))
        if name == "revoke":
            return CommandResult(reply=self._revoke(rest, conversation, principal=principal))
        if name == "log":
            return CommandResult(reply=self._log(rest))
        if name == "undo":
            return self._undo(principal=principal, conversation=conversation)
        if name == "remember":
            return CommandResult(reply=self._remember(rest, principal, conversation))
        if name in ("memory", "memories"):
            return CommandResult(reply=self._memory())
        if name == "forget":
            return CommandResult(reply=self._forget(rest))
        if name in ("every", "schedule"):
            return CommandResult(reply=self._every(rest, principal, conversation))
        if name in ("schedules", "jobs"):
            return CommandResult(reply=self._schedules(conversation))
        if name in ("unschedule", "cancel_job"):
            return CommandResult(reply=self._unschedule(rest, conversation))
        if name in ("blueprint", "blueprints"):
            return CommandResult(reply=self._blueprints(rest, principal, conversation))
        if name == "skills":
            return CommandResult(reply=self._skills())
        if name == "tools":
            return CommandResult(reply=self._tools())
        if name == "autonomy":
            return CommandResult(reply=self._autonomy(rest, principal=principal))
        if name == "policy":
            return CommandResult(reply=self._policy(rest, principal))
        if name == "whoami":
            return CommandResult(reply=self._whoami(principal, conversation))
        if name == "version":
            return CommandResult(reply=self._version())
        if name == "usage":
            return CommandResult(reply=self._usage())
        if name == "model":
            if self.model_picker is None:
                return CommandResult(reply=self._model())
            message = (
                self.model_picker.select_typed(rest, principal=principal)
                if rest
                else self.model_picker.open(principal=principal, conversation=conversation)
            )
            return CommandResult(structured=message)
        if name == "reasoning":
            return CommandResult(reply=self._reasoning())
        if name == "debug":
            return CommandResult(reply=self._debug(conversation))
        if name == "health":
            return CommandResult(reply=self._health(conversation))
        return CommandResult(reply=f"Unknown command /{name}. /help lists them all.")

    # --- Steuerung ---------------------------------------------------------------
    def _start(self) -> str:
        """Persönliche Begrüßung mit belegten Fakten statt der Diagnose-Konsole."""
        facts: Mapping[str, object] = {}
        unavailable = False
        if self.start_status is not None:
            try:
                supplied = self.start_status()
                facts = supplied if isinstance(supplied, Mapping) else {}
            except Exception:
                unavailable = True

        # Der Name kommt aus SOUL.md, nicht aus dieser Zeile: eine zweite Stelle, an der er
        # ausgeschrieben steht, ist genau die, die beim Umbenennen vergessen wird — und
        # dann stellt sich der Waechter mit einem Namen vor, den er nicht mehr traegt.
        lines = [f"Hallo 👋 — {agent_name()} hier.", "", "**Kurzstatus**", "- Dienst: ✅ aktiv"]
        rows = (
            ("location", "Läuft auf"),
            ("model", "Modell"),
            ("selected_model", "Modell"),
            ("vault", "Vault"),
            ("vault_search", "Vault search"),
        )
        seen: set[str] = set()
        for key, label in rows:
            value = facts.get(key)
            if label in seen or value in (None, ""):
                continue
            seen.add(label)
            lines.append(f"- {label}: {_status_value(value)}")
        if unavailable:
            lines.append("- Weitere Statusdaten: werden geprüft")
        lines.extend(["", "Schreib direkt los oder nutze /model, /status, /help."])
        return "\n".join(lines)

    def _remember(self, text: str, principal: Principal, conversation: str) -> str:
        """`/remember <Text>` — der EINZIGE Weg, wie etwas ins Langzeitgedaechtnis kommt.

        Bewusst ein Kommando und kein Werkzeug: was der Agent dauerhaft ueber den
        Betreiber glaubt, soll der Betreiber gesagt haben. Ein Modell, das sich selbst
        etwas merken darf, traegt einen erfolgreichen Einfluesterungsversuch in jeden
        kuenftigen Lauf weiter — und niemand sieht, wann er hineinkam.
        """
        if self.recall is None:
            return "No long-term memory configured."
        body = text.strip()
        if not body:
            return "Nothing to remember. Use: /remember <what I should keep>"
        try:
            note = self.recall.remember(
                body, kind="fact", conversation=conversation, principal=str(principal)
            )
        except ValueError as error:
            # Sieht wie ein Zugangsdatum aus: abgewiesen statt geschwaerzt.
            return f"Refused: {error}"
        except Exception as error:
            return f"Could not store that: {error}"
        if note is None:
            return "Nothing stored."
        return f"Kept it. /memory shows what I hold, /forget {note.id} drops this one."

    def _memory(self) -> str:
        if self.recall is None:
            return "No long-term memory configured."
        try:
            notes = self.recall.recent(limit=20)
            total = self.recall.count()
        except Exception as error:
            return f"Could not read the memory: {error}"
        if not total:
            return "Nothing stored yet. /remember <text> keeps something."
        lines = [f"{total} note(s) held; showing the newest {len(notes)}:"]
        lines += [f"  {note.id}  {note.text}" for note in notes]
        lines.append("")
        lines.append("/forget <id> drops one · /forget all drops everything")
        return "\n".join(lines)

    def _forget(self, rest: str) -> str:
        if self.recall is None:
            return "No long-term memory configured."
        target = rest.strip()
        if not target:
            return "Which one? /memory lists them. /forget all drops everything."
        try:
            if target == "all":
                return f"Dropped {self.recall.forget_all()} note(s)."
            return "Dropped it." if self.recall.forget(target) else "No note with that id."
        except Exception as error:
            return f"Could not change the memory: {error}"

    def _every(self, rest: str, principal: Principal, conversation: str) -> str:
        """`/every <minuten> <auftrag>` — ein wiederkehrender Auftrag.

        Der Hinweis auf die engere Decke steht in der Bestaetigung, nicht im Kleingedruckten:
        wer einen Zeitplan anlegt, soll sofort wissen, dass der Lauf WENIGER darf als er
        selbst — sonst wundert er sich spaeter ueber einen Bericht statt eines Vollzugs.
        """
        if self.schedules is None:
            return "No schedule store wired."
        from .cron import CronError, looks_like_cron

        roh = rest.strip()
        felder: dict = {}
        # Ein Kalender-Ausdruck statt eines Abstands — erkannt, nicht als zweites
        # Kommando gelernt. Ein Intervall kann „alle 90 Minuten", aber nie
        # „werktags um 08:00", und das ist der Unterschied zwischen Timer und Zeitplan.
        if looks_like_cron(roh):
            teile = roh.split(maxsplit=5)
            if len(teile) < 6:
                return "Usage: /every <min> <task>  ·  /every <M H DOM MON DOW> <task>"
            felder = {"cron": " ".join(teile[:5])}
            auftrag = teile[5]
        else:
            teile = roh.split(maxsplit=1)
            if len(teile) < 2 or not teile[0].isdigit():
                return (
                    "Usage: /every <minutes> <what to do>\n"
                    "   or: /every <M H DOM MON DOW> <what to do>   e.g. /every 0 9 * * MON-FRI …\n"
                    "/schedules lists them"
                )
            felder = {"interval_s": int(teile[0]) * 60}
            auftrag = teile[1]
        try:
            task = self.schedules.add(
                conversation=conversation,
                principal=str(principal),
                prompt=auftrag,
                **felder,
            )
        except (ValueError, CronError) as error:
            return f"Not scheduled: {error}"
        if task is None:
            return "Not scheduled."
        wann = f"at {task.cron}" if task.cron else f"every {task.interval_s // 60} min"
        naechster = time.strftime("%a %d.%m %H:%M", time.localtime(task.next_run))
        return (
            f"Scheduled {task.id}: {wann} — {task.prompt}\n"
            f"First run: {naechster}.\n"
            "Unattended runs do less than you do: anything that would need your approval "
            "is reported, not performed. /schedules lists them, /unschedule <id> drops one."
        )

    def _schedules(self, conversation: str) -> str:
        if self.schedules is None:
            return "No schedule store wired."
        tasks = self.schedules.list_for(conversation)
        if not tasks:
            return "No schedules in this chat. /every <minutes> <what to do> creates one."
        lines = [f"{len(tasks)} schedule(s):"]
        lines += [f"  {t.describe()}" for t in tasks]
        lines.append("")
        lines.append("/unschedule <id> drops one.")
        return "\n".join(lines)

    def _unschedule(self, rest: str, conversation: str) -> str:
        if self.schedules is None:
            return "No schedule store wired."
        target = rest.strip()
        if not target:
            return "Which one? /schedules lists them."
        return "Dropped it." if self.schedules.remove(target, conversation=conversation) else "No schedule with that id in this chat."

    def _blueprints(self, rest: str, principal: Principal, conversation: str) -> str:
        """`/blueprints` und `/blueprint <verb> <name>` — installierbare Automatisierungen.

        Dieselbe Disziplin wie bei `/every`: die Bestaetigung sagt sofort, dass der
        Lauf unbeaufsichtigt WENIGER darf als ein getippter Auftrag — sonst wundert
        sich der Betreiber spaeter ueber einen Bericht statt eines Vollzugs.
        """
        if self.blueprints is None:
            return "No blueprint registry wired."
        from .blueprints import BlueprintError, describe_next

        teile = rest.split()
        verb = teile[0].lower() if teile else "list"
        ziel = teile[1] if len(teile) > 1 else ""
        try:
            if verb in ("list", "ls"):
                return self._blueprint_list()
            if not ziel:
                return (
                    "Which one? Usage: /blueprint install|remove|enable|disable|status <name>\n"
                    "/blueprints lists what is available."
                )
            if verb in ("install", "add"):
                task = self.blueprints.install(
                    ziel, conversation=conversation, principal=str(principal)
                )
                return (
                    f"Installed '{ziel}' — first run: {describe_next(task.next_run)}.\n"
                    "It fires as an unattended run: anything that would need your approval "
                    "is reported, never performed. Nothing was granted here.\n"
                    f"/blueprint disable {ziel} pauses it, /blueprint remove {ziel} takes it out."
                )
            if verb in ("remove", "uninstall"):
                self.blueprints.remove(ziel)
                return f"Removed '{ziel}' — the schedule entry is gone with it."
            if verb in ("enable", "activate", "on"):
                task = self.blueprints.enable(ziel)
                return f"Active again: '{ziel}' — next run: {describe_next(task.next_run)}."
            if verb in ("disable", "deactivate", "off"):
                self.blueprints.disable(ziel)
                return f"Paused: '{ziel}'. It stays installed — /blueprint enable {ziel} switches it back on."
            if verb == "status":
                return self._blueprint_status(ziel)
            return f"Unknown verb '{verb}'. Try: list, install, remove, enable, disable, status."
        except BlueprintError as error:
            return str(error)

    def _blueprint_list(self) -> str:
        from .blueprints import describe_next

        catalog = self.blueprints.catalog()
        stand = self.blueprints.installed()
        lines: list[str] = []
        if catalog.blueprints:
            lines.append(f"{len(catalog.blueprints)} blueprint(s) available:")
            for blueprint in catalog.blueprints:
                eintrag = stand.get(blueprint.name)
                if eintrag is None:
                    zustand = "not installed"
                elif eintrag.get("enabled"):
                    zustand = f"installed, next run {describe_next(self.blueprints.next_run(blueprint.name))}"
                else:
                    zustand = "installed, paused"
                beschreibung = f" — {blueprint.description}" if blueprint.description else ""
                lines.append(f"  {blueprint.name} ({blueprint.when}){beschreibung} [{zustand}]")
        else:
            lines.append("No blueprints available.")
        if catalog.rejected:
            lines.append("")
            lines.append(f"{len(catalog.rejected)} refused:")
            lines += [f"  {grund}" for grund in catalog.rejected[:10]]
        lines.append("")
        lines.append("/blueprint install <name> installs one · /blueprint status <name> shows it.")
        return "\n".join(lines)

    def _blueprint_status(self, name: str) -> str:
        from .blueprints import describe_next

        blueprint = self.blueprints.catalog().get(name)  # wirft BlueprintError
        stand = self.blueprints.installed().get(blueprint.name)
        lines = [
            f"{blueprint.name} — {blueprint.description or '(no description)'}",
            f"when: {blueprint.when}",
            f"task: {blueprint.prompt}",
        ]
        if stand is None:
            lines.append(f"state: not installed — /blueprint install {blueprint.name}")
        elif stand.get("enabled"):
            lines.append(f"state: active — next run {describe_next(self.blueprints.next_run(blueprint.name))}")
        else:
            lines.append(f"state: paused — /blueprint enable {blueprint.name} switches it back on")
        return "\n".join(lines)

    def _skills(self) -> str:
        """Was geladen ist, was verworfen wurde — und was Talos bewusst ignoriert.

        Ein Skill ist Anweisungstext von Fremden. Wer nicht sehen kann, welcher davon im
        Prompt steht, kann auch nicht beurteilen, warum der Agent gerade etwas vorschlaegt.
        Deshalb nennt diese Ansicht auch die Verworfenen samt Grund: ein still fehlender
        Skill sieht sonst aus wie ein Agent, der eine Anweisung ignoriert.
        """
        from .skills import discover_skills

        if not self.skills_dirs:
            return "No skills directory configured."
        try:
            catalog = discover_skills(self.skills_dirs)
        except Exception as error:
            return f"Could not read the skills directories: {error}"

        lines = [f"{len(catalog.skills)} skill(s) loaded from:"]
        lines += [f"  {path}" for path in self.skills_dirs]
        lines.append("")
        lines += [f"  {skill.name}" for skill in catalog.skills[:40]]
        if len(catalog.skills) > 40:
            lines.append(f"  … and {len(catalog.skills) - 40} more")
        if catalog.rejected:
            lines.append("")
            lines.append(f"{len(catalog.rejected)} refused:")
            lines += [f"  {reason}" for reason in catalog.rejected[:10]]
        wanted = [s for s in catalog.skills if s.requested_tools]
        if wanted:
            lines.append("")
            lines.append(
                f"{len(wanted)} skill(s) declare pre-approved tools. "
                "Talos ignores that field: a skill may suggest, never permit."
            )
        return "\n".join(lines)

    def _status(self, conversation: str) -> str:
        pending = self.approvals.get(conversation)
        lines = [
            f"{agent_name()} @{self.bot_username}",
            f"Laufzeit: {_duration(time.time() - self.started_at)}",
            f"Denkt gerade: {'ja' if self.worker.busy() else 'nein'}",
            f"Warteschlange: {self.worker.pending()}",
            f"Offene Freigabe: {'ja — ' + pending.req.tool if pending else 'nein'}",
            f"Ereignisse gesamt: {self.log.count()}",
            f"Code: {self._version()}",
        ]
        if self.governor is not None:
            lines.insert(1, f"Autonomie: {self.governor.describe()}")
        if self.memory is not None:
            turns, chars = self.memory.stats(conversation)
            # „a restart forgets" gilt seit dem Archiv nur noch fuer den AKTIVEN Kontext —
            # genau das sagt der Text jetzt, sonst wuerde /status das Gegenteil der
            # session_search-Doku behaupten.
            lines.append(
                f"History: {turns} turns / {chars} chars (active context — a restart "
                "clears it; the archive keeps what was said, via session_search)"
                if turns
                else "Verlauf: leer (aktiver Kontext — das Archiv bleibt via session_search)"
            )
        if self.mint is not None:
            stats = self.mint.stats()
            lines.append(
                f"Tokens: {stats['issued']} minted, {stats['redeemed']} redeemed "
                "(je einmalig, an eine Handlung gebunden)"
            )
        return "\n".join(lines)

    def _new(self, conversation: str) -> str:
        """Vergisst den Verlauf DIESER Konversation — und sagt dazu, was bleibt.

        Der Nachsatz ist kein Schmuck: `/new` loescht das Gedaechtnis, nicht die Belege.
        Wer glaubt, damit sei auch die ausgefuehrte Handlung weg, taeuscht sich ueber
        genau die Eigenschaft, wegen der es den Event-Log gibt.
        """
        if self.memory is None:
            return "No memory wired — there is nothing to forget."
        turns = self.memory.forget(conversation)
        if not turns:
            return "Verlauf war schon leer. Nichts vergessen."
        # Beide Nachsaetze sind dieselbe Ehrlichkeits-Disziplin: `/new` leert den AKTIVEN
        # Kontext — die Belege bleiben im Log, und das Gesagte bleibt im Archiv ueber
        # session_search auffindbar. Wer glaubt, es sei restlos weg, soll das hier lesen.
        return (
            f"History cleared: {turns} turns forgotten. "
            "What actually ran is still in the log (/log), and what was said stays "
            "findable via session_search."
        )

    def _retry(self, conversation: str) -> CommandResult:
        """Stellt die letzte Frage noch einmal — als waere sie neu geschrieben.

        Das alte Paar wird dabei aus dem Verlauf genommen, sonst stuende die Frage
        doppelt drin. Der Lauf geht ganz normal durch Kernel und Executor: `/retry`
        wiederholt die Frage, nicht die Erlaubnis.
        """
        if self.memory is None:
            return CommandResult(reply="No memory wired — /retry needs the last turn.")
        # Bei offener Freigabe wuerde der weitergereichte Text als Antwort auf die Frage
        # gelesen ("Bitte nur ja oder nein") — und das Paar waere still aus dem Verlauf
        # verschwunden. Erst entscheiden, dann wiederholen.
        if self.approvals.get(conversation) is not None:
            return CommandResult(
                reply="Erst die offene Freigabe entscheiden (/pending zeigt sie), dann /retry."
            )
        last = self.memory.pop_last(conversation)
        if last is None:
            return CommandResult(reply="No last turn to repeat.")
        return CommandResult(forward_as=last)

    def _queue(self) -> str:
        if not self.worker.busy() and self.worker.pending() == 0:
            return "Nothing running, nothing waiting."
        return f"Running: {'yes' if self.worker.busy() else 'no'}\nWaiting: {self.worker.pending()}"

    def _stop(self) -> str:
        dropped = self.worker.drain()
        killed = self.reasoner.cancel()
        if not killed and dropped == 0:
            return "Nichts abzubrechen — es lief nichts und es wartete nichts."
        parts = []
        if killed:
            parts.append("laufendes Denken abgebrochen")
        if dropped:
            parts.append(f"{dropped} wartende Nachricht(en) verworfen")
        return "Abgebrochen: " + ", ".join(parts) + "."

    # --- Freigabe ----------------------------------------------------------------
    def _pending(self, conversation: str) -> str:
        rec = self.approvals.get(conversation)
        if rec is None:
            return "Keine offene Freigabe."
        remaining = max(0, int(rec.expires_at - time.time()))
        return f"{rec.prompt}\n\n(valid for another {remaining}s)"

    def _allowed(self, conversation: str, *, principal: Principal) -> str:
        """Zeigt, was the operator mit „immer" freigegeben hat — nummeriert, damit /revoke zielen kann.

        Der Text sagt bewusst dazu, was eine solche Regel NICHT festhält: bei Datei-Tools
        steht nur das Ziel im Fingerabdruck, nicht der Inhalt. Wer `~/notes.md` einmal mit
        „immer" freigibt, hat jeden künftigen Inhalt dieser Datei freigegeben. Das ist eine
        bewusste Entscheidung (ein Inhalts-Fingerabdruck wäre bei jedem Zeichen ungültig) —
        aber sie darf nicht in einer Fussnote versteckt bleiben.
        """
        if self.standing is None:
            return "No standing approvals wired."
        rules = self.standing.list(conversation, principal=principal)
        if not rules:
            return (
                "No standing approvals in this chat.\n"
                "Reply to an approval with 'always' and it will be listed here."
            )
        lines = [f"Standing approvals ({len(rules)}) — this chat only, you only:"]
        for index, rule in enumerate(rules, start=1):
            lines.append(f"{index}. {rule.label}")
        lines.append("")
        lines.append(
            "Bound is exactly what is shown: for file tools the target (any future "
            "content counts), for run_shell the command string character by character."
        )
        lines.append(
            "They only apply where the kernel would ask anyway — what it forbids "
            "stays forbidden, and below autonomy 3 none of them apply."
        )
        lines.append("/revoke <n> takes one back.")
        return "\n".join(lines)

    def _revoke(self, rest: str, conversation: str, *, principal: Principal) -> str:
        if self.standing is None:
            return "No standing approvals wired."
        if not rest.strip():
            return "/revoke braucht eine Nummer aus /allowed."
        try:
            index = int(rest.split()[0])
        except ValueError:
            return f"/revoke braucht eine Nummer aus /allowed, nicht: {rest.strip()}"
        rule = self.standing.revoke(
            conversation, index, principal=principal, run_id=new_run_id()
        )
        if rule is None:
            return f"No standing approval number {index}. /allowed shows what exists."
        return f"Revoked: {rule.label}\nFrom now on this asks again."

    # --- Nachvollziehen ----------------------------------------------------------
    def _log(self, rest: str) -> str:
        limit = LOG_DEFAULT
        if rest:
            try:
                limit = max(1, min(LOG_MAX, int(rest.split()[0])))
            except ValueError:
                return f"/log braucht eine Zahl (1–{LOG_MAX}), nicht: {rest}"
        rows = self.log.recent(limit, AUDIT_TYPES)
        if not rows:
            return "Noch keine Ereignisse mit Wirkung."
        lines = [f"Letzte {len(rows)} Ereignisse:"]
        for row in rows:
            lines.append(f"{_clock(row['ts'])} {row['type']} — {_summarise(row['payload'])}")
        return "\n".join(lines)

    def _undo(self, *, principal: Principal, conversation: str) -> CommandResult:
        # Erst die Reihenfolge klären: mit offener Freigabe wäre unklar, worauf sich ein
        # „ja" bezieht — auf die geparkte Aktion oder auf das Undo.
        if self.approvals.get(conversation) is not None:
            return CommandResult(
                reply="Erst die offene Freigabe entscheiden (ja/nein), dann /undo. /pending zeigt sie."
            )
        rows = self.log.recent(UNDO_LOOKBACK, ("snapshot.taken", "exec.result"))
        snap = None
        for row in rows:
            if row["type"] == "snapshot.taken":
                snap = row
        if snap is None:
            return CommandResult(reply="Nothing to roll back — no file change in the log.")
        if _already_undone(rows, after_id=snap["id"]):
            return CommandResult(reply="The last change was already rolled back.")

        payload = snap["payload"]
        entries = payload.get("entries") or []
        if not entries:
            return CommandResult(reply="The last snapshot has no entries — nothing to do.")
        names = ", ".join(str(entry[0]) for entry in entries)
        request = ToolRequest(
            tool="undo_last",
            identity=principal,
            args={"snapshot_id": str(payload.get("snapshot_id", "")), "entries": entries},
        )
        return CommandResult(reply=f"Rolled back: {names}", request=request)

    def _tools(self) -> str:
        lines = ["Tools (Effekt → Gating):"]
        for spec in sorted(self.policy.manifest.tools, key=lambda s: s.name):
            if spec.effect.value == "read":
                gate = "frei (Secrets gesperrt)"
            elif spec.effect.value == "exec":
                gate = "needs approval" if self.policy.shell_needs_human else "command floor decides"
            elif not spec.reversible:
                gate = "needs approval (irreversible)"
            else:
                gate = "erlaubt, Secrets/Persistenz fragen"
            lines.append(f"• {spec.name} ({spec.effect.value}) → {gate}")
        lines.append("\nWas hier nicht steht, wird abgelehnt — nicht geraten.")
        lines.append(
            "Kein Tool hat ein Dauerrecht: jeder Lauf braucht ein eigenes Token auf genau "
            "diese Anfrage — einmalig, nach Sekunden verfallen."
        )
        if self.governor is not None:
            lines.append(
                f"Above this sits the dial ({self.governor.describe()}) — it can tighten "
                "every line here, never loosen one. And it is not a tool itself."
            )
        return "\n".join(lines)

    def _autonomy(self, rest: str, *, principal: Principal) -> str:
        """Der einzige Weg an den Regler. Es gibt bewusst kein Tool dafuer.

        Ein Agent, der seine eigene Leine verlaengern kann, hat keine. Darum steht
        `set_autonomy` in keinem Manifest — der Regler ist nur hier erreichbar, im
        deterministischen Pfad, ohne dass das Modell je gefragt wird.
        """
        if self.governor is None:
            return "Kein Autonomie-Regler verdrahtet."
        if not rest:
            return f"{self.governor.describe()}\n\n{autonomy.table()}\n\n/autonomy <0-5> stellt um."
        try:
            before, after = self.governor.set_level(
                rest, principal=principal, allowed_identities=self.policy.allowed_identities
            )
        except AutonomyError as exc:
            return f"Nicht umgestellt: {exc}\n\n{autonomy.table()}"
        self.log.append(
            Event(
                new_run_id(),
                "human",
                "autonomy.set",
                {"level": after, "before": before, "principal": str(principal)},
            )
        )
        if before == after:
            return f"Bleibt bei {self.governor.describe()}"
        richtung = "zugedreht" if after < before else "aufgedreht"
        note = ""
        if after < before:
            note = "\nTokens already minted are void."
        return f"{richtung}: {before} → {after}\n{self.governor.describe()}{note}"

    def _policy(self, rest: str, principal: Principal) -> str:
        """Trockenlauf. Ruft NUR decide() — es wird nichts ausgeführt und nichts geparkt."""
        if not rest:
            return "/policy <path|command> — shows what the kernel would decide."
        if rest.startswith(("/", "~", "./", "../")) and " " not in rest:
            lines = [f"Dry run for path: {rest}"]
            for tool in ("read_file", "write_file"):
                req = ToolRequest(tool=tool, identity=principal, args={"path": rest, "content": ""})
                lines.append(f"{tool}: {_verdict_text(self.policy.decide(req))}")
            return "\n".join(lines)
        req = ToolRequest(tool="run_shell", identity=principal, args={"command": rest})
        lines = [f"Dry run for command: {rest}", f"run_shell: {_verdict_text(self.policy.decide(req))}"]
        marks = command_risk_paths(rest)
        if marks:
            lines.append("Pfade: " + ", ".join(f"{p} [{label}]" if label else p for p, label in marks))
        lines.append("(only checked, nothing ran)")
        return "\n".join(lines)

    def _whoami(self, principal: Principal, conversation: str) -> str:
        allowed = principal in self.policy.allowed_identities
        return (
            f"Identitaet: {principal}\nUnterhaltung: {conversation}\n"
            f"Zugelassen: {'ja' if allowed else 'nein'}\n"
            "Freigaben gelten nur in dem Chat, in dem gefragt wurde."
        )

    def _version(self) -> str:
        return _git_head(self.repo_dir)

    # --- Innenansicht ------------------------------------------------------------
    def _usage(self) -> str:
        """Gemessen, nicht geschaetzt — und die Kosten sind ausdruecklich rechnerisch.

        Manche Agenten zeigen unter `/usage` eine Rechnung. Hier gibt es keine: die CLI laeuft ueber ein
        Abo. Der Dollarbetrag ist der Listenpreis derselben Anfrage ueber die API — als
        Groessenordnung brauchbar, als Rechnung falsch. Das steht dabei, sonst liest ihn
        jemand als Abbuchung.
        """
        if self.usage is None:
            return "Kein Verbrauchszaehler verdrahtet."
        snap = self.usage.snapshot()
        if not snap.runs:
            return "Noch kein Denk-Lauf seit dem Start — es gibt nichts zu zaehlen."
        lines = [
            "Verbrauch seit Start",
            f"Laeufe: {snap.runs}" + (f" ({snap.failed} ohne Ergebnis)" if snap.failed else ""),
            f"Denkzeit: {_duration(snap.seconds)} gesamt, {snap.seconds / snap.runs:.0f}s im Schnitt",
            f"Token: {_tokens(snap.input_tokens)} rein / {_tokens(snap.output_tokens)} raus"
            f" · Cache {_tokens(snap.cache_read)} gelesen / {_tokens(snap.cache_write)} geschrieben",
            f"Rechnerisch: ${snap.cost_usd:.2f} — Listenpreis derselben Anfragen ueber die API. "
            "Talos laeuft ueber ein Abo; abgerechnet wird davon nichts.",
        ]
        last = snap.last
        if last is not None:
            detail = f"{_clock(last.at)} · {_short_model(last.model) or 'Modell unbekannt'} · {last.duration_s:.0f}s"
            if last.cost_usd:
                detail += f" · ${last.cost_usd:.2f}"
            if last.note:
                detail += f" · {last.note}"
            lines.append(f"Letzter Lauf: {detail}")
        per_run = snap.cache_total / snap.runs
        if per_run >= CACHE_HINT_TOKENS:
            lines.append(
                f"\nAuffaellig: ~{_tokens(int(per_run))} Cache-Token pro Lauf, bevor die Frage "
                "ueberhaupt gelesen ist. Die CLI erbt die Agenten-Konfiguration aus dem "
                "Home-Verzeichnis — Talos denkt im Kontext eines fremden Agenten. Ein eigener, "
                "kleiner Scope waere billiger und sauberer."
            )
        return "\n".join(lines)

    def _model(self) -> str:
        """Zeigt, was gedacht hat — und sagt, warum es hier keinen Schalter gibt."""
        version = _claude_version(self.claude_bin) if self.claude_bin else "unbekannt"
        lines = [
            "Modell",
            f"Backend: claude-CLI ({self.claude_bin or 'nicht gesetzt'}), Version {version}",
        ]
        last = self.usage.snapshot().last if self.usage is not None else None
        if last is None or not last.models:
            lines.append("Zuletzt benutzt: noch nichts gemeldet (kein Lauf seit dem Start).")
        else:
            names = ", ".join(_short_model(m) for m in last.models)
            if last.model and len(last.models) > 1:
                names += f" — Hauptlast: {_short_model(last.model)}"
            lines.append(f"Zuletzt benutzt: {names}")
        lines.append(
            "\nTalos waehlt kein Modell — das entscheidet die CLI mit ihrem Abo. Deshalb steht "
            "hier kein Schalter: ein /model, das nichts umstellt, waere eine Behauptung."
        )
        return "\n".join(lines)

    def _reasoning(self) -> str:
        """Die Wahrheit ueber den Denk-Pfad und den adaptiven Hermes-Aufwand."""
        lines = [
            "Denken",
            "Backend: der aktuell gewaehlte Reasoner, isoliert und ohne eigene Werkzeuge.",
            f"Zeitlimit: {self.reasoner_timeout_s}s pro Lauf, danach Abbruch"
            if self.reasoner_timeout_s
            else "Zeitlimit: nicht gesetzt",
            f"CLI-eigene Werkzeuge: {len(DISALLOWED_TOOLS_ARGV) - 1} abgeschaltet — sie saessen "
            "sonst VOR dem Kernel und waeren ein Bypass.",
        ]
        if self.memory is not None:
            lines.append(
                f"History in the prompt: at most {MAX_TURNS} turns / {MAX_CHARS} chars, "
                "als Kontext ausgezeichnet — keine Erlaubnisquelle."
            )
        lines.append(
            "\nDenktiefe wird automatisch pro Zug geroutet: kurze Antworten laufen mit low, "
            "Status-/Recherchefragen mit medium und komplexe Analyse mit high. Auf dem "
            "Hermes-Weg setzt Talos dafuer wirklich Hermes --reasoning; es ist kein "
            "Anzeige-Schalter. Das aendert nur Rechenaufwand, nie Rechte — /autonomy "
            "bleibt die Leine."
        )
        return "\n".join(lines)

    def _debug(self, conversation: str) -> str:
        """Zustand zum Hinschauen. Enthaelt bewusst keine Identitaeten und keine Secrets."""
        lines = [
            "Diagnose",
            f"Code: {self._version()} · Python {platform.python_version()} · PID {os.getpid()}",
            f"Laeuft seit: {_duration(time.time() - self.started_at)}",
        ]
        if self.eventlog_db is not None:
            lines.append(
                f"Event-Log: {self.eventlog_db} · {_perm(self.eventlog_db)} · "
                f"{_size(self.eventlog_db)} · {self.log.count()} Ereignisse"
            )
        else:
            lines.append(f"Event-Log: {self.log.count()} Ereignisse")
        if self.snapshot_dir is not None:
            lines.append(
                f"Snapshots: {self.snapshot_dir} · {_perm(self.snapshot_dir)} · "
                f"{_dir_count(self.snapshot_dir)} Stueck"
            )
        if self.transcript_db is not None and self.transcript is not None:
            # Wachstum sichtbar machen statt still begrenzen — das Archiv hat bewusst
            # keinen Deckel, also muss man ihm beim Wachsen zusehen koennen.
            turns_total = getattr(self.transcript, "count", lambda: 0)()
            lines.append(
                f"Archiv: {self.transcript_db} · {_perm(self.transcript_db)} · "
                f"{_size(self.transcript_db)} · {turns_total} Zuege"
            )
        if self.channels is not None:
            named = ", ".join(
                f"{name} ({self.channels.trust_of(name).name})" for name in self.channels.names
            )
            lines.append(f"Kanaele: {named or 'keine'}")
        lines.append(
            f"Zugelassen: {len(self.policy.allowed_identities)} Identitaet(en) — /whoami zeigt deine"
        )
        if self.governor is not None:
            lines.append(f"Dial: {self.governor.describe()}")
        lines.append(
            f"Warteschlange: laeuft {'ja' if self.worker.busy() else 'nein'}, "
            f"wartet {self.worker.pending()}"
        )
        if self.memory is not None:
            turns, chars = self.memory.stats(conversation)
            lines.append(f"History: {turns} turns / {chars} chars (memory only)")
        if self.mint is not None:
            stats = self.mint.stats()
            lines.append(f"Token: {stats['issued']} gepraegt, {stats['redeemed']} eingeloest")
        if self.usage is not None:
            last = self.usage.snapshot().last
            if last is None:
                lines.append("Letzter Denk-Lauf: keiner")
            else:
                mark = "ok" if last.ok else "fehlgeschlagen"
                extra = f" · {last.note}" if last.note else ""
                lines.append(
                    f"Letzter Denk-Lauf: {_clock(last.at)} · {mark} · {last.duration_s:.0f}s · "
                    f"{_short_model(last.model) or '—'}{extra}"
                )
        lines.append(
            "\nKein Token, keine Kennung, kein Secret steht hier drin — /debug ist zum Zeigen da."
        )
        return "\n".join(lines)

    def _health(self, conversation: str) -> str:
        """Die kompakte Ampel — weil /debug die Fakten hat, sie aber unter vielen anderen
        Zeilen fuehrt.

        Entstanden aus einer Beobachtung im Betrieb: ein still stehender Poller und eine
        wachsende Fehlerquote blieben tagelang unbemerkt, weil man ihnen aktiv nachgraben
        musste. Alles hier kommt aus bereits injizierten Quellen — kein Netzaufruf, kein
        Schreiben. Ein Gesundheitszustand, der selbst eine Wirkung haette, waere keine
        Anzeige mehr — und genau deshalb darf diese Anzeige ein Kommando sein.
        """
        zeilen = [f"Zustand — laeuft seit {_duration(time.time() - self.started_at)}"]

        # Denken: der Zaehler misst jeden Lauf, auch den gescheiterten — eine Ampel,
        # die nur die geglueckten kennt, zeigt gruen im Sturm.
        if self.usage is None:
            zeilen.append("Denken: kein Zaehler verdrahtet")
        else:
            snap = self.usage.snapshot()
            if not snap.runs:
                zeilen.append("Denken: noch kein Lauf seit dem Start")
            else:
                zeile = f"Denken: {snap.runs} Laeufe"
                zeile += f", {snap.failed} ohne Ergebnis" if snap.failed else ", alle mit Ergebnis"
                last = snap.last
                if last is not None:
                    notiz = " ".join(str(last.note).split())[:120] if last.note else ""
                    zeile += (
                        f" · letzter {_clock(last.at)} "
                        f"{'ok' if last.ok else 'FEHLGESCHLAGEN'}"
                        f"{f' · {notiz}' if notiz else ''}"
                    )
                zeilen.append(zeile)

        # Protokoll: was als Fehler EINGETRAGEN wurde, nicht was jemand behauptet.
        try:
            fehler = self.log.recent(100, types=("error",))
        except Exception:
            # Eine Ampel, die am Messgeraet haengt, darf nie selbst die Stoerung sein.
            fehler = []
        if not fehler:
            zeilen.append("Protokoll: kein Fehler unter den letzten 100 Ereignissen")
        else:
            neueste = fehler[-1]  # recent() liefert chronologisch, aeltestes zuerst
            p = neueste.get("payload") or {}
            detail = " ".join(str(p.get("error") or p.get("stage") or "").split())[:120]
            zeilen.append(
                f"Protokoll: {len(fehler)} Fehler unter den letzten 100 Ereignissen · "
                f"neuester {_clock(float(neueste.get('ts') or 0))}: "
                f"{detail or '(ohne Fehlertext)'}"
            )

        zeilen.append(
            f"Warteschlange: {'beschaeftigt' if self.worker.busy() else 'frei'}, "
            f"{self.worker.pending()} wartend"
        )
        pending = self.approvals.get(conversation)
        zeilen.append(f"Offene Freigabe: {'ja — ' + pending.req.tool if pending else 'nein'}")
        if self.channels is not None:
            named = ", ".join(
                f"{name} ({self.channels.trust_of(name).name})" for name in self.channels.names
            )
            zeilen.append(f"Kanaele: {named or 'keine'}")
        if self.governor is not None:
            zeilen.append(f"Leine: {self.governor.describe()}")
        return "\n".join(zeilen)


# --- Helfer ---------------------------------------------------------------------
def _status_value(value: object) -> str:
    if isinstance(value, bool):
        return "✅ aktiv" if value else "⚪ inaktiv"
    text = " ".join(str(value).split())
    return text[:120] if text else "being checked"


def _already_undone(rows: list[dict], *, after_id: int) -> bool:
    """Lief nach diesem Snapshot schon ein erfolgreiches undo_last?"""
    for row in rows:
        if row["id"] <= after_id or row["type"] != "exec.result":
            continue
        payload = row["payload"]
        if payload.get("tool") == "undo_last" and payload.get("status") == "done":
            return True
    return False


def _verdict_text(decision) -> str:
    marks = {"allow": "erlaubt", "deny": "abgelehnt", "needs_human": "fragt dich"}
    return f"{marks.get(decision.verdict.value, decision.verdict.value)} — {decision.reason}"


def _summarise(payload: dict) -> str:
    """Kurzfassung eines Event-Payloads für die Chat-Ansicht."""
    parts: list[str] = []
    for key in ("tool", "status", "verdict", "principal", "snapshot_id", "grant_id"):
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}" if key != "tool" else str(value))
    args = payload.get("args")
    if isinstance(args, dict):
        inner = args.get("command") or args.get("path")
        if inner:
            parts.append(str(inner))
    detail = payload.get("detail") or payload.get("reason")
    if detail:
        parts.append(str(detail))
    text = " · ".join(parts) if parts else "(ohne Details)"
    return text if len(text) <= SUMMARY_MAX else text[:SUMMARY_MAX] + "…"


def _tokens(count: int) -> str:
    """1234 -> „1.2k". Token-Zahlen sind Groessenordnungen, keine Betraege."""
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


def _short_model(name: str) -> str:
    """„claude-opus-4-8" -> „opus-4-8". Leerer Name bleibt leer."""
    return name[len("claude-"):] if name.startswith("claude-") else name


def _perm(path: Path) -> str:
    try:
        return oct(path.stat().st_mode & 0o777)[2:].rjust(4, "0")
    except OSError:
        return "?"


def _size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "?"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _dir_count(path: Path) -> int:
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


_VERSION_CACHE: dict[str, str] = {}


def _claude_version(binary: str) -> str:
    """Einmal pro Prozess. Festes argv, keine Shell — ein Fehlschlag ist „unbekannt".

    Das ist der einzige Subprozess im Kommando-Pfad. Er ist vertretbar, weil das Argument
    nicht aus einer Nachricht kommt: `binary` steht in der Config, `--version` ist fest.
    """
    cached = _VERSION_CACHE.get(binary)
    if cached is not None:
        return cached
    version = "unbekannt"
    try:
        done = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_S,
        )
        first = (done.stdout or "").strip().splitlines()
        if done.returncode == 0 and first:
            version = first[0].strip()[:80]
    except (OSError, subprocess.SubprocessError):
        version = "unbekannt"
    _VERSION_CACHE[binary] = version
    return version


def _duration(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _git_head(repo_dir: Path) -> str:
    """Git-Stand ohne Subprozess — `/version` soll keine Shell brauchen."""
    head = repo_dir / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
    except OSError:
        return "unbekannt (kein Git)"
    if not ref.startswith("ref: "):
        return ref[:12]
    branch = ref[5:]
    try:
        sha = (repo_dir / ".git" / branch).read_text(encoding="utf-8").strip()
    except OSError:
        return branch.rsplit("/", 1)[-1]
    return f"{branch.rsplit('/', 1)[-1]}@{sha[:8]}"
