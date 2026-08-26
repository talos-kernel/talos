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


def test_missing_required_arg_raises_clear_error() -> None:
    # Ein fehlendes Pflichtargument liefert eine brauchbare Meldung statt eines nackten
    # KeyError, der als kryptisches „error · 'path'" durchschlägt und einen Plan abbricht.
    faelle = (
        ("read_file", {}, "path"),
        ("write_file", {"content": "x"}, "path"),
        ("write_file", {"path": "/tmp/nichts"}, "content"),
        ("run_shell", {}, "command"),
    )
    for tool, args, wort in faelle:
        with pytest.raises(ValueError, match=wort):
            getattr(tools, tool)(ToolRequest(tool, OWNER, args))


def test_default_manifest_declares_three_tools() -> None:
    m = tools.default_manifest()
    assert m.get("read_file") is not None
    assert m.get("write_file") is not None
    assert m.get("run_shell") is not None
