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

Kurskorrektur unterwegs (`delegate_steer`)
-------------------------------------------
Ein Hintergrundlauf ist der EINZIGE Nebenlauf, der sich unterwegs lenken laesst — und
das ist keine Produktentscheidung, sondern Bauart: er ist der einzige mit einer
Schrittgrenze in diesem Prozess. Dort liest `run_agent` ohnehin das Postfach fuer die
getippte Korrektur (`redirect.py`); ein Hintergrundlauf bekommt an derselben Naht sein
eigenes (`SteerInbox`). Was KEINE Schrittgrenze hier hat, ist ehrlicherweise nicht
steuerbar, und dafuer gibt es keine Schein-Steuerung:

- synchrone `delegate`-Untergebene laufen im Werkzeugaufruf des Hauptlaufs, ohne
  Postfach — wer sie umlenken will, wartet auf ihre Antwort und fragt neu;
- `delegate_code`/`delegate_dag`/`delegate_agy`-Jobs laufen als `claude -p` im
  Worker-Prozess, in den niemand hineinschreibt;
- Zeitplan-Laeufe stehen nicht auf diesem Schreibtisch.

Ein unbekannter Schluessel ist deshalb eine Absage mit Grund, nie ein stilles „ok".

⚠️ Eine Anweisung ist ein ZUG, kein Recht. Sie geht als gerahmter Text in die Historie
(`Steer.as_turn`), und jeder Werkzeugwunsch danach passiert denselben Kernel unter
derselben Decke wie vorher: `NEEDS_HUMAN` bleibt im Hintergrund `DENY`. Was die
Anweisung nicht kann, kann sie per Bauart nicht — sie ist Text in einer Liste.

⚠️ Herkunft wie beim Vordergrund-Postfach: nur DIESELBE Person aus DERSELBEN
Unterhaltung, die den Auftrag gestartet hat. Beides kommt aus dem Thread-Kontext des
lenkenden Laufs, nie aus den Werkzeug-Argumenten — das Modell entscheidet nicht, als
wer es lenkt. Ein Auftrag ohne aufgezeichnete Herkunft ist von niemandem lenkbar.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

__all__ = ["BackgroundDesk", "Steer", "SteerInbox", "SteerRefused", "Task"]

# Bewusst klein. Wer mehr als drei Fragen gleichzeitig stellt, bekommt keine Antworten,
# sondern eine ausgelastete Maschine.
MAX_CONCURRENT = 3
# Ein Auftrag, der laenger als das braucht, haengt. Dieselbe Groessenordnung wie das
# Zeitlimit von `talos ask`.
DEFAULT_TIMEOUT_S = 600
# Mehr als das staut sich nicht an (dieselbe Ueberlegung wie `redirect.MAX_PENDING`):
# wer dreimal nachschiebt, ohne dass ein Schritt vergeht, meint einen neuen Auftrag.
# Der Rest wird abgelehnt, und der Sprecher erfaehrt es.
MAX_PENDING_STEERS = 3
# Dieselbe Groesse wie `subagent.MAX_QUESTION_CHARS` — eine Anweisung ist so lang wie
# eine delegierte Frage. Hier als eigene Zahl, weil dieses Modul nichts aus dem Kernel
# importiert; ein Test haelt beide gleich.
MAX_STEER_CHARS = 400
# Der Rahmen, mit dem eine Anweisung in die Historie des Ziel-Laufs geht. Er sagt,
# woher sie kommt und was sie nicht ist — dass sie nichts erlaubt, entscheidet ohnehin
# der Kernel, aber der Rahmen soll es nicht einmal behaupten koennen. „Worded by the
# relaying run" ist Absicht: den Wortlaut hat das Modell des lenkenden Laufs gewaehlt,
# nicht der Mensch; nur die Herkunft (Person, Unterhaltung) ist die seine.
STEER_FRAME = (
    "[course correction via delegate_steer, relayed from the same person and conversation "
    "that started this background task — worded by the relaying run, no additional rights, "
    "this run's ceiling is unchanged]"
)

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
    """Ein laufender Auftrag. `number` ist fuer Menschen, `task_id` fuer das Protokoll.

    `principal`/`conversation` sind die HERKUNFT — wer den Auftrag getippt hat und wo.
    Sie entscheiden, wer ihn spaeter lenken darf (`steer`). Leer heisst: unbekannt,
    und unbekannt heisst: von niemandem lenkbar.
    """

    number: int
    task_id: str
    prompt: str
    principal: str = ""
    conversation: str = ""

    @property
    def short(self) -> str:
        return _short(self.prompt)


