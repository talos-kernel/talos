"""Cron-Ausdruecke — der Kalender, den ein blosses Intervall nicht kennt.

Ein Timer zaehlt, ein Zeitplan kennt den Tag. Geprueft wird die Rechnung gegen feste
Zeitpunkte, nicht gegen `time.time()` — sonst haengt das Ergebnis am Testlauf.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from talos.cron import CronError, looks_like_cron, parse
from talos.schedule import ScheduleStore

# Mittwoch, 5. August 2026, 10:30 Ortszeit — als Epoch, damit die Rechnung fix ist.
MITTWOCH_1030 = time.mktime((2026, 8, 5, 10, 30, 0, 0, 0, -1))


def _wann(ausdruck: str, ab: float = MITTWOCH_1030) -> str:
    return time.strftime("%a %d.%m %H:%M", time.localtime(parse(ausdruck).next_after(ab)))


# --- Das, was ein Intervall NICHT kann --------------------------------------------
def test_a_weekday_expression_skips_the_weekend() -> None:
    """Der Punkt der ganzen Uebung: „werktags um 08:00" ist kein Abstand."""
    # Freitag 18:00 -> naechster Werktag-Morgen ist Montag, nicht Samstag.
    freitag_abend = time.mktime((2026, 8, 7, 18, 0, 0, 0, 0, -1))
    assert _wann("0 8 * * MON-FRI", freitag_abend).startswith("Mon 10.08")


def test_a_daily_time_lands_on_the_next_day_when_it_is_past() -> None:
    assert _wann("0 9 * * *") == "Thu 06.08 09:00"
    assert _wann("0 11 * * *") == "Wed 05.08 11:00"


def test_steps_lists_and_ranges_all_parse() -> None:
    assert _wann("*/15 * * * *") == "Wed 05.08 10:45"
    assert _wann("0 8,20 * * *") == "Wed 05.08 20:00"
    assert _wann("30 9-17 * * *") == "Wed 05.08 11:30"


def test_month_and_weekday_names_work() -> None:
    """`0 9 * * MON` soll lesbar sein — Zahlen fuer Wochentage verwechselt jeder."""
    assert _wann("0 9 * * MON").startswith("Mon 10.08")
    assert _wann("0 9 1 JAN *").startswith("Fri 01.01")


def test_sunday_is_both_zero_and_seven() -> None:
    """Beide Schreibweisen sind ueblich; eine davon nicht zu kennen ist ein stiller Fehler."""
    assert _wann("0 9 * * 0") == _wann("0 9 * * 7")


def test_day_and_weekday_together_mean_OR_as_in_vixie_cron() -> None:
    """Die Regel ueberrascht jeden, der sie nicht kennt: `0 9 1 * MON` heisst
    „am 1. UND montags", nicht „nur der 1., falls Montag"."""
    c = parse("0 9 1 * MON")
    erster = time.localtime(time.mktime((2026, 9, 1, 9, 0, 0, 0, 0, -1)))  # Dienstag
    montag = time.localtime(time.mktime((2026, 8, 10, 9, 0, 0, 0, 0, -1)))
    assert c.matches(erster) and c.matches(montag)


# --- Was abgelehnt wird, und warum es dem Betreiber gesagt wird --------------------
def test_a_broken_expression_says_what_is_wrong() -> None:
    for kaputt, erwartet in (
        ("0 9 * *", "5 fields"),
        ("99 9 * * *", "outside"),
        ("0 9 * * NOPE", "not a number"),
        ("0 17-9 * * *", "backwards"),
        ("*/0 * * * *", "positive"),
    ):
        with pytest.raises(CronError) as fehler:
            parse(kaputt)
        assert erwartet in str(fehler.value)


def test_an_expression_that_never_comes_round_is_refused() -> None:
    """31. Februar ist ein Tippfehler, kein Zeitplan — und darf keine Endlossuche werden."""
    with pytest.raises(CronError):
        parse("0 9 31 2 *").next_after(MITTWOCH_1030)


def test_minutes_and_expressions_are_told_apart() -> None:
    assert looks_like_cron("0 9 * * MON-FRI tu was")
    assert not looks_like_cron("15 tu was")


# --- Im Speicher: drei Wege, ein Termin -------------------------------------------
def test_a_cron_task_gets_its_calendar_slot(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "s.db")
    task = store.add(
        conversation="c", principal="telegram:1", prompt="Bericht",
        cron="0 9 * * *", now=MITTWOCH_1030,
    )
    assert task is not None and task.cron == "0 9 * * *"
    assert time.strftime("%d.%m %H:%M", time.localtime(task.next_run)) == "06.08 09:00"
    assert "at 0 9 * * *" in task.describe()


def test_a_one_shot_disappears_after_it_ran(tmp_path: Path) -> None:
    """Ein „erinnere mich morgen um 9" darf nicht zum taeglichen Wecker werden."""
    store = ScheduleStore(tmp_path / "s.db")
    task = store.add(
        conversation="c", principal="telegram:1", prompt="einmal",
        interval_s=120, once=True, now=MITTWOCH_1030,
    )
    assert task is not None and store.count() == 1
    store.mark_run(task.id, now=MITTWOCH_1030 + 120)
    assert store.count() == 0


def test_a_cron_task_is_rescheduled_to_the_next_slot_not_to_now_plus_interval(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "s.db")
    task = store.add(
        conversation="c", principal="telegram:1", prompt="taeglich",
        cron="0 9 * * *", now=MITTWOCH_1030,
    )
    assert task is not None
    store.mark_run(task.id, now=task.next_run)
    danach = store.list_for("c")[0]
    assert time.strftime("%d.%m %H:%M", time.localtime(danach.next_run)) == "07.08 09:00"


def test_an_older_database_gains_the_new_columns(tmp_path: Path) -> None:
    """Der Fall, der eine LAUFENDE Instanz umbringt: `CREATE TABLE IF NOT EXISTS`
    ergaenzt keine Spalten, und eine laufende Installation hat ihre `schedules.db` schon."""
    import sqlite3

    pfad = tmp_path / "alt.db"
    alt = sqlite3.connect(str(pfad))
    alt.execute(
        "CREATE TABLE schedules (id TEXT PRIMARY KEY, conversation TEXT NOT NULL,"
        " principal TEXT NOT NULL, prompt TEXT NOT NULL, interval_s INTEGER NOT NULL,"
        " next_run REAL NOT NULL, created REAL NOT NULL, last_run REAL)"
    )
    alt.execute(
        "INSERT INTO schedules VALUES ('alt1','c','telegram:1','frueher',3600,1,1,NULL)"
    )
    alt.commit()
    alt.close()

    store = ScheduleStore(pfad)
    assert store.available
    assert len(store.list_for("c")) == 1          # der alte Auftrag ueberlebt
    neu = store.add(
        conversation="c", principal="telegram:1", prompt="neu", cron="0 9 * * *",
        now=MITTWOCH_1030,
    )
    assert neu is not None and neu.cron == "0 9 * * *"
