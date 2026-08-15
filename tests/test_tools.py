"""Konkrete Tool-Runner (ohne Gating — das prüft der Executor separat)."""
from __future__ import annotations

from pathlib import Path

import pytest

from talos import tools
from talos.policy import ToolRequest

OWNER = 100000001


def test_read_file_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("hallo", encoding="utf-8")
    assert tools.read_file(ToolRequest("read_file", OWNER, {"path": str(f)})) == "hallo"


def test_write_file_creates_and_writes(tmp_path: Path) -> None:
    f = tmp_path / "sub" / "b.txt"
    out = tools.write_file(ToolRequest("write_file", OWNER, {"path": str(f), "content": "xyz"}))
    assert f.read_text(encoding="utf-8") == "xyz"
    assert "3 Zeichen" in out


def test_run_shell_returns_rc_and_output() -> None:
    out = tools.run_shell(ToolRequest("run_shell", OWNER, {"command": "echo talos"}))
    assert out.startswith("rc=0")
    assert "talos" in out


def test_missing_required_arguments_raise_clear_errors_without_side_effects(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "file.txt"
    cases = (
        (tools.read_file, ToolRequest("read_file", OWNER, {}), "path"),
        (tools.write_file, ToolRequest("write_file", OWNER, {}), "path"),
        (
            tools.write_file,
            ToolRequest("write_file", OWNER, {"path": str(target)}),
            "content",
        ),
        (tools.run_shell, ToolRequest("run_shell", OWNER, {}), "command"),
    )

    for runner, request, missing in cases:
        with pytest.raises(ValueError) as error:
            runner(request)
        assert str(error.value) == f"das Werkzeug braucht das Argument '{missing}'"

    assert not target.parent.exists()


def test_default_manifest_declares_three_tools() -> None:
    m = tools.default_manifest()
    assert m.get("read_file") is not None
    assert m.get("write_file") is not None
    assert m.get("run_shell") is not None
