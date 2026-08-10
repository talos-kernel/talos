"""Secure, policy-gated vault tools."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from talos import tools
from talos.channel import Principal
from talos.manifest import Effect
from talos.policy import PolicyKernel, ToolRequest, Verdict, guard_targets
from talos.reasoner import TOOL_PROTOCOL
from talos import vault

OWNER = Principal("telegram", "100000001")

VOWNERD_NOTE = """---
type: gotcha
tags: [talos, vault]
projects: [talos]
date: 2026-08-02
confidence: high
last-verified: 2026-08-02
---

# Do not interpolate shell input
"""


def req(tool: str, **args: object) -> ToolRequest:
    return ToolRequest(tool, OWNER, dict(args))


def completed(argv: list[str], *, stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(argv, rc, stdout, stderr)


def test_manifest_and_runner_registry_make_vault_tools_first_class() -> None:
    manifest = tools.default_manifest()
    assert manifest.get("vault_search").effect is Effect.READ  # type: ignore[union-attr]
    assert manifest.get("vault_get").effect is Effect.READ  # type: ignore[union-attr]
    write = manifest.get("vault_write_note")
    assert write is not None and write.effect is Effect.WRITE and write.reversible
    assert {"vault_search", "vault_get", "vault_write_note"} <= tools.RUNNERS.keys()
    assert tools.default_manifest().get("entity_status").effect is Effect.READ  # type: ignore[union-attr]
    assert '- entity_status {"name": "known entity"}' in TOOL_PROTOCOL


def test_vault_search_uses_argv_not_shell_filters_secrets_and_redacts(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict]] = []
    payload = [
        {"file": "qmd://obsidian/credentials/api.md", "snippet": "password: hunter2"},
        {"file": "qmd://obsidian/patterns/safe.md", "snippet": "api_key = sk-supersecretvalue"},
    ]

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed(argv, stdout=json.dumps(payload))

    monkeypatch.setattr(vault.subprocess, "run", fake_run)
    injection = "auth; touch /tmp/pwned $(id)"
    runner = vault.make_vault_search_runner(tmp_path, "/opt/qmd")
    output = runner(req("vault_search", query=injection, limit=2))

    assert calls[0][0] == [
        "/opt/qmd", "search", injection, "-c", "obsidian", "-n", "2", "--format", "json"
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == vault.QMD_SEARCH_TIMEOUT_S
    assert "credentials" not in output.lower()
    assert "sk-supersecretvalue" not in output
    assert "[REDACTED]" in output
    assert not Path("/tmp/pwned").exists()


@pytest.mark.parametrize("query", ["", "x" * 301])
def test_vault_search_rejects_bad_query_length(query: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        vault.make_vault_search_runner(tmp_path, "/opt/qmd")(req("vault_search", query=query))


@pytest.mark.parametrize("limit", [0, 11, "2", True])
def test_vault_search_rejects_limit_outside_integer_range(limit: object, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        vault.make_vault_search_runner(tmp_path, "/opt/qmd")(
            req("vault_search", query="safe", limit=limit)
        )


def test_vault_search_output_is_bounded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        vault.subprocess,
        "run",
        lambda argv, **kwargs: completed(argv, stdout=json.dumps([{"file": "qmd://obsidian/patterns/a.md", "snippet": "x" * (vault.MAX_SEARCH_OUTPUT_CHARS * 2)}])),
    )
    output = vault.make_vault_search_runner(tmp_path, "/opt/qmd")(
        req("vault_search", query="safe", limit=1)
    )
    assert len(output) <= vault.MAX_SEARCH_OUTPUT_CHARS


def test_vault_search_overbroad_query_merges_single_term_results(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    by_query = {
        "hosting plan server": [],
        "hosting": [
            {"file": "qmd://obsidian/workflows/hosting.md", "score": 0.82, "snippet": "compute-large"},
        ],
        "plan": [
            {"file": "qmd://obsidian/workflows/hosting.md", "score": 0.88, "snippet": "plan"},
            {"file": "qmd://obsidian/decisions/provider.md", "score": 0.80, "snippet": "provider"},
        ],
        "server": [
            {"file": "qmd://obsidian/workflows/hosting.md", "score": 0.84, "snippet": "server"},
        ],
    }

    def fake_run(argv, **kwargs):
        if argv[1] == "query":
            return completed(argv, stderr="vectors unavailable", rc=1)
        query = argv[2]
        calls.append(query)
        return completed(argv, stdout=json.dumps(by_query[query]))

    monkeypatch.setattr(vault.subprocess, "run", fake_run)
    output = vault.make_vault_search_runner(tmp_path, "/opt/qmd")(
        req("vault_search", query="hosting plan server", limit=2)
    )
    results = json.loads(output)

    assert calls == ["hosting plan server", "hosting", "plan", "server"]
    assert len(results) == 2
    assert results[0]["file"] == "qmd://obsidian/workflows/hosting.md"
    assert results[1]["file"] == "qmd://obsidian/decisions/provider.md"


def test_vault_search_uses_bounded_hybrid_rescue_before_term_fallback(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    semantic = [
        {
            "file": "qmd://obsidian/decisions/current-compute.md",
            "score": 0.91,
            "snippet": "Current compute profile is compute-large",
        }
    ]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "search":
            return completed(argv, stdout="[]")
        return completed(argv, stdout=json.dumps(semantic))

    monkeypatch.setattr(vault.subprocess, "run", fake_run)
    output = vault.make_vault_search_runner(tmp_path, "/opt/qmd")(
        req("vault_search", query="which compute profile is currently used", limit=3)
    )

    assert [argv[1] for argv in calls] == ["search", "query"]
    assert calls[1][2] == (
        "lex: which compute profile is currently used\n"
        "vec: which compute profile is currently used"
    )
    assert "--no-rerank" in calls[1] and calls[1][-2:] == ["--format", "json"]
    assert json.loads(output)[0]["file"].endswith("current-compute.md")


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "patterns/../../outside.md",
        "/etc/passwd.md",
        "qmd://other/patterns/a.md",
        "qmd://obsidian/credentials/token.md",
        "credentials/token.md",
        "secrets/token.md",
        ".hidden/note.md",
        "patterns/.hidden.md",
        "patterns/not-markdown.txt",
    ],
)
def test_vault_get_rejects_unsafe_paths(path: str, tmp_path: Path) -> None:
    with pytest.raises(vault.VaultPathError):
        vault.canonical_vault_path(path, tmp_path, for_write=False)


def test_vault_get_accepts_uri_and_relative_path_with_same_canonical_target(tmp_path: Path) -> None:
    note = tmp_path / "patterns" / "safe-note.md"
    note.parent.mkdir()
    note.write_text("safe", encoding="utf-8")
    expected = str(note.resolve())
    assert str(vault.canonical_vault_path("patterns/safe-note.md", tmp_path)) == expected
    assert str(vault.canonical_vault_path("qmd://obsidian/patterns/safe-note.md", tmp_path)) == expected


def test_vault_get_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-vault.md"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "patterns").mkdir()
    (tmp_path / "patterns" / "escape.md").symlink_to(outside)
    with pytest.raises(vault.VaultPathError):
        vault.make_vault_get_runner(tmp_path)(req("vault_get", path="patterns/escape.md"))


def test_vault_get_reads_redacted_bounded_markdown(tmp_path: Path) -> None:
    note = tmp_path / "patterns" / "safe-note.md"
    note.parent.mkdir()
    note.write_text("token: sk-this-must-not-leak", encoding="utf-8")
    output = vault.make_vault_get_runner(tmp_path)(req("vault_get", path="qmd://obsidian/patterns/safe-note.md"))
    assert "sk-this-must-not-leak" not in output
    assert "[REDACTED]" in output

    note.write_text("x" * (vault.MAX_GET_BYTES + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="groß"):
        vault.make_vault_get_runner(tmp_path)(req("vault_get", path="patterns/safe-note.md"))


@pytest.mark.parametrize(
    "path",
    [
        "errors/Not-Kebab.md",
        "errors/not_kebab.md",
        "errors/nested/note.md",
        "notes/note.md",
        "credentials/note.md",
        "errors/.hidden.md",
        "errors/note.txt",
    ],
)
def test_vault_write_note_only_allows_category_and_kebab_case(path: str, tmp_path: Path) -> None:
    with pytest.raises(vault.VaultPathError):
        vault.canonical_vault_path(path, tmp_path, for_write=True)


@pytest.mark.parametrize(
    "content",
    [
        "# no frontmatter",
        "---\ntype: gotcha\ntags: []\n---\nmissing fields",
        "---\ntype: gotcha\ntags: []\nprojects: []\ndate: 2026-01-01\nconfidence: high\nlast-verified:\n---\nempty field",
    ],
)
def test_vault_write_note_requires_complete_frontmatter(content: str, tmp_path: Path) -> None:
    runner = vault.make_vault_write_runner(tmp_path, "/opt/qmd")
    with pytest.raises(ValueError, match="Frontmatter"):
        runner(req("vault_write_note", path="gotchas/safe-note.md", content=content))


def test_vault_write_note_rejects_oversize_content(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="groß"):
        vault.make_vault_write_runner(tmp_path, "/opt/qmd")(
            req("vault_write_note", path="errors/large-note.md", content=VOWNERD_NOTE + "x" * vault.MAX_NOTE_BYTES)
        )


def test_vault_write_is_atomic_and_updates_qmd_with_argv(monkeypatch, tmp_path: Path) -> None:
    category = tmp_path / "gotchas"
    category.mkdir()
    target = category / "safe-note.md"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    replace_observations: list[tuple[str, str]] = []
    qmd_calls: list[tuple[list[str], dict]] = []

    def observed_replace(src, dst, **kwargs):
        # Before the single rename, readers still see the complete old file.
        replace_observations.append((target.read_text(encoding="utf-8"), str(src)))
        return real_replace(src, dst, **kwargs)

    def fake_run(argv, **kwargs):
        qmd_calls.append((argv, kwargs))
        return completed(argv, stdout="Indexed 1 file")

    monkeypatch.setattr(vault.os, "replace", observed_replace)
    monkeypatch.setattr(vault.subprocess, "run", fake_run)
    result = vault.make_vault_write_runner(tmp_path, "/opt/qmd")(
        req("vault_write_note", path="gotchas/safe-note.md", content=VOWNERD_NOTE)
    )

    assert replace_observations and replace_observations[0][0] == "old"
    assert target.read_text(encoding="utf-8") == VOWNERD_NOTE
    assert not any(p.name.endswith(".tmp") for p in category.iterdir())
    assert qmd_calls == [(["/opt/qmd", "update"], {
        "capture_output": True, "text": True, "timeout": vault.QMD_UPDATE_TIMEOUT_S,
        "check": False, "shell": False,
    })]
    assert "geschrieben" in result and "aktualisiert" in result


def test_qmd_update_failure_is_reported_but_note_remains_valid(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        vault.subprocess,
        "run",
        lambda argv, **kwargs: completed(argv, stderr="index unavailable", rc=7),
    )
    result = vault.make_vault_write_runner(tmp_path, "/opt/qmd")(
        req("vault_write_note", path="decisions/safe-note.md", content=VOWNERD_NOTE)
    )
    assert (tmp_path / "decisions" / "safe-note.md").read_text(encoding="utf-8") == VOWNERD_NOTE
    assert "Warnung" in result and "index unavailable" in result


def test_vault_write_rejects_symlink_destination_without_following(monkeypatch, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-write.md"
    outside.write_text("unchanged", encoding="utf-8")
    (tmp_path / "errors").mkdir()
    (tmp_path / "errors" / "safe-note.md").symlink_to(outside)
    monkeypatch.setattr(vault.subprocess, "run", lambda *a, **k: pytest.fail("qmd must not run"))
    with pytest.raises(vault.VaultPathError):
        vault.make_vault_write_runner(tmp_path, "/opt/qmd")(
            req("vault_write_note", path="errors/safe-note.md", content=VOWNERD_NOTE)
        )
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_policy_and_guard_derive_exact_runner_target(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}), vault_dir=root)
    request = req("vault_write_note", path="patterns/exact-target.md", content=VOWNERD_NOTE)
    expected = str((root / "patterns" / "exact-target.md").resolve())
    assert kernel.guard_targets(request) == (expected,)
    assert guard_targets(request, root) == (expected,)
    assert kernel.decide(request).verdict is Verdict.ALLOW
    assert str(vault.canonical_vault_path(request.args["path"], root, for_write=True)) == expected


def test_policy_denies_invalid_vault_target_instead_of_reaching_runner(tmp_path: Path) -> None:
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}), vault_dir=tmp_path)
    decision = kernel.decide(req("vault_get", path="qmd://obsidian/credentials/key.md"))
    assert decision.verdict is Verdict.DENY
    assert "Vault-Pfad" in decision.reason


def test_tool_protocol_mandates_the_notes_workflow() -> None:
    text = TOOL_PROTOCOL.lower()
    assert "before debugging" in text
    assert "context is missing" in text
    assert "vault_search" in text
    assert ">5" in text and "vault_write_note" in text
