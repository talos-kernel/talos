"""requires_env wird vom Kernel vollstreckt — nicht nur deklariert.

Ein Werkzeug, dessen geforderte Umgebungsvariable fehlt oder leer ist, wird
DENIED, bevor irgendeine Klassifikation laeuft: ohne konfigurierten Socket gibt
es den Worker nicht, auf den ein Grant wirken wuerde. Gesetzt (und nicht leer)
faellt das Werkzeug in die normale Einordnung zurueck.
"""
from __future__ import annotations

from talos import policy, tools
from talos.channel import Principal
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest, Verdict

OWNER = Principal("telegram", "100000001")

_SOCKET = "TALOS_CLAUDE_WORKER_SOCKET"


def _kernel(manifest: ToolManifest) -> PolicyKernel:
    return PolicyKernel(manifest, frozenset({OWNER}))


def _synthetic_manifest(monkeypatch) -> ToolManifest:
    # Ein Werkzeug ohne Target-Extractor ist per Bauart DENY (decide, Schritt
    # 0.5) — die Testwerkzeuge brauchen also einen Eintrag, sonst pruefen sie
    # den Extractor-Floor statt requires_env.
    monkeypatch.setitem(policy.TARGET_EXTRACTORS, "needs_two", lambda args: ())
    monkeypatch.setitem(policy.TARGET_EXTRACTORS, "free_tool", lambda args: ())
    return (
        ToolManifest()
        .with_tool(ToolSpec("needs_two", Effect.READ, reversible=True,
                            requires_env=frozenset({"TALOS_RT_ALPHA", "TALOS_RT_BETA"})))
        .with_tool(ToolSpec("free_tool", Effect.READ, reversible=True))
    )


def test_unset_env_denies_and_names_every_missing_variable(monkeypatch):
    monkeypatch.delenv("TALOS_RT_ALPHA", raising=False)
    monkeypatch.delenv("TALOS_RT_BETA", raising=False)
    decision = _kernel(_synthetic_manifest(monkeypatch)).decide(
        ToolRequest("needs_two", OWNER, {}))
    assert decision.verdict is Verdict.DENY
    assert "TALOS_RT_ALPHA" in decision.reason
    assert "TALOS_RT_BETA" in decision.reason


def test_empty_env_counts_as_missing(monkeypatch):
    monkeypatch.setenv("TALOS_RT_ALPHA", "")
    monkeypatch.setenv("TALOS_RT_BETA", "/run/x.sock")
    decision = _kernel(_synthetic_manifest(monkeypatch)).decide(
        ToolRequest("needs_two", OWNER, {}))
    assert decision.verdict is Verdict.DENY
    assert "TALOS_RT_ALPHA" in decision.reason
    assert "TALOS_RT_BETA" not in decision.reason


def test_reason_never_carries_a_value(monkeypatch):
    monkeypatch.setenv("TALOS_RT_ALPHA", "geheimer-wert-123")
    monkeypatch.delenv("TALOS_RT_BETA", raising=False)
    decision = _kernel(_synthetic_manifest(monkeypatch)).decide(
        ToolRequest("needs_two", OWNER, {}))
    assert decision.verdict is Verdict.DENY
    assert "geheimer-wert-123" not in decision.reason


def test_env_set_falls_back_to_normal_classification(monkeypatch):
    monkeypatch.setenv("TALOS_RT_ALPHA", "/run/a.sock")
    monkeypatch.setenv("TALOS_RT_BETA", "/run/b.sock")
    decision = _kernel(_synthetic_manifest(monkeypatch)).decide(
        ToolRequest("needs_two", OWNER, {}))
    assert decision.verdict is Verdict.ALLOW
    assert decision.reason == "read"


def test_tool_without_requires_env_is_untouched(monkeypatch):
    decision = _kernel(_synthetic_manifest(monkeypatch)).decide(
        ToolRequest("free_tool", OWNER, {}))
    assert decision.verdict is Verdict.ALLOW


# --- die beiden echten env-gegateten Werkzeuge, in beide Richtungen festgezogen ---

def _delegate_kernel() -> PolicyKernel:
    return _kernel(tools.default_manifest())


def test_delegate_code_denied_without_worker_socket(monkeypatch):
    monkeypatch.delenv(_SOCKET, raising=False)
    monkeypatch.delenv("TALOS_CLAUDE_WORKER_ROOT", raising=False)
    decision = _delegate_kernel().decide(
        ToolRequest("delegate_code", OWNER, {"prompt": "rewrite the repo"}))
    assert decision.verdict is Verdict.DENY
    assert _SOCKET in decision.reason


def test_delegate_code_with_socket_gets_normal_classification(monkeypatch):
    monkeypatch.setenv(_SOCKET, "/run/talos/claude.sock")
    monkeypatch.delenv("TALOS_CLAUDE_WORKER_ROOT", raising=False)
    decision = _delegate_kernel().decide(
        ToolRequest("delegate_code", OWNER, {"prompt": "rewrite the repo"}))
    # EXEC, irreversibel, kein Shell-Kommando -> die uebliche Freigabe-Frage,
    # kein Wort von Umgebungsvariablen mehr im Grund.
    assert decision.verdict is Verdict.NEEDS_HUMAN
    assert decision.reason == "irreversible: delegate_code"


def test_delegate_status_denied_without_worker_socket(monkeypatch):
    monkeypatch.delenv(_SOCKET, raising=False)
    decision = _delegate_kernel().decide(
        ToolRequest("delegate_status", OWNER, {"job_id": "x"}))
    assert decision.verdict is Verdict.DENY
    assert _SOCKET in decision.reason


def test_delegate_status_with_socket_reads_freely(monkeypatch):
    monkeypatch.setenv(_SOCKET, "/run/talos/claude.sock")
    decision = _delegate_kernel().decide(
        ToolRequest("delegate_status", OWNER, {"job_id": "x"}))
    assert decision.verdict is Verdict.ALLOW
    assert decision.reason == "read"
