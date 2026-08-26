"""`talos health` — der Zustand von aussen, aus Quellen, die ein Reasoner nicht faelschen kann.

Geprueft wird gegen echte Datenbanken unter tmp_path: das Urteil soll an den Zahlen
haengen, nicht an einem Double, das die Wirklichkeit nur so lange abbildet, wie jemand
es nachzieht (Falle 7 in CLAUDE.md). Zeitreisen laufen ueber `now=` — so faken die
bestehenden Suites die Zeit, ganz ohne freezegun.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import health
from talos.eventlog import Event, EventLog, new_run_id
from talos.schedule import ScheduleStore

T0 = 1_800_000_000.0
DAY = health.DAY_S


def _out() -> io.StringIO:
    return io.StringIO()


def _run(tmp_path: Path, argv=(), **kw) -> tuple[int, str]:
    text = _out()
    code = health.run_health(
        list(argv),
        stdout=text,
        db_path=kw.pop("db_path", tmp_path / "eventlog.db"),
        schedule_db=kw.pop("schedule_db", tmp_path / "schedules.db"),
        anchors_path=kw.pop("anchors_path", tmp_path / "anchors.jsonl"),
        **kw,
    )
    return code, text.getvalue()


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "eventlog.db")


def test_a_healthy_installation_reports_runs_and_no_errors(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for i in range(3):
        log.append(
            Event(new_run_id(), "reasoner", "reason.done",
                  {"chars": 10, "status": "answered"}),
            now=T0 + i,
        )
    code, out = _run(tmp_path, now=T0 + 60)
    assert code == 0
    assert "3 in the last 24h" in out
    assert "none in the last 24h" in out
    assert "intact" in out and "status ok" in out


def test_yesterdays_errors_do_not_count_towards_today(tmp_path: Path) -> None:
    """Das Fenster ist ein echtes: ein Fehler von letzter Woche ist Geschichte, kein
    Dauerzustand — sonst wird die Ampel nie wieder gruen und niemand liest sie mehr."""
    log = _log(tmp_path)
    log.append(Event(new_run_id(), "conductor", "error",
                     {"stage": "reply", "error": "boom"}), now=T0 - 2 * DAY)
    log.append(Event(new_run_id(), "conductor", "error",
                     {"stage": "reason", "error": "kaputt"}), now=T0 - 60)
    code, out = _run(tmp_path, now=T0)
    assert code == 0
    assert "1 in the last 24h" in out and "kaputt" in out
    assert "boom" not in out


def test_a_broken_chain_is_the_one_critical_finding(tmp_path: Path) -> None:
    """Exit 1 gehoert der gebrochenen Kette — ein manipuliertes Protokoll ist kein
    Betriebsproblem mehr, sondern ein Integritaetsproblem."""
    log = _log(tmp_path)
    for i in range(3):
        log.append(Event(new_run_id(), "conductor", "reason.step", {"n": i}), now=T0 + i)
    conn = sqlite3.connect(str(tmp_path / "eventlog.db"))
    try:
        conn.execute("UPDATE events SET actor = 'mallory' WHERE id = 2")
        conn.commit()
    finally:
        conn.close()
    code, out = _run(tmp_path, now=T0 + 60)
    assert code == 1
    assert "BROKEN" in out and "status critical" in out


def test_pending_schedules_are_counted_without_quoting_them(tmp_path: Path) -> None:
    """Der naechste Termin gehoert in die Anzeige — der Prompt nicht: er ist
    Gespraechsstoff des Betreibers und landet sonst in jedem kopiellen Ticket."""
    store = ScheduleStore(tmp_path / "schedules.db")
    store.add(conversation="c1", principal="telegram:1", prompt="geheimer Morgenauftrag",
              interval_s=3600, now=T0)
    store.close()
    code, out = _run(tmp_path, now=T0)
    assert code == 0
    assert "1 pending" in out
    assert "geheimer Morgenauftrag" not in out


def test_the_last_anchor_is_shown_and_its_absence_named(tmp_path: Path) -> None:
    from talos import anchor

    _log(tmp_path).append(Event(new_run_id(), "conductor", "reason.step", {}), now=T0)
    anchor.run_anchor([], stdout=_out(), db_path=tmp_path / "eventlog.db",
                      anchors_path=tmp_path / "anchors.jsonl", now=T0 + 10)
    code, out = _run(tmp_path, now=T0 + 20)
    assert code == 0 and "verify ok" in out and "1 events" in out

    code, out = _run(tmp_path, now=T0 + 20,
                     anchors_path=tmp_path / "gibt-es-nicht.jsonl")
    assert code == 0 and "none yet" in out


def test_without_a_log_health_says_so_and_stays_green(tmp_path: Path) -> None:
    """„Ist nie gelaufen" ist ein ehrlicher Zustand, keine Stoerung — und der Befund
    darf das Log nicht erzeugen, das zu fehlen er meldet."""
    code, out = _run(tmp_path, now=T0)
    assert code == 0
    assert "no event log yet" in out
    assert not (tmp_path / "eventlog.db").exists()


def test_health_changes_nothing(tmp_path: Path) -> None:
    """Dieselbe Regel wie der Doktor: ein Gesundheitszustand, der selbst eine Wirkung
    haette, waere keine Anzeige mehr."""
    _log(tmp_path).append(Event(new_run_id(), "conductor", "reason.step", {}), now=T0)
    # WAL-Seitendateien (-shm/-wal) gehoeren zum LESEN der Datenbank, nicht zu ihrem
    # Inhalt — sie entstehen durch jede offene Verbindung und sind hier rausgefiltert.
    def namen() -> list[str]:
        return sorted(p.name for p in tmp_path.iterdir()
                      if not p.name.startswith("eventlog.db-"))

    vorher = namen()
    _run(tmp_path, now=T0 + 60)
    assert namen() == vorher
    assert not (tmp_path / "anchors.jsonl").exists()  # health schreibt keinen Anker


def test_json_output_is_machine_readable_and_matches_the_text(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append(Event(new_run_id(), "reasoner", "reason.done",
                     {"chars": 5, "status": "answered"}), now=T0)
    code, out = _run(tmp_path, ["--json"], now=T0 + 60)
    assert code == 0
    daten = json.loads(out)
    assert daten["status"] == "ok"
    assert daten["event_log"]["runs_24h"] == 1
    assert daten["chain"]["chain_ok"] is True
    assert daten["schedules"]["pending"] == 0
    assert daten["anchor"] is None


def test_an_unreadable_log_is_a_finding_not_a_traceback(tmp_path: Path) -> None:
    """Eine Datei, die keine Datenbank ist, ist genau der Zustand, in dem man `health`
    aufruft — er darf daran nicht selbst kaputtgehen."""
    (tmp_path / "eventlog.db").write_bytes(b"das ist keine sqlite datenbank" * 10)
    code, out = _run(tmp_path, ["--json"], now=T0)
    assert code == 0
    assert json.loads(out)["event_log"]["unreadable"] is True


def test_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    code, out = _run(tmp_path, ["--flub"])
    assert code == 2 and "usage" in out


def test_health_is_registered_in_the_cli() -> None:
    from talos import cli

    assert "health" in cli.TABLE
    assert "health" in cli.HELP
