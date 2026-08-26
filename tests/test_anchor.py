"""Der Anker fuer die Hash-Kette — was ihn tragen muss.

`verify` allein ist blind fuer das Abschneiden des Endes: eine kuerzere, in sich
stimmige Kette sieht aus wie eine intakte. Diese Tests pruefen genau den Unterschied,
den der Anker macht — und dass er dabei nichts veraendert, was er nicht soll.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import anchor
from talos.eventlog import Event, EventLog, new_run_id

T0 = 1_800_000_000.0  # fester Zeitpunkt — die Rechnung haengt nicht am Testlauf


def _out() -> io.StringIO:
    return io.StringIO()


def _fill(db: Path, n: int = 3, *, now: float = T0) -> EventLog:
    log = EventLog(db)
    run = new_run_id()
    for i in range(n):
        log.append(Event(run, "conductor", "reason.step", {"n": i}), now=now + i)
    return log


def _anchor(tmp_path: Path, argv=(), **kw) -> tuple[int, str]:
    text = _out()
    code = anchor.run_anchor(
        list(argv),
        stdout=text,
        db_path=kw.pop("db_path", tmp_path / "eventlog.db"),
        anchors_path=kw.pop("anchors_path", tmp_path / "anchors.jsonl"),
        **kw,
    )
    return code, text.getvalue()


def _records(path: Path) -> list[dict]:
    return [json.loads(z) for z in path.read_text(encoding="utf-8").splitlines() if z.strip()]


def test_an_intact_chain_anchors_with_exit_zero(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")
    code, out = _anchor(tmp_path, now=T0 + 60)
    assert code == 0
    assert "verify=ok" in out and "count=3" in out and "status ok" in out
    records = _records(tmp_path / "anchors.jsonl")
    assert len(records) == 1
    assert records[0]["verify_ok"] is True and records[0]["count"] == 3
    assert len(records[0]["head_hash"]) == 64        # der volle Hash liegt in der Datei


def test_the_recorded_head_is_the_chain_tip(tmp_path: Path) -> None:
    """Der Anker muss den WIRKLICHEN Kopf festhalten — sonst vergleicht er gegen nichts."""
    _fill(tmp_path / "eventlog.db")
    _anchor(tmp_path, now=T0 + 60)
    kopf = _records(tmp_path / "anchors.jsonl")[0]["head_hash"]
    conn = sqlite3.connect(str(tmp_path / "eventlog.db"))
    try:
        letzter = conn.execute(
            "SELECT chain_hash FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert kopf == letzter


def test_a_broken_chain_is_critical_and_names_the_entry(tmp_path: Path) -> None:
    """Eine nachtraeglich geaenderte Zeile bricht die Kette — verify nennt die id."""
    _fill(tmp_path / "eventlog.db")
    conn = sqlite3.connect(str(tmp_path / "eventlog.db"))
    try:
        conn.execute("UPDATE events SET payload_json = '{}' WHERE id = 2")
        conn.commit()
    finally:
        conn.close()
    code, out = _anchor(tmp_path, now=T0 + 60)
    assert code == 1
    assert "critical" in out and "id 2" in out and "verify=broken:2" in out
    # Auch ein kritischer Befund wird verankert — die Historie soll den schlechten
    # Stand zeigen, nicht nur die guten.
    assert _records(tmp_path / "anchors.jsonl")[0]["verify_ok"] is False


def test_tail_truncation_is_caught_against_the_previous_anchor(tmp_path: Path) -> None:
    """Der eine Fall, den verify allein NICHT sieht: das Ende fehlt, die Kette stimmt."""
    db = tmp_path / "eventlog.db"
    _fill(db, 4)
    code, _ = _anchor(tmp_path, now=T0 + 60)
    assert code == 0
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM events WHERE id > 2")   # die letzten zwei Zeilen weg
        conn.commit()
    finally:
        conn.close()
    # Die Kette ist in sich weiterhin intakt — nur der alte Anker verraet den Schnitt.
    code, out = _anchor(tmp_path, now=T0 + 120)
    assert code == 1
    assert "tail truncation" in out and "4 entries were anchored, 2 remain" in out


def test_a_stale_head_warns_but_does_not_fail(tmp_path: Path) -> None:
    """Ein Tag ohne ein einziges Ereignis ist kein normaler Zustand — aber eine Warnung,
    kein Abbruch: ein Cron-Waechter soll nicht an einem ruhigen Sonntag umfallen."""
    _fill(tmp_path / "eventlog.db")
    assert _anchor(tmp_path, now=T0 + 60)[0] == 0
    code, out = _anchor(tmp_path, now=T0 + 60 + anchor.STALE_AFTER_S + 60)
    assert code == 0
    assert "warn head unchanged" in out and "status warn" in out
    # Knapp UNTER der Schwelle bleibt es ruhig — die Grenze ist eine echte.
    assert _anchor(tmp_path, now=T0 + 60 + anchor.STALE_AFTER_S - 60)[0] == 0


def test_without_a_log_there_is_nothing_to_anchor(tmp_path: Path) -> None:
    """Dieselbe Ehrlichkeit wie `verify`: kein Log heisst „ist nie gelaufen", nicht
    „intakt" — und es wird auch kein Log erzeugt, nur um dann eines zu haben."""
    code, out = _anchor(tmp_path, now=T0)
    assert code == 0
    assert "nothing to anchor" in out
    assert not (tmp_path / "eventlog.db").exists()
    assert not (tmp_path / "anchors.jsonl").exists()


