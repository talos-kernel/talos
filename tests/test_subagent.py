"""Delegieren — und der Nachweis, dass ein Untergebener WENIGER darf als sein Auftraggeber.

Wie ueberall hier auf Verhalten geprueft, nicht auf Wortlaut: was ein Lauf unter der
Decke noch erreicht, und was nicht mehr.
"""
from __future__ import annotations

import json
from pathlib import Path

from talos import tools
from talos.agent_loop import AgentStatus, run_agent
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.capability import CapabilityError, CapabilityMint, GrantedRunner
from talos.channel import Principal, Trust
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.schedule import UnattendedCeiling
from talos.snapshot import Snapshotter
from talos.subagent import DELEGATE_MAX_STEPS, ReadOnlyCeiling, bound_answer

OWNER = Principal("telegram", "100000001")
HOME = str(Path.home())


def _kernel(ceiling: ReadOnlyCeiling, **kw) -> GovernedKernel:
    return GovernedKernel(
        PolicyKernel(tools.default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5),
        lambda _c: Trust.FULL,
        delegated=ceiling,
        **kw,
    )


def _executor(tmp_path: Path, policy: object) -> Executor:
    mint = CapabilityMint(policy)
    return Executor(
        policy=policy,
        log=EventLog(tmp_path / "ev.db"),
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )


def _tool_call(tool: str, args: dict) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args})


# --- Die Decke: der eigentliche Punkt ----------------------------------------------
def test_a_delegated_run_may_still_read() -> None:
    """Sonst waere die Decke kein Schutz, sondern ein Ausschalter."""
    ceiling = ReadOnlyCeiling()
    kernel = _kernel(ceiling)
    lesen = ToolRequest("read_file", OWNER, {"path": f"{HOME}/nichts.txt"})

    with ceiling.active():
        assert kernel.decide(lesen).verdict is Verdict.ALLOW


def test_a_delegated_run_may_not_write_what_its_caller_could() -> None:
    """Der Kern: derselbe Aufruf, einmal getippt, einmal delegiert."""
    ceiling = ReadOnlyCeiling()
    kernel = _kernel(ceiling)
    schreiben = ToolRequest("write_file", OWNER, {"path": "/tmp/talos-delegate.txt", "content": "x"})

    assert kernel.decide(schreiben).verdict is Verdict.ALLOW  # der Auftraggeber darf
    with ceiling.active():
        entschieden = kernel.decide(schreiben)
    assert entschieden.verdict is Verdict.DENY               # der Untergebene nicht
    assert "delegated" in entschieden.reason


def test_a_delegated_run_cannot_reach_anything_that_needs_a_human() -> None:
    """Nicht weil niemand da waere — sondern weil die Frage ihn ohne ihren
    Zusammenhang erreichte, mitten im Lauf eines vom Modell formulierten Auftrags."""
    ceiling = ReadOnlyCeiling()
    kernel = _kernel(ceiling)
    riskant = ToolRequest("write_file", OWNER, {"path": f"{HOME}/.bashrc", "content": "x"})

    assert kernel.decide(riskant).verdict is Verdict.NEEDS_HUMAN
    with ceiling.active():
        assert kernel.decide(riskant).verdict is Verdict.DENY


def test_the_ceiling_never_widens_anything() -> None:
    """Sie geht durch `stricter` und kann per Konstruktion nichts Milderes liefern."""
    ceiling = ReadOnlyCeiling()
    kernel = _kernel(ceiling)
    katastrophal = ToolRequest("run_shell", OWNER, {"command": "rm -rf /"})

    with ceiling.active():
        assert kernel.decide(katastrophal).verdict is Verdict.DENY


def test_the_ceiling_ends_with_the_delegated_run() -> None:
    """Sonst waere der Hauptlauf nach einer Delegation dauerhaft entmachtet."""
    ceiling = ReadOnlyCeiling()
    kernel = _kernel(ceiling)
    schreiben = ToolRequest("write_file", OWNER, {"path": "/tmp/talos-delegate.txt", "content": "x"})

    with ceiling.active():
        pass
    assert kernel.decide(schreiben).verdict is Verdict.ALLOW


