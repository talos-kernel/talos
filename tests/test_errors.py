"""Fehler-Klassifizierung — Klassen, Strategien, und wo die Zeile haengt.

Deterministisch: Muster -> Klasse -> ein Satz Strategie. Keine Klasse ohne
Muster (Ausnahme „logic" bei status=error), keine Zeile bei Erfolg, und die
Zeile haengt am zentralen Trichter `tool_history_entry`, damit sie in JEDEM
Lauf ankommt — auch im wiederaufgenommenen nach einer Freigabe.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import errors
from talos.agent_loop import tool_history_entry


# --- Die Klassen und ihre Muster -------------------------------------------------------

def test_rate_limit_wins_over_generic_error() -> None:
    assert errors.classify("HTTP 429 Too Many Requests — retry later") == "rate-limit"
    assert errors.classify("error: rate limit exceeded for this API") == "rate-limit"


def test_network_class() -> None:
    assert errors.classify("ssh: connect to host x: Connection refused") == "network"
    assert errors.classify("curl: (6) Could not resolve host: api.x") == "network"
    assert errors.classify("request timed out after 15s") == "network"


def test_auth_and_permission_are_distinct() -> None:
    assert errors.classify("HTTP 401 Unauthorized: bad credentials") == "auth"
    assert errors.classify("HTTP 403 Forbidden") == "permission"
    assert errors.classify("mv: Permission denied") == "permission"


def test_not_found_class() -> None:
    assert errors.classify("HTTP 404 Not Found") == "not-found"
    assert errors.classify("cat: /x/y.md: No such file or directory") == "not-found"


def test_no_pattern_no_class() -> None:
    assert errors.classify("total 12\n-rw-r--r-- 1 ali wheel 5 Aug 30 x") is None


# --- Die Zeile: wann sie kommt und wann sie schweigt -----------------------------------

def test_error_status_without_pattern_gets_logic() -> None:
    zeile = errors.note("error", "something odd happened")
    assert zeile.startswith("\n[error class: logic")


def test_failure_signal_in_text_triggers_without_error_status() -> None:
    zeile = errors.note("done", "rc=2\ncurl: (6) Could not resolve host: api.x")
    assert "network" in zeile
    zeile = errors.note("done", "HTTP 429 Too Many Requests")
    assert "rate-limit" in zeile


def test_success_with_lucky_words_stays_silent() -> None:
    """Ein erfolgreicher Aufruf, dessen Text zufaellig „429" enthaelt, bekommt
    keine Zeile — der Anlass ist der Fehlschlag, nicht das Wort."""
    assert errors.note("done", "der Report nennt 429 aktive Nutzer") == ""
    assert errors.note("done", "rc=0\nalles gut") == ""


def test_note_stays_one_bounded_line() -> None:
    zeile = errors.note("error", "HTTP 403 Forbidden " + "x" * 2000)
    assert zeile.count("\n") == 1
    assert len(zeile) <= errors.MAX_NOTE_CHARS


# --- Der Trichter: die Zeile reist mit der Historie ------------------------------------

def test_tool_history_entry_carries_the_hint_on_failure() -> None:
    eintrag = tool_history_entry("http_request", "done", "HTTP 429 Too Many Requests", None)
    assert "[http_request -> done]" in eintrag
    assert "error class: rate-limit" in eintrag


def test_tool_history_entry_stays_plain_on_success() -> None:
    eintrag = tool_history_entry("read_file", "done", "inhalt", None)
    assert "error class" not in eintrag
