"""Durables Gespraechsgedaechtnis — was `/new` aus dem Kontext nimmt, bleibt auffindbar.

`memory.py` haelt den Verlauf bewusst nur im Arbeitsspeicher: ein Neustart oder `/new`
vergisst wirklich, weil „ein Gedaechtnis, das man nicht loeschen kann, ist ein Archiv".
Diese Entscheidung bleibt unangetastet — sie gilt fuer den AKTIVEN Kontext, den Block, der
in jeden Prompt zurueckfliesst. Dieses Modul ist etwas anderes: ein Archiv daneben, das
niemand automatisch wieder vorgelesen bekommt. Der Unterschied ist keine Spitzfindigkeit,
er ist die ganze Idee von Hermes' `session_search`-Muster: durabel UND durchsuchbar, aber
nur auf ausdrueckliches Nachfragen — nie als stiller Zuwachs im naechsten Prompt.

**Nicht Recall, obwohl es aehnlich aussieht.** `recall.py` haelt kuratierte, einzeln
benannte Tatsachen und ist ABSICHTLICH konversationsuebergreifend durchsuchbar — ein
Fakt ueber den Betreiber gilt in jedem Kanal. Hier ist das Gegenteil richtig: ein Zug ist
der volle, unkuratierte Wortlaut eines Gespraechs, und Konversation B darf ihn nie sehen,
auch wenn derselbe Principal dahinter steht. Deshalb filtert jede Suche zwingend per
SQL-`WHERE conversation = ?` — nie erst im Anwendungscode danach, und niemals aus einer
Bedingung, die das Modell in seinen Werkzeug-Argumenten mitschicken koennte (siehe die
Runner-Fabrik in `tools.py`: die Konversation kommt aus `context()`, nie aus `req.args`,
aus demselben Grund wie bei `ask_operator`).

**Voller Text, gedeckelte Ausgabe.** `memory.MAX_TURN_CHARS` loest ein Prompt-Kosten-
Problem, das hier nicht existiert: ein Archiv, das nie am Stueck in einen Prompt
zurueckfliesst, darf vollstaendig sein. Gedeckelt wird nur das SUCHERGEBNIS
(`vault.py`s `_safe_search_output`-Bauart), nie die gespeicherte Quelle.

**Fail-open, wie `recall.py`.** Ein kaputter, gesperrter oder gar nicht anlegbarer
Speicher fuehrt zu „nichts gefunden"/„nichts gespeichert", nie zu einem gestoppten
Agenten oder einer bereits zugestellten Antwort, die an einem Schreibfehler abstuerzt.
Das ist keine Sicherheitsentscheidung, sondern dieselbe wie bei Recall: Erinnern ist kein
Gate, es erteilt keine Rechte, es faellt keine Entscheidung.

**Keine Aufbewahrungspolitik in v1.** Recalls harter Deckel (`MAX_ENTRIES`) passt hier
nicht — der ganze Zweck dieses Speichers ist, ein Archiv zu sein, nicht ein knappes
Gedaechtnis. Wachstum wird sichtbar gemacht (`/debug`), nicht automatisch begrenzt.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .vault import redact_secrets

DEFAULT_SEARCH_LIMIT = 5
QUERY_MAX_CHARS = 300
SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 10
MAX_SEARCH_OUTPUT_CHARS = 20_000
MAX_QUERY_TERMS = 12

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL,
    ts           REAL NOT NULL,
    asked        TEXT NOT NULL,
    answered     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation, ts DESC);
"""
_FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(asked, answered);"


@dataclass(frozen=True)
class Exchange:
    """Ein gespeicherter Zug — eigener Name, damit er nicht mit `memory.Turn` verwechselt
    wird (dort: ein einzelner Sprecher-Beitrag; hier: das ganze Frage-Antwort-Paar)."""

    id: int
    conversation: str
    ts: float
    asked: str
    answered: str

    def render(self) -> str:
        return f"- asked: {self.asked}\n  answered: {self.answered}"


