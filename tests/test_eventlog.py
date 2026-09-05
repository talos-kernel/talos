import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos.eventlog import Event, EventLog, new_run_id


def make_log(tmp_path) -> EventLog:
    return EventLog(tmp_path / "ev.db")


def _fill(log: EventLog, n: int = 5) -> str:
    run = new_run_id()
    for i in range(n):
        log.append(Event(run, "conductor", "reason.step", {"n": i}))
    return run


def test_append_increments_count(tmp_path):
    log = make_log(tmp_path)
    run = new_run_id()
    assert log.append(Event(run, "ingress", "task.received", {"x": 1})) is True
    assert log.count() == 1


def test_idempotency_key_blocks_duplicate(tmp_path):
    log = make_log(tmp_path)
    run = new_run_id()
    key = "tg:update:42"
    first = log.append(Event(run, "ingress", "task.received", {}, idempotency_key=key))
    second = log.append(Event(run, "ingress", "task.received", {}, idempotency_key=key))
    assert first is True
    assert second is False
    assert log.count() == 1
    assert log.has_key(key) is True


def test_events_without_key_are_all_kept(tmp_path):
    log = make_log(tmp_path)
    run = new_run_id()
    for _ in range(3):
        assert log.append(Event(run, "conductor", "reason.started", {})) is True
    assert log.count() == 3


def test_duplicate_releases_database_for_another_connection(tmp_path):
    first, second = make_log(tmp_path), make_log(tmp_path)
    second._conn.execute("PRAGMA busy_timeout=100")
    event = Event("run", "ingress", "received", {}, idempotency_key="duplicate")
    try:
        assert first.append(event)
        assert not first.append(event)
        assert second.append(Event("run", "cli", "next", {}))
        assert second.verify() is None
    finally:
        first.close()
        second.close()


def test_invalid_event_is_not_mistaken_for_an_idempotent_duplicate(tmp_path):
    log = make_log(tmp_path)
    log._conn.execute("""CREATE TRIGGER reject_write BEFORE INSERT ON events
                         BEGIN SELECT RAISE(ABORT, 'storage constraint'); END""")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            log.append(Event("run", "actor", "type", {}))
    finally:
        log.close()


