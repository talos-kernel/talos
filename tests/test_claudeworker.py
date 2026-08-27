"""Der Claude-Worker: Socket-Protokoll, Job-Deckel, Kind-Umgebung.

Die teuersten Fehler stehen zuerst: ein Job-Env, das Talos-Geheimnisse
mitschleppt, ein stilles Weiterlaufen ueber die Gesamt-Deadline hinaus
(Anti-Trickle), und „Beweis"-Dateien aus dem Stream, die ausserhalb des
Arbeitsbereichs zeigen. Kein Test startet ein echtes `claude` — der Worker
bekommt ein `spawn`-Double injiziert, das konservierte stream-json-Zeilen
liefert; der Socket ist ein echter lokaler (kurzes tmp-Verzeichnis, weil
AF_UNIX-Pfade auf ~104 Zeichen begrenzt sind).
"""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from talos import claudeworker


class FakeHandle:
    """Gibt konservierte stream-json-Zeilen her, dann den Returncode."""

    def __init__(self, lines, rc=0, delay=0.0):
        self._lines, self._rc, self._delay = lines, rc, delay

    def events(self):
        for line in self._lines:
            if self._delay:
                time.sleep(self._delay)
            yield line
        return self._rc


def _spawn_ok(argv, cwd, env, limits):
    return FakeHandle([
        {"type": "assistant", "message": "working"},
        {"type": "tool_use", "name": "Write", "input": {"file_path": str(cwd / "note.md")}},
        {"type": "result", "result": "created note.md"},
    ], rc=0)


