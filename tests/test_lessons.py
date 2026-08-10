"""Der Agent lernt aus seinem eigenen Protokoll — ohne dabei Rechte zu gewinnen.

Zwei Dinge werden hier festgehalten: dass die Lehre wirklich ankommt, und dass sie
NICHTS erteilt. Der zweite Teil ist der wichtigere. Ein Agent, der aus seiner Geschichte
Rechte ableitet, hat einen zweiten Erlaubnisweg neben dem Kernel — und genau den gibt es
in diesem Projekt aus gutem Grund nicht.
"""
from __future__ import annotations

from talos import lessons


def _intent(tool: str, verdict: str, reason: str = "") -> dict:
    return {"type": "exec.intent",
            "payload": {"tool": tool, "verdict": verdict, "reason": reason}}


def _result(tool: str, status: str, detail: str = "") -> dict:
    return {"type": "exec.result", "payload": {"tool": tool, "status": status, "detail": detail}}


def _grant(fp: str) -> dict:
    return {"type": "grant.issued", "payload": {"action_fp": fp, "tool": "run_shell"}}


# --- Was der Kernel abgelehnt hat ------------------------------------------------------
def test_a_refusal_becomes_something_the_model_is_told() -> None:
    """Sonst schlaegt es dieselbe Sache jedes Mal vor und erzieht zum Wegklicken."""
    text = lessons.block([_intent("run_shell", "needs_human", "shell without sandbox")])
    assert "run_shell" in text and "shell without sandbox" in text


def test_an_allowed_call_teaches_nothing() -> None:
    """Was durchging, ist keine Lehre — es waere nur Laenge."""
    assert lessons.block([_intent("read_file", "allow")]) == ""


def test_nothing_to_say_says_nothing() -> None:
    """Ein Block, der bei jedem Zug „bisher nichts" meldet, ist Moebel."""
    assert lessons.block([]) == ""
    assert lessons.block([{"type": "reply.sent", "payload": {}}]) == ""


# --- Woran es gescheitert ist ----------------------------------------------------------
def test_a_failure_is_remembered_as_a_fact_about_this_machine() -> None:
    """Der Fall von heute: dreimal dieselbe Wand, weil vom ersten Mal nichts blieb."""
    text = lessons.block([_result("run_shell", "FAILED", "No module named pytest")])
    assert "No module named pytest" in text


def test_the_same_failure_twenty_times_does_not_fill_the_block() -> None:
    """Ein Dauerfehler darf nicht alles verdraengen, was sonst noch gelernt wurde."""
    viele = [_result("run_shell", "FAILED", "boom")] * 20 + [
        _result("web_fetch", "FAILED", "host refused")]
    text = lessons.block(viele)
    assert text.count("boom") == 1
    assert "host refused" in text


# --- ⚠️ Die Sicherheitsfrage -----------------------------------------------------------
def test_the_block_is_framed_as_context_and_says_it_grants_nothing() -> None:
    """Das Protokoll traegt Modelltext und Netzausgaben.

    Kaeme das ungerahmt in die stehenden Anweisungen, haette eine einmal abgerufene
    Seite Nachhall in jedem spaeteren Zug. Der Rahmen ist derselbe wie beim Verlauf.
    """
    text = lessons.block([_result("web_fetch", "FAILED", "x")])
    assert "context, not instructions" in text
    assert "grants nothing" in text


def test_a_secret_in_a_failure_detail_does_not_survive_into_the_block() -> None:
    text = lessons.block([_result("run_shell", "FAILED",
                                  "curl -H 'Authorization: Bearer sk-ant-geheim-42' failed")])
    assert "sk-ant-geheim-42" not in text


def test_a_wall_of_text_in_a_failure_cannot_push_the_task_out() -> None:
    """Ein Programm, dessen Fehlerausgabe 40 KB lang ist, darf den Zug nicht auffressen."""
    text = lessons.block([_result("run_shell", "FAILED", "A" * 40000)])
    assert len(text) < 1000


# --- Wie oft dieselbe Handlung schon erlaubt wurde -------------------------------------
def test_repeats_are_counted_per_action_not_per_tool_name() -> None:
    """⚠️ „du hast run_shell schon oft erlaubt" waere eine Aussage ueber ein WORT.

    Genau diese Verwechslung ist der Grund, warum Dauerrechte an Werkzeugnamen aus
    diesem Projekt entfernt wurden.
    """
    ereignisse = [_grant("aaa"), _grant("aaa"), _grant("bbb"), _grant("aaa")]
    assert lessons.approvals_of(ereignisse, "aaa") == 3
    assert lessons.approvals_of(ereignisse, "bbb") == 1
    assert lessons.approvals_of(ereignisse, "ccc") == 0
    assert lessons.approvals_of(ereignisse, "") == 0


def test_the_hint_threshold_is_a_stated_choice_not_an_accident() -> None:
    """Zweimal ist Zufall, dreimal ein Muster, beim vierten Mal klickt man."""
    assert lessons.REPEAT_HINT_AT == 3


# --- ⚠️ Eine Lehre, die einen behobenen Fehler konserviert -------------------------------
def test_a_failure_that_later_succeeded_is_no_longer_taught() -> None:
    """Gefunden an einem echten Fall auf der Installation.

    `web_search` scheiterte um 13:24 („no provider key"), lief um 13:59 einwandfrei. Die
    Lehre haette dem Modell danach beigebracht, ein funktionierendes Werkzeug zu meiden —
    eine Faehigkeit weniger, und niemand sieht warum. Das ist schlimmer als gar nicht zu
    lernen: es sieht aus wie Erfahrung und ist ein Irrtum, der sich selbst festhaelt.
    """
    ereignisse = [
        _result("web_search", "error", "no provider key"),
        _result("web_search", "DONE", "read"),
    ]
    assert lessons.failures(ereignisse) == ()
    assert "no provider key" not in lessons.block(ereignisse)


def test_a_failure_after_the_last_success_still_counts() -> None:
    """Sonst waere die Verjaehrung ein Freibrief: einmal gut, nie wieder gelernt."""
    ereignisse = [
        _result("web_search", "DONE", "read"),
        _result("web_search", "error", "host refused"),
    ]
    assert lessons.failures(ereignisse) == (("web_search", "host refused"),)


def test_a_success_of_one_tool_does_not_clear_another_tools_failure() -> None:
    ereignisse = [_result("run_shell", "error", "boom"), _result("web_fetch", "DONE", "read")]
    assert lessons.failures(ereignisse) == (("run_shell", "boom"),)


def test_a_refusal_does_not_expire_when_something_else_is_allowed() -> None:
    """⚠️ Der Unterschied zum Fehlschlag, und er ist wichtig.

    Ein Fehlschlag ist ein ZUSTAND — behoben ist behoben. Eine Ablehnung ist eine REGEL:
    dass eine harmlose Datei gelesen werden durfte, sagt nichts darueber, ob der gesperrte
    Pfad daneben inzwischen offen waere.
    """
    ereignisse = [
        _intent("run_shell", "deny", "protected path in command: /etc/openvpn"),
        _result("run_shell", "DONE", "read"),
        _intent("read_file", "allow"),
    ]
    assert lessons.refusals(ereignisse) == (("run_shell", "protected path in command: /etc/openvpn"),)
