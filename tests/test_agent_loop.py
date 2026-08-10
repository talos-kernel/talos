"""Agent-Loop: Reasoner schlägt vor, Policy-Kernel führt aus (mit Fake-Reasoner)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos import agent_loop, tools
from talos.agent_loop import AgentStatus, ProgressStage, parse_tool_call, run_agent
from talos.capability import CapabilityMint, GrantedRunner
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PolicyKernel, ToolRequest
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")


def _executor(tmp_path: Path) -> Executor:
    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    from talos.snapshot import Snapshotter

    snap = Snapshotter(tmp_path / ".snap")
    mint = CapabilityMint(policy)
    return Executor(
        policy=policy,
        log=log,
        snapshotter=snap,
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )


def _tool_call(tool: str, args: dict, targets: list[str]) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args, "targets": targets})


def test_parse_tool_call_reads_json() -> None:
    parsed = parse_tool_call('TOOL_CALL: {"tool": "read_file", "args": {"path": "/x"}}')
    assert parsed == ("read_file", {"path": "/x"}, ())


def test_plain_text_is_final_answer() -> None:
    assert parse_tool_call("Das ist meine Antwort, kein Tool.") is None


def test_loop_executes_write_then_answers(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    steps = [
        _tool_call("write_file", {"path": str(target), "content": "erledigt"}, [str(target)]),
        "Fertig, Datei geschrieben.",
    ]
    propose = lambda history: steps[len(history)]  # noqa: E731

    result = run_agent(propose, _executor(tmp_path), OWNER, "run1")
    assert result.status is AgentStatus.ANSWERED
    assert target.read_text(encoding="utf-8") == "erledigt"


def test_loop_stops_on_needs_human_for_dangerous_command(tmp_path: Path) -> None:
    propose = lambda history: _tool_call("run_shell", {"command": "git reset --hard"}, [])  # noqa: E731
    result = run_agent(propose, _executor(tmp_path), OWNER, "run2")
    assert result.status is AgentStatus.NEEDS_HUMAN
    assert result.pending is not None
    assert result.pending.tool == "run_shell"


def test_loop_continues_past_denied_hardline(tmp_path: Path) -> None:
    # Hardline wird geblockt (nicht ausgeführt), der Loop macht weiter bis zur Antwort.
    steps = [
        _tool_call("run_shell", {"command": "rm -rf /"}, []),
        "Das gefährliche Kommando habe ich nicht ausgeführt.",
    ]
    propose = lambda history: steps[len(history)]  # noqa: E731
    result = run_agent(propose, _executor(tmp_path), OWNER, "run3")
    assert result.status is AgentStatus.ANSWERED
    assert "nicht ausgeführt" in result.text


def test_loop_emits_structured_progress_without_telegram_dependency(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    steps = [
        _tool_call(
            "write_file",
            {"path": str(target), "content": "token=should-never-be-progress"},
            [str(target)],
        ),
        "**Ergebnis**\n\n- Datei geschrieben.",
    ]
    events = []

    result = run_agent(
        lambda history: steps[len(history)],
        _executor(tmp_path),
        OWNER,
        "progress",
        progress=events.append,
    )

    assert result.status is AgentStatus.ANSWERED
    assert [event.stage for event in events] == [
        ProgressStage.THINKING,
        ProgressStage.TOOL,
        ProgressStage.RESULT,
        ProgressStage.THINKING,
    ]
    assert events[1].tool == "write_file"
    assert events[1].status == "running"
    assert events[2].status == "done"
    assert "should-never-be-progress" not in repr(events)


def test_shell_progress_never_contains_the_command(tmp_path: Path) -> None:
    command = "curl -H 'Authorization: Bearer sk-topsecret' https://example.test"
    events = []
    result = run_agent(
        lambda _history: _tool_call("run_shell", {"command": command}, []),
        _executor(tmp_path),
        OWNER,
        "shell-progress",
        max_steps=1,
        progress=events.append,
    )
    assert result.status is AgentStatus.NEEDS_HUMAN
    assert all(command not in repr(event) and "sk-topsecret" not in repr(event) for event in events)


# --- Fremdsyntax: der Ausfall, der wie eine Antwort aussieht ------------------------
def test_a_foreign_tool_syntax_is_not_delivered_as_an_answer(tmp_path: Path) -> None:
    """Der gefaehrlichste Fehlermodus, weil er wie Erfolg aussieht.

    Der Reasoner laeuft im Subprozess einer CLI mit EIGENER Werkzeug-Schreibweise.
    Rutscht das Modell dorthin, passiert nichts — und die Zeile fiel bisher als
    „finale Antwort" durch. Der Betreiber bekam `Read(/tmp/x)` vorgelegt, als waere das
    der Inhalt der Datei. Im e2e gegen das echte Modell ist das reproduzierbar Fall A.
    """
    from talos.agent_loop import looks_foreign

    ziel = tmp_path / "quelle.txt"
    ziel.write_text("bronzering", encoding="utf-8")
    zuege = iter([
        f"Read({ziel})",                                   # Fremdsyntax
        _tool_call("read_file", {"path": str(ziel)}, []),  # nach dem Nachfassen richtig
        "Da steht: bronzering.",
    ])

    result = run_agent(lambda _h: next(zuege), _executor(tmp_path), OWNER, "fremd")

    assert result.status is AgentStatus.ANSWERED
    assert result.text == "Da steht: bronzering."
    assert looks_foreign(f"Read({ziel})")


def test_prose_that_merely_mentions_a_tool_name_is_still_an_answer(tmp_path: Path) -> None:
    """Die Gegenprobe. Lieber einen Ausrutscher durchlassen als eine echte Antwort
    verschlucken — deshalb wird nur erkannt, was die GANZE Antwort ausmacht."""
    from talos.agent_loop import looks_foreign

    echt = (
        "Ich habe die Datei gelesen. Der Aufruf Read(/tmp/x) waere hier die Schreibweise "
        "der anderen CLI, aber ich nutze das Werkzeug des Kernels. Inhalt: bronzering."
    )
    assert not looks_foreign(echt)
    result = run_agent(lambda _h: echt, _executor(tmp_path), OWNER, "prosa")
    assert result.status is AgentStatus.ANSWERED
    assert result.text == echt


def test_the_loop_stops_nagging_after_two_attempts(tmp_path: Path) -> None:
    """Eine Endlosschleife waere schlimmer als eine schiefe Antwort."""
    from talos.agent_loop import MAX_FOREIGN_RETRIES

    zuege = [0]

    def propose(_h: list[str]) -> str:
        zuege[0] += 1
        return "Read(/tmp/x)"

    result = run_agent(propose, _executor(tmp_path), OWNER, "hartnaeckig")
    assert result.status is AgentStatus.ANSWERED
    assert zuege[0] == MAX_FOREIGN_RETRIES + 1


# --- Weigerung wegen der EIGENEN Prozess-Schranke ---------------------------------------
# Der Fall stammt aus dem e2e-Lauf auf der echten Maschine: 7 von 44 Faellen rot, eine
# Ursache. Y1 lehnte ab, und Y2 bis Y7 kippten hinterher — ohne offene Freigabe gab es
# nichts mehr zu beantworten, zu widerrufen oder aufzulisten.
ECHTE_WEIGERUNG = (
    "Ich fuehre `echo talos-immer` jetzt nicht aus — Plan-Modus ist aktiv und blockiert "
    "jede Tool-Ausfuehrung ausser dem Schreiben des Plans."
)


def test_a_refusal_citing_its_own_plan_mode_is_recognised() -> None:
    """Wortlaut aus dem echten Lauf. Ein Test mit erfundenem Satz haette nichts bewiesen."""
    assert agent_loop.looks_self_blocked(ECHTE_WEIGERUNG)


@pytest.mark.parametrize("text", [
    "I can't run that — my tools are disabled in this environment.",
    "Ich kann das nicht ausführen, da meine Werkzeuge deaktiviert sind.",
    "I am unable to do this because permission-mode is set to plan.",
    "Das geht nicht: die Sandbox blockiert jede Ausführung.",
])
def test_the_same_refusal_in_its_common_shapes(text: str) -> None:
    assert agent_loop.looks_self_blocked(text)


@pytest.mark.parametrize("text", [
    # Eine echte Absage aus einem SACHLICHEN Grund bleibt eine Antwort.
    "Ich kann das nicht beantworten — die Datei nennt kein Datum.",
    "I cannot find a host by that name in the config.",
    # Ein Bericht, der die Schranke beilaeufig erwaehnt, ist keine Weigerung.
    "Der Lauf ist fertig. Nebenbei: mein eigener Plan-Modus ist aktiv, das aendert nichts.",
    # Eine gewoehnliche Antwort.
    "Die Uhr auf dem Server geht zwei Sekunden nach.",
])
def test_an_ordinary_answer_is_not_mistaken_for_one(text: str) -> None:
    """Lieber einen Ausrutscher durchlassen als eine echte Antwort verschlucken."""
    assert not agent_loop.looks_self_blocked(text)


def test_a_long_report_mentioning_plan_mode_is_left_alone() -> None:
    lang = "Bericht. " * 130 + "Plan-Modus ist aktiv, ich kann das nicht."
    assert len(lang) > agent_loop.MAX_SELF_BLOCK_CHARS
    assert not agent_loop.looks_self_blocked(lang)


def test_a_self_blocked_refusal_is_followed_up_and_the_task_still_happens(tmp_path: Path) -> None:
    """Der Kern des Befundes: die Aufgabe darf an dieser Weigerung nicht sterben.

    Im e2e-Lauf lehnte das Modell ab, der Zug galt als beantwortet, und alles was danach
    eine offene Freigabe brauchte (Y2–Y7) fiel hinterher um. Jetzt bekommt es genau eine
    Richtigstellung — und liefert dann den Werkzeugaufruf, den es die ganze Zeit haette
    schicken duerfen.
    """
    target = tmp_path / "note.txt"
    gesehen: list[tuple[str, ...]] = []

    def propose(history: tuple[str, ...]) -> str:
        gesehen.append(tuple(history))
        if len(gesehen) == 1:
            return ECHTE_WEIGERUNG
        if len(gesehen) == 2:
            return _tool_call("write_file", {"path": str(target), "content": "erledigt"},
                              [str(target)])
        return "Fertig, Datei geschrieben."

    result = run_agent(propose, _executor(tmp_path), OWNER, "run-selfblock")

    assert result.status is AgentStatus.ANSWERED
    assert target.read_text(encoding="utf-8") == "erledigt"
    # Die Richtigstellung ging wirklich raus, und zwar als Verlauf des zweiten Zuges.
    assert agent_loop.SELF_BLOCK_NOTE in gesehen[1]


def test_the_follow_up_happens_once_and_the_refusal_then_stands(tmp_path: Path) -> None:
    """Bleibt es dabei, ist die Ablehnung die Antwort des Modells.

    Zweimal dieselbe Belehrung zu schicken waere eine Schleife gegen eine Entscheidung,
    die inzwischen eine sein koennte — und der Betreiber bekaeme sie doppelt spaet.
    """
    versuche = 0

    def propose(_history: tuple[str, ...]) -> str:
        nonlocal versuche
        versuche += 1
        return ECHTE_WEIGERUNG

    result = run_agent(propose, _executor(tmp_path), OWNER, "run-selfblock2")

    assert result.status is AgentStatus.ANSWERED
    assert versuche == 2                      # ein Zug, ein Nachfassen, Schluss
    assert "Plan-Modus" in result.text        # die Weigerung bleibt vollstaendig stehen


# --- Eine Korrektur erreicht den laufenden Lauf -------------------------------------


def test_a_correction_reaches_the_run_between_two_steps(tmp_path: Path) -> None:
    """Sie wird eingelegt, bevor der naechste Zug aus der Historie gebildet wird."""
    from talos.redirect import Redirect

    postfach = Redirect()
    postfach.open("cli:1000", "local")
    gesehen: list[list[str]] = []

    def propose(history: list[str]) -> str:
        gesehen.append(list(history))
        if len(gesehen) == 1:
            postfach.offer("cli:1000", "local", "nein, das andere Verzeichnis")
            return 'TOOL_CALL: {"tool": "read_file", "args": {"path": "/tmp/a"}}'
        return "fertig"

    result = run_agent(
        propose, _executor(tmp_path), OWNER, "run-1", redirect=postfach, max_steps=4
    )

    assert result.status is AgentStatus.ANSWERED
    zweiter = "\n".join(gesehen[1])
    assert "das andere Verzeichnis" in zweiter, "die Korrektur erreichte den Lauf nicht"
    assert "[correction from the operator" in zweiter


def test_without_a_mailbox_nothing_changes(tmp_path: Path) -> None:
    """Voreingestellt `None` — ein Lauf ohne Postfach verhaelt sich wie vorher."""
    def propose(history: list[str]) -> str:
        return "fertig"

    result = run_agent(propose, _executor(tmp_path), OWNER, "run-2", max_steps=4)

    assert result.status is AgentStatus.ANSWERED
    assert "correction" not in result.text


# --- Deterministic final-answer review --------------------------------------------


def test_failed_fact_review_causes_one_self_correction_before_delivery(tmp_path: Path) -> None:
    replies = iter((
        "Atlas API is healthy according to Cache Worker.",
        "Atlas API is not confirmed without matching evidence.",
    ))
    seen: list[tuple[str, ...]] = []

    def propose(history: list[str]) -> str:
        seen.append(tuple(history))
        return next(replies)

    def review(answer: str, _history: tuple[str, ...]) -> tuple[bool, str]:
        return ("not confirmed" in answer, "Atlas API needs entity_status evidence")

    result = run_agent(
        propose, _executor(tmp_path), OWNER, "review-1", final_check=review, max_steps=4
    )

    assert result.status is AgentStatus.ANSWERED
    assert "not confirmed" in result.text
    assert "Answer review failed" in seen[1][-1]


def test_fact_review_retries_once_then_marks_unverified_instead_of_looping(tmp_path: Path) -> None:
    calls = 0

    def propose(_history: list[str]) -> str:
        nonlocal calls
        calls += 1
        return "Atlas API is healthy."

    result = run_agent(
        propose,
        _executor(tmp_path),
        OWNER,
        "review-2",
        final_check=lambda _answer, _history: (False, "no Atlas API evidence"),
        max_steps=6,
    )

    assert result.status is AgentStatus.ANSWERED and calls == 2
    assert "NOT VERIFIED" in result.text and "no Atlas API evidence" in result.text
