"""Operator-Anweisungen: Quellen, Grenzen und ein gemeinsamer Prompt-Vertrag."""
from __future__ import annotations

import os
from io import StringIO
from pathlib import Path

import pytest

from talos import instructions
from talos.api_reasoner import ApiReasoner
from talos.reasoner import ClaudeCliReasoner, HermesCliReasoner, PLAN_PROTOCOL, TOOL_PROTOCOL


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_sources_precede_protocol_and_skills_in_authority_order(tmp_path: Path) -> None:
    system = instructions.assemble_system_prompt(
        tool_protocol=TOOL_PROTOCOL,
        plan_protocol=PLAN_PROTOCOL,
        soul_path=_write(tmp_path / "SOUL.md", "soul-rule"),
        agents_path=_write(tmp_path / "AGENTS.md", "agents-rule"),
        user_path=_write(tmp_path / "USER.md", "user-rule"),
        skills="- demo: catalogue-rule",
    )

    positions = [
        system.index("soul-rule"),
        system.index("agents-rule"),
        system.index("user-rule"),
        system.index("TOOL_CALL"),
        system.index("catalogue-rule"),
    ]
    assert positions == sorted(positions)
    for name in ("SOUL.md", "AGENTS.md", "USER.md"):
        assert f"BEGIN {name}" in system
        assert f"END {name}" in system


def test_missing_optional_sources_are_harmless(tmp_path: Path) -> None:
    system = instructions.assemble_system_prompt(
        tool_protocol=TOOL_PROTOCOL,
        plan_protocol=PLAN_PROTOCOL,
        soul_path=_write(tmp_path / "SOUL.md", "still-here"),
        agents_path=tmp_path / "missing-agents.md",
        user_path=tmp_path / "missing-user.md",
    )
    assert "still-here" in system
    assert "TOOL_CALL" in system
    assert "BEGIN AGENTS.md" not in system
    assert "BEGIN USER.md" not in system


def test_broken_soul_keeps_the_existing_fallback(tmp_path: Path) -> None:
    soul = tmp_path / "SOUL.md"
    soul.write_bytes(b"\xff\xfe")
    system = instructions.assemble_system_prompt(
        tool_protocol=TOOL_PROTOCOL, plan_protocol=PLAN_PROTOCOL, soul_path=soul
    )
    assert instructions.FALLBACK_PREAMBLE in system
    assert "BEGIN SOUL.md" in system


def test_optional_source_live_reloads_even_when_size_and_mtime_match(tmp_path: Path) -> None:
    agents = _write(tmp_path / "AGENTS.md", "first-rule")
    first = instructions.assemble_system_prompt(
        tool_protocol=TOOL_PROTOCOL, plan_protocol=PLAN_PROTOCOL, agents_path=agents
    )
    original = agents.stat()
    _write(agents, "other-rule")
    os.utime(agents, ns=(original.st_atime_ns, original.st_mtime_ns))
    rewritten = agents.stat()
    assert rewritten.st_size == original.st_size
    assert rewritten.st_mtime_ns == original.st_mtime_ns

    second = instructions.assemble_system_prompt(
        tool_protocol=TOOL_PROTOCOL, plan_protocol=PLAN_PROTOCOL, agents_path=agents
    )
    assert "first-rule" in first
    assert "other-rule" in second
    assert "first-rule" not in second


@pytest.mark.parametrize("filename", ["SOUL.md", "AGENTS.md", "USER.md"])
def test_each_source_truncation_is_explicit(tmp_path: Path, filename: str) -> None:
    path = _write(tmp_path / filename, "x" * (instructions.MAX_SOURCE_CHARS + 100))
    kwargs = {filename.lower().replace(".md", "_path"): path}
    system = instructions.assemble_system_prompt(
        tool_protocol=TOOL_PROTOCOL, plan_protocol=PLAN_PROTOCOL, **kwargs
    )
    assert f"{filename} TRUNCATED" in system
    assert "x" * (instructions.MAX_SOURCE_CHARS + 1) not in system


def test_combined_instruction_context_has_an_explicit_cap(tmp_path: Path) -> None:
    paths = {
        name: _write(tmp_path / f"{name}.md", name * instructions.MAX_SOURCE_CHARS)
        for name in ("SOUL", "AGENTS", "USER")
    }
    block = instructions.load_instruction_context(
        soul_path=paths["SOUL"], agents_path=paths["AGENTS"], user_path=paths["USER"]
    )
    assert len(block) <= instructions.MAX_INSTRUCTION_CONTEXT_CHARS
    assert "COMBINED INSTRUCTION CONTEXT TRUNCATED" in block


def test_instruction_reader_never_consumes_an_unbounded_source() -> None:
    class Probe(StringIO):
        requested: int | None = None

        def read(self, size: int | None = -1) -> str:
            self.requested = size
            return super().read(size)

    # Das Zusatzzeichen liegt absichtlich auf einem Zeilenumbruch: `.strip()` darf
    # den belegten Overflow nicht wieder unsichtbar machen.
    probe = Probe("x" * instructions.MAX_SOURCE_CHARS + "\nMORE")

    class Source:
        name = "SOUL.md"

        def open(self, *_args, **_kwargs):
            return probe

    value = instructions._read(Source(), fallback=None)  # type: ignore[arg-type]
    assert probe.requested == instructions.MAX_SOURCE_CHARS + 1
    assert value is not None
    assert len(value) <= instructions.MAX_SOURCE_CHARS
    assert "SOUL.md TRUNCATED" in value


def test_cli_and_api_use_the_same_system_prompt_assembly(monkeypatch, tmp_path: Path) -> None:
    marker = "SHARED-ASSEMBLY-MARKER"
    calls: list[tuple[str, str]] = []

    def shared(*, skills: str = "", final_protocol: str = "", **_parts) -> str:
        calls.append((skills, final_protocol))
        return marker + skills + final_protocol

    monkeypatch.setattr(instructions, "assemble_system_prompt", shared)
    binary = tmp_path / "hermes"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr("talos.reasoner._assert_hermes_no_tools", lambda _binary: None)
    cli = HermesCliReasoner(
        str(binary), 30, provider="test", model="test", skills=lambda: "CLI-SKILLS"
    )
    assert marker in cli.argv_for("hello")[2]

    captured: list[list[str]] = []

    class Proc:
        returncode = 0

        def __init__(self, argv, **_kwargs) -> None:
            captured.append(argv)

        def communicate(self, timeout=None):
            return '{"result":"ok","subtype":"success"}', ""

    monkeypatch.setattr("talos.reasoner.subprocess.Popen", Proc)
    claude = ClaudeCliReasoner("claude", 30, skills=lambda: "CLAUDE-SKILLS")
    assert claude.reason("hello") == "ok"
    assert marker in captured[0][captured[0].index("-p") + 1]

    api = object.__new__(ApiReasoner)
    api._skills = lambda: "API-SKILLS"
    system, message = api._compose("hello")
    assert marker in system
    assert message == "hello"
    assert len(calls) == 3
    assert "CLI-SKILLS" in calls[0][0]
    assert "CLAUDE-SKILLS" in calls[1][0]
    assert "API-SKILLS" in calls[2][0]
