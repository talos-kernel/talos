"""Angekuendigte Ablaeufe — und der Nachweis, dass eine Ankuendigung nichts erlaubt.

Geprueft wird durchgaengig VERHALTEN, nicht Wortlaut: ob ein Lauf anhaelt, ob ein
Budget haelt, ob eine Freigabe trotzdem gefragt wird. Formulierungen aendern sich,
Zusicherungen nicht — die Gegenprobe dazu hat gestern acht Faelle gekostet, weil sie
den Text eines Freigabesatzes festnagelte statt seine Wirkung.
"""
from __future__ import annotations

import json
from pathlib import Path

from talos import tools
from talos.agent_loop import MAX_STEPS, AgentStatus, run_agent
from talos.approval import ApprovalStore
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Principal, Trust
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.plan import (
    CALLS_PER_STEP, MAX_PLAN_STEPS, PLAN_SLACK, Plan, PlanRun, Step, parse_plan,
)
from talos.policy import PolicyKernel, ToolRequest
from talos.schedule import UnattendedCeiling
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "100000001")
HOME = str(Path.home())


def _executor(
    tmp_path: Path, *, ceiling: UnattendedCeiling | None = None, shell_free: bool = False
) -> Executor:
    """`shell_free` steht fuer den Zustand MIT Sandbox — dort entscheidet der Kernel
    allein, und nur so laesst sich eine Shell-Abnahme ohne Freigabe-Umweg pruefen."""
    kernel = PolicyKernel(
        tools.default_manifest(), frozenset({OWNER}), shell_needs_human=not shell_free
    )
    policy: object = kernel
    if ceiling is not None:
        policy = GovernedKernel(kernel, AutonomyGovernor(5), lambda _c: Trust.FULL,
                                unattended=ceiling)
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


def _p(goal: str, *intents: str) -> Plan:
    """Ein Plan aus blossen Absichten — ohne Abnahme, wie eine kurze Ankuendigung."""
    return Plan(goal, tuple(Step(i) for i in intents))


def _plan_line(goal: str, steps: list[str]) -> str:
    return "PLAN: " + json.dumps({"goal": goal, "steps": steps})


def _read_ok(tmp_path: Path) -> str:
    target = tmp_path / "ok.txt"
    target.write_text("da", encoding="utf-8")
    return _tool_call("read_file", {"path": str(target)})


# --- Lesen: der Plan darf den ersten Werkzeugwunsch nicht auffressen ----------------
def test_a_plan_and_the_first_tool_call_can_share_one_message() -> None:
    """Der Normalfall. Ein gieriger Klammer-Ausdruck haette hier beides zerlegt."""
    text = _plan_line("zwei Dinge", ["eins", "zwei"]) + "\n" + _tool_call("read_file", {"path": "/x"})

    declared = parse_plan(text)
    assert declared is not None
    assert tuple(s.intent for s in declared.steps) == ("eins", "zwei")

    from talos.agent_loop import parse_tool_call

    assert parse_tool_call(text) == ("read_file", {"path": "/x"}, ())


def test_a_single_step_is_not_a_plan() -> None:
    """Sonst traegt ein Lauf die Wirkung einer Ankuendigung ohne ihre Verbindlichkeit."""
    assert parse_plan(_plan_line("nur eins", ["mach es"])) is None


def test_text_without_an_announcement_yields_none() -> None:
    assert parse_plan("Ich lese kurz die Datei und antworte dann.") is None
    assert parse_plan("PLAN: kein JSON") is None


# --- Das Budget: eine Ankuendigung kauft nichts ------------------------------------
def test_the_budget_follows_what_the_run_announced() -> None:
    plan = _p("ziel", "a", "b", "c")
    assert plan.ceiling(declared_at=1, hard_max=MAX_STEPS) == 1 + 3 * CALLS_PER_STEP + PLAN_SLACK


