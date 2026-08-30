"""git — die Netz-Ops hinter dem Gate.

Jede Op fragt (Vertrauen beim Holen, oeffentliche Wirkung beim Pushen), die
Attended-Auto-Freigabe endet per `outward`, Repos leben im Arbeitsbereich, und
das Kommando wird gebaut — nie frei formuliert. Diese Tests halten die
Kernel-Verdicts, die Remote-Einordnung (https durch guard_url, git:// nie,
ssh lexikalisch), die Workspace-Grenze und die gebauten Kommandozeilen fest.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from talos import gitops, tools
from talos.autonomy import attended_routine
from talos.channel import Principal
from talos.gitops import GitOpsError, build_command, make_git_runner
from talos.manifest import Effect
from talos.policy import WORKSPACE_DIR, PolicyKernel, ToolRequest, Verdict
from talos.standing import action_key, action_label
from talos.web import UrlRefusedError

OWNER = Principal("telegram", "42")
PUBLIC_IP = "93.184.216.34"


def _req(**args: object) -> ToolRequest:
    return ToolRequest(tool="git", identity=OWNER, args=dict(args))


def _kernel() -> PolicyKernel:
    return PolicyKernel(tools.default_manifest(), frozenset({OWNER}))


# --- Manifest und Kernel-Vertrauensform ---------------------------------------------

def test_manifest_declares_outward_and_irreversible() -> None:
    spec = tools.default_manifest().get("git")
    assert spec is not None
    assert spec.effect is Effect.EXEC
    assert spec.reversible is False
    assert spec.outward is True
    assert tools.RUNNERS["git"] is gitops.git


def test_every_op_asks_a_human() -> None:
    for op in ("clone", "fetch", "pull", "push"):
        decision = _kernel().decide(_req(op=op, repo="repo1"))
        assert decision.verdict is Verdict.NEEDS_HUMAN, op
        assert op in decision.reason


def test_unknown_op_is_deny_not_a_question() -> None:
    assert _kernel().decide(_req(op="reset", repo="repo1")).verdict is Verdict.DENY
    assert _kernel().decide(_req(repo="repo1")).verdict is Verdict.DENY


def test_no_attended_autoapproval() -> None:
    spec = tools.default_manifest().get("git")
    assert attended_routine(_req(op="push", repo="repo1"), spec, _kernel()) is False


# --- Stehende Bindung: op + repo + url -------------------------------------------------

def test_standing_key_binds_op_repo_and_remote() -> None:
    clone = action_key(_req(op="clone", repo="a", url="https://github.com/x/y"))
    push = action_key(_req(op="push", repo="a", url="https://github.com/x/y"))
    assert clone is not None and push is not None and clone != push
    assert action_key(_req(op="push", repo="b", url="https://github.com/x/y")) != push
    assert "git push a https://github.com/x/y" == action_label(
        _req(op="push", repo="a", url="https://github.com/x/y")
    )


# --- Remote-Einordnung -----------------------------------------------------------------

def test_https_remotes_pass_the_guard() -> None:
    url = gitops._checked_remote(f"https://{PUBLIC_IP}/repo.git", allowed_addresses=frozenset())
    assert url.endswith("/repo.git")


def test_internal_remotes_never_pass() -> None:
    for ziel in ("https://192.168.1.1/repo.git", "https://169.254.169.254/x", "https://100.64.0.1/r"):
        with pytest.raises(UrlRefusedError):
            gitops._checked_remote(ziel, allowed_addresses=frozenset())


def test_git_protocol_is_refused() -> None:
    with pytest.raises(GitOpsError, match="unauthenticated"):
        gitops._checked_remote("git://github.com/x/y.git", allowed_addresses=frozenset())


def test_ssh_remotes_are_lexical_only() -> None:
    assert gitops._checked_remote("git@github.com:x/y.git", allowed_addresses=frozenset())
    assert gitops._checked_remote("ssh://git@github.com/x/y.git", allowed_addresses=frozenset())
    with pytest.raises(GitOpsError):
        gitops._checked_remote("git@github.com:x/y.git; id", allowed_addresses=frozenset())
    with pytest.raises(GitOpsError):
        gitops._checked_remote("ext::sh -c id", allowed_addresses=frozenset())


# --- Workspace-Grenze und Kommandobau ---------------------------------------------------

def test_repo_must_live_in_the_workspace() -> None:
    with pytest.raises(GitOpsError, match="outside the workspace"):
        gitops._checked_repo("/etc/repo")
    with pytest.raises(GitOpsError, match="outside the workspace"):
        gitops._checked_repo("../ausserhalb")
    with pytest.raises(GitOpsError, match="outside the workspace"):
        gitops._checked_repo("~/.ssh/repo")
    ziel = gitops._checked_repo("projekt")
    assert str(ziel).startswith(str(WORKSPACE_DIR))


def test_build_command_clone_and_pull() -> None:
    line, _ = build_command("clone", "projekt", "https://github.com/x/y", "")
    parts = shlex.split(line)
    assert parts[:2] == ["git", "clone"]
    assert "https://github.com/x/y" in parts
    (gitops.WORKSPACE_DIR / "projekt").mkdir(parents=True, exist_ok=True)
    line, _ = build_command("pull", "projekt", "", "")
    assert shlex.split(line)[:3] == ["git", "-C", str(gitops._checked_repo("projekt"))]
    assert "--ff-only" in shlex.split(line)


def test_build_command_push_needs_remote_with_branch() -> None:
    (gitops.WORKSPACE_DIR / "projekt").mkdir(parents=True, exist_ok=True)
    line, _ = build_command("push", "projekt", "origin", "main")
    parts = shlex.split(line)
    assert parts[-2:] == ["origin", "main"]
    with pytest.raises(GitOpsError, match="needs the remote"):
        build_command("push", "projekt", "", "main")


def test_build_command_refuses_unknown_ops() -> None:
    with pytest.raises(GitOpsError, match="op"):
        build_command("reset", "projekt", "", "")


# --- Der Runner --------------------------------------------------------------------------

def test_runner_refuses_outside_repos_before_any_backend() -> None:
    runner = make_git_runner()
    with pytest.raises(GitOpsError, match="outside the workspace"):
        runner(_req(op="fetch", repo="/etc/repo"))


def test_runner_without_confined_backend_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitops.sandbox, "select_backend", lambda _c: None)
    (gitops.WORKSPACE_DIR / "projekt").mkdir(parents=True, exist_ok=True)
    out = make_git_runner()(_req(op="fetch", repo="projekt"))
    assert out.startswith("rc=refused")


def test_runner_runs_networked_and_built(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class _Result:
        backend = "fake"
        returncode = 0
        stdout = "Already up to date.\n"
        stderr = ""
        timed_out = False
        truncated = False

    def _fake_run(command: str, **kwargs: object) -> _Result:
        seen["command"] = command
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(gitops.sandbox, "select_backend", lambda _c: object())
    monkeypatch.setattr(gitops.sandbox, "run_sandboxed", _fake_run)
    (gitops.WORKSPACE_DIR / "projekt").mkdir(parents=True, exist_ok=True)
    out = make_git_runner()(_req(op="fetch", repo="projekt"))
    assert seen["allow_network"] is True
    assert shlex.split(str(seen["command"]))[:3] == ["git", "-C", str(gitops._checked_repo("projekt"))]
    assert out == "rc=0 [fake] git fetch\nAlready up to date."
