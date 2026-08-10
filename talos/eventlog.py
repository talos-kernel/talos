"""Append-only Event-Log (SQLite/WAL) mit Idempotenz.

Kernlehre aus Hermes + OpenClaw: durabler Event-Log, überlebt Reboot/OOM.
Idempotency-Key verhindert Doppelverarbeitung derselben Telegram-Update.

Seit dem Worker-Thread schreiben zwei Threads: der Poll-Thread (Kommandos, Freigaben)
und der Worker (Reasoner-Läufe). `check_same_thread=False` erlaubt das nur — es
serialisiert nichts. Deshalb liegt hier ein Lock: sonst können zwei `execute`+`commit`
verschränken und ein Commit die Transaktion des anderen mitnehmen.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    run_id          TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    idempotency_key TEXT    UNIQUE,
    payload_json    TEXT    NOT NULL,
    chain_hash      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""

# --- Die Hash-Kette: das Log beweist sich selbst -------------------------------------
# Jeder Eintrag traegt den Hash seines Vorgaengers (Git-/Merkle-Prinzip). Wer einen
# Eintrag nachtraeglich aendert oder loescht, bricht die Kette an genau dieser Stelle,
# und `verify` nennt die erste gebrochene id. ⚠️ Ehrliche Grenze: local root kann die
# GANZE Kette neu berechnen — dagegen schuetzt das nicht und behauptet es auch nicht
# (siehe SECURITY.md). Es macht das PUNKTUELLE Faelschen einzelner Zeilen unmoeglich,
# ohne alle folgenden neu zu schreiben, und jeden Bruch sichtbar.
_GENESIS = "talos-eventlog-genesis-v1"


def _norm_ts(ts: float) -> float:
    """Ein Zeitstempel, der den SQLite-REAL-Roundtrip bitgleich uebersteht.

    ⚠️ Sonst haengt die Kette an einer stillen Annahme. Ein Review zeigte: SQLite macht
    aus `-0.0` ein `0.0` und aus `NaN` ein `NULL` — beim Zurueckhashen in `verify` waeche
    der String dann ab und ein unberuehrtes Log meldete sich als gebrochen. `time.time()`
    liefert das nie, aber `append(now=...)` nimmt jeden Wert. Endliche Werte roundtrippen
    exakt; nicht-endliche haben als Ereigniszeit keinen Sinn und fliegen laut.
    """
    ts = float(ts)
    if not math.isfinite(ts):
        raise ValueError(f"event timestamp must be finite, got {ts!r}")
    return ts + 0.0  # normalisiert -0.0 -> 0.0


def _chain_hash(prev: str, ts: float, run_id: str, actor: str, type_: str,
                key: str | None, payload_json: str) -> str:
    """Der Kettenglied-Hash ueber Vorgaenger + alle unveraenderlichen Felder der Zeile.

    Laengen-gerahmt statt mit einem Trennzeichen verbunden: jedes Feld geht mit seiner
    Byte-Laenge voran in den Hash. ⚠️ Ein blosses `sep.join(...)` waere nicht injektiv —
    ein Review konstruierte zwei verschiedene Feld-Tupel, die zu demselben String joinen
    (`key="k", payload="p\\x1eq"` gegen `key="k\\x1ep", payload="q"`). `json.dumps` escaped
    Steuerzeichen zwar, sodass der reale Pfad das Trennzeichen nie enthaelt — aber eine
    Sicherheitseigenschaft an „kein Feld enthaelt je das Trennzeichen" aufzuhaengen ist
    genau die Art stille Annahme, die spaeter kippt. Mit Laengenpraefix ist die
    Serialisierung eindeutig, egal was in den Feldern steht.

    ⚠️ `repr(ts)` (nicht `str`): die kuerzeste rundreisetreue float-Darstellung. Zusammen
    mit `_norm_ts` hashen `append` und `verify` garantiert denselben String.

    ⚠️ Die `id` (rowid) ist BEWUSST nicht drin: sie ist keine Ereignis-Aussage, sondern
    die Position. `verify` liest `ORDER BY id`, also faellt jedes Umnummerieren, das die
    Reihenfolge aendert, ohnehin ueber die gebrochene Vorgaenger-Verkettung auf; ein
    Relabel, das die Reihenfolge WAHRT, aendert nichts, was das Log behauptet.
    """
    h = hashlib.sha256()
    for part in (prev, repr(ts), run_id, actor, type_, key or "", payload_json):
        raw = part.encode("utf-8")
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return h.hexdigest()


@dataclass(frozen=True)
class Event:
    run_id: str
    actor: str
    type: str
    payload: dict
    idempotency_key: str | None = None


def new_run_id() -> str:
    return uuid.uuid4().hex


class EventLog:
    """Dünner, thread-sicherer Wrapper um eine WAL-SQLite-Datei."""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # Migration fuer Logs, die vor der Hash-Kette entstanden sind (z.B. eine laufende
        # Installation): die Spalte fehlt dort, `CREATE TABLE IF NOT EXISTS` ergaenzt
        # sie nicht. Bestehende Zeilen bleiben chain_hash=NULL — `verify` behandelt diesen
        # Alt-Praefix als unbewiesen und beginnt die Kette bei der ersten neuen Zeile.
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(events)")}
        if "chain_hash" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN chain_hash TEXT")
        self._conn.commit()
        self._lock = threading.Lock()

    def append(self, event: Event, *, now: float | None = None) -> bool:
        """Hängt ein Event an. Gibt False zurück, wenn der idempotency_key schon existiert.

        `default=str` ist kein Komfort, sondern Schutz für das Write-ahead-Prinzip:
        der Intent-Beleg entsteht VOR der Wirkung. Ein Payload mit einem Objekt, das
        `json` nicht kennt (etwa ein `Principal`), würde hier fliegen und damit den
        Beleg verhindern — also genau an der Stelle, an der Lückenlosigkeit zählt.
        """
        ts = _norm_ts(time.time() if now is None else now)
        payload_json = json.dumps(event.payload, ensure_ascii=False, default=str)
        with self._lock:
            # Der Vorgaenger-Hash wird INNERHALB des Locks geholt: sonst koennten zwei
            # Threads denselben prev lesen und die Kette gabeln. `prev` ist der chain_hash
            # der letzten Zeile — oder GENESIS, wenn es keine gibt ODER die letzte noch
            # aus der Alt-Zeit ohne Hash stammt (dann beginnt die Kette hier, exakt wie
            # `verify` es nachrechnet).
            prev_row = self._conn.execute(
                "SELECT chain_hash FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev = prev_row[0] if prev_row and prev_row[0] else _GENESIS
            chain = _chain_hash(prev, ts, event.run_id, event.actor, event.type,
                                event.idempotency_key, payload_json)
            try:
                self._conn.execute(
                    "INSERT INTO events "
                    "(ts, run_id, actor, type, idempotency_key, payload_json, chain_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        event.run_id,
                        event.actor,
                        event.type,
                        event.idempotency_key,
                        payload_json,
                        chain,
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # idempotency_key bereits vorhanden -> schon verarbeitet

    def has_key(self, idempotency_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM events WHERE idempotency_key = ? LIMIT 1", (idempotency_key,)
            )
            return cur.fetchone() is not None

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def protected_count(self) -> int:
        """Wie viele Eintraege die Hash-Kette deckt (chain_hash gesetzt).

        Der Rest ist der Alt-Praefix aus der Zeit vor der Kette. `verify` ueberspringt ihn,
        deshalb gehoert seine Groesse in die Antwort: „intakt" ueber lauter ungeschuetzten
        Zeilen waere eine Halbwahrheit, und Halbwahrheiten sind hier das eigentliche Risiko.
        """
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE chain_hash IS NOT NULL").fetchone()[0])

    def verify(self) -> int | None:
        """`None`, wenn die Kette lueckenlos ist; sonst die id des ERSTEN gebrochenen Eintrags.

        Gegangen wird in id-Reihenfolge. Alt-Zeilen (chain_hash NULL, vor dem Feature) sind
        ein zusammenhaengender Praefix am ANFANG; sie werden uebersprungen, und die Kette
        beginnt bei der ersten gehashten Zeile mit prev=GENESIS — genau so, wie `append` sie
        berechnet hat.

        ⚠️ Ein NULL NACH der ersten gehashten Zeile ist kein Alt-Eintrag, sondern eine
        Faelschung: ein Review zeigte, dass man sonst die LETZTE Zeile auf NULL setzen und so
        als „alt" tarnen koennte — verify meldete faelschlich intakt. Sobald die Kette
        begonnen hat, ist ein geleerter chain_hash daher ein Bruch, kein Reset.

        Wird eine Zeile geaendert, weicht ihr nachgerechneter Hash ab und die id faellt hier.
        Wird eine mittige geloescht, findet die Folgezeile ihren Vorgaenger nicht wieder.

        ⚠️ Was verify NICHT sieht (ehrlich, siehe SECURITY.md): das Abschneiden des ENDES —
        die letzten n Zeilen zu loeschen laesst eine kuerzere, in sich stimmige Kette zurueck.
        Ohne einen ausserhalb verankerten Kopf ist das grundsaetzlich unsichtbar, ebenso ein
        korrekt gehaengter Fake-Append. Punktuelles Faelschen und mittiges Loeschen fallen;
        Tail-Truncation braucht einen externen Anker, den diese Version bewusst noch nicht hat.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, run_id, actor, type, idempotency_key, payload_json, "
                "chain_hash FROM events ORDER BY id"
            ).fetchall()
        prev = _GENESIS
        started = False
        for id_, ts, run_id, actor, type_, key, payload_json, chain_hash in rows:
            if chain_hash is None:
                if started:
                    return int(id_)  # NULL nach dem Kettenbeginn = Tarnung, kein Alt-Eintrag
                continue             # legitimer Alt-Praefix, prev bleibt GENESIS
            started = True
            if _chain_hash(prev, ts, run_id, actor, type_, key, payload_json) != chain_hash:
                return int(id_)
            prev = chain_hash
        return None

    def recent(self, limit: int = 10, types: tuple[str, ...] = ()) -> list[dict]:
        """Die letzten `limit` Events (chronologisch, ältestes zuerst).

        Das ist die Lesekante für `/log` und `/undo`: der Log ist die einzige Quelle,
        die ein Reasoner nicht fälschen kann, deshalb liest `/undo` seine Snapshot-Daten
        von hier und nicht aus dem, was das Modell in die Argumente schreibt.
        """
        columns = "id, ts, run_id, actor, type, payload_json"
        if types:
            marks = ",".join("?" * len(types))
            sql = f"SELECT {columns} FROM events WHERE type IN ({marks}) ORDER BY id DESC LIMIT ?"
            params: tuple = (*types, int(limit))
        else:
            sql = f"SELECT {columns} FROM events ORDER BY id DESC LIMIT ?"
            params = (int(limit),)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [
            {
                "id": row[0],
                "ts": row[1],
                "run_id": row[2],
                "actor": row[3],
                "type": row[4],
                "payload": _load(row[5]),
            }
            for row in rows
        ]
        out.reverse()  # DESC geholt (LIMIT greift am neuen Ende), chronologisch geliefert
        return out

    def by_id(self, event_id: int) -> dict | None:
        """Ein einzelnes Ereignis — fuer `talos why`, das genau eines erklaeren soll.

        Ueber `recent()` mit grossem Limit zu suchen waere derselbe Weg mit mehr Arbeit
        und einer stillen Grenze: was aelter ist als das Limit, waere „nicht gefunden"
        statt gefunden. Ein Protokoll, das eine Zeile verschweigt, weil sie alt ist,
        taugt nicht als Beleg.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id, ts, run_id, actor, type, payload_json FROM events WHERE id = ?",
                (int(event_id),),
            ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "ts": row[1], "run_id": row[2],
                "actor": row[3], "type": row[4], "payload": _load(row[5])}

    def by_run(self, run_id: str, limit: int = 200) -> list[dict]:
        """Alle Ereignisse eines Laufs, chronologisch — der Zusammenhang um ein Urteil."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, run_id, actor, type, payload_json FROM events "
                "WHERE run_id = ? ORDER BY id LIMIT ?",
                (str(run_id), int(limit)),
            ).fetchall()
        return [{"id": r[0], "ts": r[1], "run_id": r[2],
                 "actor": r[3], "type": r[4], "payload": _load(r[5])} for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _load(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}
