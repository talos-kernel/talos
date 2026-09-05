"""Completion-Push — die Meldung, wenn ein delegierter Job fertig ist.

Ein `delegate_code`-Auftrag ist abgeschickt, sobald der Worker ihn annimmt; der Chat
weiss danach eine `job_id` und sonst nichts. Ob der Job in zwei Minuten fertig ist
oder am Zeitlimit stirbt, erfaehrt niemand — es sei denn, jemand fragt mit
`delegate_status` nach. Fuer einen Lauf, der bewusst NEBENHER geht, ist das die eine
fehlende Richtung: der Weg zurueck.

Dieser Baustein ist dieser Weg: ein Waechter, der angenommene Jobs beobachtet und bei
einem Endzustand (`done`/`failed`/`timeout`) eine kurze, faktische Meldung dorthin
schickt, wo der Auftrag herkam. Der Ticker, der ihn aufruft, steht in `__main__` —
dieselbe Bauart wie der Zeitplan-Ticker.

⚠️ Drei Entscheidungen:

- **Beweis aus dem Worker-Protokoll, nie aus Modellprosa.** Die Meldung zeigt, was der
  Worker aufgezeichnet hat: state, summary (das `result`-Event des Streams), files,
  returncode, error. Kein Satz davon schreibt ein Modell — dieselbe Regel wie bei
  `delegate_status`, nur als Push statt auf Nachfrage.
- **Der Rueckweg kommt aus dem Thread-Kontext, nie aus Argumenten.** Die Konversation
  hinterlegt der Conductor am ausfuehrenden Thread (`AskContexts`); die Anmeldung
  uebernimmt sie von dort. Stuende sie in den Werkzeug-Argumenten, entschiede das
  Modell, WOHIN gemeldet wird.
- **Fail-open wie jede Zustellung.** Ein unerreichbarer Worker kostet den Tick, nicht
  den Agenten; ein unzustellbarer Push bleibt angemeldet und kommt beim naechsten Tick
  erneut. Das hier ist Komfort, kein Gate — und wird deshalb nie zu einem.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Callable

from .eventlog import Event, EventLog, new_run_id

__all__ = [
    "CompletionDesk",
    "TERMINAL",
    "Watch",
    "completion_text",
    "gone_text",
    "poll_once",
    "submitted_job_id",
    "watching",
]

# Die Endzustaende des Workers (claudeworker._Jobs). Alles andere heisst: laeuft noch.
TERMINAL = frozenset({"done", "failed", "timeout"})

# Ein Push ist kurz; der volle Beleg bleibt `delegate_status` und das Event-Log.
MAX_SUMMARY_CHARS = 300
MAX_FILES = 5
MAX_PROMPT_SHORT = 56

# Das Antwortformat des delegate_code-Runners (tools.make_delegate_code_runner):
# "delegate_code job_id=<id> state=accepted (workspace …)". Gelesen wird NUR die
# angenommene Anmeldung — eine Fehlerzeile ("worker unavailable — …") traegt keine
# job_id und meldet folgerichtig auch nichts an.
_SUBMITTED = re.compile(r"^delegate_(?:code|agy|codex) job_id=(\S+) state=accepted\b")


def _zeile(text: object, limit: int) -> str:
    """Eine Zeile, gedeckelt. Mehrzeiliges aus einem Worker-Frame darf den Push nicht
    sprengen — und keine zweite Zeile faelschen, die wie eine eigene Meldung aussieht."""
    einzeilig = " ".join(str(text or "").split())
    return einzeilig if len(einzeilig) <= limit else einzeilig[: limit - 1] + "…"


@dataclass(frozen=True)
class Watch:
    """Ein angemeldeter Job. `conversation` ist der Rueckweg — er steht hier, weil er
    bei der Anmeldung aus dem Thread-Kontext kam, nicht weil ihn jemand uebergab."""

    job_id: str
    conversation: str
    short: str = ""
    tool: str = "delegate_code"


@dataclass
class CompletionDesk:
    """Welche Jobs gerade beobachtet werden. Eine Instanz, geteilt zwischen dem
    Werkzeug-Thread (Anmeldung) und dem Ticker (Abfrage, Zustellung)."""

    _watched: dict[str, Watch] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def watch(self, watch: Watch) -> None:
        with self._lock:
            self._watched[watch.job_id] = watch

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._watched.pop(job_id, None)

    def pending(self) -> tuple[Watch, ...]:
        with self._lock:
            return tuple(self._watched.values())

    def busy(self) -> int:
        with self._lock:
            return len(self._watched)


def submitted_job_id(text: str) -> str | None:
    """Die job_id einer ANGENOMMENEN Anmeldung — oder `None` bei jeder anderen Zeile."""
    treffer = _SUBMITTED.search(text or "")
    return treffer.group(1) if treffer else None


def watching(runner: Callable, *, desk: CompletionDesk,
             context: Callable[[], object | None]) -> Callable:
    """Legt die Anmeldung um den `delegate_code`-Runner. Dumm wie der Runner selbst:
    aufrufen, die Antwort lesen, bei einer angenommenen job_id den Rueckweg aus dem
    Thread-Kontext merken. Die Antwort geht unveraendert zurueck — der Push aendert
    nichts am Lauf, er beobachtet ihn nur.

    Ohne Kontext (kein laufender Auftrag an diesem Thread) wird nichts angemeldet:
    ein Push ohne bekannten Empfaenger waere ein Raten, und geratene Zustellwege sind
    die, die in falschen Chats landen.
    """

    def delegate_code(req: object) -> str:
        antwort = runner(req)
        job_id = submitted_job_id(antwort)
        if job_id:
            ziel = context()
            if ziel is not None:
                kurz = _zeile(getattr(req, "args", {}).get("prompt", ""), MAX_PROMPT_SHORT)
                desk.watch(Watch(job_id, ziel.conversation, kurz, antwort.split()[0]))
        return antwort

    return delegate_code


def completion_text(watch: Watch, frame: dict) -> str:
    """Die kurze, faktische Meldung eines Endzustands. Alles Sichtbare kommt aus dem
    Worker-Frame (Stream-Beleg) und der Anmeldung — kein Wort davon ist Modelltext."""
    state = str(frame.get("state", "?"))
    kopf = f"{watch.tool} job {watch.job_id} finished — {state}"
    if watch.short:
        kopf += f"\n  {watch.short}"
    zeilen = [kopf]
    if state == "done":
        zeilen.append(f"summary: {_zeile(frame.get('summary'), MAX_SUMMARY_CHARS) or '(none)'}")
        dateien = [str(f) for f in (frame.get("files") or [])][:MAX_FILES]
        zeilen.append(f"files: {', '.join(dateien) if dateien else '(none)'}")
    zeilen.append(f"returncode: {frame.get('returncode')}")
    fehler = _zeile(frame.get("error"), MAX_SUMMARY_CHARS)
    if fehler:
        zeilen.append(f"error: {fehler}")
    return "\n".join(zeilen)


def gone_text(watch: Watch) -> str:
    """Der Worker kennt den Job nicht mehr. Das ist ein Endzustand, kein Wackeln:
    ein neu gestarteter Worker weiss von nichts (siehe claudeworker) — die Meldung
    sagt das ehrlich, statt ein Ergebnis zu erfinden."""
    kopf = f"{watch.tool} job {watch.job_id} — the worker no longer knows this job"
    if watch.short:
        kopf += f"\n  {watch.short}"
    return kopf + "\n(worker restarted? no result available)"


def _log(log: EventLog | None, typ: str, payload: dict) -> None:
    if log is not None:
        log.append(Event(new_run_id(), "notify", typ, payload))


def poll_once(desk: CompletionDesk, *, status: Callable[[str], dict],
              send: Callable[[str, str], None],
              log: EventLog | None = None) -> int:
    """Ein Tick: jeden angemeldeten Job einmal fragen, Endzustaende zustellen.

    Gibt die Zahl der Zustellungen zurueck. Wirft NIE — ein kaputter Worker oder ein
    kaputter Kanal kostet den Tick, nicht den Waechter. Ein Push, dessen Zustellung
    scheitert, bleibt angemeldet und kommt beim naechsten Tick erneut: lieber zweimal
    gemeldet als gar nicht.
    """
    zugestellt = 0
    for watch in desk.pending():
        try:
            frame = status(watch.job_id)
        except Exception as fehler:
            _log(log, "notify.error",
                 {"job_id": watch.job_id, "stage": "status", "error": str(fehler)})
            continue
        text = None
        if not frame.get("ok"):
            # `unknown_job` ist ein Endzustand: der Worker wurde neu gestartet und
            # weiss von nichts. Alles andere (`unavailable`) ist voruebergehend —
            # der naechste Tick fragt erneut, statt einen laufenden Job fuer tot
            # zu erklaeren.
            if frame.get("kind") == "unknown_job":
                text = gone_text(watch)
        elif str(frame.get("state", "")) in TERMINAL:
            text = completion_text(watch, frame)
        if text is None:
            continue
        try:
            send(watch.conversation, text)
        except Exception as fehler:
            _log(log, "notify.error",
                 {"job_id": watch.job_id, "stage": "send", "error": str(fehler)})
            continue
        desk.drop(watch.job_id)
        _log(log, "notify.pushed", {"job_id": watch.job_id,
                                    "conversation": watch.conversation,
                                    "state": str(frame.get("state", "unknown_job"))})
        zugestellt += 1
    return zugestellt
