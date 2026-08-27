# Claude Worker (`delegate_code`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Talos 0.11 gains a persistent Claude Code worker daemon and two gated tools (`delegate_code`, `delegate_status`), plus consolidation of talos-agent.ch into one guarded structure.

**Architecture:** A systemd user daemon (`talos/claudeworker.py`) serves a Unix socket (JSON-lines, one request per connection — the `modelworker.py` pattern, filesystem-permissioned, **no bearer token**). Per accepted job it spawns `claude -p --output-format stream-json` under the existing sandbox backends (bubblewrap/`sandbox-exec`), network on, only the kernel-derived job workspace writable. Talos-side, `delegate_code` is one kernel-gated action with the `run_shell` trust shape (Effect.EXEC, `sandbox_required=True`); `delegate_status` is Effect.READ.

**Tech Stack:** Python 3.13 stdlib only (repo rule), pytest, bubblewrap/`sandbox-exec`, systemd.

**Spec:** `docs/superpowers/specs/2026-08-27-claude-worker-design.md`

## Global Constraints

- The model proposes, the kernel decides. No security logic in `tools.py`; runners stay dumb.
- Job workspace paths are derived by `policy.claude_job_workspace()`, never taken from model arguments (the `frame_output_path` pattern).
- No bearer token: socket access control is filesystem permissioning (`0660` + group), exactly like `modelworker.py`. The worker never imports `config.py` — it reads its own env file ("the worker should know LESS than the agent").
- Child env of a spawned job: positive allowlist only (`PATH`, `HOME` = dedicated worker home, locale, `TMPDIR`/`PWD` into workspace). No Talos secrets, no bridge token, no deployment env.
- Unconfined is refused: worker jobs never honor `TALOS_SANDBOX_ALLOW_UNCONFINED`; `UnconfinedSandbox` is filtered out of backend selection.
- Network ON for job sandboxes (Claude OAuth/API is required — operator-confirmed 2026-08-27); root stays read-only.
- Branch `feature/claude-worker`; never commit to `main`. Commit messages state plainly what each change makes possible (kernel-change rule).
- Tests run with `.venv/bin/python -m pytest tests/ -q` (global python3.13 shadows `tests`). After any fix: target test twice, then suite once.
- `python redteam.py` must stay green; any loosening adds a case.
- Nothing private crosses into public: no hosts, aliases, tokens, vault paths. Public sync only via `scripts/sync-public.sh`.
- Glyphs come from `talos/ux.py` only; machine console strings stay English.
- Model-config invariance: no task may touch model choice, routing, picker, fallbacks or credentials.

## Current numbers (will drift, updated in Task 7)

Tests 1737 · adversarial 166 · `policy.py` 548 lines · tools 19 → **21**.

---

### Task 1: Schema + config keys

**Files:**
- Modify: `talos/schema.py` (KEYS tuple, near `:94-116`)
- Modify: `talos/config.py` (`TalosConfig` dataclass `:141-207`, `_value` loading `:285-368`)
- Test: `tests/test_claude_config.py` (new)