def test_separate_connections_cannot_fork_the_audit_chain(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from talos import eventlog

    first, second = make_log(tmp_path), make_log(tmp_path)
    reached, release, other_hashed = threading.Event(), threading.Event(), threading.Event()
    original = eventlog._chain_hash
    def paused(prev, ts, run_id, *rest):
        if run_id == "first":
            reached.set()
            assert release.wait(3)
        elif run_id == "second":
            other_hashed.set()
        return original(prev, ts, run_id, *rest)
    monkeypatch.setattr(eventlog, "_chain_hash", paused)
    try:
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(first.append, Event("first", "service", "write", {}))
            assert reached.wait(3)
            b = pool.submit(second.append, Event("second", "cli", "write", {}))
            # The unfixed second writer reads the same head while the first pauses.
            other_hashed.wait(.15)
            release.set()
            assert a.result(3) and b.result(3)
        monkeypatch.setattr(eventlog, "_chain_hash", original)
        assert first.count() == 2 and first.verify() is None
    finally:
        release.set()
        first.close()
        second.close()


# --- Die Hash-Kette: das Log beweist sich selbst ---------------------------------------
import pytest

from talos.eventlog import _GENESIS, _chain_hash, _norm_ts


_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    run_id          TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    idempotency_key TEXT    UNIQUE,
    payload_json    TEXT    NOT NULL
);
"""


def _raw(tmp_path):
    """Eine zweite Verbindung, um wie ein local-root-Angreifer direkt am Log zu drehen."""
    return sqlite3.connect(str(tmp_path / "ev.db"))


def test_a_fresh_chain_verifies(tmp_path):
    log = make_log(tmp_path)
    _fill(log, 5)
    assert log.verify() is None
    assert log.count() == 5
    assert log.protected_count() == 5


def test_editing_an_event_names_its_id(tmp_path):
    log = make_log(tmp_path)
    _fill(log, 5)
    log.close()
    conn = _raw(tmp_path)
    conn.execute("UPDATE events SET payload_json = ? WHERE id = 3", ('{"n": 999}',))
    conn.commit()
    conn.close()
    assert make_log(tmp_path).verify() == 3


def test_deleting_a_middle_event_is_caught(tmp_path):
    log = make_log(tmp_path)
    _fill(log, 5)
    log.close()
    conn = _raw(tmp_path)
    conn.execute("DELETE FROM events WHERE id = 3")
    conn.commit()
    conn.close()
    # Zeile 4 findet ihren Vorgaenger (3) nicht wieder -> bricht dort.
    assert make_log(tmp_path).verify() == 4


def test_nulling_a_chain_hash_is_caught(tmp_path):
    log = make_log(tmp_path)
    _fill(log, 5)
    log.close()
    conn = _raw(tmp_path)
    conn.execute("UPDATE events SET chain_hash = NULL WHERE id = 3")
    conn.commit()
    conn.close()
    assert make_log(tmp_path).verify() == 3


def test_nulling_the_last_row_cannot_disguise_it_as_legacy(tmp_path):
    """Sonst tarnt man die letzte Zeile als Alt-Eintrag und verify luegt."""
    log = make_log(tmp_path)
    _fill(log, 5)
    log.close()
    conn = _raw(tmp_path)
    conn.execute("UPDATE events SET chain_hash = NULL WHERE id = 5")
    conn.commit()
    conn.close()
    assert make_log(tmp_path).verify() == 5


def test_a_legacy_prefix_then_new_events_verifies(tmp_path):
    """Der echte Migrationspfad: alte DB ohne Spalte, dann die neue Version haengt an."""
    db = tmp_path / "ev.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    for i in range(3):
        conn.execute(
            "INSERT INTO events (ts, run_id, actor, type, idempotency_key, payload_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (1000.0 + i, "oldrun", "legacy", "old.event", None, f'{{"n": {i}}}'),
        )
    conn.commit()
    conn.close()

    log = EventLog(db)                 # loest die Migration aus (Spalte wird ergaenzt)
    _fill(log, 4)                      # vier neue, gekettete Zeilen
    assert log.verify() is None
    assert log.count() == 7
    assert log.protected_count() == 4  # nur die neuen sind bewiesen, die alten nicht


def test_a_non_finite_timestamp_is_refused(tmp_path):
    log = make_log(tmp_path)
    run = new_run_id()
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            log.append(Event(run, "conductor", "reason.step", {}), now=bad)
    assert log.count() == 0


def test_negative_zero_timestamp_does_not_break_the_chain(tmp_path):
    """SQLite macht -0.0 zu 0.0; ohne Normalisierung meldete verify einen Falsch-Bruch."""
    log = make_log(tmp_path)
    run = new_run_id()
    assert log.append(Event(run, "conductor", "reason.step", {}), now=-0.0) is True
    assert log.append(Event(run, "conductor", "reason.step", {}), now=1.0) is True
    assert make_log(tmp_path).verify() is None


def test_the_frame_is_injective_against_separator_injection():
    """Ein blosses join waere mehrdeutig. Zwei verschiedene Feld-Tupel duerfen
    NIE denselben Hash ergeben, auch wenn ein Feld das alte Trennzeichen enthaelt."""
    a = _chain_hash(_GENESIS, 1.0, "r", "a", "t", "k", "p\x1eq")
    b = _chain_hash(_GENESIS, 1.0, "r", "a", "t", "k\x1ep", "q")
    assert a != b


def test_norm_ts_normalises_negative_zero():
    import math

    assert math.copysign(1.0, _norm_ts(-0.0)) == 1.0  # aus -0.0 wird +0.0