def _frame(sock_path, obj):
    """Ein nackter Protokoll-Roundtrip: eine Zeile rein, eine Zeile raus."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall(json.dumps(obj).encode() + b"\n")
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            # Der Server darf schon geantwortet und geschlossen haben — die
            # Zeile ist durch "\n" terminiert, EOF ist nur Hoeflichkeit.
            pass
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


@pytest.fixture
def sock_dir(tmp_path):
    """Kurze Socket-Pfade (siehe Modul-Docstring)."""
    pfad = tempfile.mkdtemp(prefix="cw-")
    yield pfad
    shutil.rmtree(pfad, ignore_errors=True)


def _start(sock_dir, tmp_path, spawn, *, extra_env=""):
    """Startet `claudeworker.serve` an einem temporaeren Socket; liefert
    (pfad, stop). `environ={}`: der Worker liest NUR seine Env-Datei."""
    home = tmp_path / "claude-home"
    home.mkdir()
    env_file = tmp_path / "claude-worker.env"
    env_file.write_text(
        f"TALOS_CLAUDE_WORKER_HOME={home}\n" + extra_env, encoding="utf-8"
    )
    sock = Path(sock_dir) / "cw.sock"
    stop = threading.Event()
    thread = threading.Thread(
        target=claudeworker.serve,
        args=(str(sock), str(env_file)),
        kwargs={"environ": {}, "spawn": spawn, "stop": stop},
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if sock.exists():
            break
        time.sleep(0.01)
    else:  # pragma: no cover — waere ein Defekt des Fixtures selbst
        raise RuntimeError("Worker-Socket ist nicht erschienen")
    return str(sock), stop


@pytest.fixture
def worker(tmp_path, sock_dir):
    sock, stop = _start(sock_dir, tmp_path, _spawn_ok)
    yield sock
    stop.set()


@pytest.fixture
def worker_busy(tmp_path, sock_dir):
    """Spawn blockiert: die ersten MAX_PARALLEL Jobs bleiben laufend belegt."""
    def spawn_blocking(argv, cwd, env, limits):
        return FakeHandle(
            [{"type": "assistant", "message": "…"}] * 50, rc=0, delay=0.5
        )
    sock, stop = _start(sock_dir, tmp_path, spawn_blocking)
    yield sock
    stop.set()


@pytest.fixture
def worker_slow(tmp_path, sock_dir):
    """Job-Timeout 1s, Handle tropfelt alle 0.5s — die Deadline muss gewinnen."""
    def spawn_slow(argv, cwd, env, limits):
        return FakeHandle(
            [{"type": "assistant", "message": "…"}] * 10, rc=0, delay=0.5
        )
    sock, stop = _start(
        sock_dir, tmp_path, spawn_slow,
        extra_env="TALOS_CLAUDE_WORKER_JOB_TIMEOUT=1\n",
    )
    yield sock
    stop.set()


class _Recorded(str):
    """Socket-Pfad, an dem das zuletzt vom Job gesehene Env haengt."""

    def __new__(cls, path, store):
        obj = super().__new__(cls, path)
        obj._store = store
        return obj

    @property
    def last_env(self):
        return self._store.get("env")


@pytest.fixture
def recorded_env_worker(tmp_path, sock_dir):
    gesehen: dict[str, dict] = {}

    def spawn_recording(argv, cwd, env, limits):
        gesehen["env"] = dict(env)
        return FakeHandle([{"type": "result", "result": "ok"}], rc=0)

    sock, stop = _start(sock_dir, tmp_path, spawn_recording)
    yield _Recorded(sock, gesehen)
    stop.set()


def test_submit_then_status_done(worker, tmp_path):
    ws = tmp_path / "job-abc"
    r1 = _frame(worker, {"op": "submit", "job_id": "abc", "prompt": "make a note",
                         "workspace": str(ws)})
    assert r1 == {"ok": True, "state": "accepted"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r2 = _frame(worker, {"op": "status", "job_id": "abc"})
        if r2.get("state") == "done":
            break
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
    r = _frame(worker, {"op": "submit", "job_id": "big",
                        "prompt": "x" * 20001, "workspace": str(tmp_path)})
    assert r["ok"] is False and r["kind"] == "invalid_request"


def test_busy_when_parallel_limit_hit(worker_busy):
    ids = ["a", "b"]
    for jid in ids:
        r = _frame(worker_busy, {"op": "submit", "job_id": jid, "prompt": "p",
                                 "workspace": "/tmp/w"})
        assert r["ok"] is True
    r = _frame(worker_busy, {"op": "submit", "job_id": "c", "prompt": "p",
                             "workspace": "/tmp/w"})
    assert r["ok"] is False and r["kind"] == "busy"


def test_job_overall_deadline(worker_slow):
    r = _frame(worker_slow, {"op": "submit", "job_id": "slow", "prompt": "p",
                             "workspace": "/tmp/w"})
    assert r["ok"] is True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        s = _frame(worker_slow, {"op": "status", "job_id": "slow"})
        if s.get("state") == "timeout":
            break
        time.sleep(0.1)
    assert s["state"] == "timeout"


def test_spawn_env_contains_no_talos_secrets(recorded_env_worker, monkeypatch):
    monkeypatch.setenv("TALOS_AGENT_CONSULT_TOKEN", "supersecret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "alsasecret")
    _frame(recorded_env_worker, {"op": "submit", "job_id": "env", "prompt": "p",
                                 "workspace": "/tmp/w"})
    deadline = time.monotonic() + 5
    while recorded_env_worker.last_env is None and time.monotonic() < deadline:
        time.sleep(0.05)
    env = recorded_env_worker.last_env
    assert env is not None
    assert env["HOME"]                      # dedicated worker home, set
    leaked = [k for k in env if "TALOS" in k or "TELEGRAM" in k or "TOKEN" in k]
    assert leaked == []
    assert "supersecret" not in json.dumps(env) and "alsasecret" not in json.dumps(env)


def test_unconfined_backend_never_selected(monkeypatch):
    monkeypatch.setenv("TALOS_SANDBOX_ALLOW_UNCONFINED", "1")
    backends = claudeworker.job_backends("linux")
    assert all(b.name != "unconfined" for b in backends)


def test_parse_stream_event_extracts_summary_and_files(tmp_path):
    """Beweis kommt aus dem Stream, nie aus Prosa: die Summary nur aus einem
    `result`-Event, Dateien nur aus `tool_use`-Inputs — und nur, wenn der
    aufgeloeste Pfad INNERHALB des Arbeitsbereichs bleibt. Ein behaupteter
    Pfad ausserhalb faellt weg, er wird nicht umgeschrieben."""
    ws = tmp_path / "job-x"
    lines = [
        {"type": "assistant", "message": "I wrote /etc/passwd, trust me"},
        {"type": "tool_use", "name": "Write", "input": {"file_path": str(ws / "note.md")}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/passwd"}},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": str(ws / "sub" / "a.py")}},
        {"type": "result", "result": "created note.md"},
    ]
    summary, files = None, []
    for line in lines:
        s, f = claudeworker.parse_stream_event(line, ws)
        if s is not None:
            summary = s
        if f is not None:
            files.append(f)
    assert summary == "created note.md"
    assert files == ["note.md", "sub/a.py"]      # /etc/passwd dropped
