"""Live-Anzeige im Terminal — belegt, was lief, und bleibt stumm, wenn nichts lief.

Die Anzeige ist die einzige Stelle, an der ein Lauf im Terminal sichtbar wird, bevor
die Antwort da ist. Deshalb prueft diese Datei ihre zwei Kanten: sie zeigt Werkzeug,
Urteil und Dauer — und sie erfindet nichts fuer eine Antwort, die keins brauchte.
"""
from __future__ import annotations

import io

from talos.agent_loop import AgentProgress, ProgressStage
from talos.cli_activity import CliActivity


class Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt

    def tick(self, s: float) -> None:
        self.jetzt += s


def _activity():
    aus, uhr = io.StringIO(), Uhr()
    return CliActivity(out=aus, clock=uhr), aus, uhr


def denken(schritt: int = 1, von: int = 10) -> AgentProgress:
    return AgentProgress(ProgressStage.THINKING, step=schritt, max_steps=von)


def werkzeug(tool: str = "read_file", summary: str = "/tmp/x") -> AgentProgress:
    return AgentProgress(ProgressStage.TOOL, tool=tool, status="running",
                         summary=summary, step=2, max_steps=10)


def ergebnis(tool: str = "read_file", status: str = "done", summary: str = "/tmp/x") -> AgentProgress:
    return AgentProgress(ProgressStage.RESULT, tool=tool, status=status,
                         summary=summary, step=2, max_steps=10)


def test_a_tool_free_answer_gets_no_display() -> None:
    """Trap 6: eine reine Textantwort bekommt keinen Kopf — auch keinen Denk-Strich."""
    a, aus, uhr = _activity()
    a.progress(denken())
    uhr.tick(3)
    a.succeed()
    assert aus.getvalue() == ""


def test_the_thinking_phase_appears_once_the_display_exists() -> None:
    """Sie war die laengste Phase des Laufs und die einzige, die nichts zeigte."""
    a, aus, uhr = _activity()
    a.progress(denken())
    uhr.tick(12)
    a.progress(werkzeug())
    uhr.tick(1.5)
    a.progress(ergebnis())
    a.succeed()
    text = aus.getvalue()
    assert "thinking (12.0s)" in text
    assert "▸ read_file /tmp/x" in text
    assert "✓ read_file (1.5s)" in text
    assert "done in" in text and "1 tool(s)" in text


def test_a_denied_call_wears_the_kernel_glyph_not_the_failure_one() -> None:
    """Der Kernel hat entschieden, nicht das Werkzeug versagt — zwei verschiedene Fakten."""
    a, aus, _ = _activity()
    a.progress(werkzeug("run_shell", "rm -rf /"))
    a.progress(ergebnis("run_shell", "denied", "rm -rf /"))
    text = aus.getvalue()
    assert "⛒ run_shell" in text and "✕ run_shell" not in text


def test_an_approval_wait_wears_the_gate_glyph() -> None:
    a, aus, _ = _activity()
    a.progress(werkzeug())
    a.progress(ergebnis(status="needs_human"))
    assert "⏸ read_file" in aus.getvalue()


def test_a_plan_announcement_is_a_line_not_a_permission() -> None:
    a, aus, _ = _activity()
    a.progress(AgentProgress(ProgressStage.PLAN, summary="3 steps: read, edit, verify",
                             step=1, max_steps=10))
    assert "≡ plan — 3 steps: read, edit, verify" in aus.getvalue()


def test_fail_closes_the_display_and_succeed_adds_nothing() -> None:
    a, aus, _ = _activity()
    a.progress(werkzeug())
    a.fail("boom")
    a.succeed()
    text = aus.getvalue()
    assert "✕ failed — boom" in text and "done in" not in text


def test_both_cli_channels_offer_the_display() -> None:
    """Die Verdrahtung, nicht nur die Klasse: der Conductor fragt den Kanal per
    `getattr(channel, 'begin_activity')` — fehlt die Methode, bleibt es still,
    und kein Test der Klasse wuerde es merken (Trap 7)."""
    from talos.askcli import CliChannel
    from talos.chatcli import ChatChannel

    assert isinstance(ChatChannel(uid=1000).begin_activity("cli:1000"), CliActivity)
    assert isinstance(CliChannel(question="q", uid=1000).begin_activity("cli:1000"), CliActivity)


def test_the_configured_style_reaches_the_display() -> None:
    """TALOS_STATUS_STYLE=expressive gilt auch im Terminal — sonst stuende der
    Betreiber mit Emoji im Messenger und Glyphen im CLI, zwei Wahrheiten."""
    from talos.askcli import CliChannel

    kanal = CliChannel(question="q", uid=1000, style="expressive")
    anzeige = kanal.begin_activity("cli:1000")
    assert anzeige._style.tool_symbol("agent_consult") == "🤝"
    assert anzeige._style.tool_symbol("read_file") == "📖"


def test_every_gated_tool_has_an_expressive_glyph_and_verb() -> None:
    """Die Emoji-Karte driftete schon einmal hinter dem Manifest her (fetch_page
    statt web_fetch — ein Schluessel, den es als Werkzeug nie gab)."""
    from talos.tools import default_manifest
    from talos.ux import EXPRESSIVE

    echte = default_manifest().tools
    namen = sorted(echte) if isinstance(echte, dict) else sorted(s.name for s in echte)
    fehlend_glyph = [n for n in namen if n not in EXPRESSIVE.tool_glyphs]
    fehlend_verb = [n for n in namen if n not in EXPRESSIVE.tool_verbs]
    assert not fehlend_glyph and not fehlend_verb, (
        f"Emoji-Karte unvollstaendig: glyphs {fehlend_glyph}, verbs {fehlend_verb}"
    )
