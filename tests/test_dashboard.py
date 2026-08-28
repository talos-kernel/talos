"""`talos dashboard` — der Beobachter, der nicht eingreifen kann.

Geprueft wird gegen echte Datenbanken unter tmp_path und einen echten Server auf
Port 0 (ephemer): das Urteil soll an den Antworten haengen, nicht an einem Double.
Die drei Haertefaelle sind die, an denen ein Dashboard seine Daseinsberechtigung
verliert: ein Schreibweg, ein Geheimnis im Response-Body, ein Prompt der Zeitplan-DB.
"""
from __future__ import annotations

import http.client
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import dashboard
from talos.eventlog import Event, EventLog, new_run_id
from talos.schedule import ScheduleStore

def _seed_runs(tmp_path: Path) -> tuple[str, str, str]:
    """Ein abgeschlossener, ein laufender Lauf — und einer, der auf einen Menschen wartet.

    Die Zeitbasis ist die echte Uhr minus fuenf Minuten: das Alter eines Laufs wird
    gegen `time.time()` gerechnet, eine Konstante laeuefe irgendwann in die Zukunft.
    """
    t0 = time.time() - 300
    log = EventLog(tmp_path / "eventlog.db")
    fertig = new_run_id()
    log.append(Event(fertig, "conductor", "reason.started", {}), now=t0)
    log.append(Event(fertig, "executor", "exec.intent",
                     {"tool": "read_file", "verdict": "allow", "reason": "read"}), now=t0 + 1)
    log.append(Event(fertig, "executor", "exec.result",
                     {"tool": "read_file", "status": "ok"}), now=t0 + 2)
    log.append(Event(fertig, "reasoner", "reason.done",
                     {"chars": 42, "status": "answered"}), now=t0 + 9)

    offen = new_run_id()
    log.append(Event(offen, "conductor", "reason.started", {}), now=t0 + 100)

    wartend = new_run_id()
    log.append(Event(wartend, "conductor", "reason.started", {}), now=t0 + 200)
    log.append(Event(wartend, "executor", "exec.intent",
                     {"tool": "write_file", "verdict": "needs_human",
                      "reason": "writes a file"}), now=t0 + 201)
    log.append(Event(wartend, "executor", "exec.result",
                     {"tool": "write_file", "status": "needs_human"}), now=t0 + 202)
    log.close()
    return fertig, offen, wartend