def test_both_ceilings_compose_without_knowing_about_each_other() -> None:
    """Unbeaufsichtigt UND delegiert: das Ergebnis ist das strengere, nie ein Kompromiss."""
    unbeaufsichtigt, delegiert = UnattendedCeiling(), ReadOnlyCeiling()
    kernel = _kernel(delegiert, unattended=unbeaufsichtigt)
    schreiben = ToolRequest("write_file", OWNER, {"path": "/tmp/talos-delegate.txt", "content": "x"})

    with unbeaufsichtigt.active(), delegiert.active():
        assert kernel.decide(schreiben).verdict is Verdict.DENY


# --- Der Laeufer -------------------------------------------------------------------
def test_the_delegated_run_answers_from_a_real_read(tmp_path: Path) -> None:
    ziel = tmp_path / "quelle.txt"
    ziel.write_text("bronzering", encoding="utf-8")
    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))
    zuege = iter([_tool_call("read_file", {"path": str(ziel)}), "Da steht: bronzering."])

    runner = tools.make_delegate_runner(
        executor=lambda: executor,
        ceiling=ceiling,
        propose=lambda _frage: (lambda _h: next(zuege)),
        run_id=lambda: "sub1",
    )
    antwort = runner(ToolRequest("delegate", OWNER, {"question": "Was steht in der Datei?"}))
    assert "bronzering" in antwort


def test_the_delegated_run_cannot_write_even_though_the_caller_could(tmp_path: Path) -> None:
    """Der Beweis am laufenden Objekt, nicht nur am Urteil: die Datei entsteht NICHT."""
    ziel = tmp_path / "verboten.txt"
    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))

    # Ohne Decke laeuft derselbe Schritt durch — sonst belegte der Fall nichts.
    frei = run_agent(
        lambda _h: _tool_call("write_file", {"path": str(ziel), "content": "x"}),
        executor, OWNER, "frei", max_steps=2,
    )
    assert ziel.exists() and frei.status is not None
    ziel.unlink()

    runner = tools.make_delegate_runner(
        executor=lambda: executor,
        ceiling=ceiling,
        propose=lambda _frage: (lambda _h: _tool_call(
            "write_file", {"path": str(ziel), "content": "x"}
        )),
        run_id=lambda: "sub2",
    )
    runner(ToolRequest("delegate", OWNER, {"question": "Schreib die Datei."}))
    assert not ziel.exists()


def test_a_delegated_run_gets_a_small_budget_of_its_own(tmp_path: Path) -> None:
    """Es laeuft synchron im Werkzeugaufruf des Hauptlaufs — ein langer Nebenlauf
    blockiert ihn. Deshalb eine eigene, kleine Decke statt des Hausmasses."""
    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))
    quelle = tmp_path / "q.txt"
    quelle.write_text("da", encoding="utf-8")
    zuege = [0]

    def propose(_frage):
        def inner(_h):
            zuege[0] += 1
            return _tool_call("read_file", {"path": str(quelle)})
        return inner

    runner = tools.make_delegate_runner(
        executor=lambda: executor, ceiling=ceiling, propose=propose, run_id=lambda: "sub3",
    )
    runner(ToolRequest("delegate", OWNER, {"question": "lies endlos"}))
    assert zuege[0] == DELEGATE_MAX_STEPS


def test_a_question_is_required() -> None:
    """Ein leerer Auftrag ist ein Werkzeugfehler und geht als solcher zurueck."""
    runner = tools.make_delegate_runner(
        executor=lambda: None, ceiling=ReadOnlyCeiling(),
        propose=lambda _f: (lambda _h: ""), run_id=lambda: "sub4",
    )
    try:
        runner(ToolRequest("delegate", OWNER, {"question": "   "}))
    except ValueError:
        return
    raise AssertionError("eine leere Frage muss ein Fehler sein")