class TranscriptStore:
    """Persistentes Gespraechsarchiv, eigene Datei neben Event-Log und Recall.

    Zwei Threads greifen zu (Poll-Thread, Worker) — dieselbe Begruendung wie bei
    `EventLog`/`Recall`: `check_same_thread=False` erlaubt das nur, ein Lock serialisiert.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._fts = False
        self.reason = ""
        try:
            conn = _connect(Path(db_path))
            conn.executescript(_SCHEMA)
            self._fts = _enable_fts(conn)
            if self._fts:
                _sync_fts(conn)
            conn.commit()
            self._conn = conn
        except (sqlite3.Error, OSError, ValueError) as error:
            # Fail-open: kein Archiv ist eine Einschraenkung, kein Stillstand.
            self._conn = None
            self.reason = str(error)

    @property
    def available(self) -> bool:
        return self._conn is not None

    @property
    def full_text(self) -> bool:
        """False = FTS5 fehlt in dieser SQLite, gesucht wird mit `LIKE`."""
        return self._fts

    # --- schreiben ----------------------------------------------------------------
    def record(self, conversation: str, *, asked: str, answered: str, now: float | None = None) -> None:
        """Legt einen beantworteten Zug ab. Nie ein Fehler nach aussen — siehe Modul-
        Docstring (fail-open): der Aufrufer hat die Antwort bereits zugestellt."""
        asked_clean = " ".join(str(asked).split())
        answered_clean = " ".join(str(answered).split())
        if not asked_clean or not answered_clean or self._conn is None:
            return
        ts = time.time() if now is None else float(now)
        try:
            self._insert(str(conversation), ts, asked_clean, answered_clean)
        except (sqlite3.Error, OSError):
            pass

    def _insert(self, conversation: str, ts: float, asked: str, answered: str) -> None:
        """Zeile plus Index in EINER Transaktion — sonst gibt es Text ohne Index."""
        with self._lock:
            if self._conn is None:
                return
            try:
                cursor = self._conn.execute(
                    "INSERT INTO turns (conversation, ts, asked, answered) VALUES (?, ?, ?, ?)",
                    (conversation, ts, asked, answered),
                )
                turn_id = int(cursor.lastrowid or 0)
                if self._fts:
                    self._conn.execute(
                        "INSERT INTO turns_fts (rowid, asked, answered) VALUES (?, ?, ?)",
                        (turn_id, asked, answered),
                    )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()

    # --- nachschlagen ---------------------------------------------------------------
    def search(
        self, conversation: str, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> tuple[Exchange, ...]:
        """Volltextsuche — IMMER nur innerhalb `conversation`, nie darueber hinaus.

        Diese Grenze ist per SQL-WHERE erzwungen, nicht durch Filtern danach: eine Zeile,
        die die Datenbank nie verlaesst, kann durch keinen spaeteren Programmierfehler an
        eine andere Konversation zurueckfliessen.
        """
        if self._conn is None:
            return ()
        terms = _terms(query)
        if not terms:
            return self.recent(conversation, limit=limit)
        if self._fts:
            found = self._search_fts(conversation, terms, limit)
            if found is not None:
                return found
        return self._search_like(conversation, terms, limit)

    def _search_fts(
        self, conversation: str, terms: tuple[str, ...], limit: int
    ) -> tuple[Exchange, ...] | None:
        """`None` = FTS hat nicht geantwortet; der Aufrufer nimmt dann `LIKE`."""
        match = " OR ".join(f'"{term}"' for term in terms)
        sql = (
            "SELECT t.id, t.conversation, t.ts, t.asked, t.answered"
            " FROM turns_fts f JOIN turns t ON t.id = f.rowid"
            " WHERE turns_fts MATCH ? AND t.conversation = ?"
            " ORDER BY bm25(turns_fts), t.ts DESC, t.id DESC LIMIT ?"
        )
        with self._lock:
            if self._conn is None:
                return ()
            try:
                rows = self._conn.execute(
                    sql, (match, conversation, max(0, int(limit)))
                ).fetchall()
            except sqlite3.Error:
                return None
        return tuple(_exchange(row) for row in rows)

    def _search_like(
        self, conversation: str, terms: tuple[str, ...], limit: int
    ) -> tuple[Exchange, ...]:
        """Fallback ohne FTS5: findet weniger klug, aber es faellt nichts aus."""
        clause = " OR ".join(
            "asked LIKE ? ESCAPE '\\' OR answered LIKE ? ESCAPE '\\'" for _ in terms
        )
        params: list = [conversation]
        for term in terms:
            params.extend([f"%{_escape_like(term)}%"] * 2)
        params.append(max(0, int(limit)))
        sql = (
            "SELECT id, conversation, ts, asked, answered FROM turns"
            f" WHERE conversation = ? AND ({clause})"
            " ORDER BY ts DESC, id DESC LIMIT ?"
        )
        return self._rows(sql, tuple(params))

    def recent(self, conversation: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> tuple[Exchange, ...]:
        """Die juengsten Zuege dieser Konversation — ohne Suchbegriff."""
        return self._rows(
            "SELECT id, conversation, ts, asked, answered FROM turns"
            " WHERE conversation = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (conversation, max(0, int(limit))),
        )

    def count(self) -> int:
        with self._lock:
            if self._conn is None:
                return 0
            try:
                return int(self._conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
            except sqlite3.Error:
                return 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- gemeinsame Kante -----------------------------------------------------------
    def _rows(self, sql: str, params: tuple) -> tuple[Exchange, ...]:
        with self._lock:
            if self._conn is None:
                return ()
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                return ()  # kaputter Speicher heisst „nichts gefunden", nicht „Absturz"
        return tuple(_exchange(row) for row in rows)


def render_results(exchanges: tuple[Exchange, ...]) -> str:
    """Baut die Werkzeug-Ausgabe: redigiert, gedeckelt — wie `vault._safe_search_output`.

    Der gespeicherte Text bleibt voll erhalten; nur was hier zurueckfliesst, ist begrenzt.
    """
    if not exchanges:
        return "No matching turns in this conversation."
    body = "\n".join(exchange.render() for exchange in exchanges)
    return _bounded(redact_secrets(body), MAX_SEARCH_OUTPUT_CHARS)


# --- kleine Helfer -------------------------------------------------------------------
def _bounded(text: str, maximum: int) -> str:
    """Kappt sichtbar — eigene Kopie statt `vault._bounded` (privat), damit dieses Modul
    nicht an einer fremden internen Signatur haengt (dieselbe Haltung wie `recall._clip`)."""
    if len(text) <= maximum:
        return text
    suffix = "\n…[Ausgabe gekürzt]"
    return text[: maximum - len(suffix)] + suffix


def _connect(path: Path) -> sqlite3.Connection:
    """Verzeichnis 0700, Datei 0600 — beim Anlegen, nicht danach (wie `recall._connect`,
    bewusst eigenstaendig gehalten, damit dieses Modul nicht an dessen Signatur haengt)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _enable_fts(conn: sqlite3.Connection) -> bool:
    try:
        conn.executescript(_FTS_SCHEMA)
        return True
    except sqlite3.Error:
        return False


def _sync_fts(conn: sqlite3.Connection) -> None:
    """Zieht den Index nach, wenn er hinter der Tabelle liegt (etwa nach einem Update von
    einer SQLite ohne FTS5 auf eine mit FTS5 — siehe `recall._sync_fts`)."""
    indexed = int(conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0])
    total = int(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
    if indexed == total:
        return
    conn.execute("DELETE FROM turns_fts")
    conn.execute(
        "INSERT INTO turns_fts (rowid, asked, answered) SELECT id, asked, answered FROM turns"
    )


def _exchange(row: tuple) -> Exchange:
    return Exchange(
        id=int(row[0]), conversation=row[1], ts=float(row[2]), asked=row[3], answered=row[4]
    )


def _terms(query: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", str(query), re.UNICODE))[:MAX_QUERY_TERMS]


def _escape_like(term: str) -> str:
    for char in ("\\", "%", "_"):
        term = term.replace(char, "\\" + char)
    return term
