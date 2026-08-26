"""Wer der Agent ist, steht in EINER Datei.

the operator will den Agenten umbenennen koennen, ohne den Code zu durchsuchen. Die SOUL traegt
darum beides: das Wesen im Text und den Namen in ihrer ersten Ueberschrift. Diese Tests
halten fest, dass ein zweiter Ort nie wieder entsteht — und dass eine kaputte oder
fehlende Datei den Agenten nicht stumm schaltet.
"""
from __future__ import annotations

from pathlib import Path

from talos.identity import DEFAULT_NAME, FALLBACK_PREAMBLE, MAX_SOUL_CHARS, agent_name, load_soul
from talos.instructions import load_instruction_context


def _soul(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "SOUL.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_name_comes_from_the_first_heading(tmp_path: Path) -> None:
    assert agent_name(_soul(tmp_path, "# Argus\n\nYou watch.\n")) == "Argus"


def test_renaming_takes_effect_without_a_restart(tmp_path: Path) -> None:
    """Umbenennen wirkt sofort — der Prozess muss dafuer nicht neu starten.

    Genau das ging live schief: der Name wurde beim Start einmal gelesen, danach
    aenderte die SOUL ihre Ueberschrift, und der Waechter stellte sich weiter unter
    dem alten Namen vor. Der Zwischenspeicher haengt deshalb am Zeitstempel der
    Datei, nicht an der Laufzeit des Prozesses.
    """
    soul = _soul(tmp_path, "# TALOS\n\nYou guard.\n")
    assert agent_name(soul) == "Talos"
    assert "You guard." in load_soul(soul)

    # Gleiche Laenge wie vorher — der Zwischenspeicher darf sich nicht auf die
    # Dateigroesse verlassen, sonst ueberlebt „# TALOS" ein „# ARGUS" unbemerkt.
    _soul(tmp_path, "# ARGUS\n\nYou guard.\n")
    assert agent_name(soul) == "Argus"

    _soul(tmp_path, "# WARDEN\n\nYou watch.\n")
    assert agent_name(soul) == "Warden"
    assert "You watch." in load_soul(soul)


def test_shouting_heading_becomes_a_title(tmp_path: Path) -> None:
    """Die Ueberschrift darf schreien; die Kopfzeile im Chat soll es nicht."""
    assert agent_name(_soul(tmp_path, "# TALOS\n\nYou guard.\n")) == "Talos"


def test_mixed_case_names_survive_untouched(tmp_path: Path) -> None:
    """`title()` wuerde „ExampleAgent" zu „Exampleagent" verstuemmeln — also nur bei Versalien."""
    assert agent_name(_soul(tmp_path, "# ExampleAgent\n")) == "ExampleAgent"
    assert agent_name(_soul(tmp_path, "# McCoy\n")) == "McCoy"


def test_missing_file_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"
    assert agent_name(missing) == DEFAULT_NAME
    assert load_soul(missing) == FALLBACK_PREAMBLE


def test_soul_without_heading_keeps_the_default_name(tmp_path: Path) -> None:
    assert agent_name(_soul(tmp_path, "You guard the island.\n")) == DEFAULT_NAME


def test_empty_soul_falls_back_to_a_working_persona(tmp_path: Path) -> None:
    """Eine leere Datei ist ein Versehen, kein Wunsch nach einem Agenten ohne Wesen."""
    assert load_soul(_soul(tmp_path, "   \n\n")) == FALLBACK_PREAMBLE


def test_long_heading_is_bounded(tmp_path: Path) -> None:
    """Die Kopfzeile ist einzeilig — ein Roman darin wuerde die Anzeige sprengen."""
    assert len(agent_name(_soul(tmp_path, "# " + "Na" * 200 + "\n"))) <= 32


def test_soul_is_capped_because_it_enters_every_prompt(tmp_path: Path) -> None:
    """Eine versehentlich hineinkopierte Logdatei darf nicht still den Kontext auffressen."""
    assert len(load_soul(_soul(tmp_path, "# A\n" + "x" * 50_000))) == MAX_SOUL_CHARS


def test_the_operator_context_names_the_agent_and_sets_a_language_rule() -> None:
    """SOUL darf personalisiert sein; die Sprachregel kann auch in USER.md stehen."""
    text = load_soul()
    # Kein fester Name — der Agent darf umbenannt werden. Zugesichert wird der
    # Vertrag: die ausgelieferte Datei traegt eine Ueberschrift, und genau die ist
    # der Name. Ein `== "Talos"` hier waere dieselbe Falle wie ein fester Pfad.
    heading = text.lstrip().splitlines()[0]
    assert heading.startswith("# ")
    assert agent_name().lower() == heading[2:].strip().lower()
    lowered = load_instruction_context().lower()
    # Geprueft wird die REGEL, nicht ihre Sprache oder ihr exakter Wortlaut. Private
    # Installationen duerfen sie in USER.md konkretisieren, ohne die Suite zu brechen.
    assert (
        "answer in the language" in lowered
        or "verbindliche antwortsprache" in lowered
        or "default to concise german" in lowered
    )
    assert "umlaut" in lowered