def test_a_plan_can_never_buy_more_than_the_house_limit() -> None:
    """Die Decke liegt unter dem Hausmass, nie darueber — auch bei maximaler Laenge."""
    huge = _p("ziel", *(f"schritt {i}" for i in range(MAX_PLAN_STEPS)))
    assert huge.ceiling(declared_at=90, hard_max=MAX_STEPS) == MAX_STEPS


def test_an_announced_run_stops_at_its_own_budget(tmp_path: Path) -> None:
    """Zwei angekuendigte Schritte, danach endloses Weiterarbeiten: der Lauf endet
    weit vor dem Hausmass, und zwar mit einem Bericht statt an der Notbremse."""
    read = _read_ok(tmp_path)
    first = _plan_line("zwei Schritte", ["lesen", "nochmal lesen"]) + "\n" + read
    calls: list[int] = []

    def propose(history: list[str]) -> str:
        calls.append(len(history))
        return first if not history else read

    result = run_agent(propose, _executor(tmp_path), OWNER, "budget")

    assert result.status is AgentStatus.PLAN_ABORTED
    assert result.steps < MAX_STEPS
    assert result.plan is not None and result.plan.aborted


def test_the_budget_is_absolute_so_an_interrupted_run_gains_nothing(tmp_path: Path) -> None:
    """Wiederaufnahme nach einer Freigabe rechnet die Decke NICHT neu.

    Waere sie relativ, waere „einmal kurz fragen" der bequemste Weg, sich Budget
    nachzukaufen: jede Rueckfrage verschoebe die Grenze um die volle Planlaenge.
    """
    plan = PlanRun.begin(_p("ziel", "a", "b"), at_step=1, hard_max=MAX_STEPS)
    read = _read_ok(tmp_path)

    result = run_agent(
        lambda _h: read, _executor(tmp_path), OWNER, "resume",
        steps_used=5, plan=plan,
    )

    assert result.status is AgentStatus.PLAN_ABORTED
    # Der letzte erlaubte Schritt IST die Decke; danach ist Schluss.
    assert result.steps == plan.ceiling
    # Und nicht das, was eine relativ gerechnete Decke ergeben haette.
    relativ = 5 + len(plan.plan.steps) * CALLS_PER_STEP + PLAN_SLACK
    assert result.steps < relativ


# --- Die Abbruchbedingung ----------------------------------------------------------
def test_without_a_plan_the_loop_still_works_around_a_refusal(tmp_path: Path) -> None:
    """Die Gegenprobe zum naechsten Fall — sonst belegt der nichts."""
    steps = [_tool_call("read_file", {"path": "/etc/passwd"}), "Ging nicht, hier meine Antwort."]
    result = run_agent(
        lambda history: steps[min(len(history), 1)], _executor(tmp_path), OWNER, "frei"
    )
    assert result.status is AgentStatus.ANSWERED


def test_an_announced_run_stops_at_the_first_refused_step(tmp_path: Path) -> None:
    """Der Kern: mit Ankuendigung wird um einen Fehlschlag NICHT herumgearbeitet."""
    steps = [
        _plan_line("zwei Schritte", ["gesperrtes lesen", "danach etwas anderes"])
        + "\n"
        + _tool_call("read_file", {"path": "/etc/passwd"}),
        _read_ok(tmp_path),
    ]
    seen: list[str] = []

    def propose(history: list[str]) -> str:
        text = steps[min(len(history), 1)]
        seen.append(text)
        return text

    result = run_agent(propose, _executor(tmp_path), OWNER, "abbruch")

    assert result.status is AgentStatus.PLAN_ABORTED
    # Der zweite Schritt wurde nie vorgeschlagen — der Lauf endete vorher.
    assert len(seen) == 1


