"""Betreiber-Allowlist fuer rekursive Loeschungen (TALOS_DELETABLE_ROOTS).

Der Cache-Cleanup-Fall: `rm -rf /home/<user>/<tiefer/pfad>` ist kein
Systemverzeichnis und gehoert nicht unter die Totalsperre — aber der Betreiber
kann Wurzeln benennen, unter denen ein schlichtes `rm -rf` ohne Einzelfreigabe
laeuft. Alles ausserhalb dieser Wurzeln und alles, was mehr ist als genau ein
schlichtes rm-Kommando, bleibt bei der Einzelfreigabe (NEEDS_HUMAN).
"""
from __future__ import annotations

import pytest

from talos.channel import Principal
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest, Verdict, deletable_roots

OWNER = Principal("telegram", "100000001")
ROOTS = "/home/ada/.cache,/home/ada/build"


def _kernel() -> PolicyKernel:
    # Sandbox-Modus wie in Produktion (SHELL_NEEDS_HUMAN=0): sonst waere ohnehin
    # jedes Kommando freigabepflichtig und die Allowlist haette nichts zu zeigen.
    manifest = ToolManifest().with_tool(ToolSpec("run_shell", Effect.EXEC, reversible=False))
    return PolicyKernel(manifest, frozenset({OWNER}), shell_needs_human=False)


def _req(cmd: str) -> ToolRequest:
    return ToolRequest("run_shell", OWNER, {"command": cmd})


@pytest.fixture(autouse=True)
def _roots_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALOS_DELETABLE_ROOTS", ROOTS)


def test_delete_under_operator_root_is_allowed() -> None:
    d = _kernel().decide(_req("rm -rf /home/ada/.cache/build-x"))
    assert d.verdict is Verdict.ALLOW


def test_delete_outside_roots_still_needs_human() -> None:
    assert _kernel().decide(_req("rm -rf /home/ada/other")).verdict is Verdict.NEEDS_HUMAN


def test_mixed_targets_need_human() -> None:
    # Ein einziges Ziel ausserhalb der Wurzeln kippt das ganze Kommando.
    cmd = "rm -rf /home/ada/.cache/a /home/ada/other"
    assert _kernel().decide(_req(cmd)).verdict is Verdict.NEEDS_HUMAN


def test_dotdot_escape_needs_human() -> None:
    assert _kernel().decide(_req("rm -rf /home/ada/.cache/../other")).verdict is Verdict.NEEDS_HUMAN


def test_chained_command_needs_human() -> None:
    cmd = "rm -rf /home/ada/.cache/a; touch /home/ada/.cache/pwned"
    assert _kernel().decide(_req(cmd)).verdict is Verdict.NEEDS_HUMAN


def test_substitution_needs_human() -> None:
    assert _kernel().decide(_req("rm -rf $(echo /home/ada/.cache/a)")).verdict is Verdict.NEEDS_HUMAN


def test_relative_target_needs_human() -> None:
    assert _kernel().decide(_req("rm -rf ./cache")).verdict is Verdict.NEEDS_HUMAN


def test_without_env_everything_needs_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALOS_DELETABLE_ROOTS")
    assert _kernel().decide(_req("rm -rf /home/ada/.cache/a")).verdict is Verdict.NEEDS_HUMAN


def test_home_root_stays_hardline_even_when_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ein Betreiber-Eintrag kann die Totalsperre nicht aufweichen: /home/ada als
    # Wurzel ist ungueltig, und das Kommando bleibt hardline-DENY.
    monkeypatch.setenv("TALOS_DELETABLE_ROOTS", "/home/ada")
    d = _kernel().decide(_req("rm -rf /home/ada"))
    assert d.verdict is Verdict.DENY
    assert "hardline" in d.reason


def test_system_root_entry_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALOS_DELETABLE_ROOTS", "/etc,/home/ada/.cache")
    assert deletable_roots() == ("/home/ada/.cache",)
    d = _kernel().decide(_req("rm -rf /etc/nginx"))
    assert d.verdict is Verdict.DENY


def test_non_recursive_rm_is_not_flagged() -> None:
    # `rm <datei>` ohne -r faellt nicht unter „recursive delete" — nichts zu tun.
    assert _kernel().decide(_req("rm /home/ada/.cache/a")).verdict is Verdict.ALLOW


def test_deletable_roots_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "TALOS_DELETABLE_ROOTS",
        " /home/ada/.cache ,,relativ/ohne/slash,/home,/home/ada,/home/ada/.cache",
    )
    # Relatives, /home und Home-Wurzeln fallen heraus; Duplikate dedupliziert.
    assert deletable_roots() == ("/home/ada/.cache",)


def test_deletable_roots_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALOS_DELETABLE_ROOTS")
    assert deletable_roots() == ()
