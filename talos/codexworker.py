"""Codex adapter for the confined worker; no process or authority of its own.

Only the worker chooses the binary, credential home and optional model. Each job
gets an isolated Codex home containing auth only, never the operator's config,
skills, hooks or MCP servers. The outer worker sandbox remains mandatory.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodexBackend:
    bin: str
    home: str
    model: str = ""


def gate(binary: str, home: str, model: str = "") -> CodexBackend | None:
    if not binary or not home:
        return None
    executable, auth_home = Path(binary), Path(home)
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        return None
    if not auth_home.is_absolute() or not auth_home.is_dir():
        return None
    return CodexBackend(str(executable), str(auth_home), model)


def stage_auth(backend: CodexBackend, workspace: Path) -> Path:
    """The source home is CODEX_HOME itself, not its parent; no config is copied."""
    content = (Path(backend.home) / "auth.json").read_bytes()
    home = workspace / ".home" / ".codex"
    if home.parent.is_symlink():
        raise OSError("job home must not be a symlink")
    home.mkdir(mode=0o700, parents=True, exist_ok=False)
    auth = home / "auth.json"
    # Create private from the first byte; chmod after copy leaves a readable window.
    try:
        with os.fdopen(
            os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as dest:
            dest.write(content)
    except OSError:
        shutil.rmtree(home)
        raise
    return home


def argv(prompt: str, workspace: Path, model: str = "") -> list[str]:
    # Codex's nested bubblewrap fails on the outer read-only mounts (measured
    # on Linux). The mandatory outer worker sandbox owns confinement, exactly
    # as for Claude/agy. This argv must only run through claudeworker.make_spawn.
    args = ["exec", "--json", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "danger-full-access", "-c", 'approval_policy="never"',
            "--cd", str(workspace), "--color", "never"]
    if model:
        args.extend(["--model", model])
    # '--' keeps a prompt beginning with a flag from becoming an option.
    return [*args, "--", prompt]


def evidence(event: dict, workspace: Path) -> tuple[str | None, list[str]]:
    if event.get("type") != "item.completed":
        return None, []
    item = event.get("item")
    if not isinstance(item, dict):
        return None, []
    if item.get("type") == "agent_message":
        text = item.get("text")
        return (text if isinstance(text, str) else None), []
    if item.get("type") != "file_change" or item.get("status") != "completed":
        return None, []
    files = []
    changes = item.get("changes")
    if not isinstance(changes, list):
        return None, []
    root = workspace.resolve()
    for change in changes:
        path = change.get("path") if isinstance(change, dict) else None
        if not isinstance(path, str) or not path:
            continue
        try:
            relative = (root / path).resolve().relative_to(root)
        except (OSError, ValueError, RuntimeError):
            continue
        if relative.parts and relative.parts[0] != ".home":
            files.append(relative.as_posix())
    return None, files


def failure(event: dict) -> str | None:
    # A transient `error` can be followed by recovery. turn.failed is terminal.
    if event.get("type") != "turn.failed":
        return None
    error = event.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    return str(message or "Codex turn failed")[-2000:]