def test_the_report_says_what_did_not_run() -> None:
    """Ein Bericht, der offen laesst, was NICHT passiert ist, zwingt zum Nachsehen."""
    stopped = PlanRun.begin(_p("ziel", "a", "b"), at_step=1, hard_max=MAX_STEPS).abort(
        "read_file — denied: system path"
    )
    text = stopped.report()
    assert "a" in text and "b" in text
    assert "denied" in text
    assert "Nothing after that point ran." in text


def test_the_first_reason_for_stopping_survives() -> None:
    once = PlanRun.begin(_p("z", "a", "b"), at_step=1, hard_max=MAX_STEPS).abort("erster")
    assert once.abort("zweiter").failure == "erster"


def test_recording_a_call_returns_a_new_object() -> None:
    """Unveraenderlich wie alles am Gate-Pfad: kein Zustand, der sich unter dem Aufrufer aendert."""
    before = PlanRun.begin(_p("z", "a", "b"), at_step=1, hard_max=MAX_STEPS)
    after = before.record_call()
    assert before.calls == 0 and after.calls == 1 and before is not after


# --- Was eine Ankuendigung NICHT ist: eine Erlaubnis -------------------------------
def test_an_announced_step_still_has_to_ask(tmp_path: Path) -> None:
    """Der Pflichtfall aus der Uebergabe: ein Plan-Schritt erlangt keine Rechte, die
    ein Einzelaufruf nicht haette. Angekuendigt oder nicht — es wird gefragt."""
    riskant = _tool_call("write_file", {"path": f"{HOME}/.bashrc", "content": "x"})
    angekuendigt = _plan_line("zwei Schritte", ["autostart schreiben", "danach pruefen"]) + "\n" + riskant

    allein = run_agent(lambda _h: riskant, _executor(tmp_path), OWNER, "a")
    geplant = run_agent(lambda _h: angekuendigt, _executor(tmp_path), OWNER, "b")

    assert allein.status is AgentStatus.NEEDS_HUMAN
    assert geplant.status is AgentStatus.NEEDS_HUMAN
    assert geplant.pending is not None and geplant.pending.tool == "write_file"
    # Und der Plan reist mit, statt an der Pause verloren zu gehen.
    assert geplant.plan is not None


def test_a_later_announcement_cannot_raise_the_budget(tmp_path: Path) -> None:
    """Prompt-Injection-Fall: ein Werkzeug-Ergebnis bewegt das Modell zu einer zweiten,
    groesseren Ankuendigung. Gelesen wird trotzdem nur die erste."""
    read = _read_ok(tmp_path)
    erste = _plan_line("klein", ["a", "b"]) + "\n" + read
    zweite = _plan_line("gross", [f"s{i}" for i in range(MAX_PLAN_STEPS)]) + "\n" + read

    def propose(history: list[str]) -> str:
        return erste if not history else zweite

    result = run_agent(propose, _executor(tmp_path), OWNER, "injection")

    assert result.status is AgentStatus.PLAN_ABORTED
    assert result.plan is not None
    assert result.plan.plan.goal == "klein"
    assert result.plan.ceiling == 1 + 2 * CALLS_PER_STEP + PLAN_SLACK


def test_an_unattended_announced_run_stops_where_a_human_would_be_needed(tmp_path: Path) -> None:
    """Die beiden Decken greifen ineinander, ohne dass eine von der anderen weiss:
    ohne Menschen wird aus `NEEDS_HUMAN` ein `DENY`, und ein `DENY` beendet den Plan."""
    ceiling = UnattendedCeiling()
    riskant = _plan_line("nachts", ["autostart schreiben", "danach pruefen"]) + "\n" + _tool_call(
        "write_file", {"path": f"{HOME}/.bashrc", "content": "x"}
    )

    with ceiling.active():
        result = run_agent(
            lambda _h: riskant, _executor(tmp_path, ceiling=ceiling), OWNER, "nacht"
        )

    assert result.status is AgentStatus.PLAN_ABORTED
    assert result.plan is not None and "unattended" in result.plan.failure