**Interfaces:**
- Produces (used by Tasks 2–4): config fields `claude_worker_enabled: bool`, `claude_worker_socket: str`, `claude_worker_root: str`, `claude_worker_home: str`, `claude_worker_bin: str`, `claude_worker_max_parallel: int`, `claude_worker_job_timeout_s: int` on `TalosConfig`; env names exactly:
  `TALOS_CLAUDE_WORKER_ENABLED` (POLICY, default `"0"`, `_bool01`),
  `TALOS_CLAUDE_WORKER_SOCKET` (POLICY, default `""`),
  `TALOS_CLAUDE_WORKER_ROOT` (POLICY, default `""` → resolved to `WORKSPACE_DIR/"claude-jobs"` by policy),
  `TALOS_CLAUDE_WORKER_HOME` (POLICY, default `""`),
  `TALOS_CLAUDE_WORKER_BIN` (POLICY, default `"claude"`),
  `TALOS_CLAUDE_WORKER_MAX_PARALLEL` (SETTING, default `"2"`, `_positive_int`),
  `TALOS_CLAUDE_WORKER_JOB_TIMEOUT` (SETTING, default `"900"`, `_positive_int`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_config.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_config.py -q`
Expected: FAIL (`AttributeError: claude_worker_enabled` / `KeyError` in BY_NAME)

- [ ] **Step 3: Implement keys + config fields**

In `talos/schema.py` KEYS, beside the agent_consult entries:

```python
    Key("TALOS_CLAUDE_WORKER_ENABLED", POLICY,
        "master switch for the Claude worker delegation tool", default="0",
        validate=_bool01),
    Key("TALOS_CLAUDE_WORKER_SOCKET", POLICY,
        "unix socket of the Claude worker — changing it redirects delegated jobs"),
    Key("TALOS_CLAUDE_WORKER_ROOT", POLICY,
        "root directory for Claude job workspaces — jobs may write only below it"),
    Key("TALOS_CLAUDE_WORKER_HOME", POLICY,
        "dedicated HOME for Claude jobs holding only the Claude OAuth state"),
    Key("TALOS_CLAUDE_WORKER_BIN", POLICY,
        "pinned claude binary path for worker jobs", default="claude"),
    Key("TALOS_CLAUDE_WORKER_MAX_PARALLEL", SETTING,
        "max concurrent Claude worker jobs", default="2", validate=_positive_int),
    Key("TALOS_CLAUDE_WORKER_JOB_TIMEOUT", SETTING,
        "overall deadline per Claude job in seconds", default="900",
        validate=_positive_int),
```

In `talos/config.py`: add the seven fields to `TalosConfig` (types per Interfaces) and load them in `load_config` next to the agent_consult block (`:351-353`), booleans/ints via the existing `_flag`/`_int` helpers used for `shell_needs_human` (check the exact helper names in config.py and mirror them).

- [ ] **Step 4: Run tests (twice)**

Run: `.venv/bin/python -m pytest tests/test_claude_config.py tests/test_schema.py -q` (2×)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add talos/schema.py talos/config.py tests/test_claude_config.py
git commit -m "feat: config keys for the Claude worker (default off)

Makes possible: an operator can point Talos at a Claude worker socket.
Nothing is delegated yet — no tool exists in this commit."
```

---

### Task 2: Kernel-side targets (`policy.py`)

**Files:**
- Modify: `talos/policy.py` (add beside `frame_output_path` `:189` and `TARGET_EXTRACTORS` `:201-271`)
- Test: `tests/test_claude_policy.py` (new)

**Interfaces:**
- Consumes: `config.TalosConfig.claude_worker_root` (Task 1).
- Produces: `policy.claude_work_root() -> str` (env `TALOS_CLAUDE_WORKER_ROOT` or `WORKSPACE_DIR/"claude-jobs"`), `policy.claude_job_workspace(job_id: str) -> str` (sanitized, `job-<safe>` under root, same `_FRAME_SAFE` sanitizing pattern); `TARGET_EXTRACTORS["delegate_code"]` → `(claude_work_root(),)`; `TARGET_EXTRACTORS["delegate_status"]` → `()`.

Kernel-change rule applies (policy.py): branch ✓, redteam addition in Task 6, commit message states blast radius. Keep additions minimal — the 548-line budget is guarded; expect ~+15 lines (number updated in Task 7).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_policy.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_policy.py -q`
Expected: FAIL (`AttributeError: claude_job_workspace`)

- [ ] **Step 3: Implement**

In `talos/policy.py`, directly under `frame_output_path`:

```python
_CLAUDE_JOB_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def claude_work_root() -> str:
    """Root below which every Claude job workspace lives. Read from the
    environment here, not from config.py — a floor that asked config would
    protect the directory only after it had been read (the _config_files rule)."""
    configured = os.environ.get("TALOS_CLAUDE_WORKER_ROOT", "")
    return configured or str(WORKSPACE_DIR / "claude-jobs")


def claude_job_workspace(job_id: str) -> str:
    """The directory a delegated Claude job may write in. Derived by the
    kernel, never taken from the arguments — the model cannot pick where a
    foreign agent's bytes land (the frame_output_path pattern)."""
    safe = _CLAUDE_JOB_SAFE.sub("-", job_id)[:64]
    return str(Path(claude_work_root()) / f"job-{safe}")
```

In `TARGET_EXTRACTORS` add:

```python
    "delegate_code": lambda args: (claude_work_root(),),
    "delegate_status": lambda args: (),
```

- [ ] **Step 4: Run tests (twice)**

Run: `.venv/bin/python -m pytest tests/test_claude_policy.py tests/test_policy_targets.py -q` (2×)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add talos/policy.py tests/test_claude_policy.py
git commit -m "feat(kernel): target extraction for delegate_code/delegate_status

Makes possible: the kernel can classify delegation requests; the job
workspace is kernel-derived, so a model argument can never choose where
a delegated Claude writes. No tool or runner exists yet — deciding on
one would still fail at the manifest."
```

---

### Task 3: The worker daemon (`talos/claudeworker.py`)

**Files:**
- Create: `talos/claudeworker.py`
- Test: `tests/test_claudeworker.py`

**Interfaces:**
- Consumes: `sandbox.default_backends(platform)`, `sandbox.SandboxLimits`, `sandbox.SandboxResult`, `sandbox.SandboxedShell` (sandbox.py `:89-121,:443,:574`); socket fixture pattern from `tests/test_modelworker.py:87-147`.
- Produces (wire protocol, Tasks 4 + 6 rely on this exactly):
  - Request frames (JSON-lines, one request per connection, `\n`-terminated):
    `{"op": "submit", "job_id": str, "prompt": str, "workspace": str}`
    `{"op": "status", "job_id": str}`
  - Responses: `{"ok": true, "state": "accepted"|"running"|"done"|"failed"|"timeout", ...}` or `{"ok": false, "kind": "invalid_request"|"unknown_job"|"busy"|"unavailable", "message": str}`. On `"done"`: extra fields `"summary"` (str, from the stream-json result event), `"files"` (list[str], paths relative to the job workspace, derived from stream-json tool events — never from prose), `"returncode"` (int).
  - `claudeworker.serve(socket_path, env_path, *, environ=None, spawn=None, stop=None)`, `claudeworker.handle_frame(raw: bytes, jobs, *, spawn=None, limits=None) -> dict`, `claudeworker.main()`.
  - `spawn` injection seam: `Spawn = Callable[[list[str], Path, dict[str,str], SandboxLimits], "JobHandle"]` — tests inject a fake emitting canned stream-json lines.

Daemon shape mirrors `modelworker.py` (`serve` `:313`, `_bediene` `:270`, `_lese_zeile` framing `:253`, `main()` `:364`): sequential accept loop, `listen(8)`, `settimeout(0.25)` for stop checks, `os.chmod(socket, 0o660)`, refuse to unlink non-socket files, own tiny env reader, **no `config.py` import**. Constants:

```python
DEFAULT_SOCKET = "/run/talos/claude.sock"
DEFAULT_ENV = "/etc/talos/claude-worker.env"
SOCKET_ENV_VAR = "TALOS_CLAUDE_WORKER_SOCKET"
ENV_FILE_VAR = "TALOS_CLAUDE_WORKER_ENV"
MAX_FRAME_BYTES = 1 << 20          # prompts are bigger than model frames
READ_TIMEOUT_S = 30.0              # per-read timeout on the socket
DEFAULT_JOB_TIMEOUT_S = 900        # overall job deadline (anti-trickle)
MAX_JOB_TIMEOUT_S = 3600
MAX_PARALLEL = 2
MAX_PROMPT_CHARS = 20000
MAX_SUMMARY_CHARS = 6000
MAX_FILES = 200
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claudeworker.py
import json, os, socket, threading, time
from pathlib import Path
import pytest
from talos import claudeworker

class FakeHandle:  # yields canned stream-json lines, then a returncode
    def __init__(self, lines, rc=0, delay=0.0):
        self._lines, self._rc, self._delay = lines, rc, delay
    def events(self):
        for line in self._lines:
            if self._delay: time.sleep(self._delay)
            yield line
        return self._rc

def _spawn_ok(argv, cwd, env, limits):
    return FakeHandle([
        {"type": "assistant", "message": "working"},
        {"type": "tool_use", "name": "Write", "input": {"file_path": str(cwd / "note.md")}},
        {"type": "result", "result": "created note.md"},
    ], rc=0)

def _frame(sock_path, obj):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall(json.dumps(obj).encode() + b"\n")
        s.shutdown(socket.SHUT_WR)
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk: break
            data += chunk
    return json.loads(data)

# fixture `worker(sock_dir, spawn)`: start serve() in a daemon thread
# (mirror tests/test_modelworker.py:97-129), yield socket path.

def test_submit_then_status_done(worker, tmp_path):
    ws = tmp_path / "job-abc"
    r1 = _frame(worker, {"op": "submit", "job_id": "abc", "prompt": "make a note", "workspace": str(ws)})
    assert r1 == {"ok": True, "state": "accepted"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r2 = _frame(worker, {"op": "status", "job_id": "abc"})
        if r2.get("state") == "done": break
        time.sleep(0.05)
    assert r2["ok"] is True and r2["state"] == "done"
    assert r2["summary"] == "created note.md"
    assert r2["files"] == ["note.md"]            # relative, from tool events
    assert r2["returncode"] == 0

def test_unknown_job(worker):
    r = _frame(worker, {"op": "status", "job_id": "nope"})
    assert r["ok"] is False and r["kind"] == "unknown_job"

def test_invalid_frame_rejected(worker):
    r = _frame(worker, {"op": "nuke"})
    assert r["ok"] is False and r["kind"] == "invalid_request"

def test_prompt_cap(worker, tmp_path):
    r = _frame(worker, {"op": "submit", "job_id": "big", "prompt": "x" * 20001, "workspace": str(tmp_path)})
    assert r["ok"] is False and r["kind"] == "invalid_request"

def test_busy_when_parallel_limit_hit(worker_busy):
    # worker_busy: fake spawn blocks; submit MAX_PARALLEL jobs, third refused
    ids = ["a", "b"]
    for jid in ids:
        r = _frame(worker_busy, {"op": "submit", "job_id": jid, "prompt": "p", "workspace": "/tmp/w"})
        assert r["ok"] is True
    r = _frame(worker_busy, {"op": "submit", "job_id": "c", "prompt": "p", "workspace": "/tmp/w"})
    assert r["ok"] is False and r["kind"] == "busy"

def test_job_overall_deadline(worker_slow):
    # worker_slow: FakeHandle delay > job timeout → state becomes "timeout"
    r = _frame(worker_slow, {"op": "submit", "job_id": "slow", "prompt": "p", "workspace": "/tmp/w"})
    assert r["ok"] is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        s = _frame(worker_slow, {"op": "status", "job_id": "slow"})
        if s.get("state") == "timeout": break
        time.sleep(0.1)
    assert s["state"] == "timeout"

def test_spawn_env_contains_no_talos_secrets(recorded_env_worker, monkeypatch):
    monkeypatch.setenv("TALOS_AGENT_CONSULT_TOKEN", "supersecret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "alsasecret")
    _frame(recorded_env_worker, {"op": "submit", "job_id": "env", "prompt": "p", "workspace": "/tmp/w"})
    time.sleep(0.2)
    env = recorded_env_worker.last_env
    assert env["HOME"]                      # dedicated worker home, set
    leaked = [k for k in env if "TALOS" in k or "TELEGRAM" in k or "TOKEN" in k]
    assert leaked == []
    assert "supersecret" not in json.dumps(env) and "alsasecret" not in json.dumps(env)

def test_unconfined_backend_never_selected(monkeypatch):
    monkeypatch.setenv("TALOS_SANDBOX_ALLOW_UNCONFINED", "1")
    backends = claudeworker.job_backends("linux")
    assert all(b.name != "unconfined" for b in backends)
```

Also a parsing unit test: feed `parse_stream_event` the three canned lines and assert `(summary, files)` extraction — summary only from a `result` event, files only from `tool_use` events with paths **inside** the workspace (paths outside are dropped, not rewritten).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_claudeworker.py -q`
Expected: FAIL (`ModuleNotFoundError: talos.claudeworker`)

- [ ] **Step 3: Implement the daemon**

Skeleton (fill out; mirror `modelworker.py` idioms, German private helpers OK per repo style):

```python
"""Persistent Claude Code worker: accepts bounded coding jobs over a local
socket and runs them confined. Access control is the filesystem (socket 0660
+ group), like the model worker — a token would be one more secret to keep
out of a child environment that must not see any."""

import json, os, socket, subprocess, threading, time
from pathlib import Path
from . import sandbox

# constants per plan above

class _Jobs:
    """In-memory job table. Continuity lives in Talos' event log, not here —
    a restarted worker knows nothing, and that is deliberate."""
    # submit(job_id, prompt, workspace, spawn, limits) -> "accepted" | raises _Busy
    # status(job_id) -> dict | raises KeyError
    # each job runs in a daemon thread with an overall deadline (monotonic)

def job_backends(platform):
    """Sandbox backends for jobs: the unconfined backend is filtered out —
    an unconfined foreign agent is not a degradation, it is a different
    product. TALOS_SANDBOX_ALLOW_UNCONFINED does not apply here."""
    return [b for b in sandbox.default_backends(platform) if b.name != "unconfined"]

def job_env(worker_home: str, workspace: Path) -> dict[str, str]:
    """Positive allowlist only. No Talos secret, no bridge token, no
    deployment env may leak into a job — the child-env hardening."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": worker_home, "LANG": "C.UTF-8",
           "TMPDIR": str(workspace), "PWD": str(workspace)}
    return env

def parse_stream_event(line: dict, workspace: Path) -> tuple[str | None, str | None]:
    """(summary, file) from one stream-json line. Summary only from a
    top-level {"type":"result"} event; files only from tool_use inputs whose
    resolved path stays inside the workspace — evidence comes from the
    stream, never from prose, and a claimed path outside the jail is dropped."""
    ...

def _run_job(job, spawn, limits, deadline): ...   # thread body; honors deadline,
    # kills the process group on timeout (sandbox._kill_group pattern)

def handle_frame(raw: bytes, jobs: _Jobs, *, spawn=None, limits=None) -> dict:
    ...  # pure, never raises; unknown ops/fields -> invalid_request

def serve(socket_path=DEFAULT_SOCKET, env_path=DEFAULT_ENV, *, environ=None,
          spawn=None, stop=None): ...   # accept loop per modelworker.serve

def main(): ...   # python -m talos.claudeworker
```

The real `spawn` (production default): build argv
`[bin, "-p", prompt, "--output-format", "stream-json", "--verbose",
 "--allowedTools", "Read,Edit,Write,Bash,Glob,Grep"]` (never
`--dangerously-skip-permissions`), wrap it via the selected job backend's
`argv()` with `allow_network=True` (jobs need the Anthropic API — the one
documented difference from `run_shell`), workspace = job dir, launch with
`subprocess.Popen(..., env=job_env(...), cwd=workspace, start_new_session=True)`
and stream stdout lines into `parse_stream_event`.

- [ ] **Step 4: Run tests (twice)**

Run: `.venv/bin/python -m pytest tests/test_claudeworker.py -q` (2×)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add talos/claudeworker.py tests/test_claudeworker.py
git commit -m "feat: Claude worker daemon (socket, confined jobs, deadlines)

Makes possible: a local service can run claude -p jobs confined to a
job workspace with an overall deadline. Nothing in Talos can reach it
yet — no tool, no client, no wiring."
```

---

### Task 4: Talos-side client, tools, wiring

**Files:**
- Create: `talos/claudejobs.py`
- Modify: `talos/tools.py` (runner factories + `default_manifest()` `:254-313`)
- Modify: `talos/reasoner.py` (`TOOL_PROTOCOL` `:39-58`)
- Modify: `talos/__main__.py` (runner assembly `:301-365`)
- Modify: `talos/ux.py` (EXPRESSIVE glyphs `:132-154`)
- Test: `tests/test_claude_jobs.py` (new)

**Interfaces:**
- Consumes: wire protocol + constants from Task 3; `policy.claude_work_root()`/`claude_job_workspace()` from Task 2; config fields from Task 1; client pattern from `api_reasoner._exchange_via_worker` (`:629-711`, wall-clock deadline, ≤1s recv slices, fail-closed).
- Produces:
  - `claudejobs.submit_job(socket_path: str, job_id: str, prompt: str, workspace: str, *, timeout_s: float = 30.0, exchange: Exchange | None = None) -> dict`
  - `claudejobs.job_status(socket_path: str, job_id: str, *, timeout_s: float = 30.0, exchange: Exchange | None = None) -> dict`
  - `Exchange = Callable[[str, bytes, float], bytes]` (socket path, frame, deadline seconds → response line) — injection seam.
  - `tools.make_delegate_code_runner(*, socket_path, work_root, exchange=None) -> Callable[[ToolRequest], str]`
  - `tools.make_delegate_status_runner(*, socket_path, exchange=None) -> Callable[[ToolRequest], str]`
  - Manifest ToolSpecs: `ToolSpec("delegate_code", Effect.EXEC, reversible=False, requires_env=frozenset({"TALOS_CLAUDE_WORKER_SOCKET"}), sandbox_required=True)`, `ToolSpec("delegate_status", Effect.READ, reversible=True, requires_env=frozenset({"TALOS_CLAUDE_WORKER_SOCKET"}))`
    (check first how `requires_env` is consumed in policy.decide and mirror an existing env-gated tool, e.g. vault/qmd specs).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_jobs.py — agent_consult-style triple assertion first
from talos import claudejobs, policy, reasoner, tools

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
```

`_req` mirrors the ToolRequest construction in `tests/test_agent_consult.py`. Also add the wiring guard per CLAUDE.md trap §7 style: ast/symtable check that `__main__.run()` registers both runners when `config.claude_worker_enabled` (mirror the existing runner-map check in `tests/test_media.py`).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_claude_jobs.py -q`
Expected: FAIL (module/spec missing)

- [ ] **Step 3: Implement**

`talos/claudejobs.py` (compact, fail-closed):

```python
"""Client for the Claude worker socket. Fail-closed like the model-worker
path in api_reasoner: an unreachable worker is a named failure, never a
fallback to running claude in-process."""

import json, os, socket, time, uuid

READ_SLICE_S = 1.0
MAX_LINE = 1 << 20

def _default_exchange(socket_path: str, frame: bytes, timeout_s: float) -> bytes:
    deadline = time.monotonic() + timeout_s          # wall clock, not per-recv
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(max(0.1, deadline - time.monotonic()))
        conn.connect(socket_path)
        conn.sendall(frame + b"\n")
        conn.shutdown(socket.SHUT_WR)
        data = b""
        while not data.endswith(b"\n"):
            rest = deadline - time.monotonic()
            if rest <= 0: raise TimeoutError("claude worker read deadline")
            conn.settimeout(min(READ_SLICE_S, rest))
            chunk = conn.recv(65536)
            if not chunk: break
            data += chunk
            if len(data) > MAX_LINE: raise ValueError("claude worker frame too large")
    return data

def submit_job(socket_path, job_id, prompt, workspace, *, timeout_s=30.0, exchange=None): ...
def job_status(socket_path, job_id, *, timeout_s=30.0, exchange=None): ...
```

In `tools.py`: the two runner factories (dumb — format the reply, name failures, never decide). `delegate_code` generates `job_id = uuid.uuid4().hex[:12]` itself and computes `workspace = policy.claude_job_workspace(job_id)` — wait: runners must not import policy for *decisions*, but deriving the path via the kernel's own function is the established pattern ("the runner calls that same function rather than rebuilding the rule" — grab_frame). Follow exactly that.

Manifest additions in `default_manifest()`; `TOOL_PROTOCOL` lines:

```
- delegate_code {"prompt": "…"} — hand a bounded coding task to the confined Claude worker; returns a job_id
- delegate_status {"job_id": "…"} — read a delegated job's state and result
```

`__main__.py` assembly (beside agent_consult registration):

```python
    **({
        "delegate_code": tools.make_delegate_code_runner(
            socket_path=config.claude_worker_socket,
            work_root=policy.claude_work_root()),
        "delegate_status": tools.make_delegate_status_runner(
            socket_path=config.claude_worker_socket),
    } if config.claude_worker_enabled else {}),
```

`ux.py` EXPRESSIVE: `"delegate_code": "🛠️"` / `"delegate_code": "Delegating code"`, `"delegate_status": "🔍"` / `"delegate_status": "Checking job"`.

- [ ] **Step 4: Run tests, then composition smoke**

Run: `.venv/bin/python -m pytest tests/test_claude_jobs.py tests/test_question_wiring.py tests/test_media.py -q` (target 2×), then `.venv/bin/python -m talos --once` (trap §7: a green suite can miss a broken service)
Expected: PASS; `--once` exits clean

- [ ] **Step 5: Commit**

```bash
git add talos/claudejobs.py talos/tools.py talos/reasoner.py talos/__main__.py talos/ux.py tests/test_claude_jobs.py
git commit -m "feat: delegate_code/delegate_status tools wired through the kernel

Makes possible: an allowed turn can submit a bounded coding job to the
Claude worker and read its state. Gating is unchanged — Effect.EXEC with
sandbox_required, target = kernel-derived worker root; unattended ceilings
tighten as before. Disabled unless TALOS_CLAUDE_WORKER_ENABLED=1."
```

---

### Task 5: Unit, docs, `.env.example`

**Files:**
- Create: `deploy/talos-claude-worker.service`
- Create: `docs/claude-worker.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: env names from Task 1; unit skeleton from `deploy/talos-model.service`; doc skeleton from `docs/model-worker.md` (intro → Protokoll → Installation → Agent-Seite → Fail-closed → Grenzen (ehrlich)).

- [ ] **Step 1: Unit file** — mirror `deploy/talos-model.service`: `Type=simple`, dedicated user `talos-claude`, `RuntimeDirectory=talos`, `RuntimeDirectoryMode=0750`, `Environment=TALOS_CLAUDE_WORKER_SOCKET=/run/talos/claude.sock` + `Environment=TALOS_CLAUDE_WORKER_ENV=/etc/talos/claude-worker.env`, `ExecStart=/usr/bin/env python3 -m talos.claudeworker`, `Restart=on-failure`, hardening identical to the model worker unit (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, etc.) **except** `ProtectHome=read-only` (the worker must read its dedicated HOME with the Claude OAuth state) and **no network blocking** (jobs need the API). Add `ReadWritePaths=` for the configured worker root.

- [ ] **Step 2: `docs/claude-worker.md`** — follow the model-worker doc skeleton exactly. Must state honestly:
  - optional/installable architecture; default off; delegation is one kernel-gated action (run_shell trust shape), what happens inside is bounded by confinement, not per-action gates.
  - network is ON inside job sandboxes (API requirement) — the one difference from `run_shell`.
  - unconfined is refused, `TALOS_SANDBOX_ALLOW_UNCONFINED` does not apply.
  - the dedicated HOME holds only Claude OAuth; no Talos secret reaches a job env.
  - job workspaces are kernel-derived and disposable; continuity lives in the event log.

- [ ] **Step 3: `.env.example`** — add the seven keys, commented out, one-line help each, no values.

- [ ] **Step 4: Verify + commit**

Run: `.venv/bin/python -m pytest tests/ -q -k "schema or config"` (doc/unit changes break nothing), `scripts/check-public-hygiene.py` on the tree.

```bash
git add deploy/talos-claude-worker.service docs/claude-worker.md .env.example
git commit -m "docs: claude worker unit + operator doc + env example"
```

---

### Task 6: Red-team cases

**Files:**
- Modify: `redteam.py` (CASES `:64` + standalone checks near `:2613`)
- Test: `redteam.py` itself

**Interfaces:**
- Consumes: everything above. CASES entries are `(name, ToolRequest, Status)` run against the real kernel with a recording runner (`:52-54,:301-311`).

- [ ] **Step 1: Add cases** (expected statuses against the real kernel, unattended identity where the ceiling matters):

```python
("Delegate code without worker configured",
 ToolRequest("delegate_code", OWNER, {"prompt": "rewrite the repo"}),
 Status.DENIED),            # requires_env unmet → no grant, runner never runs
("delegate_status without worker configured",
 ToolRequest("delegate_status", OWNER, {"job_id": "x"}),
 Status.DENIED),
```

Plus standalone checks (counted via `_result`):
1. **Child-env isolation** — assert `claudeworker.job_env()` output contains no key matching `TALOS|TELEGRAM|TOKEN|SECRET` even when those are set in `os.environ`.
2. **Unconfined refusal** — with `TALOS_SANDBOX_ALLOW_UNCONFINED=1`, `claudeworker.job_backends()` still excludes the unconfined backend.
3. **Workspace escape parse** — `parse_stream_event` drops a `tool_use` path of `/etc/passwd` claimed from inside a job (evidence cannot point outside the jail).
4. **Prompt injection containment** — a `delegate_code` prompt containing `TOOL_CALL: {"tool":"read_file"...}` is opaque text to the kernel: exactly one tool call is decided (the delegation itself), no nested grant.
5. **Extractor honesty** — `delegate_code` targets equal `policy.claude_work_root()`, never a model-supplied path.

- [ ] **Step 2: Run redteam twice, suite once**

Run: `.venv/bin/python redteam.py` (2× green), `.venv/bin/python -m pytest tests/ -q`
Expected: `N/N cases behaved as expected.` with the new N (record it for Task 7)

- [ ] **Step 3: Commit**

```bash
git add redteam.py
git commit -m "redteam: delegate_code containment, child-env, unconfined refusal, evidence jail"
```

---

### Task 7: Website consolidation + number claims

**Files:**
- Modify: `site/index.html` (absorb dossier-unique sections; tools 19→21 with `<code>delegate_code</code>`/`<code>delegate_status</code>`; new numbers)
- Delete: `site/dossier.html`
- Modify: `site/.htaccess` (permanent redirect `dossier.html` → `/#ledger` etc.)
- Modify: `site/console.html`, `site/docs/index.html`, `site/vergleich/index.html` (numbers; vergleich may describe delegate_code generically — no private claims)
- Modify: `README.md` (badges URL-decoded counts + tool list backticks), `CLAUDE.md` (table: tools 21, suites, policy.py lines)
- Modify: `tests/test_site_claims.py` (`EXTRA_PAGES` without dossier; guard **every** remaining page for Tests/Adversarial/Kernel-lines/Tools)
- Modify: `sitemap.xml`

**Interfaces:**
- Consumes: final numbers from Task 6 run (tests collected, redteam N/N, `wc -l talos/policy.py`, 21 tools).

- [ ] **Step 1: Consolidate structure** — move dossier-unique sections (ledger specimens, name provenance, known limits) into `index.html` as anchored sections (`#ledger`, `#myth`, `#limits`); delete `dossier.html`; `.htaccess` 301 redirects; nav updated; `console.html` untouched in purpose (demo), only numbers.
- [ ] **Step 2: Extend the guard** — `tests/test_site_claims.py`: every remaining page under `site/` stating numbers must state Tests/Adversarial/Kernel-lines/Tools; add tool-name `<code>` presence for the two new tools on index; README backtick check auto-covers new tools via manifest.
- [ ] **Step 3: Update all numbers** (measured, not hand-copied): run `.venv/bin/python -m pytest tests/ --collect-only -q`, `.venv/bin/python redteam.py`, `wc -l talos/policy.py`; update README badges (check the URL-decoded form!), CLAUDE.md table, all site pages.
- [ ] **Step 4: Run the guard twice**

Run: `.venv/bin/python -m pytest tests/test_site_claims.py -q` (2×)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add site/ README.md CLAUDE.md tests/test_site_claims.py sitemap.xml
git commit -m "site: one structure, one guard — dossier merged into index, claims guard extended to every page

Makes possible: numbers cannot drift between parallel page copies again;
delegate_code is documented publicly as the confined delegation tool."
```

---

### Task 8: Verification chain + release steps (operator-supervised)

- [ ] Full suite: `.venv/bin/python -m pytest tests/ -q` green; fixed target tests have each run twice.
- [ ] `.venv/bin/python redteam.py` twice green.
- [ ] `scripts/check-public-hygiene.py` green on private tree; sync via `scripts/sync-public.sh` into the separate public clone; hygiene green there too.
- [ ] Pi (operator gates each step): model-config snapshot **pre** → `scripts/deploy-pi.sh --apply` with read-back → snapshot **post** (any diff = stop/rollback) → install/enable `talos-claude-worker.service` with dedicated user + env file (`install -m 0600`) → Pi suite green → **two real `delegate_code` E2Es** (Claude makes a small change in a test workspace; verify via event log `exec.result` + filesystem read-back, never via the answer's prose) → credential-isolation check of the live worker (`/proc/<pid>/environ` shows no Talos/bridge secrets).
- [ ] Vault parity note (private/public/runtime SHAs, numbers, backups). Tarball only from the final commit; signing stays paused for the offline key.

---

## Self-Review Notes

- Spec coverage: worker daemon (T3), two tools + wiring (T4), config (T1), kernel targets (T2), unit/docs (T5), 7 red-team areas (T6 covers all: child-env, trickle, evidence, auth→fs-permissioning, path escape, injection, unconfined), website (T7), verification chain (T8). Spec's bearer-token item was replaced by filesystem permissioning (modelworker pattern) — recorded in the plan header; spec stays as history.
- Type consistency: `submit_job/job_status` (claudejobs) ↔ `make_delegate_*_runner` (tools) ↔ wire protocol (claudeworker) cross-checked; `claude_job_workspace`/`claude_work_root` identical in T2/T4/T6.
- `policy.py` line growth (~+15) is accounted for in T7's number updates.
