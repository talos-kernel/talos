from __future__ import annotations

import json
from pathlib import Path

from talos.evaluation import EvaluationCase, evaluate_case, evaluate_suite, load_cases


def test_evaluation_scores_grounding_tool_choice_and_answer_contract() -> None:
    case = EvaluationCase(
        name="api-not-worker",
        prompt="Check Atlas API, not Cache Worker",
        required_tools=("entity_status",),
        forbidden_tools=("run_shell",),
        answer_contains=("Atlas API",),
        answer_excludes=("Cache Worker is healthy",),
    )

    good = evaluate_case(case, "Atlas API is reachable.", ("entity_status",))
    bad = evaluate_case(case, "Cache Worker is healthy.", ("run_shell",))

    assert good.passed and good.score == 1.0 and good.failures == ()
    assert not bad.passed and bad.score == 0.0
    assert {failure.kind for failure in bad.failures} == {
        "required_tool", "forbidden_tool", "answer_contains", "answer_excludes"
    }


def test_suite_reports_pass_rate_and_component_metrics() -> None:
    cases = (
        EvaluationCase("a", "a", required_tools=("vault_search",), answer_contains=("compute-large",)),
        EvaluationCase("b", "b", forbidden_tools=("run_shell",), answer_contains=("Atlas API",)),
    )
    report = evaluate_suite(
        cases,
        (
            ("compute-large", ("vault_search",)),
            ("Atlas API", ("run_shell",)),
        ),
    )

    assert report.total == 2 and report.passed == 1 and report.pass_rate == 0.5
    assert report.metrics["required_tool"] == 1.0
    assert report.metrics["forbidden_tool"] == 0.5


def test_evaluation_cases_load_from_versioned_json(tmp_path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"version": 1, "cases": [{
        "name": "compute-profile",
        "prompt": "Which compute profile?",
        "required_tools": ["vault_search"],
        "answer_contains": ["compute-large"],
    }]}), encoding="utf-8")

    cases = load_cases(path)

    assert len(cases) == 1 and cases[0].name == "compute-profile"
    assert cases[0].required_tools == ("vault_search",)


def test_public_agent_regression_pack_is_loadable_and_unique() -> None:
    path = Path(__file__).resolve().parent.parent / "evals" / "agent-intelligence.json"
    cases = load_cases(path)

    assert len(cases) >= 5
    assert len({case.name for case in cases}) == len(cases)
    assert all(case.prompt for case in cases)
