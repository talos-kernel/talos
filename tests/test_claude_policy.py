import re
from talos import policy

def test_job_workspace_is_kernel_derived_and_sanitized(monkeypatch):
    monkeypatch.setenv("TALOS_CLAUDE_WORKER_ROOT", "/tmp/claude-root")
    ws = policy.claude_job_workspace("abc123")
    assert ws == "/tmp/claude-root/job-abc123"
    hostile = policy.claude_job_workspace("../../etc/passwd")
    assert hostile.startswith("/tmp/claude-root/job-")
    assert ".." not in hostile.split("job-")[1]
    assert "/" not in hostile.split("job-")[1]

def test_work_root_defaults_under_workspace(monkeypatch):
    monkeypatch.delenv("TALOS_CLAUDE_WORKER_ROOT", raising=False)
    root = policy.claude_work_root()
    assert root.endswith("claude-jobs")
    assert root.startswith(str(policy.WORKSPACE_DIR))

def test_delegate_tools_have_extractors():
    assert "delegate_code" in policy.TARGET_EXTRACTORS
    assert "delegate_status" in policy.TARGET_EXTRACTORS
    (target,) = policy.TARGET_EXTRACTORS["delegate_code"]({"prompt": "x"})
    assert target == policy.claude_work_root()
    assert policy.TARGET_EXTRACTORS["delegate_status"]({"job_id": "j1"}) == ()
