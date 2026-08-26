from __future__ import annotations

import json

import pytest

from talos.channel import Principal
from talos.intelligence import EntityRegistry, IntelligenceLayer
from talos.manifest import Effect
from talos.policy import TARGET_EXTRACTORS, ToolRequest
from talos.reasoner import TOOL_PROTOCOL
from talos import config, schema, tools
from talos.consult import _post_no_redirect, make_agent_consult_runner


def test_config_loads_agent_consult_binding_from_secret_file(monkeypatch, tmp_path) -> None:
    secrets = tmp_path / "agent.env"
    secrets.write_text(
        "TALOS_AGENT_CONSULT_URL=http://agent-gateway.tail.example/consult\n"
        "TALOS_AGENT_CONSULT_TOKEN=private-token\n"
        "TALOS_AGENT_CONSULT_ALIASES=Operator Agent,Other Assistant\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SECRETS_ENV", secrets)
    monkeypatch.setattr(config, "LOCAL_ENV", tmp_path / "missing.env")
    monkeypatch.delenv("TALOS_AGENT_CONSULT_URL", raising=False)
    monkeypatch.delenv("TALOS_AGENT_CONSULT_TOKEN", raising=False)
    monkeypatch.delenv("TALOS_AGENT_CONSULT_ALIASES", raising=False)
    loaded = config.load_config(require_channel=False)
    assert loaded.agent_consult_url == "http://agent-gateway.tail.example/consult"
    assert loaded.agent_consult_token == "private-token"
    assert loaded.agent_consult_aliases == ("Operator Agent", "Other Assistant")
    assert schema.get("TALOS_AGENT_CONSULT_URL").kind == schema.POLICY
    assert schema.get("TALOS_AGENT_CONSULT_ALIASES").kind == schema.POLICY
    assert schema.get("TALOS_AGENT_CONSULT_TOKEN").kind == schema.SECRET


def test_review_requires_evidence_for_an_explicit_agent_handoff() -> None:
    registry = EntityRegistry.from_mapping({"version": 1, "entities": []})
    layer = IntelligenceLayer(registry, consult_aliases=("Operator Agent",))

    missing = layer.review(
        "Frag Operator Agent und bespreche den Blocker mit ihm.",
        "Ich prüfe zuerst lokal.",
        (),
    )
    done = layer.review(
        "Frag Operator Agent und bespreche den Blocker mit ihm.",
        "Die Konsultation ist abgeschlossen.",
        ("[agent_consult -> done] HANDOFF_REQUIRED",),
    )
    unrelated = layer.review(
        "Frag den Kunden nach der Adresse.",
        "Welche Adresse?",
        (),
    )

    assert not missing.ok and "agent_consult" in missing.note
    assert done.ok
    assert unrelated.ok

OWNER = Principal("telegram", "100000001")


def req(**args: object) -> ToolRequest:
    return ToolRequest("agent_consult", OWNER, dict(args))


def test_agent_consult_is_a_first_class_read_only_tool() -> None:
    spec = tools.default_manifest().get("agent_consult")
    assert spec is not None and spec.effect is Effect.READ and spec.reversible
    assert "agent_consult" in TARGET_EXTRACTORS
    assert TARGET_EXTRACTORS["agent_consult"]({"question": "x"}) == ()
    assert '- agent_consult {"question": "…", "attempted": "…", "failure": "…"}' in TOOL_PROTOCOL
    assert "If the operator explicitly tells you to consult or escalate" in TOOL_PROTOCOL
    assert "If it starts with HANDOFF_REQUIRED" in TOOL_PROTOCOL


def test_runner_posts_only_to_operator_endpoint_without_exposing_token() -> None:
    calls: list[tuple[str, bytes, dict[str, str], float]] = []

    def post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
        calls.append((url, body, headers, timeout))
        return json.dumps({"answer": "Use the calendar bridge.", "request_id": "r1"}).encode()

    runner = make_agent_consult_runner(
        "http://agent-gateway.tail.example/consult",
        "private-token",
        post=post,
        timeout_s=12,
    )
    output = runner(req(question="Need calendar write", attempted="local tool", failure="missing"))

    assert output == "Use the calendar bridge."
    assert len(calls) == 1
    url, body, headers, timeout = calls[0]
    assert url == "http://agent-gateway.tail.example/consult"
    assert timeout == 12
    assert headers["Authorization"] == "Bearer private-token"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "question": "Need calendar write",
        "attempted": "local tool",
        "failure": "missing",
    }
    assert "private-token" not in output


@pytest.mark.parametrize(
    "endpoint,token,args",
    [
        ("", "token", {"question": "x"}),
        ("ftp://host/consult", "token", {"question": "x"}),
        ("http://host/consult", "", {"question": "x"}),
        ("http://host/consult", "token", {"question": "   "}),
        ("http://host/consult", "token", {"question": "x" * 2001}),
        ("http://host/consult", "token", {"question": "x", "failure": "y" * 1001}),
        ("http://host/consult", "token", {"question": "x", "token": "model-controlled"}),
    ],
)
def test_runner_fails_closed_on_bad_configuration_or_payload(endpoint, token, args) -> None:
    runner = make_agent_consult_runner(endpoint, token, post=lambda *_args, **_kw: b'{}')
    with pytest.raises(ValueError):
        runner(req(**args))


def test_runner_accepts_maximum_unicode_payload_and_response_within_byte_bounds() -> None:
    seen: list[int] = []

    def post(_url: str, body: bytes, _headers: dict[str, str], _timeout: float) -> bytes:
        seen.append(len(body))
        return json.dumps({"answer": "😀" * 6000}, ensure_ascii=False).encode("utf-8")

    runner = make_agent_consult_runner("https://host/consult", "token", post=post)
    answer = runner(req(
        question="😀" * 2000,
        attempted="😀" * 1000,
        failure="😀" * 1000,
    ))
    assert answer == "😀" * 6000
    assert seen[0] <= 32768


