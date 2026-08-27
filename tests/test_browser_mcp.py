"""Browser-Automatisierung via Worker-Sandbox (chrome-devtools-mcp).

Die Doktrin: chrome-devtools-mcp ist NIEMALS ein natives Talos-Werkzeug — ein
Klick hat kein ableitbares Ziel und waere im Agent-Loop per Bauart DENY. Er
laeuft als MCP-Server INNERHALB des confined Claude-Jobs; der Kernel gated
weiterhin genau eine Aktion, die Delegation. Diese Tests pruefen die vier
teuren Fehler daran: eine MCP-Konfiguration, die ohne das Flag entsteht, ein
Browser-Flag, das in der Policy-Evidenz fehlt, ein Secret, das ueber das
Job-Env hinaus in den MCP-Server laeuft, und ein Vorgabe-Verhalten, das sich
unter der Hand geaendert hat. Kein Test startet npx, claude oder einen
Browser — der Worker bekommt wie in test_claudeworker ein spawn-Double.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from talos import claudejobs, claudeworker, config, executor, policy, schema


# --- MCP-Konfiguration: reine Struktur, keine Geheimnisse --------------------

def test_mcp_config_default_is_headless_without_chrome_path():
    cfg = claudeworker.browser_mcp_config(claudeworker.BrowserMcp(enabled=True))
    server = cfg["mcpServers"]["chrome-devtools"]
    assert server["command"] == "npx"
    assert server["args"] == ["chrome-devtools-mcp@latest", "--headless=true"]
    # Kein eigenes "env": der Server erbt exakt das minimierte Job-Env — eine
    # zusaetzliche Env-Tabelle waere ein zweiter, ungepruefter Credential-Weg.
    assert "env" not in server


def test_mcp_config_headless_off_and_chrome_path():
    cfg = claudeworker.browser_mcp_config(
        claudeworker.BrowserMcp(enabled=True, headless=False,
                                chrome_path="/usr/bin/chromium"))
    args = cfg["mcpServers"]["chrome-devtools"]["args"]
    assert "--headless=true" not in args
    assert "--executablePath=/usr/bin/chromium" in args


def test_mcp_config_fixed_command_replaces_npx():
    """Auf dem Pi ist der npm-Cache im Job schreibgeschuetzt (Workspace-only):
    ein `npx @latest` pro Job laedt das Paket in jedes wegwerfbare HOME erneut.
    Die fest installierte Binary ersetzt den Wrapper — Flags bleiben gleich."""
    cfg = claudeworker.browser_mcp_config(
        claudeworker.BrowserMcp(enabled=True, chrome_path="/usr/bin/chromium",
                                command="/usr/local/bin/chrome-devtools-mcp"))
    server = cfg["mcpServers"]["chrome-devtools"]
    assert server["command"] == "/usr/local/bin/chrome-devtools-mcp"
    assert "chrome-devtools-mcp@latest" not in server["args"]
    assert server["args"] == ["--headless=true", "--executablePath=/usr/bin/chromium"]
    assert "env" not in server


def test_mcp_config_carries_no_secret_values():
    kanari = "KANARI-oauth-token-123"
    cfg = claudeworker.browser_mcp_config(
        claudeworker.BrowserMcp(enabled=True, chrome_path="/opt/chrome"))
    assert kanari not in json.dumps(cfg)
    # Statisch: die Konfiguration besteht aus genau einem bekannten Server.
    assert set(cfg) == {"mcpServers"}
    assert set(cfg["mcpServers"]) == {"chrome-devtools"}


# --- Job-Protokoll: das Flag steht nur im Frame, wenn es gemeint ist ---------

class FakeExchange:
    def __init__(self, replies): self.replies, self.sent = replies, []

    def __call__(self, path, frame, deadline):
        self.sent.append(frame)
        return self.replies.pop(0)


def test_submit_job_frame_unchanged_by_default():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    claudejobs.submit_job("/s/c.sock", "j1", "p", "/w", exchange=fx)
    sent = json.loads(fx.sent[0])
    assert sent == {"op": "submit", "job_id": "j1", "prompt": "p", "workspace": "/w"}


def test_submit_job_frame_carries_browser_flag_when_requested():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    claudejobs.submit_job("/s/c.sock", "j1", "p", "/w", exchange=fx,
                          browser_mcp=True)
    sent = json.loads(fx.sent[0])
    assert sent["browser_mcp"] is True


# --- Worker: Gate, Ablehnung, Konfigurationsdatei, argv ----------------------

def _spawn_recording(gesehen):
    def spawn(argv, cwd, env, limits):
        gesehen["argv"] = list(argv)
        gesehen["env"] = dict(env)

        class _Handle:
            def events(self):
                yield {"type": "result", "result": "ok"}
                return 0
        return _Handle()
    return spawn


def _submit(jobs, jid, workspace, spawn, **extra):
    from talos.sandbox import SandboxLimits
    rahmen = {"op": "submit", "job_id": jid, "prompt": "p",
              "workspace": str(workspace), **extra}
    return claudeworker.handle_frame(
        json.dumps(rahmen).encode(), jobs, spawn=spawn,
        limits=SandboxLimits(timeout_s=30))


def _warte(jobs, jid, zustand, sekunden=5.0):
    ende = time.monotonic() + sekunden
    s = {}
    while time.monotonic() < ende:
        s = claudeworker.handle_frame(
            json.dumps({"op": "status", "job_id": jid}).encode(), jobs)
        if s.get("state") == zustand:
            return s
        time.sleep(0.05)
    return s


def test_browser_request_rejected_when_worker_gate_off(tmp_path):
    """Fail-closed: ein Frame, der den Browser anfordert, ohne dass der Dienst
    ihn freigeschaltet hat, laeuft NICHT still ohne Browser — er wird benannt
    abgelehnt, weil die Policy-Evidenz des Agenten sonst eine Luege zeigte."""
    jobs = claudeworker._Jobs(worker_home=str(tmp_path))
    r = _submit(jobs, "b0", tmp_path / "job-b0", _spawn_recording({}),
                browser_mcp=True)
    assert r["ok"] is False and r["kind"] == "invalid_request"
    assert "browser" in r["message"]


def test_browser_mcp_field_must_be_boolean(tmp_path):
    jobs = claudeworker._Jobs(worker_home=str(tmp_path),
                              browser=claudeworker.BrowserMcp(enabled=True))
    r = _submit(jobs, "bt", tmp_path / "job-bt", _spawn_recording({}),
                browser_mcp="yes")
    assert r["ok"] is False and r["kind"] == "invalid_request"


def test_browser_job_writes_config_and_extends_argv(tmp_path):
    gesehen: dict = {}
    jobs = claudeworker._Jobs(worker_home=str(tmp_path),
                              browser=claudeworker.BrowserMcp(enabled=True))
    ws = tmp_path / "job-b1"
    r = _submit(jobs, "b1", ws, _spawn_recording(gesehen), browser_mcp=True)
    assert r["ok"] is True
    assert _warte(jobs, "b1", "done")["state"] == "done"
    # Der Worker hat die Konfiguration VOR dem Kind in den Arbeitsbereich
    # geschrieben — das Kind fand sie fertig vor und waehlte ihren Inhalt nie.
    datei = ws / claudeworker.BROWSER_MCP_FILE
    assert datei.is_file()
    cfg = json.loads(datei.read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["chrome-devtools"]["args"][0] == \
        "chrome-devtools-mcp@latest"
    argv = gesehen["argv"]
    assert "--mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(datei)
    erlaubt = argv[argv.index("--allowedTools") + 1]
    assert "mcp__chrome-devtools" in erlaubt
    assert claudeworker.ALLOWED_TOOLS in erlaubt


def test_default_job_has_no_mcp_trace(tmp_path):
    gesehen: dict = {}
    jobs = claudeworker._Jobs(worker_home=str(tmp_path),
                              browser=claudeworker.BrowserMcp(enabled=True))
    ws = tmp_path / "job-n1"
    r = _submit(jobs, "n1", ws, _spawn_recording(gesehen))
    assert r["ok"] is True
    assert _warte(jobs, "n1", "done")["state"] == "done"
    assert "--mcp-config" not in gesehen["argv"]
    assert "mcp__" not in gesehen["argv"][gesehen["argv"].index("--allowedTools") + 1]
    assert not (ws / claudeworker.BROWSER_MCP_FILE).exists()


def test_browser_job_env_identical_to_plain_job_env(tmp_path):
    """Adversarialer Env-Diff: der Browser-Job bekommt exakt dasselbe Env wie
    jeder andere Job — der MCP-Server erbt es unveraendert, und kein Bridge-/
    OAuth-Secret reist ueber diesen Rahmen hinaus."""
    oauth = tmp_path / ".claude"
    oauth.mkdir()
    kanari = "KANARI-oauth-token-456"
    (oauth / "oauth-token").write_text(kanari, encoding="utf-8")
    envs: list[dict] = []

    def spawn(argv, cwd, env, limits):
        envs.append(dict(env))

        class _Handle:
            def events(self):
                yield {"type": "result", "result": "ok"}
                return 0
        return _Handle()

    jobs = claudeworker._Jobs(worker_home=str(tmp_path),
                              browser=claudeworker.BrowserMcp(enabled=True))
    # Derselbe Arbeitsbereich fuer beide: PWD/TMPDIR/HOME leiten sich aus ihm
    # ab — so stehen im Diff NUR echte Env-Unterschiede, keine Pfadfolgen.
    ws = tmp_path / "job-gemeinsam"
    _submit(jobs, "plain", ws, spawn)
    _warte(jobs, "plain", "done")
    _submit(jobs, "browse", ws, spawn, browser_mcp=True)
    _warte(jobs, "browse", "done")
    assert len(envs) == 2
    assert envs[0] == envs[1]                       # kein einziger neuer Schlüssel
    # Der Job-eigene OAuth-Token steht im Env (die EIGENE Credential des Jobs),
    # aber niemals in der MCP-Konfigurationsdatei.
    assert envs[1].get("CLAUDE_CODE_OAUTH_TOKEN") == kanari
    datei = ws / claudeworker.BROWSER_MCP_FILE
    assert kanari not in datei.read_text(encoding="utf-8")


# --- Policy-Sichtbarkeit: das Flag taucht in Entscheidung und Evidenz auf ----

def test_browser_flag_survives_target_derivation_and_audit():
    """Die Delegation bleibt das EINZige Gate: der Extractor leitet unveraendert
    die kernel-eigene Wurzel ab, auch mit Browser-Flag in den Argumenten. Und
    das Flag selbst landet in der exec.intent-Evidenz — `audit_args` schreibt
    ALLE Argument-Schluessel ins Event-Log, ohne dass policy.py es kennen muss."""
    ziele = policy.TARGET_EXTRACTORS["delegate_code"](
        {"prompt": "x", "browser": True})
    assert ziele == (policy.claude_work_root(),)
    protokolliert = executor.audit_args({"prompt": "x", "browser": True})
    assert protokolliert["browser"] == "True"


# --- Config und Schema --------------------------------------------------------

def test_config_default_off_and_opt_in(monkeypatch):
    monkeypatch.delenv("TALOS_BROWSER_MCP_ENABLED", raising=False)
    aus = config.load_config(require_channel=False)
    assert aus.browser_mcp_enabled is False
    monkeypatch.setenv("TALOS_BROWSER_MCP_ENABLED", "1")
    an = config.load_config(require_channel=False)
    assert an.browser_mcp_enabled is True


def test_browser_keys_declared():
    by_name = schema.BY_NAME
    assert by_name["TALOS_BROWSER_MCP_ENABLED"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_BROWSER_MCP"].kind == schema.POLICY
    assert by_name["TALOS_CLAUDE_WORKER_BROWSER_HEADLESS"].kind == schema.SETTING
    assert by_name["TALOS_CLAUDE_WORKER_BROWSER_CHROME"].kind == schema.SETTING
    # POLICY-Schluessel sind ueber `config set` nicht schreibbar — der Schalter
    # gehoert der Installation, nicht dem Lauf.
    assert not by_name["TALOS_BROWSER_MCP_ENABLED"].writable
    assert not by_name["TALOS_CLAUDE_WORKER_BROWSER_MCP"].writable


# --- Worker-Daemon: das Gate kommt aus der Env-Datei --------------------------

@pytest.fixture
def kurzer_sock():
    """AF_UNIX-Pfade sind auf ~104 Zeichen begrenzt — tmp_path ist auf macOS
    laenger. Der Socket geht in ein kurzes Verzeichnis, wie in test_claudeworker."""
    import shutil
    import tempfile
    pfad = tempfile.mkdtemp(prefix="bm-")
    yield str(Path(pfad) / "cw.sock")
    shutil.rmtree(pfad, ignore_errors=True)


def _serve_gate(tmp_path, sock, umgebung, gesehen):
    import threading
    import unittest.mock as mock
    orig = claudeworker._Jobs

    class _Spion(orig):
        def __init__(self, **kwargs):
            gesehen.update(kwargs)
            super().__init__(**kwargs)

    stop = threading.Event()
    stop.set()  # Schleife endet sofort — uns geht es nur um die Konfiguration
    with mock.patch.object(claudeworker, "_Jobs", _Spion):
        claudeworker.serve(sock, str(tmp_path / "fehlt.env"),
                           environ=umgebung, spawn=_spawn_recording({}),
                           stop=stop)
    return gesehen["browser"]


def test_serve_reads_browser_gate_from_env_file(tmp_path, kurzer_sock):
    """Der Dienst liest sein Gate selbst (Vorgabe AUS, headless AN) — kein
    Agenten-Frame kann es von aussen kippen."""
    browser = _serve_gate(tmp_path, kurzer_sock, {
        "TALOS_CLAUDE_WORKER_HOME": str(tmp_path),
        "TALOS_CLAUDE_WORKER_BROWSER_MCP": "1",
        "TALOS_CLAUDE_WORKER_BROWSER_HEADLESS": "0",
        "TALOS_CLAUDE_WORKER_BROWSER_CHROME": "/usr/bin/chromium",
    }, {})
    assert browser.enabled is True
    assert browser.headless is False
    assert browser.chrome_path == "/usr/bin/chromium"


def test_serve_browser_gate_defaults_off(tmp_path, kurzer_sock):
    browser = _serve_gate(tmp_path, kurzer_sock,
                          {"TALOS_CLAUDE_WORKER_HOME": str(tmp_path)}, {})
    assert browser.enabled is False
    assert browser.headless is True
    assert browser.chrome_path == ""
