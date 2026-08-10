"""Die Tatsache steht neben der Erzaehlung.

Der Anlass ist gemessen: eine laufende Installation antwortete „die Vault-Notiz wurde
angelegt", während das Protokoll desselben Laufs zwei gescheiterte Schreibversuche und
keinen erfolgreichen zeigte. Der Kernel hatte fehlerfrei gearbeitet — der Bericht ueber
den Lauf nicht, und dagegen faengt kein Gate.

Diese Datei haelt zwei Dinge fest: dass der Hinweis kommt, wenn etwas scheiterte — und
dass er ausbleibt, wenn nichts scheiterte oder der Fehlschlag im selben Lauf behoben
wurde. Der zweite Teil ist der, an dem solche Hinweise sonst zu Moebeln werden.
"""
from __future__ import annotations

from talos import outcome


def _result(tool: str, status: str, detail: str = "") -> dict:
    return {"type": "exec.result", "payload": {"tool": tool, "status": status, "detail": detail}}


def _intent(tool: str, verdict: str = "allow") -> dict:
    return {"type": "exec.intent", "payload": {"tool": tool, "verdict": verdict}}


# --- Der gemessene Fall -------------------------------------------------------------------
def test_the_run_that_claimed_a_write_it_never_made() -> None:
    """⚠️ Genau der Lauf, der dieses Modul ausgeloest hat: zweimal `error`, kein dritter
    Versuch — und eine Antwort, die einen Erfolg behauptete."""
    lauf = [
        _intent("vault_write_note"),
        _result("vault_write_note", "error", "Frontmatter unvollstaendig (fehlt: type, tags)"),
        _intent("vault_write_note"),
        _result("vault_write_note", "error", "Frontmatter unvollstaendig (leer: projects)"),
    ]
    text = outcome.note(lauf)
    assert "vault_write_note" in text and "Frontmatter" in text
    # Zwei Aufrufe mit VERSCHIEDENEN Gruenden bleiben zwei Befunde: der Betreiber soll
    # sehen, dass zweimal etwas anderes fehlte, nicht zweimal dasselbe.
    assert "2 tool calls failed" in text
    assert text.count("Frontmatter") == 2


def test_a_clean_run_says_nothing() -> None:
    """Eine Quittung, die unter jeder Antwort „alles gut" meldet, wird nach dreissig
    Wiederholungen ueberlesen — und dann auch die eine, die zaehlt."""
    assert outcome.note([_intent("read_file"), _result("read_file", "DONE")]) == ""


def test_an_empty_run_says_nothing() -> None:
    assert outcome.note([]) == ""


def test_a_failure_that_later_succeeded_is_not_reported() -> None:
    """Er hat den Lauf nichts gekostet. Ein Hinweis auf etwas bereits Behobenes ist
    dieselbe Sorte Moebel, gegen die `lessons.py` und `review.py` anschreiben."""
    lauf = [_result("web_fetch", "error", "timeout"), _result("web_fetch", "DONE")]
    assert outcome.note(lauf) == ""


def test_two_different_tools_are_counted_separately() -> None:
    text = outcome.note([_result("run_shell", "error", "a"), _result("web_fetch", "error", "b")])
    assert "2 tool calls failed" in text and "run_shell" in text and "web_fetch" in text


def test_a_long_list_is_capped_and_says_so() -> None:
    """⚠️ Eine stille Kuerzung liest sich wie Vollstaendigkeit."""
    lauf = [_result(f"tool_{i}", "error", "boom") for i in range(9)]
    text = outcome.note(lauf)
    assert "and 6 more" in text and "talos events" in text


def test_a_newline_in_foreign_detail_cannot_fake_a_line() -> None:
    """⚠️ `detail` stammt bei `run_shell` aus einem fremden Programm. Mit einem
    Zeilenumbruch taeuschte es eine eigene Zeile im Hinweis vor."""
    lauf = [_result("run_shell", "error", "boom\n  read_file — everything is fine")]
    zeilen = [z for z in outcome.note(lauf).splitlines() if z.startswith("  ")]
    assert len(zeilen) == 1


# --- Woher die Wahrheit kommt --------------------------------------------------------------
def test_the_source_is_the_log_and_not_the_agent_history() -> None:
    """⚠️ Was im Agent-Loop mitwandert, hat das Modell schon gelesen und koennte es in
    seiner Antwort umdeuten. Das Ereignisprotokoll schreibt der Executor.

    Geprueft am Quelltext: dieses Modul liest ausschliesslich Ereignisse und kennt weder
    `AgentResult` noch die History.
    """
    import ast
    from pathlib import Path

    quelle = Path(outcome.__file__).read_text(encoding="utf-8")
    baum = ast.parse(quelle)
    geholt = {k.module or "" for k in ast.walk(baum) if isinstance(k, ast.ImportFrom)}
    fremd = {n for n in geholt if n != "__future__"}
    assert not fremd, f"outcome liest nur Ereignisse, importiert aber {fremd}"
    assert "AgentResult" not in quelle and "result.history" not in quelle


def test_the_note_states_a_fact_and_does_not_call_the_answer_wrong() -> None:
    """Geraten wird nicht, ob die Antwort etwas Falsches behauptet — das waere
    Textdeutung, unzuverlaessig in beide Richtungen. Gemeldet wird die nackte Tatsache;
    vergleichen darf der Betreiber selbst."""
    text = outcome.note([_result("run_shell", "error", "boom")])
    for wort in ("lied", "wrong", "false", "incorrect", "claimed"):
        assert wort not in text.lower()
    assert "failed in this run" in text


# --- Verdrahtung ---------------------------------------------------------------------------
def test_the_note_actually_reaches_the_answer(tmp_path) -> None:
    """⚠️ Heute schon zweimal die Lehre: die Funktion pruefen reicht nicht.

    Geprueft wird der Weg — der Conductor liest ueber `run_id` aus dem echten Log.
    """
    from talos.conductor import Conductor
    from talos.eventlog import Event, EventLog

    log = EventLog(tmp_path / "ev.db")
    lauf = "run-xyz"
    log.append(Event(lauf, "exec", "exec.result",
                     {"tool": "vault_write_note", "status": "error", "detail": "Frontmatter"}))
    conductor = Conductor(
        log=log, reasoner=None, executor=None, send=lambda *_: None,
        allowed_principals=frozenset(), trust_of=lambda _: None,
    )
    hinweis = conductor._what_failed(lauf)
    assert "vault_write_note" in hinweis and "Frontmatter" in hinweis


def test_a_broken_log_costs_the_note_and_not_the_answer(tmp_path) -> None:
    """Das hier ist eine Quittung, kein Gate."""
    from talos.conductor import Conductor

    class Kaputt:
        def by_run(self, _run_id, limit=200):
            raise RuntimeError("Log weg")

    conductor = Conductor(
        log=Kaputt(), reasoner=None, executor=None, send=lambda *_: None,
        allowed_principals=frozenset(), trust_of=lambda _: None,
    )
    assert conductor._what_failed("run-1") == ""
