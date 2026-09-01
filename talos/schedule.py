"""Zeitgesteuerte Auftraege — und warum sie WENIGER duerfen als getippte.

Die groesste Luecke gegenueber verbreiteten Agenten-Frameworks: dort laufen
Cron-Jobs, Heartbeats und Watcher ohne Anstoss. Die entscheidende Formulierung
trifft es — „Talos wartet auf eine Nachricht, sonst existiert der Tag fuer ihn nicht."

Der naheliegende Weg waere, es genauso zu machen. Genau das geht hier nicht, und der
Grund ist keine Schwaeche, sondern die Bauart: bei den anderen haengt die Schranke am
Urteil des Modells, also kostet ein unbeaufsichtigter Lauf dort nichts extra. Bei Talos
entscheidet ein Kernel, und der kennt genau ein Urteil, das einen MENSCHEN braucht:
`NEEDS_HUMAN`. Ein Lauf um 04:00 Uhr kann diesen Menschen nicht fragen.

Drei Antworten waeren moeglich gewesen, zwei davon falsch:

  1. **Parken und warten.** Der Auftrag bleibt liegen, bis jemand aufwacht. Klingt
     harmlos, ist es nicht: um 09:00 Uhr steht eine Freigabefrage im Chat, deren Anlass
     sechs Stunden zurueckliegt und deren Vorgeschichte niemand mehr im Kopf hat. Genau
     so entsteht das Wegklicken, gegen das der Kernel gebaut wurde.
  2. **Automatisch zustimmen.** Damit waere die zeitgesteuerte Ausfuehrung ein zweiter
     Erlaubnisweg neben dem Kernel — das, was `CLAUDE.md` als Kardinalfehler benennt.
  3. **Nicht ausfuehren, sondern berichten.** Was ohne Rueckfrage laufen darf, laeuft.
     Alles andere wird NICHT geparkt, sondern abgebrochen und im Bericht genannt — mit
     dem Grund des Kernels. Der Betreiber entscheidet danach wach und im Kontext.

Wir nehmen die dritte. Damit ist ein unbeaufsichtigter Lauf **strukturell schwaecher**
als ein beaufsichtigter — die Umkehrung dessen, was verbreitete Agenten-Frameworks tun, wo ein
Cron-Job dieselbe (volle) Macht hat wie ein getippter Befehl. Das ist der Punkt, an dem
Talos nicht gleichzieht, sondern vorbeigeht: **Autonomie ohne Machtzuwachs.**

Die Decke (`UnattendedCeiling`) ist die dritte im Haus, neben Autonomie-Regler und
Kanal-Stufe, und gehorcht derselben Regel wie die beiden anderen: sie kann
ausschliesslich verschaerfen. `stricter` laesst per Konstruktion nichts Milderes durch.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .manifest import ToolSpec
from .policy import Decision, ToolRequest, Verdict, stricter

# Kein Sekundentakt: ein Zeitplan ist kein Timer. Der kleinste sinnvolle Abstand haelt
# auch versehentliche Dauerlaeufe („alle 5 Sekunden") vom System fern.
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 7 * 24 * 3600
MAX_TASKS = 20
MAX_PROMPT_CHARS = 500
# Eine Sonde ist ein Sensor, kein Skript: eine Zeile, kurz genug, dass `/schedules`
# und das Protokoll sie ganz zeigen. Was mehr braucht, gehoert in den Auftrag selbst.
MAX_PROBE_CHARS = 300
# Was ein Lauf seinem Nachfolger hinterlaesst. Gedeckelt, weil das Ergebnis als Daten
# in den naechsten Prompt wandert — ein ungedeckeltes Gedaechtnis waere ein Prompt,
# der mit jedem Lauf waechst.
MAX_RESULT_CHARS = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id               TEXT PRIMARY KEY,
    conversation     TEXT NOT NULL,
    principal        TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    interval_s       INTEGER NOT NULL,
    cron             TEXT NOT NULL DEFAULT '',
    once             INTEGER NOT NULL DEFAULT 0,
    next_run         REAL NOT NULL,
    created          REAL NOT NULL,
    last_run         REAL,
    continuity       INTEGER NOT NULL DEFAULT 0,
    monitor          INTEGER NOT NULL DEFAULT 0,
    probe            TEXT NOT NULL DEFAULT '',
    last_fingerprint TEXT NOT NULL DEFAULT '',
    last_result      TEXT NOT NULL DEFAULT '',
    last_error_key   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_schedules_next ON schedules(next_run);
"""

