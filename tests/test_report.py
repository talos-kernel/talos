"""Der Bericht ist ein Beweisstueck — also wird er wie eines geprueft.

Zwei Dinge zaehlen: dass das ABGELEHNTE sichtbar ist (das ist der ganze Grund, warum
jemand diesen Agenten einsetzt), und dass der Bericht seine eigenen Grenzen mitliefert.
Ein Auszug, der nur das Gelungene zeigt, waere Werbung.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from talos import report


def _log(tmp_path: Path, zeilen: list[tuple]) -> Path:
    pfad = tmp_path / "eventlog.db"
    conn = sqlite3.connect(pfad)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                 "run_id TEXT, actor TEXT, type TEXT, idempotency_key TEXT, payload_json TEXT)")
    for ts, run, actor, typ, payload in zeilen:
        conn.execute("INSERT INTO events (ts, run_id, actor, type, payload_json) VALUES (?,?,?,?,?)",
                     (ts, run, actor, typ, json.dumps(payload)))
    conn.commit()
    conn.close()
    return pfad


LAUF = [
    (1.0, "r1", "ingress", "task.received", {"principal": "telegram:7", "conversation": "chat:7"}),
    (2.0, "r1", "executor", "exec.intent",
     {"tool": "run_shell", "targets": ["/home/x/.ssh/id_ed25519"], "verdict": "deny",
      "reason": "reading secrets refused"}),
    (3.0, "r1", "conductor", "reply.sent", {}),
]


def test_the_refusal_is_visible_with_its_reason(tmp_path: Path) -> None:
    """Der wertvollste Teil. Was ein Agent getan hat, behauptet jedes Werkzeug."""
    text = report.render(report.collect(_log(tmp_path, LAUF)))
    assert "run_shell" in text
    assert "DENY" in text
    assert "reading secrets refused" in text
    assert "/home/x/.ssh/id_ed25519" in text


def test_a_proposal_without_an_authorisation_is_counted_as_refused(tmp_path: Path) -> None:
    """Es gibt kein Ereignis „abgelehnt" — eine Ablehnung ist das AUSBLEIBEN der Erlaubnis.

    Deshalb wird die Zahl berechnet, nicht gezaehlt. Wer das spaeter auf ein Ereignis
    umstellt, muss daran denken, dass ein Lauf auch abbrechen kann.
    """
    text = report.render(report.collect(_log(tmp_path, LAUF)))
    assert "    1 tool call(s) proposed" in text
    assert "    0 authorised" in text
    assert "    1 proposed but never authorised" in text


def test_the_record_states_what_it_does_not_prove(tmp_path: Path) -> None:
    """Wer ein Beweisstueck ausstellt, liefert dessen Grenzen mit — sonst ist es Werbung."""
    text = report.render(report.collect(_log(tmp_path, LAUF)))
    assert "does NOT prove" in text
    assert "before this export" in text


def test_the_fingerprint_changes_when_a_line_is_altered(tmp_path: Path) -> None:
    """Sonst bindet er nichts."""
    eintraege = report.collect(_log(tmp_path, LAUF))
    vorher = report.digest(eintraege)

    veraendert = LAUF[:1] + [(2.0, "r1", "executor", "exec.intent",
                              {"tool": "run_shell", "targets": ["/tmp/harmlos"],
                               "verdict": "allow"})] + LAUF[2:]
    zweitens = tmp_path / "zwei"
    zweitens.mkdir()
    nachher = report.digest(report.collect(_log(zweitens, veraendert)))

    assert vorher != nachher


def test_reordering_two_events_changes_the_fingerprint(tmp_path: Path) -> None:
    """Kette statt Summe: „erst geurteilt, dann ausgefuehrt" ist die halbe Aussage.

    Eine Summe ueber eine Menge bliebe hier gleich — und genau die Reihenfolge ist es,
    die den Kernel vom Protokollschreiber unterscheidet.
    """
    a = report.collect(_log(tmp_path, LAUF))
    getauscht = [LAUF[0], LAUF[2], LAUF[1]]
    (tmp_path / "b").mkdir()
    b = report.collect(_log(tmp_path / "b", getauscht))

    assert [e.type for e in a] != [e.type for e in b]
    assert report.digest(a) != report.digest(b)


def test_transport_noise_is_counted_not_deleted(tmp_path: Path) -> None:
    """Ein Auszug, der Stoerungen verschweigt, ist geschoent — einer, der in 1034
    identischen Zeilen ertrinkt, ist unlesbar. Beides waere dasselbe Ergebnis."""
    laut = LAUF + [(4.0 + i, "r1", "channel", "channel.error", {"error": "409 Conflict"})
                   for i in range(12)]
    text = report.render(report.collect(_log(tmp_path, laut)))

    assert "12 suppressed" in text
    assert text.count("409 Conflict") == 0
    assert "counted, not removed" in text


def test_an_empty_log_says_so_instead_of_pretending(tmp_path: Path) -> None:
    text = report.render(report.collect(tmp_path / "gibtsnicht.db"))
    assert "No events" in text


def test_a_secret_in_a_command_does_not_reach_the_record(tmp_path: Path) -> None:
    """Der Bericht ist zum Weitergeben gedacht. Was hier durchrutscht, geht mit."""
    zeilen = [(1.0, "r2", "executor", "exec.intent",
               {"tool": "run_shell", "verdict": "allow",
                "args": {"command": "curl -H 'Authorization: Bearer sk-ant-supergeheim-123' x"}})]
    text = report.render(report.collect(_log(tmp_path, zeilen)))
    assert "sk-ant-supergeheim-123" not in text