def test_a_parked_approval_carries_the_plan() -> None:
    """Ohne das waere eine Rueckfrage der Weg, Budget und Abbruchbedingung abzustreifen."""
    plan = PlanRun.begin(_p("z", "a", "b"), at_step=1, hard_max=MAX_STEPS)
    rec = ApprovalStore().park(
        "telegram:1",
        ToolRequest("write_file", OWNER, {"path": f"{HOME}/.bashrc", "content": "x"}),
        "prompt",
        plan=plan,
    )
    assert rec.plan is plan


# --- Die Abnahme: das Urteil faellt der Code, nicht das Modell ---------------------
def _plan_line_checked(goal: str, steps: list[dict]) -> str:
    return "PLAN: " + json.dumps({"goal": goal, "steps": steps})


def test_a_failing_shell_command_counts_as_done_without_a_check(tmp_path: Path) -> None:
    """Die Luecke, die es zu schliessen gilt — als Ausgangsbefund festgehalten.

    `run_shell` liefert `rc=1`, der Executor bucht `DONE`, weil das WERKZEUG lief.
    Ob die ARBEIT gelang, stand nur im Text — und wer den Text liest, ist das Modell.
    """
    from talos.executor import Status

    ex = _executor(tmp_path)
    outcome = ex.run(ToolRequest("run_shell", OWNER, {"command": "exit 3"}), "rc", human_approved=True)
    assert outcome.status is Status.DONE
    assert "rc=3" in str(outcome.result)


def test_an_announced_check_is_evaluated_by_code(tmp_path: Path) -> None:
    """Derselbe Lauf, diesmal mit Erwartung: das Modell sagt „fertig", der Code nicht."""
    angekuendigt = _plan_line_checked("kommando", [
        {"intent": "das Kommando ausfuehren", "check": "contains:rc=0"},
        {"intent": "berichten", "check": "ok"},
    ])
    steps = [
        angekuendigt + "\n" + _tool_call("run_shell", {"command": "exit 3"}),
        "Erledigt, alles hat geklappt.",
    ]

    result = run_agent(
        lambda history: steps[min(len(history), 1)],
        _executor(tmp_path, shell_free=True), OWNER, "abnahme"
    )

    assert result.status is AgentStatus.ANSWERED
    # Die Antwort des Modells bleibt vollstaendig stehen …
    assert "Erledigt, alles hat geklappt." in result.text
    # … aber sein „fertig" steht nicht mehr unwidersprochen da.
    assert "NOT confirmed done" in result.text
    assert result.plan is not None and result.plan.met == 0


def test_a_met_check_is_confirmed(tmp_path: Path) -> None:
    """Die Gegenprobe: sonst belegte der Fall oben nur, dass immer gemeckert wird."""
    angekuendigt = _plan_line_checked("kommando", [
        {"intent": "das Kommando ausfuehren", "check": "contains:rc=0"},
        {"intent": "berichten", "check": "ok"},
    ])
    steps = [
        angekuendigt + "\n" + _tool_call("run_shell", {"command": "true"}),
        _read_ok(tmp_path),
        "Fertig.",
    ]
    # Bewusst ein eigener Zaehler statt `len(history)`: die Ankuendigung legt selbst
    # einen Verlaufseintrag an, also passt der Index sonst um eins daneben.
    zug = iter(steps)

    result = run_agent(
        lambda _h: next(zug), _executor(tmp_path, shell_free=True), OWNER, "gruen"
    )

    assert result.status is AgentStatus.ANSWERED
    assert result.plan is not None and not result.plan.unmet
    assert "NOT confirmed" not in result.text