@pytest.fixture()
def server(tmp_path: Path):
    """Ein echter Server auf Port 0 — die Tests sprechen HTTP, keinen Handler direkt."""
    server = dashboard.make_server(
        eventlog_db=tmp_path / "eventlog.db",
        schedule_db=tmp_path / "schedules.db",
        blueprint_state=tmp_path / "blueprints.json",
        anchors_path=tmp_path / "anchors.jsonl",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _get(server, path: str, method: str = "GET") -> tuple[int, str, str]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request(method, path)
    antwort = conn.getresponse()
    body = antwort.read().decode("utf-8")
    typ = antwort.getheader("Content-Type") or ""
    conn.close()
    return antwort.status, typ, body


# --- Routen und ihre Kernfelder ------------------------------------------------------------

def test_status_route_reports_health_and_version(server, tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    code, typ, body = _get(server, "/api/status")
    assert code == 200 and "application/json" in typ
    daten = json.loads(body)
    for feld in ("ts", "event_log", "chain", "schedules", "anchor", "status", "version"):
        assert feld in daten, f"Feld {feld} fehlt in /api/status"
    assert daten["chain"]["chain_ok"] is True


def test_runs_route_separates_running_from_completed(server, tmp_path: Path) -> None:
    fertig, offen, _wartend = _seed_runs(tmp_path)
    code, _typ, body = _get(server, "/api/runs")
    assert code == 200
    daten = json.loads(body)
    laufend = {r["run_id"] for r in daten["running"]}
    assert offen in laufend and fertig not in laufend
    eintrag = next(r for r in daten["running"] if r["run_id"] == offen)
    assert eintrag["age_s"] >= 0
    beendet = {r["run_id"] for r in daten["completed"]}
    assert fertig in beendet
    satz = next(r for r in daten["completed"] if r["run_id"] == fertig)
    assert satz["status"] == "answered"
    assert satz["duration_s"] == pytest.approx(9.0)
    assert satz["tool_calls"] == 1


def test_approvals_route_labels_the_heuristic_honestly(server, tmp_path: Path) -> None:
    _fertig, _offen, wartend = _seed_runs(tmp_path)
    code, _typ, body = _get(server, "/api/approvals")
    assert code == 200
    daten = json.loads(body)
    assert daten["open"]["basis"].startswith("heuristic")
    runs = {e["run_id"] for e in daten["open"]["items"]}
    assert wartend in runs
    assert isinstance(daten["standing"], list)


def test_answered_approval_is_no_longer_open(server, tmp_path: Path) -> None:
    _fertig, _offen, wartend = _seed_runs(tmp_path)
    t0 = time.time() - 300
    log = EventLog(tmp_path / "eventlog.db")
    log.append(Event(wartend, "human", "approval.granted", {"tool": "write_file"}), now=t0 + 300)
    log.append(Event(wartend, "executor", "exec.result",
                     {"tool": "write_file", "status": "ok"}), now=t0 + 301)
    log.close()
    code, _typ, body = _get(server, "/api/approvals")
    assert code == 200
    daten = json.loads(body)
    runs = {e["run_id"] for e in daten["open"]["items"]}
    assert wartend not in runs


def test_events_route_caps_and_lists_newest(server, tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    code, _typ, body = _get(server, "/api/events")
    assert code == 200
    daten = json.loads(body)
    assert daten["count"] > 0 and len(daten["events"]) == daten["count"]
    assert all(len(e["run_id"]) <= 8 for e in daten["events"])


def test_schedules_route_counts_without_prompts(server, tmp_path: Path) -> None:
    speicher = ScheduleStore(tmp_path / "schedules.db")
    speicher.add(conversation="telegram:1", principal="telegram:1",
                 prompt="MARKER-Erkennungstext-7f3a9c", interval_s=3600,
                 now=time.time() - 300)
    speicher.close()
    code, _typ, body = _get(server, "/api/schedules")
    assert code == 200
    daten = json.loads(body)
    assert daten["count"] == 1 and daten["upcoming"]
    assert "MARKER-Erkennungstext-7f3a9c" not in body
    assert "prompt" not in json.dumps(daten)


def test_schedules_route_lists_installed_blueprints(server, tmp_path: Path) -> None:
    (tmp_path / "blueprints.json").write_text(json.dumps({
        "morgenlage": {"task_id": "abc123", "conversation": "telegram:1",
                       "principal": "telegram:1", "enabled": True},
    }), encoding="utf-8")
    code, _typ, body = _get(server, "/api/schedules")
    assert code == 200
    daten = json.loads(body)
    assert daten["blueprints"]["morgenlage"]["enabled"] is True


def test_index_serves_a_self_contained_page(server) -> None:
    code, typ, body = _get(server, "/")
    assert code == 200 and "text/html" in typ
    # Keine externe Referenz: der Beobachter darf nichts nachladen, was Dritte sehen.
    assert "http://" not in body and "https://" not in body
    assert "link " not in body and "<link" not in body
    # Fonts und Sigil sind base64-eingebettet (dashboard_assets): jedes src= und
    # jedes url( muss eine data:-URI sein — geladen wird aus dem Prozess, nie fremd.
    for ref in re.findall(r'(?:src=|url\()\s*"([^"]+)"', body):
        assert ref.startswith("data:"), ref
    assert "/api/status" in body  # die Seite pollt die eigenen Endpunkte


# --- Fehlende Quellen: benannte Leere statt 500 ---------------------------------------------

def test_missing_databases_yield_named_emptiness(server) -> None:
    for pfad, felder in (
        ("/api/status", ("ts", "status", "version")),
        ("/api/runs", ("available", "running", "completed")),
        ("/api/approvals", ("standing", "open")),
        ("/api/events", ("available", "events")),
        ("/api/schedules", ("available", "count", "blueprints")),
    ):
        code, _typ, body = _get(server, pfad)
        assert code == 200, f"{pfad} antwortete {code} statt benannter Leere"
        daten = json.loads(body)
        for feld in felder:
            assert feld in daten, f"{pfad}: Feld {feld} fehlt"


# --- Kein Eingriffsweg -----------------------------------------------------------------------

def test_write_verbs_are_refused(server) -> None:
    for methode in ("POST", "PUT", "DELETE"):
        code, _typ, _body = _get(server, "/api/events", method=methode)
        assert code == 405, f"{methode} wurde nicht mit 405 abgewiesen"
        code, _typ, _body = _get(server, "/", method=methode)
        assert code == 405


def test_unknown_paths_are_404(server) -> None:
    for pfad in ("/api/approve", "/admin", "/api/status/delete", "/../etc/passwd"):
        code, _typ, _body = _get(server, pfad)
        assert code == 404, f"{pfad} antwortete nicht 404"


def test_query_strings_change_nothing(server, tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    code1, _t, body1 = _get(server, "/api/events")
    code2, _t, body2 = _get(server, "/api/events?limit=5000&format=raw")
    assert code1 == code2 == 200
    assert json.loads(body1) == json.loads(body2)


# --- Read-only ist durchgesetzt, nicht behauptet ---------------------------------------------

def test_ro_connection_refuses_writes(tmp_path: Path) -> None:
    _seed_runs(tmp_path)
    conn = dashboard._ro(tmp_path / "eventlog.db")
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("INSERT INTO events (ts, run_id, actor, type, payload_json) "
                         "VALUES (0, 'x', 'x', 'x', '{}')")
    finally:
        conn.close()


def test_ro_missing_file_raises_named_error(tmp_path: Path) -> None:
    with pytest.raises(dashboard.SourceError):
        dashboard._ro(tmp_path / "gibt-es-nicht.db")


def test_bind_is_loopback_only(server) -> None:
    assert dashboard.BIND == "127.0.0.1"
    assert server.server_address[0] == "127.0.0.1"


# --- Geheimnisse und grosse Payloads ----------------------------------------------------------

def test_secrets_never_reach_the_response_body(server, tmp_path: Path) -> None:
    log = EventLog(tmp_path / "eventlog.db")
    log.append(Event(new_run_id(), "agent", "error", {
        "error": "call failed: token=sk-live-9f8e7d6c5b4a ghp-abcdef1234567890 lang",
        "note": "x" * 5000,
    }), now=time.time() - 60)
    log.close()
    code, _typ, body = _get(server, "/api/events")
    assert code == 200
    assert "sk-live-9f8e7d6c5b4a" not in body
    assert "ghp-abcdef1234567890" not in body
    assert "x" * 5000 not in body
    assert "[REDACTED]" in body


def test_redaction_matches_the_telegram_pattern() -> None:
    """Dieselben Muster wie telegram._redact — bewusst dupliziert (stdlib-Prozess),
    deshalb hier gegen das Original gehalten: driften sie auseinander, faellt dieser Test."""
    from talos.telegram import _redact as telegram_redact

    proben = [
        "token=abc123 geheim",
        "key: sk-proj-abcdef123456",
        "ghp_abcdef1234567890",
        "password: hunter2",
        "nichts zu verbergen",
    ]
    for probe in proben:
        assert dashboard._redact(probe) == telegram_redact(probe)


# --- CLI --------------------------------------------------------------------------------------

def test_dashboard_help_explains_the_boundary(capsys) -> None:
    code = dashboard.run_dashboard(["--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "127.0.0.1" in out
