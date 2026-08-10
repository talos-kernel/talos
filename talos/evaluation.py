"""Small deterministic regression metrics for real agent traces.

The harness scores the things Talos can actually prove: which tools ran and whether the
answer obeyed explicit content contracts. It deliberately does not use BLEU or an LLM
judge for factual status tasks; a fluent paraphrase is not evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Sequence

MAX_EVAL_FILE_BYTES = 128 * 1024
MAX_CASES = 200


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    prompt: str
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    answer_contains: tuple[str, ...] = ()
    answer_excludes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Failure:
    kind: str
    detail: str


@dataclass(frozen=True)
class CaseResult:
    name: str
    passed: bool
    score: float
    failures: tuple[Failure, ...]
    components: Mapping[str, float]


@dataclass(frozen=True)
class SuiteResult:
    total: int
    passed: int
    pass_rate: float
    metrics: Mapping[str, float]
    cases: tuple[CaseResult, ...]


def _tuple_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item)[:300] for item in value[:32] if str(item).strip())


def load_cases(path: Path) -> tuple[EvaluationCase, ...]:
    candidate = Path(path)
    if not candidate.is_file() or candidate.stat().st_size > MAX_EVAL_FILE_BYTES:
        raise ValueError("evaluation file missing or too large")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"invalid evaluation JSON: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported evaluation format")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError("evaluation cases must be a list")
    cases = []
    for raw in raw_cases[:MAX_CASES]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "")).strip()[:120]
        prompt = str(raw.get("prompt", "")).strip()[:2_000]
        if not name or not prompt:
            continue
        cases.append(
            EvaluationCase(
                name,
                prompt,
                required_tools=_tuple_strings(raw.get("required_tools")),
                forbidden_tools=_tuple_strings(raw.get("forbidden_tools")),
                answer_contains=_tuple_strings(raw.get("answer_contains")),
                answer_excludes=_tuple_strings(raw.get("answer_excludes")),
            )
        )
    return tuple(cases)


def evaluate_case(case: EvaluationCase, answer: str, tools: Sequence[str]) -> CaseResult:
    used = tuple(str(tool) for tool in tools)
    lower_answer = str(answer).casefold()
    checks = {
        "required_tool": all(tool in used for tool in case.required_tools),
        "forbidden_tool": all(tool not in used for tool in case.forbidden_tools),
        "answer_contains": all(text.casefold() in lower_answer for text in case.answer_contains),
        "answer_excludes": all(text.casefold() not in lower_answer for text in case.answer_excludes),
    }
    failures = []
    if not checks["required_tool"]:
        failures.append(Failure("required_tool", f"missing: {case.required_tools}"))
    if not checks["forbidden_tool"]:
        failures.append(Failure("forbidden_tool", f"used: {case.forbidden_tools}"))
    if not checks["answer_contains"]:
        failures.append(Failure("answer_contains", f"missing: {case.answer_contains}"))
    if not checks["answer_excludes"]:
        failures.append(Failure("answer_excludes", f"present: {case.answer_excludes}"))
    components = {name: 1.0 if ok else 0.0 for name, ok in checks.items()}
    score = sum(components.values()) / len(components)
    return CaseResult(case.name, not failures, score, tuple(failures), components)


def evaluate_suite(
    cases: Sequence[EvaluationCase],
    traces: Sequence[tuple[str, Sequence[str]]],
) -> SuiteResult:
    if len(cases) != len(traces):
        raise ValueError("one trace is required for every evaluation case")
    results = tuple(
        evaluate_case(case, answer, tools)
        for case, (answer, tools) in zip(cases, traces, strict=True)
    )
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    names = ("required_tool", "forbidden_tool", "answer_contains", "answer_excludes")
    metrics = {
        name: (sum(result.components[name] for result in results) / total if total else 0.0)
        for name in names
    }
    return SuiteResult(total, passed, passed / total if total else 0.0, metrics, results)


__all__ = [
    "CaseResult",
    "EvaluationCase",
    "Failure",
    "SuiteResult",
    "evaluate_case",
    "evaluate_suite",
    "load_cases",
]
