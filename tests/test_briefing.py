"""Das Morgen-Briefing — was es melden muss, und was es nie still lassen darf.

Der Vertrag des Briefings: sein Inhalt kommt aus den haltbaren Quellen (Event-Log,
Zeitplan-DB, Anker-Datei), ein gebrochenes Log macht ihn laut statt leise, `--install`
schreibt ueber den bestehenden ScheduleStore-Pfad (und erteilt dabei nichts), und der
Mail-Versand des Ankers ist ein zweiter Versandweg — keine neue Empfangsflaeche.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from talos import anchor, briefing
from talos.channel import Principal
from talos.config import TalosConfig
from talos.eventlog import Event, EventLog, new_run_id
from talos.schedule import ScheduleStore

T0 = 1_800_000_000.0  # fester Zeitpunkt — die Rechnung haengt nicht am Testlauf


def _fill(db: Path, n: int = 3, *, now: float = T0) -> EventLog:
    log = EventLog(db)
    run = new_run_id()
    for i in range(n):
        log.append(Event(run, "conductor", "reason.step", {"n": i}), now=now + i)
    return log


def _briefing(tmp_path: Path, argv=(), **kw) -> tuple[int, str]:
    text = io.StringIO()
    code = briefing.run_briefing(
        list(argv),
        stdout=text,
        db_path=kw.pop("db_path", tmp_path / "eventlog.db"),
        schedule_db=kw.pop("schedule_db", tmp_path / "schedules.db"),
        anchors_path=kw.pop("anchors_path", tmp_path / "anchors.jsonl"),
        **kw,
    )
    return code, text.getvalue()


def _config(principals=()) -> TalosConfig:
    return TalosConfig(
        bot_token="",
        bot_username="b",
        allowed_principals=frozenset(principals),
        eventlog_db=Path("/tmp/talos-test/eventlog.db"),
        snapshot_dir=Path("/tmp/talos-test/snapshots"),
    )


def _pin_anchor(tmp_path: Path, *, now: float) -> None:
    anchor.run_anchor(
        [],
        stdout=io.StringIO(),
        db_path=tmp_path / "eventlog.db",
        anchors_path=tmp_path / "anchors.jsonl",
        now=now,
    )


def _break_chain(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("UPDATE events SET payload_json = '{}' WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


# --- Inhalt aus den Quellen --------------------------------------------------------

def test_the_briefing_reports_runs_errors_chain_and_anchor(tmp_path: Path) -> None:
    db = tmp_path / "eventlog.db"
    log = _fill(db)
    run = new_run_id()
    log.append(Event(run, "conductor", "reason.done", {"status": "answered"}), now=T0 + 10)
    log.append(Event(run, "worker", "error", {"error": "boom"}), now=T0 + 20)
    _pin_anchor(tmp_path, now=T0 + 30)

    code, out = _briefing(tmp_path, now=T0 + 30 + 2 * 3600)
    assert code == 0
    assert "runs: 1 in the last 24h" in out
    assert "errors: 1 in the last 24h — newest: boom" in out
    assert "chain: intact" in out
    assert "(2h ago)" in out and "verify ok" in out
    assert "approvals: none pending" in out
    assert "status: ok" in out


def test_a_pending_approval_is_named_with_its_tool(tmp_path: Path) -> None:
    db = tmp_path / "eventlog.db"
    log = _fill(db)
    log.append(
        Event(new_run_id(), "conductor", "approval.parked",
              {"tool": "write_file", "targets": ["x"]}),
        now=T0 + 10,
    )
    code, out = _briefing(tmp_path, now=T0 + 60)
    assert code == 0
    assert "approvals: 1 pending — write_file" in out


def test_a_decided_approval_is_no_longer_pending(tmp_path: Path) -> None:
    db = tmp_path / "eventlog.db"
    log = _fill(db)
    log.append(Event(new_run_id(), "conductor", "approval.parked", {"tool": "run_shell"}),
               now=T0 + 10)
    log.append(Event(new_run_id(), "human", "approval.granted", {"tool": "run_shell"}),
               now=T0 + 20)
    code, out = _briefing(tmp_path, now=T0 + 60)
    assert "approvals: none pending" in out


def test_an_expired_approval_is_not_reported_as_pending(tmp_path: Path) -> None:
    """Nach Ablauf der TTL ist die Anfrage faktisch tot — sie als „offen" zu melden,
    wuerde den Betreiber einen Geist suchen lassen."""
    db = tmp_path / "eventlog.db"
    log = _fill(db)
    log.append(Event(new_run_id(), "conductor", "approval.parked", {"tool": "write_file"}),
               now=T0 + 10)
    code, out = _briefing(tmp_path, now=T0 + 10 + briefing.TTL_SECONDS + 60)
    assert "approvals: none pending" in out


def test_no_event_log_is_said_not_hidden(tmp_path: Path) -> None:
    code, out = _briefing(tmp_path, now=T0)
    assert code == 0
    assert "no event log yet" in out
    assert "anchor: none yet" in out


# --- Gebrochenes Log: laut, nie still ------------------------------------------------

def test_a_broken_chain_makes_the_briefing_critical(tmp_path: Path) -> None:
    db = tmp_path / "eventlog.db"
    _fill(db)
    _break_chain(db)
    code, out = _briefing(tmp_path, now=T0 + 60)
    assert code == 1
    assert "chain: BROKEN — first altered entry id 1" in out
    assert "status: critical" in out


def test_an_unreadable_log_is_named_explicitly(tmp_path: Path) -> None:
    db = tmp_path / "eventlog.db"
    db.write_text("das ist keine datenbank", encoding="utf-8")
    code, out = _briefing(tmp_path, now=T0)
    assert "NOT a readable database" in out
    assert "chain: not checked" in out


# --- Versand -------------------------------------------------------------------------

def test_send_delivers_the_briefing_to_the_owner_chat(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")
    geschickt: list[str] = []
    code, out = _briefing(tmp_path, ["--send"], now=T0 + 60, sender=geschickt.append)
    assert code == 0
    assert "sent owner chat" in out
    assert len(geschickt) == 1
    assert "briefing" in geschickt[0] and "chain: intact" in geschickt[0]
    assert "chain: intact" in out                      # stdout steht auch MIT --send


def test_a_failed_send_is_critical_for_the_call(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")

    def kaputt(_text: str) -> None:
        raise OSError("network down")

    code, out = _briefing(tmp_path, ["--send"], now=T0 + 60, sender=kaputt)
    assert code == 1
    assert "send failed" in out and "network down" in out


# --- --install ueber den bestehenden ScheduleStore-Pfad --------------------------------

def test_install_writes_a_daily_schedule_entry(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.db")
    cfg = _config([Principal("telegram", "100000001")])
    code, out = _briefing(tmp_path, ["--install"], config=cfg, schedules=store)
    assert code == 0, out
    eintraege = store.list_for("telegram:100000001")
    assert len(eintraege) == 1
    eintrag = eintraege[0]
    assert eintrag.cron == briefing.BRIEFING_CRON
    assert eintrag.prompt == briefing.BRIEFING_PROMPT
    assert eintrag.principal == "telegram:100000001"
    assert "UnattendedCeiling" in out                  # die Decke wird mitgesagt
    store.close()


def test_install_is_idempotent(tmp_path: Path) -> None:
    """Ein Installer, der bei jedem Lauf eine Zeile mehr schreibt, stirbt an MAX_TASKS."""
    store = ScheduleStore(tmp_path / "schedules.db")
    cfg = _config([Principal("telegram", "100000001")])
    _briefing(tmp_path, ["--install"], config=cfg, schedules=store)
    code, out = _briefing(tmp_path, ["--install"], config=cfg, schedules=store)
    assert code == 0 and "already installed" in out
    assert len(store.list_for("telegram:100000001")) == 1
    store.close()


def test_install_without_a_telegram_principal_fails_clearly(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.db")
    code, out = _briefing(tmp_path, ["--install"], config=_config(), schedules=store)
    assert code == 1
    assert "no telegram principal" in out
    assert store.count() == 0                          # nichts halb installiert
    store.close()


# --- anchor --mail ----------------------------------------------------------------------

def _anchor(tmp_path: Path, argv=(), **kw) -> tuple[int, str]:
    text = io.StringIO()
    code = anchor.run_anchor(
        list(argv),
        stdout=text,
        db_path=kw.pop("db_path", tmp_path / "eventlog.db"),
        anchors_path=kw.pop("anchors_path", tmp_path / "anchors.jsonl"),
        **kw,
    )
    return code, text.getvalue()


def test_anchor_mail_delivers_the_digest(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")
    gemailt: list[str] = []
    code, out = _anchor(tmp_path, ["--mail"], now=T0 + 60, mail_sender=gemailt.append)
    assert code == 0
    assert "sent owner mail" in out
    assert len(gemailt) == 1
    assert "anchor" in gemailt[0] and "verify ok" in gemailt[0]


def test_anchor_send_and_mail_both_deliver(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")
    geschickt: list[str] = []
    gemailt: list[str] = []
    code, out = _anchor(
        tmp_path, ["--send", "--mail"], now=T0 + 60,
        sender=geschickt.append, mail_sender=gemailt.append,
    )
    assert code == 0
    assert len(geschickt) == 1 and len(gemailt) == 1
    assert "sent owner chat" in out and "sent owner mail" in out


def test_anchor_mail_failure_is_critical_for_the_call(tmp_path: Path) -> None:
    _fill(tmp_path / "eventlog.db")

    def kaputt(_text: str) -> None:
        raise OSError("smtp down")

    code, out = _anchor(tmp_path, ["--mail"], now=T0 + 60, mail_sender=kaputt)
    assert code == 1
    assert "mail failed" in out and "smtp down" in out


def test_mail_sender_needs_configuration_and_an_owner() -> None:
    """Der Aufbau des Mail-Versandwegs scheitert mit einem Satz, keinem Traceback."""
    with pytest.raises(ValueError, match="mail is not configured"):
        anchor._build_mail_sender_for(_config())
    with pytest.raises(ValueError, match="no mail principal"):
        anchor._build_mail_sender_for(
            TalosConfig(
                bot_token="",
                bot_username="b",
                allowed_principals=frozenset(),
                eventlog_db=Path("/tmp/talos-test/eventlog.db"),
                snapshot_dir=Path("/tmp/talos-test/snapshots"),
                mail_host="mail.example.ch",
                mail_user="talos@example.ch",
                mail_password="geheim",
            )
        )


# --- Rahmen ------------------------------------------------------------------------------

def test_unknown_option_is_a_usage_error(tmp_path: Path) -> None:
    code, out = _briefing(tmp_path, ["--flub"])
    assert code == 2 and "usage" in out


def test_help_exits_zero(tmp_path: Path) -> None:
    code, out = _briefing(tmp_path, ["--help"])
    assert code == 0 and "usage" in out


def test_briefing_is_registered_in_the_cli() -> None:
    """Die Registrierung ist Teil des Vertrags — test_cli haelt HELP gegen TABLE."""
    from talos import cli

    assert "briefing" in cli.TABLE
    assert "briefing" in cli.HELP