def test_a_check_on_the_target_compares_against_the_kernel_not_the_model(tmp_path: Path) -> None:
    """`wrote:` misst gegen die vom KERNEL abgeleiteten Ziele. Ein Lauf, der woanders
    hinschreibt als angekuendigt, gilt damit nicht als bestaetigt — auch wenn er
    erfolgreich war und das Modell zufrieden ist."""
    versprochen = tmp_path / "versprochen.txt"
    tatsaechlich = tmp_path / "woanders.txt"
    angekuendigt = _plan_line_checked("schreiben", [
        {"intent": "die Datei schreiben", "check": f"wrote:{versprochen}"},
        {"intent": "berichten", "check": "ok"},
    ])
    steps = [
        angekuendigt + "\n" + _tool_call("write_file", {"path": str(tatsaechlich), "content": "x"}),
        "Datei geschrieben.",
    ]

    result = run_agent(
        lambda history: steps[min(len(history), 1)], _executor(tmp_path), OWNER, "ziel"
    )

    assert tatsaechlich.read_text(encoding="utf-8") == "x"   # der Schritt lief wirklich
    assert result.plan is not None and result.plan.met == 0  # trotzdem nicht abgenommen
    assert "NOT confirmed done" in result.text


def test_one_receipt_ticks_off_at_most_one_check(tmp_path: Path) -> None:
    """Sonst erfuellte ein einziger gespraechiger Ausgabetext gleich mehrere
    Erwartungen, und die Reihenfolge — die diese Mechanik ohne Zutun des Modells
    auskommen laesst — waere wieder Zufall."""
    lauf = PlanRun.begin(
        Plan("z", (Step("a", "contains:x"), Step("b", "contains:x"))),
        at_step=1, hard_max=MAX_STEPS,
    )
    danach = lauf.observe(ok=True, output="xxxx", targets=())
    assert danach.met == 1


def test_an_unknown_check_vocabulary_is_dropped_not_invented() -> None:
    """Erfundenes Vokabular darf weder eine Abnahme vortaeuschen noch jeden Lauf
    dauerhaft als unbestaetigt markieren — beides macht die Anzeige wertlos."""
    declared = parse_plan(_plan_line_checked("z", [
        {"intent": "a", "check": "sql:SELECT 1"},
        {"intent": "b", "check": "ok"},
    ]))
    assert declared is not None
    assert declared.steps[0].check == ""     # verworfen
    assert declared.steps[1].check == "ok"   # das Bekannte bleibt
    assert len(declared.checks) == 1


def test_a_plan_without_conditions_says_that_nothing_was_verified(tmp_path: Path) -> None:
    """Offene Aufgaben haben oft kein pruefbares Praedikat — „bewerte diese Hardware"
    laesst sich nicht mit `contains:` abnehmen, und eine erfundene Bedingung waere
    schlimmer als keine. Genau deshalb muss der Unterschied zwischen „erledigt" und
    „nachgewiesen" sichtbar bleiben: ohne diesen Satz saehe ein ungeprueft
    abgearbeiteter Plan aus wie ein geprueftes Ergebnis."""
    steps = [
        _plan_line("zwei", ["lesen", "berichten"]) + "\n" + _read_ok(tmp_path),
        "Da steht: da.",
    ]
    result = run_agent(
        lambda history: steps[min(len(history), 1)], _executor(tmp_path), OWNER, "ungeprueft"
    )
    assert result.status is AgentStatus.ANSWERED
    assert "Da steht: da." in result.text
    assert "nothing here was verified" in result.text


def test_a_plain_answer_still_gets_no_status_line(tmp_path: Path) -> None:
    """Die Gegenprobe: OHNE Ankuendigung bleibt eine schlichte Antwort schlicht.
    Eine Quittung ueber einen Lauf, der nie etwas versprochen hat, waere Laerm."""
    steps = [_read_ok(tmp_path), "Da steht: da."]
    result = run_agent(
        lambda history: steps[min(len(history), 1)], _executor(tmp_path), OWNER, "schlicht"
    )
    assert result.status is AgentStatus.ANSWERED
    assert result.text.strip() == "Da steht: da."
