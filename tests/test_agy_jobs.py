"""Das agy-Backend des Claude-Workers: zweiter Motor hinter derselben Grenze.

Spiegelt tests/test_claude_jobs.py: Agent-Seite (delegate_agy-Runner,
Manifest-Gate) und Worker-Seite (backend-Frame, agy-Gate, Token-Staging,
Strom-Parsing). Kein Test startet ein echtes `agy` — der Worker bekommt wie
beim claude-Backend ein `spawn`-Double injiziert, das konservierte
stream-json-Zeilen in der agy-Form liefert (`{"event": …}` statt
`{"type": …}`). Die teuersten Fehler stehen zuerst: ein Job, der bei RC 0
trotzdem gescheitert ist (der Auth-Fall — der Returncode luegt), und ein
Worker-agy-HOME, das in Env oder argv des Kindes auftaucht.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import time
from pathlib import Path

from talos import claudejobs, claudeworker, policy, reasoner, tools
from talos.channel import Principal
from talos.policy import ToolRequest
from talos.sandbox import SandboxLimits

OWNER = Principal("telegram", "100000001")


def _req(tool, args):
    return ToolRequest(tool, OWNER, dict(args))


# --- Agent-Seite -----------------------------------------------------------


def test_delegate_agy_is_a_first_class_tool():
    manifest = tools.default_manifest()
    spec = {t.name: t for t in manifest.tools}["delegate_agy"]
    assert spec.effect.name == "EXEC" and spec.sandbox_required
    assert spec.requires_env == frozenset({"TALOS_CLAUDE_WORKER_SOCKET"})
    assert "delegate_agy" in policy.TARGET_EXTRACTORS
    assert any(line.startswith("- delegate_agy ") for line in reasoner.TOOL_PROTOCOL.splitlines())


def test_delegate_agy_stays_out_of_the_manifest_without_the_gate():
    """Zwei-Gate-Muster wie bei den MCP-Servern: ohne TALOS_AGY_BACKEND=1
    existiert das Werkzeug im Agenten gar nicht."""
    namen = {t.name for t in tools.default_manifest(agy_backend=False).tools}
    assert "delegate_agy" not in namen
    assert "delegate_code" in namen          # der claude-Weg bleibt unangetastet


class FakeExchange:
    def __init__(self, replies): self.replies, self.sent = replies, []

    def __call__(self, path, frame, deadline):
        self.sent.append(frame)
        return self.replies.pop(0)


def test_delegate_agy_submits_and_returns_job():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    run = tools.make_delegate_agy_runner(socket_path="/s/c.sock",
                                         work_root="/tmp/root", exchange=fx)
    out = run(_req("delegate_agy", {"prompt": "add a README note"}))
    assert "job_id" in out
    sent = json.loads(fx.sent[0])
    assert sent["op"] == "submit" and sent["prompt"] == "add a README note"
    assert sent["backend"] == "agy"
    assert sent["workspace"].startswith("/tmp/root/job-")   # kernel-derived
    # Kein MCP/Browser im agy-Frame — das ist dem claude-Backend vorbehalten.
    assert "browser_mcp" not in sent and "mcp_servers" not in sent


def test_delegate_agy_fail_closed_when_worker_down():
    def down(path, frame, deadline): raise OSError("no such socket")
    run = tools.make_delegate_agy_runner(socket_path="/s/c.sock",
                                         work_root="/tmp/root", exchange=down)
    out = run(_req("delegate_agy", {"prompt": "x"}))
    assert "unavailable" in out          # named failure, never a silent fallback


def test_submit_job_default_frame_has_no_backend_flag():
    """Byte-Kompatibilitaet: der Vorgabe-Frame (claude) bleibt exakt der alte —
    ein alter Worker sieht nie ein Feld, das er nicht kennt."""
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    claudejobs.submit_job("/s/c.sock", "j1", "p", "/tmp/w", exchange=fx)
    assert "backend" not in json.loads(fx.sent[0])


def test_main_run_registers_the_agy_runner_behind_both_gates():
    """Wie Falle 7 aus `CLAUDE.md` (tests/test_claude_jobs.py): die
    Registrierung steht hinter `config.claude_worker_enabled` UND
    `config.agy_backend` — ein verdrahteter Runner ohne eingeschaltetes
    Backend waere ein stilles Versprechen."""
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
    assert "delegate_agy" in verdrahtet
    rumpf = ast.get_source_segment(quelle, lauf)
    assert "agy_backend" in rumpf and "claude_worker_enabled" in rumpf


# --- Worker-Seite ----------------------------------------------------------


class FakeHandle:
    """Gibt konservierte stream-json-Zeilen her, dann den Returncode."""

    def __init__(self, lines, rc=0):
        self._lines, self._rc = lines, rc

    def events(self):
        for line in self._lines:
            yield line
        return self._rc


def _agy_home(tmp_path):
    """Ein Worker-agy-HOME mit Login — die Form, die das Gate verlangt."""
    home = tmp_path / "agy-home"
    token_dir = home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text("oauth-token-123", encoding="utf-8")
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    return claudeworker.AgyBackend(bin=str(binary), home=str(home))


def _agy_jobs(tmp_path, **kwargs):
    return claudeworker._Jobs(worker_home=str(tmp_path / "claude-home"),
                              agy=_agy_home(tmp_path), **kwargs)


def _frame_submit(jobs, jid, workspace, spawn, *, backend=None, extra=None, timeout_s=30):
    frame = {"op": "submit", "job_id": jid, "prompt": "p",
             "workspace": str(workspace)}
    if backend is not None:
        frame["backend"] = backend
    if extra:
        frame.update(extra)
    return claudeworker.handle_frame(json.dumps(frame).encode(), jobs, spawn=spawn,
                                     limits=SandboxLimits(timeout_s=timeout_s))


def _warte_auf(jobs, jid, zustaende, sekunden=5.0):
    ende = time.monotonic() + sekunden
    s = {}
    while time.monotonic() < ende:
        s = claudeworker.handle_frame(
            json.dumps({"op": "status", "job_id": jid}).encode(), jobs)
        if s.get("state") in zustaende:
            return s
        time.sleep(0.05)
    return s


def _agy_ok_stream(ws):
    """Die gemessene agy-Form: `event` statt `type`, `result` als Objekt."""
    return [
        {"event": "assistant", "text": "working"},
        {"event": "tool_use", "input": {"file_path": str(ws / "note.md")}},
        {"event": "result", "result": {
            "conversation_id": "c1", "status": "OK",
            "response": "created note.md", "error": "",
            "duration_seconds": 1, "num_turns": 1,
            "usage": {"input_tokens": 10}}},
    ]


def test_agy_submit_then_status_done(tmp_path):
    jobs = _agy_jobs(tmp_path)
    ws = tmp_path / "job-agy1"
    r1 = _frame_submit(jobs, "agy1", ws, lambda a, c, e, l: FakeHandle(_agy_ok_stream(ws)),
                       backend="agy")
    assert r1 == {"ok": True, "state": "accepted"}
    s = _warte_auf(jobs, "agy1", {"done", "failed"})
    assert s["ok"] is True and s["state"] == "done"
    assert s["backend"] == "agy"
    assert s["summary"] == "created note.md"      # aus result.response
    assert s["files"] == ["note.md"]
    assert s["returncode"] == 0
    # Der Token wurde NUR in das wegwerfbare Job-HOME kopiert, Mode 0600.
    kopie = ws / ".home" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    assert kopie.read_text(encoding="utf-8") == "oauth-token-123"
    assert stat.S_IMODE(kopie.stat().st_mode) == 0o600


def test_agy_stream_error_fails_despite_rc0(tmp_path):
    """Der Auth-Fall, an dem alles haengt: `status: "ERROR"` mit RC 0 — der
    Returncode luegt, der Strom nicht."""
    jobs = _agy_jobs(tmp_path)

    def spawn_auth(argv, cwd, env, limits):
        return FakeHandle([{"event": "result", "result": {
            "conversation_id": "c2", "status": "ERROR", "response": "",
            "error": "authentication failed or timed out",
            "duration_seconds": 0, "num_turns": 0,
            "usage": {"input_tokens": 0}}}], rc=0)

    _frame_submit(jobs, "agy2", tmp_path / "job-agy2", spawn_auth, backend="agy")
    s = _warte_auf(jobs, "agy2", {"failed"})
    assert s["state"] == "failed"
    assert s["backend"] == "agy"
    assert "authentication failed" in s["error"]


def test_agy_non_ok_status_without_error_text_fails(tmp_path):
    jobs = _agy_jobs(tmp_path)

    def spawn_cancel(argv, cwd, env, limits):
        return FakeHandle([{"event": "result", "result": {
            "status": "CANCELLED", "response": "", "error": ""}}], rc=0)

    _frame_submit(jobs, "agy3", tmp_path / "job-agy3", spawn_cancel, backend="agy")
    s = _warte_auf(jobs, "agy3", {"failed"})
    assert s["state"] == "failed" and "CANCELLED" in s["error"]


def test_agy_malformed_stream_events_are_ignored(tmp_path):
    """Kaputte oder fremde Events sind kein Beweis: sie fallen weg, und wo
    nichts passt, bleibt die Datei-Liste leer — Belege duerfen fehlen, sie
    werden nie erfunden."""
    jobs = _agy_jobs(tmp_path)
    ws = tmp_path / "job-agy4"
    lines = [
        {"event": "something-unknown", "payload": 1},
        {"foo": "bar"},
        {"event": "tool_use"},                                  # kein input
        {"event": "tool_use", "input": {"file_path": "/etc/passwd"}},  # ausserhalb
        {"event": "result", "result": {"status": "DONE", "response": "ok", "error": ""}},
    ]
    _frame_submit(jobs, "agy4", ws, lambda a, c, e, l: FakeHandle(lines), backend="agy")
    s = _warte_auf(jobs, "agy4", {"done", "failed"})
    assert s["state"] == "done"           # DONE zaehlt tolerant als OK
    assert s["summary"] == "ok"
    assert s["files"] == []               # /etc/passwd verworfen, nicht umgeschrieben


def test_unknown_backend_rejected_without_spawn(tmp_path):
    aufgerufen = []

    def spion(argv, cwd, env, limits):
        aufgerufen.append(argv)
        return FakeHandle([])

    jobs = _agy_jobs(tmp_path)
    r = _frame_submit(jobs, "bad1", tmp_path / "job-bad1", spion, backend="kronos")
    assert r["ok"] is False and r["kind"] == "invalid_request"
    assert "kronos" in r["message"] and not aufgerufen
    r2 = _frame_submit(jobs, "bad2", tmp_path / "job-bad2", spion, backend=5)
    assert r2["ok"] is False and r2["kind"] == "invalid_request" and not aufgerufen


def test_agy_submit_without_gate_is_unavailable_without_spawn(tmp_path):
    aufgerufen = []

    def spion(argv, cwd, env, limits):
        aufgerufen.append(argv)
        return FakeHandle([])

    jobs = claudeworker._Jobs(worker_home=str(tmp_path))   # kein agy konfiguriert
    r = _frame_submit(jobs, "nog1", tmp_path / "job-nog1", spion, backend="agy")
    assert r["ok"] is False and r["kind"] == "unavailable"
    assert "not configured" in r["message"] and not aufgerufen


def test_agy_submit_without_token_is_unavailable_without_spawn(tmp_path):
    aufgerufen = []

    def spion(argv, cwd, env, limits):
        aufgerufen.append(argv)
        return FakeHandle([])

    home = tmp_path / "agy-home"
    home.mkdir()                                          # kein Token darin
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    jobs = claudeworker._Jobs(worker_home=str(tmp_path),
                              agy=claudeworker.AgyBackend(bin=str(binary), home=str(home)))
    r = _frame_submit(jobs, "notok1", tmp_path / "job-notok1", spion, backend="agy")
    assert r["ok"] is False and r["kind"] == "unavailable"
    assert "oauth token" in r["message"] and not aufgerufen


def test_agy_with_mcp_or_browser_is_invalid_request(tmp_path):
    aufgerufen = []

    def spion(argv, cwd, env, limits):
        aufgerufen.append(argv)
        return FakeHandle([])

    jobs = _agy_jobs(tmp_path)
    r = _frame_submit(jobs, "mcp1", tmp_path / "job-mcp1", spion, backend="agy",
                      extra={"mcp_servers": ["filesystem"]})
    assert r["ok"] is False and r["kind"] == "invalid_request"
    assert "claude backend" in r["message"] and not aufgerufen
    r2 = _frame_submit(jobs, "mcp2", tmp_path / "job-mcp2", spion, backend="agy",
                       extra={"browser_mcp": True})
    assert r2["ok"] is False and r2["kind"] == "invalid_request" and not aufgerufen


def test_agy_worker_home_leaks_into_neither_env_nor_argv(tmp_path):
    gesehen = {}

    def spion(argv, cwd, env, limits):
        gesehen["argv"], gesehen["env"] = list(argv), dict(env)
        return FakeHandle([{"event": "result", "result": {
            "status": "OK", "response": "ok", "error": ""}}])

    jobs = _agy_jobs(tmp_path)
    ws = tmp_path / "job-leak"
    _frame_submit(jobs, "leak", ws, spion, backend="agy", timeout_s=30)
    s = _warte_auf(jobs, "leak", {"done", "failed"})
    assert s["state"] == "done"
    agy_home = str(tmp_path / "agy-home")
    assert agy_home not in json.dumps(gesehen["env"])
    assert agy_home not in json.dumps(gesehen["argv"])
    assert gesehen["argv"] == ["-p", "p", "--output-format", "stream-json",
                               "--dangerously-skip-permissions",
                               "--print-timeout", "30s"]
    # Kein Claude-OAuth in der Env eines agy-Jobs — die Credential liegt als
    # Datei-Kopie im Job-HOME, die andere gehoert hier nicht hinein.
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in gesehen["env"]
    assert gesehen["env"]["HOME"] == str(ws / ".home")


def test_claude_jobs_report_the_claude_backend(tmp_path):
    """Abwaertskompatibel: ein Frame ohne `backend` laeuft wie bisher — und
    die Status-Antwort sagt ehrlich, welcher Motor den Job gefahren hat."""
    jobs = _agy_jobs(tmp_path)
    ws = tmp_path / "job-cl1"
    lines = [{"type": "result", "result": "claude did it"}]
    _frame_submit(jobs, "cl1", ws, lambda a, c, e, l: FakeHandle(lines))
    s = _warte_auf(jobs, "cl1", {"done", "failed"})
    assert s["state"] == "done"
    assert s["backend"] == "claude"
    assert s["summary"] == "claude did it"


def test_agy_gate_requires_existing_absolute_paths(tmp_path):
    assert claudeworker._agy_gate("", "") is None
    assert claudeworker._agy_gate("/usr/local/bin/agy", "") is None
    assert claudeworker._agy_gate("agy", str(tmp_path)) is None          # nicht absolut
    assert claudeworker._agy_gate(str(tmp_path / "fehlt"), str(tmp_path)) is None
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    gate = claudeworker._agy_gate(str(binary), str(tmp_path))
    assert gate is not None and gate.bin == str(binary) and gate.home == str(tmp_path)


def test_stream_failure_tolerant_reading():
    """OK-Menge und Fehlerfeld, Grossschreibung egal — der Live-E2E bestaetigt
    die genaue Schreibweise, der Parser darf daran nicht haengen."""
    ok = {"event": "result", "result": {"status": "ok", "response": "x", "error": ""}}
    assert claudeworker.stream_failure(ok) is None
    erfolg = {"event": "result", "result": {"status": "SUCCESS", "response": "x"}}
    assert claudeworker.stream_failure(erfolg) is None
    fehler = {"event": "result", "result": {"status": "ERROR", "error": " boom "}}
    assert claudeworker.stream_failure(fehler) == "boom"
    # Claude-Result-Events (Text) sind hier bewusst kein Misserfolg — ihr
    # Fehlschlag bleibt der Returncode wie bisher.
    assert claudeworker.stream_failure({"type": "result", "result": "done"}) is None
    assert claudeworker.stream_failure({"event": "assistant"}) is None


def test_production_path_builds_the_spawn_per_backend(tmp_path, monkeypatch):
    """Der Produktionsweg injiziert KEINEN Spawn: handle_frame muss ihn pro
    Backend selbst bauen — die agy-Argv darf nie an der claude-Binary landen
    (gemessener Live-E2E-Befund: claude antwortete "unknown option
    '--print-timeout'", weil serve() genau einen Spawn vorgebaut hatte)."""
    jobs = _agy_jobs(tmp_path)
    agy = jobs._agy
    claude_marker = str(tmp_path / "claude")
    monkeypatch.setenv("TALOS_CLAUDE_WORKER_BIN", claude_marker)
    lief_ueber: list[str] = []

    def fake_make_spawn(binary, **_kwargs):
        def spawn(_argv, _cwd, _env, _limits):
            lief_ueber.append(binary)
            return FakeHandle(_agy_ok_stream(tmp_path / "x"))
        return spawn

    monkeypatch.setattr(claudeworker, "make_spawn", fake_make_spawn)
    r1 = _frame_submit(jobs, "prod-agy", tmp_path / "job-p1", None, backend="agy")
    assert r1 == {"ok": True, "state": "accepted"}
    assert _warte_auf(jobs, "prod-agy", {"done"})["state"] == "done"
    r2 = _frame_submit(jobs, "prod-claude", tmp_path / "job-p2", None)
    assert r2 == {"ok": True, "state": "accepted"}
    _warte_auf(jobs, "prod-claude", {"done", "failed"})
    assert lief_ueber[0] == agy.bin            # agy-Job -> agy-Binary
    assert lief_ueber[1] == claude_marker      # claude-Job -> claude-Binary


def test_agy_real_tool_shape_yields_file_evidence(tmp_path):
    """Die am Live-Lauf gemessene agy-Form: `step_update` mit
    `step_type: "tool"`, Pfad in `tool_info.parameters.TargetFile`
    (Grossschreibung genau so). Innen -> Beleg, aussen -> verworfen,
    Nicht-Pfad-Werte (Kommandos) -> kein Beleg."""
    ws = tmp_path / "ws"
    inside = {"event": "step_update", "step_update": {
        "step_type": "tool", "tool_name": "write_to_file",
        "tool_info": {"name": "write_to_file",
                      "parameters": {"TargetFile": str(ws / "agy-e2e.txt")}}}}
    assert claudeworker.parse_stream_event(inside, ws) == (None, "agy-e2e.txt")
    outside = {"event": "step_update", "step_update": {
        "step_type": "tool", "tool_name": "write_to_file",
        "tool_info": {"name": "write_to_file",
                      "parameters": {"TargetFile": "/etc/passwd"}}}}
    assert claudeworker.parse_stream_event(outside, ws) == (None, None)
    kommando = {"event": "step_update", "step_update": {
        "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command",
                      "parameters": {"CommandLine": "rm -rf /"}}}}
    assert claudeworker.parse_stream_event(kommando, ws) == (None, None)
    kein_tool = {"event": "step_update", "step_update": {
        "step_type": "agent_response", "text_delta": "text"}}
    assert claudeworker.parse_stream_event(kein_tool, ws) == (None, None)
