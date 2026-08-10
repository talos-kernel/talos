"""Policy-Kernel exec-Pfad: Command-Floor entscheidet mit (Hermes-Muster)."""
from __future__ import annotations

from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")


def _kernel() -> PolicyKernel:
    manifest = ToolManifest().with_tool(ToolSpec("run_shell", Effect.EXEC, reversible=False))
    return PolicyKernel(manifest, frozenset({OWNER}))


def _req(cmd: str) -> ToolRequest:
    return ToolRequest("run_shell", OWNER, {"command": cmd})


def test_safe_command_needs_human_without_sandbox() -> None:
    # Ohne Isolation ist auch ein harmloses Kommando freigabepflichtig.
    assert _kernel().decide(_req("ls -la")).verdict is Verdict.NEEDS_HUMAN


def test_safe_command_allowed_once_sandboxed() -> None:
    # Der Schalter dokumentiert, was die Sandbox spaeter freischaltet.
    manifest = ToolManifest().with_tool(ToolSpec("run_shell", Effect.EXEC, reversible=False))
    kernel = PolicyKernel(manifest, frozenset({OWNER}), shell_needs_human=False)
    assert kernel.decide(_req("ls -la")).verdict is Verdict.ALLOW


def test_hardline_command_is_denied() -> None:
    d = _kernel().decide(_req("rm -rf /"))
    assert d.verdict is Verdict.DENY
    assert "hardline" in d.reason


def test_dangerous_command_needs_human() -> None:
    assert _kernel().decide(_req("git reset --hard")).verdict is Verdict.NEEDS_HUMAN


def test_hardline_beats_the_sandbox_switch() -> None:
    """Der Floor steht vor jeder Erlaubnis — auch vor der, die die Sandbox spaeter gibt.

    Frueher hiess dieser Test „hardline schlaegt fehlende Allowlist". Die Liste ist weg;
    die Aussage bleibt und wird am staerksten moeglichen Fall geprueft: selbst wenn
    Shell-Laeufe generell freigeschaltet waeren, bleibt mkfs hart gesperrt.
    """
    manifest = ToolManifest().with_tool(ToolSpec("run_shell", Effect.EXEC, reversible=False))
    kernel = PolicyKernel(manifest, frozenset({OWNER}), shell_needs_human=False)
    d = kernel.decide(_req("mkfs.ext4 /dev/sda"))
    assert d.verdict is Verdict.DENY
    assert "hardline" in d.reason
