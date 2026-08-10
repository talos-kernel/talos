"""Das Missions-Panel — und die Zahlen, die bewusst NICHT drinstehen."""
from __future__ import annotations

from talos.ux import bar, mission_panel


def test_the_bar_reflects_real_progress() -> None:
    assert bar(0, 10) == "░" * 10
    assert bar(10, 10) == "█" * 10
    assert bar(5, 10).count("█") == 5


def test_no_bar_without_a_known_target() -> None:
    """Ohne Ziel gibt es keinen Fortschritt — ein voller Balken waere gelogen."""
    assert bar(3, 0) == "░" * 10


def test_the_panel_shows_only_measured_values() -> None:
    """Der Punkt des Panels: keine Zuversicht, keine Restzeit.

    Beides kennt ein Agent nicht. Ein Sprachmodell hat keine kalibrierte Confidence,
    und wie lange ein Lauf noch dauert, weiss es erst hinterher. Solche Zahlen sehen
    aus wie Messwerte und sind geraten — genau die vorgetaeuschte Praezision, die
    SOUL.md verbietet.
    """
    panel = mission_panel(step=3, max_steps=100, elapsed_s=9.4, tools_run=2,
                          last_event="read — notes.md")
    lowered = panel.lower()
    for erfunden in ("conf", "eta", "%", "remaining"):
        assert erfunden not in lowered, f"geratener Wert im Panel: {erfunden}"
    assert "3/100" in panel and "9s" in panel and "tools 2" in panel
    assert "read — notes.md" in panel


def test_the_panel_survives_a_run_without_a_step_counter() -> None:
    panel = mission_panel(step=0, max_steps=0, elapsed_s=2.0, tools_run=0)
    assert "step" not in panel and "2s" in panel
