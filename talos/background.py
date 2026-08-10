"""Hintergrund-Laeufe — eine zweite Aufgabe, ohne die erste anzuhalten.

Der Anlass ist alltaeglich: „durchsuch mal die Protokolle" dauert zwei Minuten, und
solange steht der Chat. Andere Agenten loesen das mit einer nebenherlaufenden Sitzung;
Talos hatte dafuer nichts — der Worker hat genau einen Platz, und der ist besetzt.

⚠️ **Ein Hintergrundlauf ist sicherheitstechnisch ein ZEITPLAN-Lauf, kein Subagent.**

Das ist die ganze Einordnung, und sie erspart ein neues Sicherheitskonzept:

- **Kein Subagent.** Der entsteht aus Modelltext und darf deshalb ausschliesslich lesen
  (`subagent.ReadOnlyCeiling`). Hier tippt ein MENSCH den Auftrag — die Herkunft ist
  dieselbe wie bei jeder anderen Nachricht.
- **Wie ein Zeitplan.** Niemand sitzt davor und wartet. Also gilt dieselbe Decke wie beim
  zeitgesteuerten Lauf: `NEEDS_HUMAN` wird `DENY`, mit Ansage. Eine Rueckfrage in einen
  Chat zu stellen, in dem gerade ein ganz anderes Gespraech laeuft, ist die zuverlaessigste
  Art, ein „ja" auf den falschen Vorgang fallen zu lassen.

Zwei weitere Entscheidungen:

⚠️ **Der Kontext ist leer.** Der Hintergrundlauf bekommt den Auftrag und sonst nichts —
keinen Verlauf, kein Gedaechtnis der laufenden Unterhaltung. Nicht aus Sparsamkeit: zwei
Laeufe, die sich denselben Verlauf teilen, schreiben einander hinein, und hinterher ist
nicht mehr zu sagen, welcher was gesagt hat. Das Ergebnis wandert aus demselben Grund
auch NICHT in den Verlauf zurueck — es kommt als eigene Nachricht an und ist damit das,
was es ist: ein Bericht, keine Fortsetzung.

⚠️ **Gedeckelt.** Jeder Lauf kostet einen Thread, Token und Modellzeit. Ohne Deckel legt
ein Dutzend getippter Auftraege die Maschine lahm — und der Betreiber saehe nur, dass
nichts mehr antwortet.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

__all__ = ["BackgroundDesk", "Task"]

# Bewusst klein. Wer mehr als drei Fragen gleichzeitig stellt, bekommt keine Antworten,
# sondern eine ausgelastete Maschine.
MAX_CONCURRENT = 3
# Ein Auftrag, der laenger als das braucht, haengt. Dieselbe Groessenordnung wie das
# Zeitlimit von `talos ask`.
DEFAULT_TIMEOUT_S = 600

STARTED = "Background #{n} started: {kurz}\n  id {task_id} · runs unattended, so anything needing approval is refused"
FULL = "Too many background tasks already running ({n}). Wait for one to finish."
EMPTY = "usage: /background <what should run on the side>"
# ⚠️ Der Bericht ist als BERICHT ausgewiesen, nicht als Fortsetzung des Gespraechs. Ohne
# diese Kennzeichnung liest sich eine Hintergrundantwort wie eine Antwort auf die zuletzt
# gestellte Frage — und genau das ist sie nicht.
RESULT = "Background #{n} finished — {kurz}\n\n{text}"
FAILED = "Background #{n} failed — {kurz}\n\n{text}"


def _short(prompt: str, limit: int = 56) -> str:
    einzeilig = " ".join(str(prompt).split())
    return einzeilig if len(einzeilig) <= limit else einzeilig[: limit - 1] + "…"


@dataclass(frozen=True)
class Task:
    """Ein laufender Auftrag. `number` ist fuer Menschen, `task_id` fuer das Protokoll."""

    number: int
    task_id: str
    prompt: str

    @property
    def short(self) -> str:
        return _short(self.prompt)


@dataclass
class BackgroundDesk:
    """Wer gerade nebenher laeuft. Eine Instanz, geteilt zwischen Poll-Thread und Worker."""

    _running: dict[str, Task] = field(default_factory=dict, init=False)
    _count: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def accept(self, prompt: str, *, run_id: str) -> Task | None:
        """Nimmt einen Auftrag an — oder `None`, wenn schon genug laufen.

        Der Deckel wird HIER geprueft und nicht im Thread: eine Absage soll der Betreiber
        sofort lesen, nicht erst, wenn irgendwann nichts kommt.
        """
        with self._lock:
            if len(self._running) >= MAX_CONCURRENT:
                return None
            self._count += 1
            task = Task(self._count, f"bg_{run_id[:12]}", prompt)
            self._running[task.task_id] = task
            return task

    def finish(self, task_id: str) -> None:
        with self._lock:
            self._running.pop(task_id, None)

    def running(self) -> tuple[Task, ...]:
        with self._lock:
            return tuple(self._running.values())

    def busy(self) -> int:
        with self._lock:
            return len(self._running)

    def full(self) -> bool:
        return self.busy() >= MAX_CONCURRENT


def receipt(task: Task) -> str:
    """Die Quittung, die der Betreiber sofort bekommt.

    Sie nennt die Decke ausdruecklich. Sonst wundert er sich in zwei Minuten ueber ein
    `DENY`, das im Vordergrund eine Rueckfrage gewesen waere — und haelt es fuer einen
    Fehler statt fuer die Regel.
    """
    return STARTED.format(n=task.number, kurz=task.short, task_id=task.task_id)


def header(task: Task) -> str:
    """Die Zeile ÜBER dem Bericht.

    ⚠️ Ein Kopf, kein Fuss — und das ist gemessen, nicht gemeint: der erste Lauf im
    Betrieb lieferte seine Antwort voellig ungekennzeichnet mitten ins Gespraech, direkt
    hinter den Eingabe-Prompt. Wer das liest, haelt es fuer die Antwort auf seine letzte
    Frage. Ein Hinweis unter dem Text kaeme zu spaet: da ist er schon falsch verstanden.
    """
    return f"— Background #{task.number} · {task.short}"


def report(task: Task, text: str, *, ok: bool = True) -> str:
    vorlage = RESULT if ok else FAILED
    return vorlage.format(n=task.number, kurz=task.short, text=(text or "").strip() or "(no output)")