def test_the_anchor_file_is_append_only_and_tight_from_the_first_byte(tmp_path: Path) -> None:
    import os
    import stat

    _fill(tmp_path / "eventlog.db")
    _anchor(tmp_path, now=T0 + 60)
    _anchor(tmp_path, now=T0 + 120)
    ziel = tmp_path / "anchors.jsonl"
    assert len(_records(ziel)) == 2                   # angehaengt, nie ueberschrieben
    assert stat.S_IMODE(ziel.stat().st_mode) & 0o077 == 0


def test_send_delivers_the_digest_to_the_owner_chat(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")
    geschickt: list[str] = []
    code, out = _anchor(tmp_path, ["--send"], now=T0 + 60, sender=geschickt.append)
    assert code == 0
    assert "sent owner chat" in out
    assert len(geschickt) == 1
    digest = geschickt[0]
    assert "anchor" in digest and "3 events" in digest and "verify ok" in digest
    assert "count=3" in out                          # stdout steht auch MIT --send


def test_send_without_a_way_to_send_fails_loudly(tmp_path: Path) -> None:
    """Wer --send in einen Cron schreibt, will den Anker AUS der Maschine — ein stiller
    Versandfehler waere genau der Ausfall, den der Anker verhindern soll."""
    _fill(tmp_path / "eventlog.db")

    def kaputt(_text: str) -> None:
        raise OSError("network down")

    code, out = _anchor(tmp_path, ["--send"], now=T0 + 60, sender=kaputt)
    assert code == 1
    assert "send failed" in out and "network down" in out


def test_no_token_and_no_principal_are_clear_errors_not_tracebacks() -> None:
    """Der Aufbau des Versandwegs scheitert mit einem Satz, den man befolgen kann."""
    import pytest

    from talos.config import TalosConfig

    basis = dict(
        bot_token="", bot_username="b", allowed_principals=frozenset(),
        eventlog_db=Path("/tmp/talos-test/eventlog.db"),
        snapshot_dir=Path("/tmp/talos-test/snapshots"),
    )
    with pytest.raises(ValueError, match="no telegram token"):
        anchor._build_sender_for(TalosConfig(**basis))
    with pytest.raises(ValueError, match="no telegram principal"):
        anchor._build_sender_for(
            TalosConfig(**{**basis, "bot_token": "123:abc"})
        )


def test_the_digest_never_carries_the_token(tmp_path: Path) -> None:
    """Der Versandweg schwaerzt im Client; der Digest selbst darf das Geheimnis gar
    nicht erst kennen — er wird aus dem Anker-Record gebaut, nicht aus der Config."""
    _fill(tmp_path / "eventlog.db")
    geschickt: list[str] = []
    _anchor(tmp_path, ["--send"], now=T0 + 60, sender=geschickt.append)
    assert "123:abc" not in geschickt[0]


def test_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    code, out = _anchor(tmp_path, ["--flub"])
    assert code == 2 and "usage" in out


def test_help_exits_zero(tmp_path: Path) -> None:
    code, out = _anchor(tmp_path, ["--help"])
    assert code == 0 and "usage" in out


def test_anchor_is_registered_in_the_cli() -> None:
    """Die Registrierung ist Teil des Vertrags — test_cli haelt HELP gegen TABLE."""
    from talos import cli

    assert "anchor" in cli.TABLE
    assert "anchor" in cli.HELP
