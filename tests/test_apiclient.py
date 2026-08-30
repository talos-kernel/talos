"""http_request — der API-Connector hinter derselben Tuer.

Lesemethoden laufen wie web_fetch, Schreibmethoden fragen ausnahmslos — und die
Attended-Auto-Freigabe endet an der Aussengrenze (`outward`). Diese Tests halten
fest: die Kernel-Verdicts je Methode, die Transport-Weigerungen vor dem ersten
Byte, die Sprungpruefung pro Weiterleitung, die Redaktion im Audit-Log und die
stehende Bindung an exakt (methode, url).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from talos import apiclient, tools
from talos.apiclient import ApiRequestError, make_http_request_runner
from talos.autonomy import attended_routine
from talos.channel import Principal
from talos.executor import audit_args
from talos.manifest import Effect
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.standing import action_key, action_label
from talos.web import UrlRefusedError

OWNER = Principal("telegram", "42")
PUBLIC_IP = "93.184.216.34"


def _req(**args: object) -> ToolRequest:
    return ToolRequest(tool="http_request", identity=OWNER, args=dict(args))


def _kernel() -> PolicyKernel:
    return PolicyKernel(tools.default_manifest(), frozenset({OWNER}))


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"{}", headers: dict | None = None):
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = body
        self.closed = False

    def iter_content(self, _n: int):
        yield self._body

    def close(self) -> None:
        self.closed = True


def _runner(calls: list, responses: list[_FakeResponse]):
    def transport(url: str, **kwargs: object) -> _FakeResponse:
        calls.append((url, kwargs))
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return make_http_request_runner(
        get=transport, resolve=lambda _h: (PUBLIC_IP,)
    )


# --- Manifest und Kernel-Vertrauensform ---------------------------------------------

def test_manifest_declares_outward_and_irreversible() -> None:
    spec = tools.default_manifest().get("http_request")
    assert spec is not None
    assert spec.effect is Effect.EXEC
    assert spec.reversible is False
    assert spec.outward is True
    assert tools.RUNNERS["http_request"] is apiclient.http_request


def test_read_methods_run() -> None:
    for method in ("GET", "HEAD", "OPTIONS"):
        decision = _kernel().decide(_req(method=method, url="https://api.example.com/x"))
        assert decision.verdict is Verdict.ALLOW, method


def test_mutating_methods_always_ask() -> None:
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        decision = _kernel().decide(_req(method=method, url="https://api.example.com/x"))
        assert decision.verdict is Verdict.NEEDS_HUMAN, method
        assert method in decision.reason


def test_missing_url_is_deny() -> None:
    assert _kernel().decide(_req(method="GET")).verdict is Verdict.DENY


def test_no_attended_autoapproval_despite_no_requires_env() -> None:
    """`outward` schliesst die Routineklasse per Bauart aus — ein POST nach
    draussen darf auch im attended Chat nie still durchlaufen."""
    kernel = _kernel()
    spec = tools.default_manifest().get("http_request")
    assert attended_routine(
        _req(method="POST", url="https://api.example.com/x"), spec, kernel
    ) is False


# --- Stehende Bindung: methode + url, nicht der Body ---------------------------------

def test_standing_key_binds_method_and_url() -> None:
    get = action_key(_req(method="GET", url="https://a.example.com/x"))
    post = action_key(_req(method="POST", url="https://a.example.com/x"))
    assert get is not None and post is not None and get != post
    assert action_key(_req(method="POST", url="https://a.example.com/x",
                           body="anders")) == post
    assert action_label(_req(method="POST", url="https://a.example.com/x")) == \
        "http_request POST https://a.example.com/x"


# --- Audit-Redaktion: Namen ja, Werte nie ---------------------------------------------

def test_audit_args_redact_header_values_and_body() -> None:
    out = audit_args(
        {"method": "POST", "url": "https://a.example.com",
         "headers": {"Authorization": "Bearer geheim", "X-Trace": "1"},
         "body": '{"password": "hunter2"}'},
        "http_request",
    )
    assert "geheim" not in str(out) and "hunter2" not in str(out)
    assert "Authorization" in out["headers"]
    assert out["method"] == "POST"


def test_audit_args_keep_skill_bodies() -> None:
    out = audit_args({"name": "x", "body": "der Skill-Text"}, "skill_write")
    assert out["body"] == "der Skill-Text"


# --- Weigerungen vor dem ersten Byte --------------------------------------------------

def test_blocked_transport_headers() -> None:
    with pytest.raises(ApiRequestError, match="transport"):
        apiclient._validated({"method": "GET", "url": "https://x.io", "headers": {"Host": "evil"}})


def test_body_on_a_read_method_is_refused() -> None:
    with pytest.raises(ApiRequestError, match="body"):
        apiclient._validated({"method": "GET", "url": "https://x.io", "body": "x"})


def test_unknown_method_is_refused() -> None:
    with pytest.raises(ApiRequestError, match="method"):
        apiclient._validated({"method": "TRACE", "url": "https://x.io"})


# --- Der Runner: Tuer, Sprungpruefung, Antwortform ------------------------------------

def test_get_returns_status_and_bounded_text() -> None:
    calls: list = []
    runner = _runner(calls, [_FakeResponse(200, b'{"ok": true}')])
    out = runner(_req(method="GET", url="https://api.example.com/x"))
    assert out.startswith("HTTP 200 GET https://api.example.com/x")
    assert '{"ok": true}' in out
    assert calls[0][1]["pin"] == (PUBLIC_IP,)


def test_internal_addresses_never_pass() -> None:
    calls: list = []
    runner = _runner(calls, [_FakeResponse(200)])
    with pytest.raises(UrlRefusedError):
        runner(_req(method="GET", url="http://192.168.1.1/internal"))
    with pytest.raises(UrlRefusedError):
        runner(_req(method="GET", url="http://100.64.0.1/tailnet"))
    assert not calls


def test_redirect_is_re_guarded() -> None:
    calls: list = []
    runner = _runner(calls, [
        _FakeResponse(302, headers={"Location": "http://169.254.169.254/latest"}),
        _FakeResponse(200),
    ])
    with pytest.raises(UrlRefusedError):
        runner(_req(method="GET", url="https://api.example.com/x"))


def test_mutating_redirect_is_reported_not_followed() -> None:
    calls: list = []
    runner = _runner(calls, [
        _FakeResponse(302, headers={"Location": "https://anderes.example.com/x"}),
    ])
    out = runner(_req(method="POST", url="https://api.example.com/x", body="{}"))
    assert "NOT followed" in out
    assert len(calls) == 1
