"""Ein Mangel bekommt einen Weg — ein Urteil bekommt keinen.

Diese Datei haelt beide Haelften derselben Regel fest. Die erste ist Komfort: der Agent
soll nicht „ich kann nicht" antworten, wenn eine Zeile Installation die Sache erledigt.
Die zweite ist die Sicherheitshaelfte, und sie ist die wichtigere: dieselbe
Hilfsbereitschaft auf ein Kernel-Nein angewandt waere ein Umgehungsvorschlag. Deshalb
kennt dieses Modul Urteile ueberhaupt nicht — und der letzte Test hier laesst sich nicht
dadurch beruhigen, dass man es gut meint.
"""
from __future__ import annotations

import ast
from pathlib import Path

from talos import remedy
from talos.doctor import FAIL, OK, WARN, Check


def test_a_missing_library_arrives_as_a_step_not_as_a_wall() -> None:
    """Der Doktor kennt die Abhilfe seit jeher — nur hat das Modell sie nie gesehen."""
    text = remedy.block([
        Check("capabilities", "web_search (ddgs)", WARN,
              "missing — `pip install ddgs`, no key needed"),
    ])
    assert "web_search" in text and "pip install ddgs" in text


def test_the_tool_name_survives_because_that_is_what_the_model_recognises() -> None:
    """„ddgs fehlt" sagt dem Modell nichts. „web_search fehlt" trifft den Namen, den es
    selbst aufrufen wollte — der Doktor beschriftet deshalb nach Werkzeug, nicht nach
    Bibliothek, und diese Beschriftung muss den Weg in den Zug ueberleben."""
    (name, _), = remedy.gaps([Check("capabilities", "hear (faster-whisper)", WARN, "missing")])
    assert name.startswith("hear")


def test_a_healthy_machine_says_nothing() -> None:
    """Ein Block, der jeden Zug mit „alles in Ordnung" eroeffnet, ist Moebel."""
    assert remedy.block([Check("capabilities", "web_search (ddgs)", OK, "installed")]) == ""


def test_nothing_at_all_is_also_nothing() -> None:
    assert remedy.block([]) == ""


def test_what_blocks_stands_above_what_is_merely_optional() -> None:
    """Sonst verschwindet der eine Befund, der den Agenten wirklich aufhaelt, zwischen
    vier fehlenden Annehmlichkeiten."""
    text = remedy.block([
        Check("capabilities", "speak (piper)", WARN, "piper missing"),
        Check("model", "provider key", FAIL, "missing — this provider needs a key", critical=True),
    ])
    assert text.index("provider key") < text.index("speak")


def test_the_block_stays_short_enough_to_ride_along_every_turn() -> None:
    """Er steht in JEDEM Zug. Was hier waechst, verdraengt die eigentliche Aufgabe."""
    viele = [Check("capabilities", f"tool_{i}", WARN, "missing — " + "x" * 400) for i in range(12)]
    zeilen = remedy.block(viele).strip().splitlines()
    assert len(zeilen) <= remedy.MAX_GAPS + 1          # Kopfzeile plus hoechstens MAX_GAPS
    assert max(len(zeile) for zeile in zeilen[1:]) < remedy.DETAIL_CHARS + 40


# --- Die Haelfte, die nicht verhandelbar ist -------------------------------------------
def test_this_module_cannot_reach_a_verdict_even_if_someone_wanted_it_to() -> None:
    """⚠️ Der eigentliche Test dieser Datei.

    „Wenn etwas nicht geht, such einen Weg" ist als Haltung richtig und als Regel fuer
    den Kernel toedlich: auf ein DENY angewandt heisst derselbe Satz „schlag eine
    Umgehung vor". Die Trennlinie haelt nicht, weil sie im Docstring steht, sondern
    weil dieses Modul keinen Zugriff auf das Urteil hat — und das prueft man am
    Quelltext, nicht an der Absicht.
    """
    baum = ast.parse(Path(remedy.__file__).read_text(encoding="utf-8"))
    geholt = {
        knoten.module or ""
        for knoten in ast.walk(baum)
        if isinstance(knoten, ast.ImportFrom)
    } | {
        alias.name
        for knoten in ast.walk(baum)
        if isinstance(knoten, ast.Import)
        for alias in knoten.names
    }
    verboten = {name for name in geholt if "policy" in name or "capability" in name}
    assert not verboten, f"remedy darf das Urteil nicht kennen, importiert aber {verboten}"


def test_the_gaps_actually_reach_the_prompt() -> None:
    """⚠️ Die Funktion zu pruefen reicht nicht — die Verdrahtung ist der Ort, an dem es
    zuletzt schiefging.

    Beim DNS-Pinning stimmte das Modul und der Weg dorthin nicht, und der Kommentar
    behauptete beides. Hier wird deshalb der fertige Prompt gelesen, nicht der
    Rueckgabewert von `remedy.block`.
    """
    from talos.conductor import Conductor

    gesehen: list[str] = []

    class Echo:
        def reason(self, prompt: str) -> str:
            gesehen.append(prompt)
            return "ok"

    conductor = Conductor(
        log=None, reasoner=Echo(), executor=None, send=lambda *_: None,
        allowed_principals=frozenset(), trust_of=lambda _: None,
        capability_gaps=lambda: (("web_search (ddgs)", "missing — `pip install ddgs`"),),
    )
    conductor._propose("finde mir etwas")([])
    assert "pip install ddgs" in gesehen[0]


def test_a_broken_doctor_costs_the_hint_and_not_the_run() -> None:
    """Ein Gate faellt nie offen. Das hier ist keines: es ist Komfort, und Komfort, der
    einen Lauf mitreisst, ist schlimmer als kein Komfort."""
    from talos.conductor import Conductor

    def kaputt():
        raise RuntimeError("doctor am Boden")

    conductor = Conductor(
        log=None, reasoner=None, executor=None, send=lambda *_: None,
        allowed_principals=frozenset(), trust_of=lambda _: None,
        capability_gaps=kaputt,
    )
    assert conductor._available() == "" and conductor._gaps() == ()


def test_no_public_name_here_promises_a_permission() -> None:
    """Dieselbe Regel wie in `skills.py`: was nach Erlaubnis klingt, wird irgendwann als
    Erlaubnis gelesen — von einem Menschen im Review oder vom Modell im Prompt."""
    verdaechtig = {"allow", "permit", "grant", "approve", "bypass", "override", "unlock"}
    for name in remedy.__all__:
        assert not any(wort in name.lower() for wort in verdaechtig), name
