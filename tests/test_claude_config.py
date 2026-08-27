import os
import pytest
from talos import config, schema

def _load(monkeypatch, env):
    for key in env:
        monkeypatch.setenv(key, env[key])
    return config.load_config(require_channel=False)

def test_claude_worker_keys_declared():
    by_name = schema.BY_NAME
    assert by_name["TALOS_CLAUDE_WORKER_ENABLED"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_SOCKET"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_ROOT"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_HOME"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_BIN"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_MAX_PARALLEL"].kind == schema.SETTING
    assert by_name["TALOS_CLAUDE_WORKER_JOB_TIMEOUT"].kind == schema.SETTING

def test_claude_worker_defaults(monkeypatch):
    cfg = _load(monkeypatch, {})
    assert cfg.claude_worker_enabled is False
    assert cfg.claude_worker_socket == ""
    assert cfg.claude_worker_bin == "claude"
    assert cfg.claude_worker_max_parallel == 2
    assert cfg.claude_worker_job_timeout_s == 900

def test_claude_worker_loaded(monkeypatch):
    cfg = _load(monkeypatch, {
        "TALOS_CLAUDE_WORKER_ENABLED": "1",
        "TALOS_CLAUDE_WORKER_SOCKET": "/tmp/claude.sock",
        "TALOS_CLAUDE_WORKER_HOME": "/srv/claude-home",
        "TALOS_CLAUDE_WORKER_MAX_PARALLEL": "3",
        "TALOS_CLAUDE_WORKER_JOB_TIMEOUT": "120",
    })
    assert cfg.claude_worker_enabled is True
    assert cfg.claude_worker_socket == "/tmp/claude.sock"
    assert cfg.claude_worker_home == "/srv/claude-home"
    assert cfg.claude_worker_max_parallel == 3
    assert cfg.claude_worker_job_timeout_s == 120

def test_policy_keys_not_writable_via_config_set():
    # POLICY/SECRET keys are refused by `config set` even with confirmation.
    assert not schema.BY_NAME["TALOS_CLAUDE_WORKER_ENABLED"].writable
    assert schema.BY_NAME["TALOS_CLAUDE_WORKER_MAX_PARALLEL"].writable
