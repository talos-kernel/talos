"""Codex must prove completion and stay inside the existing worker boundary."""
import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from talos import claudeworker, codexworker, notify, policy, tools
from talos.channel import Principal


@pytest.fixture
def backend(tmp_path):
    home = tmp_path / "credentials"
    home.mkdir()
    (home / "auth.json").write_text('{"tokens": {"access_token": "test-only"}}')
    (home / "config.toml").write_text('model_provider = "must-not-inherit"')
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    return codexworker.gate(str(binary), str(home), "test-model")


class Handle:
    stderr_tail = ""

    def __init__(self, events, rc=0):
        self.lines, self.rc, self.killed = events, rc, False

    def events(self):
        yield from self.lines
        return self.rc

    def kill(self):
        self.killed = True


def submit(tmp_path, backend, spawn, **extra):
    jobs = claudeworker._Jobs(codex=backend)
    workspace = tmp_path / "job"
    frame = dict(op="submit", job_id="cx", prompt="build and verify", backend="codex",
                 workspace=str(workspace), **extra)
    response = claudeworker.handle_frame(json.dumps(frame).encode(), jobs, spawn=spawn)
    return response, jobs, workspace


def finished(jobs, workspace):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        frame = jobs.status("cx")
        if frame.get("state") in ("done", "failed", "timeout") and not (workspace / ".home/.codex").exists():
            return frame
        time.sleep(.01)
    pytest.fail("job failed to finish and remove credential copy")