class SteerRefused(ValueError):
    """Die Anweisung wurde NICHT abgelegt — und der Grund steht im Text.

    Ein `ValueError`, damit der Executor daraus einen fehlgeschlagenen Werkzeugaufruf
    macht (`Status.ERROR` mit genau diesem Satz), der im Protokoll und in der
    Fussnote der Antwort auftaucht. Ein „ok" fuer etwas, das nicht geschah, waere die
    teuerste Antwort dieses Werkzeugs.
    """


@dataclass(frozen=True)
class Steer:
    """Eine Kurskorrektur, die einen laufenden Hintergrundauftrag erreicht hat."""

    text: str
    origin: str

    def as_turn(self) -> str:
        """Der Wortlaut, wie er in die Historie geht — gerahmt wie `redirect.Correction`."""
        return f"{STEER_FRAME}\n{self.text}"


@dataclass
class BackgroundDesk:
    """Wer gerade nebenher laeuft. Eine Instanz, geteilt zwischen Poll-Thread und Worker."""

    _running: dict[str, Task] = field(default_factory=dict, init=False)
    # Abgemeldete, aber noch auslaufende Auftraege (`/stopall`): die Markierung
    # ueberlebt den Platz in `_running`, weil der Worker-Thread sie an der
    # Schrittgrenze und beim Bericht noch abfragt — erst sein eigenes `finish`
    # tilgt sie.
    _cancelled: set[str] = field(default_factory=set, init=False, repr=False)
    # Wartende Kurskorrekturen je Auftrag (`delegate_steer`). Unter DEMSELBEN Schloss
    # wie der Rest: abgelegt wird aus dem Thread des lenkenden Laufs, genommen aus dem
    # des gelenkten — eine Liste ohne Schloss verliert dabei Eintraege.
    _steering: dict[str, tuple[Steer, ...]] = field(default_factory=dict, init=False, repr=False)
    _count: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def accept(self, prompt: str, *, run_id: str, principal: str = "",
               conversation: str = "") -> Task | None:
        """Nimmt einen Auftrag an — oder `None`, wenn schon genug laufen.

        Der Deckel wird HIER geprueft und nicht im Thread: eine Absage soll der Betreiber
        sofort lesen, nicht erst, wenn irgendwann nichts kommt.
        """
        with self._lock:
            if len(self._running) >= MAX_CONCURRENT:
                return None
            self._count += 1
            task = Task(self._count, f"bg_{run_id[:12]}", prompt, str(principal), str(conversation))
            self._running[task.task_id] = task
            return task

    def finish(self, task_id: str) -> None:
        with self._lock:
            self._running.pop(task_id, None)
            self._cancelled.discard(task_id)
            self._steering.pop(task_id, None)

    def steer(self, task_id: str, text: str, *, principal: str, conversation: str) -> Steer:
        """Legt eine Kurskorrektur fuer einen LAUFENDEN Auftrag ab — oder sagt, warum nicht.

        Steuerbar ist genau, was hier auf dem Schreibtisch liegt: Laeufe aus
        `/background`. Sie lesen das Postfach an ihrer naechsten Schrittgrenze; in den
        laufenden Modellaufruf greift niemand hinein. Alles andere (siehe Modul-Doku)
        hat keinen Einspeisepunkt und faellt hier als „unbekannt" heraus — ehrlich,
        statt eine Steuerung vorzutaeuschen.

        Abgelehnt wird, bevor etwas liegt: leere oder zu lange Anweisung (nicht
        gekuerzt — eine gekuerzte Anweisung kann das Gegenteil meinen), abgemeldeter
        Auftrag (`/stopall`: er beginnt keinen Schritt mehr, eine Anweisung dort waere
        ein Versprechen ohne Empfaenger), unbekannter oder fertiger Auftrag, fremde
        Herkunft, volles Postfach.
        """
        clean = " ".join(str(text).split())
        if not clean:
            raise SteerRefused("delegate_steer needs an instruction — nothing was queued")
        if len(clean) > MAX_STEER_CHARS:
            raise SteerRefused(
                f"the instruction has {len(clean)} characters, the cap is {MAX_STEER_CHARS} — "
                "shorten it; nothing was truncated and nothing was queued"
            )
        if not principal or not conversation:
            raise SteerRefused(
                "no conversation context — a steer must come from a run that belongs to a "
                "person and a conversation; nothing was queued"
            )
        with self._lock:
            if task_id in self._cancelled:
                raise SteerRefused(
                    f"background task {task_id} was stopped (/stopall) — it takes no more "
                    "instructions; nothing was queued"
                )
            task = self._running.get(task_id)
            if task is None:
                raise SteerRefused(
                    f"no running background task with id {task_id!r} (unknown, or already "
                    "finished — only /background tasks can be steered); nothing was queued"
                )
            # BEIDES muss stimmen, wie im Vordergrund-Postfach (`redirect.offer`). Ein
            # Auftrag ohne aufgezeichnete Herkunft faellt hier per Konstruktion durch.
            if (task.principal, task.conversation) != (principal, conversation):
                raise SteerRefused(
                    f"background task {task_id} belongs to another person or conversation — "
                    "only the one that started it may steer it; nothing was queued"
                )
            pending = self._steering.get(task_id, ())
            if len(pending) >= MAX_PENDING_STEERS:
                raise SteerRefused(
                    f"background task {task_id} already has {len(pending)} pending "
                    "instructions — wait for its next step; nothing was queued"
                )
            steer = Steer(clean, principal)
            self._steering = {**self._steering, task_id: pending + (steer,)}
            return steer

    def take_steering(self, task_id: str) -> tuple[Steer, ...]:
        """Holt alles Wartende fuer diesen Auftrag und leert sein Postfach."""
        with self._lock:
            wartend = self._steering.get(task_id, ())
            self._steering = {k: v for k, v in self._steering.items() if k != task_id}
            return wartend

    def inbox(self, task_id: str, *, taken: Callable[[Steer], None] | None = None) -> "SteerInbox":
        """Das Postfach EINES Auftrags in der Form, die `run_agent` liest."""
        return SteerInbox(self, task_id, taken=taken)

    def cancel(self, task_id: str) -> bool:
        """Meldet einen laufenden Auftrag ab. True, wenn er noch lief.

        ⚠️ „Abgemeldet", nicht „getoetet": ein laufender Modellaufruf ist ein
        blockierender Subprozess und bleibt unantastbar. Der Auftrag beendet den
        aktuellen Denkschritt noch, beginnt aber keinen neuen (die Schleife fragt
        `was_cancelled` an jeder Schrittgrenze), und sein Bericht wird verworfen
        statt zugestellt. Der Platz wird sofort frei — wer abgemeldet ist, soll
        keinen der drei Plaetze halten, sonst stuende der Betreiber nach einem
        Not-Halt vor „Too many background tasks".
        """
        with self._lock:
            if task_id not in self._running:
                return False
            self._running.pop(task_id)
            self._cancelled.add(task_id)
            # Wartende Anweisungen fallen mit: der Lauf beginnt keinen Schritt mehr,
            # an dem er sie laese.
            self._steering = {k: v for k, v in self._steering.items() if k != task_id}
            return True

    def cancel_all(self) -> tuple[Task, ...]:
        """Meldet alles Laufende ab (`/stopall`) — und gibt zurueck, was wirklich lief.

        Die Bilanz des Kommandos baut auf diesem Rueckgabewert auf: eine Zahl,
        die Auftraege mitzaehlt, die gar nicht liefen, waere eine Behauptung.
        """
        with self._lock:
            tasks = tuple(self._running.values())
            self._cancelled |= set(self._running)
            self._running = {}
            self._steering = {}
            return tasks

    def was_cancelled(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def running(self) -> tuple[Task, ...]:
        with self._lock:
            return tuple(self._running.values())

    def busy(self) -> int:
        with self._lock:
            return len(self._running)

    def full(self) -> bool:
        return self.busy() >= MAX_CONCURRENT


class SteerInbox:
    """Das Postfach EINES Hintergrundlaufs, wie `run_agent` es an der Schrittgrenze liest.

    Dieselbe Naht wie `redirect.Redirect` (`take()`, Eintraege mit `as_turn()`) — mit
    Absicht keine zweite: eine Korrektur wird genau dort eingelegt, wo der naechste Zug
    aus der Historie gebildet wird, egal ob sie getippt oder ueber `delegate_steer`
    adressiert wurde. `taken` ist der Beleg des Conductors (Event `background.steered`
    unter dem run_id des GELENKTEN Laufs); der Schreibtisch selbst kennt kein Protokoll.
    """

    def __init__(self, desk: BackgroundDesk, task_id: str, *,
                 taken: Callable[[Steer], None] | None = None) -> None:
        self._desk = desk
        self._task_id = task_id
        self._taken = taken

    def take(self) -> tuple[Steer, ...]:
        steers = self._desk.take_steering(self._task_id)
        if self._taken is not None:
            for steer in steers:
                self._taken(steer)
        return steers


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