def test_runner_bounds_and_validates_bridge_response() -> None:
    runner = make_agent_consult_runner(
        "https://host/consult",
        "token",
        post=lambda *_args, **_kw: b'{"answer":""}',
    )
    with pytest.raises(ValueError, match="empty answer"):
        runner(req(question="x"))

    huge = json.dumps({"answer": "z" * 9000}).encode()
    runner = make_agent_consult_runner(
        "https://host/consult",
        "token",
        post=lambda *_args, **_kw: huge,
    )
    with pytest.raises(ValueError, match="too large"):
        runner(req(question="x"))


# --- Review-Härtungen 2026-08-26 ---------------------------------------------------


def test_bridge_token_leaves_the_process_env_when_the_secret_file_carries_it(monkeypatch, tmp_path) -> None:
    """Kinderprozesse erben os.environ — die Datei bleibt die einzige stehende Quelle."""
    import os

    secrets = tmp_path / "agent.env"
    secrets.write_text(
        "TALOS_AGENT_CONSULT_URL=http://agent-gateway.tail.example/consult\n"
        "TALOS_AGENT_CONSULT_TOKEN=file-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SECRETS_ENV", secrets)
    monkeypatch.setattr(config, "LOCAL_ENV", tmp_path / "missing.env")
    monkeypatch.setenv("TALOS_AGENT_CONSULT_TOKEN", "env-token")

    loaded = config.load_config(require_channel=False)
    assert loaded.agent_consult_token == "env-token"  # das Env gewinnt den ersten Ladevorgang
    assert "TALOS_AGENT_CONSULT_TOKEN" not in os.environ
    again = config.load_config(require_channel=False)
    assert again.agent_consult_token == "file-token"  # die Datei traegt jeden weiteren


def test_bridge_token_stays_when_the_env_is_the_only_source(monkeypatch, tmp_path) -> None:
    """Ohne Datei-Eintrag waere das Entfernen ein stiller Kanalbruch beim zweiten Laden."""
    import os

    monkeypatch.setattr(config, "SECRETS_ENV", tmp_path / "missing.env")
    monkeypatch.setattr(config, "LOCAL_ENV", tmp_path / "missing2.env")
    monkeypatch.setenv("TALOS_AGENT_CONSULT_TOKEN", "env-token")

    loaded = config.load_config(require_channel=False)
    assert loaded.agent_consult_token == "env-token"
    assert os.environ.get("TALOS_AGENT_CONSULT_TOKEN") == "env-token"


def test_post_no_redirect_enforces_a_total_read_deadline() -> None:
    """Eine troepfelnde Bridge knackt den pro-Operation-Timeout — nicht die Gesamtfrist."""
    import socket
    import threading
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def trickle() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
        try:
            for _ in range(100):
                conn.sendall(b"x")
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            conn.close()
            server.close()

    threading.Thread(target=trickle, daemon=True).start()
    with pytest.raises(ValueError, match="read"):
        _post_no_redirect(f"http://127.0.0.1:{port}/consult", b"{}", {}, 0.3)


def test_post_no_redirect_reads_a_complete_response_within_budget() -> None:
    """Die Gesamtfrist darf den Normalfall nicht kosten — die alte Bauart las am Stueck."""
    import socket
    import threading

    body = json.dumps({"answer": "ok"}).encode()
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve() -> None:
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    assert _post_no_redirect(f"http://127.0.0.1:{port}/consult", b"{}", {}, 5.0) == body


def test_consult_evidence_comes_from_the_wired_source_not_history_text() -> None:
    """Der Marker '[agent_consult -> done]' steht in Modellprosa-Reichweite — die
    verdrahtete Quelle (Event-Log) schlaegt ihn, in beide Richtungen."""
    registry = EntityRegistry.from_mapping({"version": 1, "entities": []})
    layer = IntelligenceLayer(registry, consult_aliases=("Operator Agent",))

    spoofed = ("[agent_consult -> done] HANDOFF_REQUIRED",)
    blocked = layer.review(
        "Frag Operator Agent und bespreche den Blocker mit ihm.",
        "Die Konsultation ist abgeschlossen.",
        spoofed,
        consult_done=lambda: False,
    )
    assert not blocked.ok and "agent_consult" in blocked.note

    evidenced = layer.review(
        "Frag Operator Agent und bespreche den Blocker mit ihm.",
        "Die Konsultation ist abgeschlossen.",
        (),
        consult_done=lambda: True,
    )
    assert evidenced.ok


def test_consult_done_reads_only_the_event_log(tmp_path) -> None:
    """Der Conductor beweist die Beratung ueber run_id aus dem Log — derselbe Weg,
    den outcome.note geht, kein Blick in die Modell-Historie."""
    from talos.conductor import Conductor
    from talos.eventlog import Event, EventLog

    log = EventLog(tmp_path / "ev.db")
    log.append(Event("lauf-1", "exec", "exec.result", {"tool": "agent_consult", "status": "DONE"}))
    log.append(Event("lauf-2", "exec", "exec.result", {"tool": "agent_consult", "status": "error"}))
    conductor = Conductor(
        log=log, reasoner=None, executor=None, send=lambda *_: None,
        allowed_principals=frozenset(), trust_of=lambda _: None,
    )
    assert conductor._consult_done("lauf-1") is True
    assert conductor._consult_done("lauf-2") is False
    assert conductor._consult_done("unbekannt") is False