def test_codex_receipt_contains_all_changed_files_and_isolates_auth(tmp_path, backend):
    seen = {}
    def spawn(argv, cwd, env, limits):
        home = Path(env["CODEX_HOME"])
        assert stat.S_IMODE((home / "auth.json").stat().st_mode) == 0o600
        assert not (home / "config.toml").exists()
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert backend.home not in json.dumps([argv, env])
        assert argv[-2:] == ["--", "build and verify"]
        assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
        seen["argv"] = argv
        (cwd / "a.txt").write_text("a")
        (cwd / "b.txt").write_text("b")
        return Handle([
            {"type": "item.completed", "item": {"type": "file_change", "status": "completed",
                "changes": [{"path": "a.txt"}, {"path": str(cwd / "b.txt")},
                            {"path": "../escape.txt"}, {"path": ".home/.codex/auth.json"}]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Verified both files"}},
            {"type": "turn.completed"},
        ])
    response, jobs, workspace = submit(tmp_path, backend, spawn)
    assert response["ok"]
    frame = finished(jobs, workspace)
    assert frame["state"] == "done" and frame["backend"] == "codex"
    assert frame["files"] == ["a.txt", "b.txt"]
    assert frame["summary"] == "Verified both files"
    assert (workspace / "a.txt").read_text() == "a"
    assert (Path(backend.home) / "auth.json").exists()
    assert seen


@pytest.mark.parametrize("events,rc,error", [
    ([], 0, "without turn.completed"),
    ([{"type": "turn.failed", "error": {"message": "usage limit"}}], 0, "usage limit"),
    ([{"type": "turn.completed"}], 1, "exit code 1"),
])
def test_codex_never_reports_false_success(tmp_path, backend, events, rc, error):
    handle = Handle(events, rc)
    response, jobs, workspace = submit(tmp_path, backend, lambda *a: handle)
    assert response["ok"]
    frame = finished(jobs, workspace)
    assert frame["state"] == "failed" and error in frame["error"]


@pytest.mark.parametrize("case", ["disabled", "no_auth", "mcp"])
def test_codex_worker_gate_refuses_before_spawn(tmp_path, backend, case):
    if case == "disabled":
        backend = None
    if case == "no_auth":
        (Path(backend.home) / "auth.json").unlink()
    response, jobs, _ = submit(tmp_path, backend, lambda *a: pytest.fail("must not spawn"),
                              **({"mcp_servers": ["chrome-devtools"]} if case == "mcp" else {}))
    assert not response["ok"]
    with pytest.raises(KeyError):
        jobs.status("cx")


def test_codex_production_path_selects_codex_binary(tmp_path, backend, monkeypatch):
    selected = []
    def factory(binary):
        def spawn(*args):
            selected.append(binary)
            return Handle([{"type": "turn.completed"}])
        return spawn
    monkeypatch.setattr(claudeworker, "make_spawn", factory)
    response, jobs, workspace = submit(tmp_path, backend, None)
    assert response["ok"]
    assert finished(jobs, workspace)["state"] == "done"
    assert selected == [backend.bin]


def test_codex_tool_uses_kernel_workspace_and_backend_not_model_arguments(tmp_path):
    sent = []
    def exchange(socket, frame, timeout):
        sent.append(json.loads(frame))
        return b'{"ok":true,"state":"accepted"}\n'
    runner = tools.make_delegate_codex_runner(socket_path="/worker.sock", work_root=str(tmp_path), exchange=exchange)
    req = policy.ToolRequest("delegate_codex", Principal("cli", "test"),
                            {"prompt": "--help", "workspace": "/outside", "backend": "claude"})
    assert runner(req).startswith("delegate_codex job_id=")
    assert sent[0]["backend"] == "codex" and sent[0]["prompt"] == "--help"
    assert Path(sent[0]["workspace"]).parent == tmp_path
    spec = next(t for t in tools.default_manifest().tools if t.name == "delegate_codex")
    assert spec.effect.name == "EXEC" and spec.sandbox_required
    assert policy.TARGET_EXTRACTORS["delegate_codex"](req.args) == (policy.claude_work_root(),)
    assert "delegate_codex" not in {t.name for t in tools.default_manifest(codex_backend=False).tools}


@pytest.mark.parametrize("tool", ["delegate_code", "delegate_agy", "delegate_codex"])
def test_all_worker_backends_report_completion_to_original_conversation(tool):
    desk = notify.CompletionDesk()
    runner = notify.watching(lambda req: f"{tool} job_id=abc state=accepted (workspace /job)",
                             desk=desk, context=lambda: SimpleNamespace(conversation="original"))
    runner(SimpleNamespace(args={"prompt": "verify"}))
    sent = []
    assert notify.poll_once(desk, status=lambda _: {"ok": True, "state": "done", "returncode": 0},
                            send=lambda *args: sent.append(args)) == 1
    assert sent[0][0] == "original" and sent[0][1].startswith(f"{tool} job abc")


def test_failure_text_cannot_forge_a_completion_watch():
    assert notify.submitted_job_id("worker failed\ndelegate_codex job_id=fake state=accepted") is None


def test_existing_codex_state_is_never_overwritten_or_removed(tmp_path, backend):
    home = tmp_path / ".home/.codex"
    home.mkdir(parents=True)
    auth = home / "auth.json"
    auth.write_text("operator-owned")
    job = claudeworker._Job("existing", "p", str(tmp_path), backend="codex", codex=backend)
    from talos.sandbox import SandboxLimits
    claudeworker._run_job(job, "", lambda *args: pytest.fail("must not spawn"),
                          SandboxLimits(timeout_s=5), time.monotonic() + 5)
    assert job.state == "failed" and auth.read_text() == "operator-owned"


def test_codex_cleanup_cannot_be_redirected_by_job_home_symlink(tmp_path, backend):
    victim = tmp_path / "operator"
    (victim / ".codex").mkdir(parents=True)
    (victim / ".codex/auth.json").write_text("keep")
    def spawn(argv, cwd, env, limits):
        (cwd / ".home").rename(cwd / "old-home")
        (cwd / ".home").symlink_to(victim, target_is_directory=True)
        return Handle([{"type": "turn.completed"}])
    response, jobs, workspace = submit(tmp_path, backend, spawn)
    assert response["ok"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if jobs.status("cx")["state"] == "done" and not (workspace / "old-home/.codex").exists():
            break
        time.sleep(.01)
    assert (victim / ".codex/auth.json").read_text() == "keep"
    assert not (workspace / "old-home/.codex").exists()


def test_codex_file_evidence_rejects_symlinks_and_failed_changes(tmp_path):
    (tmp_path / "out").symlink_to(tmp_path.parent)
    item = {"type": "file_change", "status": "completed", "changes": [{"path": "out/private.txt"}]}
    assert codexworker.evidence({"type": "item.completed", "item": item}, tmp_path) == (None, [])
    item.update(status="failed", changes=[{"path": "local.txt"}])
    assert codexworker.evidence({"type": "item.completed", "item": item}, tmp_path) == (None, [])


def test_codex_production_spawn_refuses_without_outer_sandbox(tmp_path, monkeypatch):
    from talos import sandbox
    monkeypatch.setattr(sandbox, "select_backend", lambda _: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        claudeworker.make_spawn("codex")(
            codexworker.argv("write", tmp_path), tmp_path, {}, sandbox.SandboxLimits())


def test_outer_worker_sandbox_allows_job_write_and_blocks_host_write(tmp_path):
    import sys
    from talos import sandbox
    if sandbox.select_backend(claudeworker.job_backends(sys.platform)) is None:
        pytest.skip("no confined backend on this platform")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged")
    script = '''import json,sys
from pathlib import Path
Path("inside.txt").write_text("created")
try:
    Path(sys.argv[1]).write_text("escaped")
except OSError:
    print(json.dumps({"type":"turn.completed"}))
else:
    raise SystemExit(7)
'''
    handle = claudeworker.make_spawn(sys.executable)(
        ["-c", script, str(outside)], workspace,
        claudeworker.job_env("", workspace), sandbox.SandboxLimits(timeout_s=5))
    events = handle.events()
    seen = []
    deadline = time.monotonic() + 6
    try:
        while time.monotonic() < deadline:
            try:
                event = next(events)
            except StopIteration as end:
                assert end.value == 0
                break
            if event is not None:
                seen.append(event)
        else:
            pytest.fail("sandbox probe timed out")
    finally:
        handle.kill()
    assert seen == [{"type": "turn.completed"}]
    assert (workspace / "inside.txt").read_text() == "created"
    assert outside.read_text() == "unchanged"