# Spalten, die eine BESTEHENDE Datei nachtraeglich bekommt — additiv, mit Vorgabe
# „aus". Reihenfolge = Reihenfolge der Einfuehrung; nie eine Zeile entfernen.
_MIGRATIONS = (
    ("cron", "TEXT NOT NULL DEFAULT ''"),
    ("once", "INTEGER NOT NULL DEFAULT 0"),
    ("continuity", "INTEGER NOT NULL DEFAULT 0"),
    ("monitor", "INTEGER NOT NULL DEFAULT 0"),
    ("probe", "TEXT NOT NULL DEFAULT ''"),
    ("last_fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("last_result", "TEXT NOT NULL DEFAULT ''"),
    ("last_error_key", "TEXT NOT NULL DEFAULT ''"),
)

# EINE Spaltenliste fuer jede Leseabfrage: `_task_from_row` zaehlt Positionen, und zwei
# abweichende SELECTs waeren zwei Gelegenheiten, ein Feld in die falsche Spalte zu lesen.
_COLUMNS = (
    "id, conversation, principal, prompt, interval_s, next_run, created, last_run,"
    " cron, once, continuity, monitor, probe, last_fingerprint, last_result, last_error_key"
)

UNATTENDED_REASON = (
    "unattended run — anything that needs your approval is reported, never performed"
)


@dataclass(frozen=True)
class Task:
    """Ein wiederkehrender Auftrag. `prompt` ist Text des Betreibers, nie des Modells."""

    id: str
    conversation: str
    principal: str
    prompt: str
    interval_s: int
    next_run: float
    created: float
    last_run: float | None = None
    # Ein Ausdruck statt eines Abstands. Leer heisst Intervall — beide Wege enden im
    # selben `next_run`, damit `due()` nichts von der Unterscheidung wissen muss.
    cron: str = ""
    # Einmalig: nach dem Lauf wird der Auftrag geloescht statt neu terminiert. Ein
    # „erinnere mich morgen um 9" darf nicht zum taeglichen Wecker werden.
    once: bool = False
    # Gedaechtnis: das Ergebnis des Vorlaufs geht als DATEN in den naechsten Prompt, und
    # ein Lauf, der im selben Fehler endet wie sein Vorgaenger, wird nur protokolliert.
    continuity: bool = False
    # Monitor: vor dem Modellzug liest eine Sonde (`probe`, ein Shell-Kommando) den
    # Zustand; ist er unveraendert, faellt der Modellzug aus. Die Sonde ist ein
    # gewoehnlicher `run_shell` des Auftrag-Principals — sie bekommt nichts, was ein
    # Zeitplan-Lauf nicht auch bekaeme (siehe `continuity.py`).
    monitor: bool = False
    probe: str = ""
    # Lauf-Zustand, vom Ticker geschrieben, nie vom Betreiber. Leer = noch kein Lauf.
    last_fingerprint: str = ""
    last_result: str = ""
    last_error_key: str = ""

    def describe(self) -> str:
        if self.cron:
            wann = f"at {self.cron}"
        elif self.once:
            wann = "once"
        else:
            minutes = self.interval_s // 60
            wann = f"every {minutes} min" if minutes < 60 else f"every {minutes // 60} h"
        schalter = [name for name, an in (("continuity", self.continuity),
                                          ("monitor", self.monitor)) if an]
        zusatz = f"  [{', '.join(schalter)}]" if schalter else ""
        return f"{self.id}  {wann}  — {self.prompt}{zusatz}"


def _flag(value: object) -> bool:
    """Nur eine echte 1 schaltet ein. NULL, Text, 2, „banana" — alles heisst AUS.

    Bewusst strenger als `bool()`: diese Schalter geben dem Lauf etwas dazu (ein
    Gedaechtnis, eine Sonde). Ein kaputter Wert darf eine Funktion nur abschalten,
    nie eine anschalten, die der Betreiber nicht angelegt hat. `once` bleibt bei
    `bool()`, weil dort die sichere Richtung die andere ist: ein Einmal-Auftrag, der
    wegen Muell in der Spalte zum Dauerwecker wuerde, ist das schlimmere Ergebnis.
    """
    return value is True or (type(value) is int and value == 1)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _task_from_row(r: tuple) -> Task:
    probe = _text(r[12])
    return Task(
        id=r[0], conversation=r[1], principal=r[2], prompt=r[3], interval_s=int(r[4]),
        next_run=float(r[5]), created=float(r[6]),
        last_run=float(r[7]) if r[7] is not None else None,
        cron=str(r[8]) if r[8] else "",
        once=bool(r[9]),
        continuity=_flag(r[10]),
        # Ein Monitor ohne Sonde hat nichts zu lesen — also ist er keiner, egal was
        # die Spalte sagt. Sonst liefe der Auftrag gegen einen leeren Befehl.
        monitor=_flag(r[11]) and bool(probe),
        probe=probe,
        last_fingerprint=_text(r[13]),
        last_result=_text(r[14]),
        last_error_key=_text(r[15]),
    )


class UnattendedCeiling:
    """Die dritte Decke: kein Mensch da, also nichts, was einen Menschen braucht.

    Wirkt NUR waehrend eines zeitgesteuerten Laufs (`active()`), thread-gebunden wie
    `AskContexts` — ein gleichzeitig getippter Auftrag im selben Prozess bleibt davon
    unberuehrt. Ohne diese Bindung waere die Decke entweder immer an (dann koennte der
    Betreiber nichts mehr freigeben) oder immer aus (dann waere sie wirkungslos).

    `NEEDS_HUMAN` wird zu `DENY`. Nicht, weil die Handlung schlimmer geworden waere,
    sondern weil der einzige, der sie erlauben koennte, gerade nicht da ist. Der Grund
    sagt das auch — er wird im Bericht woertlich zitiert.
    """

    def __init__(self) -> None:
        self._threads: set[int] = set()
        self._lock = threading.Lock()

    def active(self) -> "_Unattended":
        return _Unattended(self)

    def is_unattended(self) -> bool:
        with self._lock:
            return threading.get_ident() in self._threads

    def _enter(self) -> None:
        with self._lock:
            self._threads = self._threads | {threading.get_ident()}

    def _leave(self) -> None:
        with self._lock:
            self._threads = self._threads - {threading.get_ident()}

    def apply(self, decision: Decision, spec: ToolSpec | None = None) -> Decision:
        """Verschaerft — und kann per Konstruktion nichts erlauben."""
        if not self.is_unattended() or decision.verdict is not Verdict.NEEDS_HUMAN:
            return decision
        return stricter(decision, Decision(Verdict.DENY, UNATTENDED_REASON))


class _Unattended:
    def __init__(self, ceiling: UnattendedCeiling) -> None:
        self._ceiling = ceiling

    def __enter__(self) -> None:
        self._ceiling._enter()

    def __exit__(self, *_exc: object) -> None:
        self._ceiling._leave()


class ScheduleStore:
    """Durable Zeitplaene neben Event-Log und Archiv. Fail-open wie `recall.py`.

    Ein kaputter Speicher heisst „keine Zeitplaene", nie „Agent steht". Zeitplaene sind
    kein Gate: sie erteilen keine Rechte, sie stossen nur an — und was dann laeuft, geht
    ohnehin durch den Kernel.
    """

    def __init__(self, db_path: Path, *, max_tasks: int = MAX_TASKS) -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._max = max(0, int(max_tasks))
        self.reason = ""
        try:
            path = Path(db_path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
            os.chmod(path, 0o600)
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _migrate(conn)
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError, ValueError) as error:
            self._conn = None
            self.reason = str(error)

    @property
    def available(self) -> bool:
        return self._conn is not None

    def add(
        self, *, conversation: str, principal: str, prompt: str, interval_s: int = 0,
        cron: str = "", once: bool = False, now: float | None = None,
        continuity: bool = False, monitor: bool = False, probe: str = "",
    ) -> Task | None:
        """Legt einen Auftrag an. `None` = abgelehnt (Grenzen, kein Speicher).

        Drei Wege, ein Ergebnis: Intervall, Cron-Ausdruck oder Einmal-Termin muenden
        alle in ein `next_run`. `due()` kennt die Unterscheidung deshalb gar nicht —
        sie faellt beim Anlegen und beim Nachterminieren, sonst nirgends.

        `continuity`, `monitor` und `probe` sind Schalter des Betreibers (Blueprint),
        nie des Modells. Sie erteilen nichts: die Sonde ist ein `run_shell` wie jeder
        andere, das Gedaechtnis ist Text, der als Daten in den Prompt geht.
        """
        text = " ".join(str(prompt).split())[:MAX_PROMPT_CHARS]
        seconds = int(interval_s)
        if not text or self._conn is None:
            return None
        sonde = validate_probe(probe, monitor=bool(monitor))
        started = time.time() if now is None else float(now)
        if cron:
            from .cron import parse as _cron_parse

            naechster = _cron_parse(cron).next_after(started)
            seconds = 0
        elif once:
            if seconds < 0:
                raise ValueError("a one-shot needs a moment in the future")
            naechster = started + seconds
            seconds = 0
        elif not MIN_INTERVAL_S <= seconds <= MAX_INTERVAL_S:
            raise ValueError(
                f"interval must be {MIN_INTERVAL_S}..{MAX_INTERVAL_S} seconds"
            )
        else:
            naechster = started + seconds
        with self._lock:
            if self._conn is None:
                return None
            try:
                total = int(self._conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0])
                if total >= self._max:
                    raise ValueError(f"at most {self._max} schedules")
                task = Task(
                    id=uuid.uuid4().hex[:8],
                    conversation=str(conversation),
                    principal=str(principal),
                    prompt=text,
                    interval_s=seconds,
                    next_run=naechster,
                    created=started,
                    cron=str(cron),
                    once=bool(once),
                    continuity=bool(continuity),
                    monitor=bool(monitor),
                    probe=sonde,
                )
                self._conn.execute(
                    "INSERT INTO schedules (id, conversation, principal, prompt, interval_s,"
                    " cron, once, next_run, created, last_run, continuity, monitor, probe)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                    (task.id, task.conversation, task.principal, task.prompt,
                     task.interval_s, task.cron, int(task.once), task.next_run, task.created,
                     int(task.continuity), int(task.monitor), task.probe),
                )
                self._conn.commit()
                return task
            except sqlite3.Error:
                self._conn.rollback()
                return None

    def due(self, *, now: float | None = None) -> tuple[Task, ...]:
        """Faellige Auftraege. Der Aufrufer quittiert mit `mark_run`."""
        moment = time.time() if now is None else float(now)
        return self._rows(
            f"SELECT {_COLUMNS} FROM schedules WHERE next_run <= ? ORDER BY next_run",
            (moment,),
        )

    def list_for(self, conversation: str) -> tuple[Task, ...]:
        return self._rows(
            f"SELECT {_COLUMNS} FROM schedules WHERE conversation = ? ORDER BY created",
            (str(conversation),),
        )

    def record_probe(self, task_id: str, fingerprint: str) -> None:
        """Merkt den Abdruck einer ERFOLGREICH gelesenen Sonde.

        Nur der Ticker ruft das, und nur nach einer Lesung, die der Kernel durchgelassen
        und die Shell zu Ende gebracht hat. Ein Fehlversuch ist kein Messwert und
        ueberschreibt den letzten guten Abdruck nicht — sonst hiesse „Sonde kaputt"
        beim naechsten Mal „unveraendert".
        """
        self._update("UPDATE schedules SET last_fingerprint = ? WHERE id = ?",
                     (str(fingerprint), str(task_id)))

    def record_result(self, task_id: str, *, result: str, error_key: str) -> None:
        """Merkt, wie der Lauf ausging: seinen Text (gedeckelt) und seinen Fehlerschluessel.

        Der Deckel liegt HIER und nicht beim Aufrufer, weil der Text als Daten in den
        naechsten Prompt wandert — die Grenze gehoert an die Stelle, die schreibt.
        """
        self._update(
            "UPDATE schedules SET last_result = ?, last_error_key = ? WHERE id = ?",
            (str(result)[:MAX_RESULT_CHARS], str(error_key), str(task_id)),
        )

    def _update(self, sql: str, params: tuple) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()

    def mark_run(self, task_id: str, *, now: float | None = None) -> None:
        """Setzt den naechsten Termin — VOR der Ausfuehrung aufzurufen.

        Sonst laeuft ein Auftrag, der laenger dauert als sein Intervall, beim naechsten
        Tick erneut an und ueberholt sich selbst.

        Drei Faelle: ein Ausdruck bekommt seinen naechsten Kalendertermin, ein
        Einmal-Auftrag wird GELOESCHT (ein „erinnere mich morgen um 9" darf nicht zum
        taeglichen Wecker werden), ein Intervall zaehlt weiter wie bisher.
        """
        moment = time.time() if now is None else float(now)
        with self._lock:
            if self._conn is None:
                return
            try:
                zeile = self._conn.execute(
                    "SELECT cron, once FROM schedules WHERE id = ?", (str(task_id),)
                ).fetchone()
                ausdruck = (zeile[0] if zeile else "") or ""
                einmal = bool(zeile[1]) if zeile else False
                if einmal:
                    self._conn.execute("DELETE FROM schedules WHERE id = ?", (str(task_id),))
                elif ausdruck:
                    from .cron import CronError, parse as _cron_parse

                    try:
                        naechster = _cron_parse(ausdruck).next_after(moment)
                    except CronError:
                        # Ein Ausdruck, der nicht mehr aufgeht, darf den Auftrag nicht in
                        # eine Dauerschleife schicken: dann lieber weg als jede Minute.
                        self._conn.execute("DELETE FROM schedules WHERE id = ?", (str(task_id),))
                        self._conn.commit()
                        return
                    self._conn.execute(
                        "UPDATE schedules SET last_run = ?, next_run = ? WHERE id = ?",
                        (moment, naechster, str(task_id)),
                    )
                else:
                    self._conn.execute(
                        "UPDATE schedules SET last_run = ?, next_run = ? + interval_s"
                        " WHERE id = ?",
                        (moment, moment, str(task_id)),
                    )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()

    def remove(self, task_id: str, *, conversation: str) -> bool:
        """Loescht — nur aus der eigenen Konversation.

        Die Einschraenkung ist keine Kosmetik: ohne sie koennte ein zweiter Chat die
        Auftraege des ersten abstellen, und „mein Waechter meldet sich nicht mehr" waere
        von einem Defekt nicht zu unterscheiden.
        """
        with self._lock:
            if self._conn is None:
                return False
            try:
                cursor = self._conn.execute(
                    "DELETE FROM schedules WHERE id = ? AND conversation = ?",
                    (str(task_id), str(conversation)),
                )
                self._conn.commit()
                return cursor.rowcount > 0
            except sqlite3.Error:
                self._conn.rollback()
                return False

    def count(self) -> int:
        with self._lock:
            if self._conn is None:
                return 0
            try:
                return int(self._conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0])
            except sqlite3.Error:
                return 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _rows(self, sql: str, params: tuple) -> tuple[Task, ...]:
        with self._lock:
            if self._conn is None:
                return ()
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                return ()
        return tuple(_task_from_row(r) for r in rows)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bringt eine BESTEHENDE Datei auf den Stand von `_SCHEMA` — additiv und tolerant.

    `CREATE TABLE IF NOT EXISTS` ergaenzt keine Spalten. Ohne diese Funktion startet ein
    Update gegen eine aeltere Datenbank und faellt beim ersten Anlegen um — auf einer
    laufenden Instanz, deren Zeitplaene bis dahin funktioniert haben.

    Tolerant heisst: schlaegt ein `ALTER` fehl (ein zweiter Prozess war schneller), zaehlt
    nur, ob die Spalte danach da ist. Fehlt sie wirklich, fliegt der Fehler weiter und der
    Speicher gilt als nicht verfuegbar — „keine Zeitplaene" ist ehrlicher als Zeitplaene
    gegen ein Schema, das die Leseabfrage nicht versteht.
    """
    vorhanden = {row[1] for row in conn.execute("PRAGMA table_info(schedules)")}
    for spalte, typ in _MIGRATIONS:
        if spalte in vorhanden:
            continue
        try:
            conn.execute(f"ALTER TABLE schedules ADD COLUMN {spalte} {typ}")
        except sqlite3.OperationalError:
            jetzt = {row[1] for row in conn.execute("PRAGMA table_info(schedules)")}
            if spalte not in jetzt:
                raise


def validate_probe(probe: object, *, monitor: bool) -> str:
    """Die Sonde beim Anlegen pruefen — Tippfehler sollen den Betreiber sofort erreichen.

    Monitor und Sonde gehoeren zusammen: ein Monitor ohne Sonde haette nichts zu
    vergleichen, eine Sonde ohne Monitor liefe nie. Beides ist ein Irrtum, kein
    Zustand. Eine Zeile und gedeckelt, weil eine Sonde ein Sensor ist, kein Skript —
    und weil `/schedules` und das Protokoll sie ganz zeigen sollen.
    """
    text = str(probe or "").strip()
    if bool(text) != monitor:
        raise ValueError("monitor and probe belong together — set both or neither")
    if "\n" in text or "\r" in text:
        raise ValueError("a probe is one line — put anything longer into the task itself")
    if len(text) > MAX_PROBE_CHARS:
        raise ValueError(f"a probe is at most {MAX_PROBE_CHARS} characters")
    return text