def test_the_answer_comes_back_bounded() -> None:
    """Fremder Text betritt den Hauptlauf begrenzt — wie jedes Werkzeug-Ergebnis."""
    lang = bound_answer("x" * 20_000)
    assert len(lang) < 20_000
    assert lang.endswith("truncated]")


# --- Nebeneinander: genau dafuer ist die Decke thread-gebunden ----------------------
def test_several_subordinates_run_side_by_side(tmp_path: Path) -> None:
    """Die zweite Haelfte der Luecke. Der Beweis ist die Gleichzeitigkeit, nicht die Zahl:
    ohne echte Parallelitaet koennten die drei nie zusammen im Rendezvous stehen."""
    import threading

    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))
    rendezvous = threading.Barrier(3, timeout=5.0)

    def propose(frage: str):
        def inner(_h: list[str]) -> str:
            rendezvous.wait()      # scheitert, wenn die drei nacheinander liefen
            return f"Antwort auf {frage}"
        return inner

    runner = tools.make_delegate_runner(
        executor=lambda: executor, ceiling=ceiling, propose=propose, run_id=lambda: "par",
    )
    antwort = runner(ToolRequest("delegate", OWNER, {"questions": ["a", "b", "c"]}))
    for frage in ("a", "b", "c"):
        assert f"Antwort auf {frage}" in antwort


def test_a_failing_subordinate_does_not_take_the_others_down(tmp_path: Path) -> None:
    """Eine Luecke muss sichtbar sein, statt still zu fehlen."""
    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))

    def propose(frage: str):
        def inner(_h: list[str]) -> str:
            if frage == "kaputt":
                raise RuntimeError("Reasoner weg")
            return f"Antwort auf {frage}"
        return inner

    runner = tools.make_delegate_runner(
        executor=lambda: executor, ceiling=ceiling, propose=propose, run_id=lambda: "mix",
    )
    antwort = runner(ToolRequest("delegate", OWNER, {"questions": ["gut", "kaputt"]}))
    assert "Antwort auf gut" in antwort
    assert "failed" in antwort


def test_the_number_of_subordinates_is_bounded(tmp_path: Path) -> None:
    """Jeder Untergebene ist ein eigener Modellaufruf — unbegrenzt waere eine Nachricht,
    die beliebig viel Verbrauch erzeugt."""
    from talos.subagent import MAX_PARALLEL

    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))
    gestartet: list[str] = []

    def propose(frage: str):
        def inner(_h: list[str]) -> str:
            gestartet.append(frage)
            return "fertig"
        return inner

    runner = tools.make_delegate_runner(
        executor=lambda: executor, ceiling=ceiling, propose=propose, run_id=lambda: "viele",
    )
    runner(ToolRequest("delegate", OWNER, {"questions": [f"f{i}" for i in range(20)]}))
    assert len(gestartet) == MAX_PARALLEL


def test_each_parallel_subordinate_carries_its_own_ceiling(tmp_path: Path) -> None:
    """Der Sicherheitsteil der Parallelitaet: die Decke ist thread-gebunden, also darf
    auch ein NEBENLAEUFIGER Untergebener nicht schreiben — und der Hauptlauf schon."""
    ziel = tmp_path / "parallel-verboten.txt"
    ceiling = ReadOnlyCeiling()
    executor = _executor(tmp_path, _kernel(ceiling))

    def propose(_frage: str):
        return lambda _h: _tool_call("write_file", {"path": str(ziel), "content": "x"})

    runner = tools.make_delegate_runner(
        executor=lambda: executor, ceiling=ceiling, propose=propose, run_id=lambda: "par2",
    )
    runner(ToolRequest("delegate", OWNER, {"questions": ["a", "b", "c"]}))
    assert not ziel.exists()

    # Gegenprobe: derselbe Schritt im Hauptlauf laeuft durch.
    run_agent(
        lambda _h: _tool_call("write_file", {"path": str(ziel), "content": "x"}),
        executor, OWNER, "haupt", max_steps=2,
    )
    assert ziel.exists()
