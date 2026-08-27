import ast
import json
from pathlib import Path

from talos import claudejobs, policy, reasoner, tools
from talos.channel import Principal
from talos.policy import ToolRequest

OWNER = Principal("telegram", "100000001")


def _req(tool, args):
    return ToolRequest(tool, OWNER, dict(args))


def test_delegate_code_is_a_first_class_tool():
    manifest = tools.default_manifest()
    spec = {t.name: t for t in manifest.tools}["delegate_code"]
    assert spec.effect.name == "EXEC" and spec.sandbox_required
    assert "delegate_code" in policy.TARGET_EXTRACTORS
    assert any(line.startswith("- delegate_code ") for line in reasoner.TOOL_PROTOCOL.splitlines())
    assert any(line.startswith("- delegate_status ") for line in reasoner.TOOL_PROTOCOL.splitlines())


class FakeExchange:
    def __init__(self, replies): self.replies, self.sent = replies, []

    def __call__(self, path, frame, deadline):
        self.sent.append(frame)
        return self.replies.pop(0)


def test_delegate_code_submits_and_returns_job():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    run = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=fx)
    out = run(_req("delegate_code", {"prompt": "add a README note"}))
    assert "job_id" in out
    sent = json.loads(fx.sent[0])
    assert sent["op"] == "submit" and sent["prompt"] == "add a README note"
    assert sent["workspace"].startswith("/tmp/root/job-")   # kernel-derived


def test_delegate_code_fail_closed_when_worker_down():
    def down(path, frame, deadline): raise OSError("no such socket")
    run = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=down)
    out = run(_req("delegate_code", {"prompt": "x"}))
    assert "unavailable" in out          # named failure, never a silent fallback


def test_delegate_status_reads_worker_state():
    fx = FakeExchange([b'{"ok": true, "state": "done", "summary": "did it", "files": ["a.md"], "returncode": 0}\n'])
    run = tools.make_delegate_status_runner(socket_path="/s/c.sock", exchange=fx)
    out = run(_req("delegate_status", {"job_id": "abc"}))
    assert "done" in out and "did it" in out and "a.md" in out


def test_main_run_registers_both_delegate_runners_behind_the_flag():
    """Falle 7 aus `CLAUDE.md`: ein Werkzeug im Manifest ohne Runner faellt erst
    auf, wenn das Modell es aufruft. Statisch gelesen wie in tests/test_media.py —
    und zusätzlich: die Registrierung steht hinter `config.claude_worker_enabled`,
    denn ein verdrahteter Runner ohne eingeschalteten Worker waere ein stilles
    Versprechen."""
    from talos import __main__ as hauptmodul

    quelle = Path(hauptmodul.__file__).read_text(encoding="utf-8")
    baum = ast.parse(quelle)
    lauf = next(k for k in ast.walk(baum) if isinstance(k, ast.FunctionDef) and k.name == "run")
    verdrahtet = {
        s.value
        for knoten in ast.walk(lauf) if isinstance(knoten, ast.Dict)
        for s in knoten.keys
        if isinstance(s, ast.Constant) and isinstance(s.value, str)
    }
    assert "delegate_code" in verdrahtet
    assert "delegate_status" in verdrahtet
    rumpf = ast.get_source_segment(quelle, lauf)
    assert "claude_worker_enabled" in rumpf


def test_delegate_status_prints_error_for_failed_job():
    fx = FakeExchange([b'{"ok": true, "state": "failed", "returncode": 1, "error": "bwrap: no proc"}\n'])
    run = tools.make_delegate_status_runner(socket_path="/s/c.sock", exchange=fx)
    out = run(_req("delegate_status", {"job_id": "f1"}))
    assert "failed" in out and "bwrap: no proc" in out and "returncode: 1" in out
