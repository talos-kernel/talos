"""Generischer MCP-Server-Mechanismus fuer delegate_code-Jobs.

Die Verallgemeinerung des chrome-devtools-Pfads: der Betreiber beschreibt
MCP-Server deklarativ in `data/mcp-servers.json` (operator-owned, fail-closed
wie entities.json), und BEIDE Seiten gates sie getrennt — der Agent ueber
`TALOS_MCP_SERVERS` (Schnittmenge mit der Registry), der Worker ueber
`TALOS_CLAUDE_WORKER_MCP_SERVERS`. Der Frame traegt nur NAMEN, nie command/args:
die ausfuehrbare Wahrheit liegt ausschliesslich in der Registry-Datei und der
Worker-Env. Talos selbst spricht nie MCP — das `claude -p`-Kind im Job tut es.

Die teuren Fehler, die diese Tests bewachen: ein Servername, den niemand
freigeschaltet hat, laeuft trotzdem; ein "env"-Schluessel schmuggelt eine
Credential in die MCP-Konfiguration; die erzeugte Konfiguration enthaelt mehr
Server als angefragt; und der alte Browser-Pfad bricht, weil die
Verallgemeinerung ihn vergaessen hat.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from talos import claudejobs, claudeworker, config, mcpservers, schema, tools
from talos.channel import Principal
from talos.policy import ToolRequest

OWNER = Principal("telegram", "100000001")


def _req(tool, args):
    return ToolRequest(tool, OWNER, dict(args))


# --- Registry-Parser: fail-closed wie entities.json ---------------------------

def _registry_datei(tmp_path, payload):
    pfad = tmp_path / "mcp-servers.json"
    pfad.write_text(json.dumps(payload), encoding="utf-8")
    return pfad


EINTRAG_FS = {
    "name": "filesystem",
    "command": "/usr/bin/fs-mcp",
    "args": ["--root", "/var/lib/talos/claude-jobs"],
    "description": "Dateizugriff, workspace-gerootet",
}


def test_registry_reads_a_valid_file(tmp_path):
    pfad = _registry_datei(tmp_path, {
        "version": 1,
        "servers": [EINTRAG_FS, {
            "name": "chrome-devtools",
            "command": "",
            "package": "chrome-devtools-mcp@latest",
            "args": ["--headless=true"],
        }],
    })
    reg = mcpservers.McpServerRegistry.from_path(pfad)
    assert reg.names() == frozenset({"filesystem", "chrome-devtools"})
    fs = reg.get("filesystem")
    assert fs.command == "/usr/bin/fs-mcp"
    assert fs.args == ("--root", "/var/lib/talos/claude-jobs")
    assert reg.get("unbekannt") is None


def test_registry_missing_file_is_empty(tmp_path):
    reg = mcpservers.McpServerRegistry.from_path(tmp_path / "fehlt.json")
    assert reg.names() == frozenset()


def test_registry_broken_json_is_empty(tmp_path):
    pfad = tmp_path / "mcp-servers.json"
    pfad.write_text("{kaputt", encoding="utf-8")
    assert mcpservers.McpServerRegistry.from_path(pfad).names() == frozenset()


def test_registry_wrong_or_missing_version_is_empty(tmp_path):
    for payload in ({}, {"version": 2, "servers": [EINTRAG_FS]},
                    {"servers": [EINTRAG_FS]}, ["kein-objekt"]):
        pfad = _registry_datei(tmp_path, payload)
        assert mcpservers.McpServerRegistry.from_path(pfad).names() == frozenset()


def test_registry_oversized_file_is_empty(tmp_path):
    pfad = tmp_path / "mcp-servers.json"
    eintraege = [dict(EINTRAG_FS, name=f"server-{i:03d}") for i in range(400)]
    pfad.write_text(json.dumps({"version": 1, "servers": eintraege}), encoding="utf-8")
    assert pfad.stat().st_size > mcpservers.MAX_FILE_BYTES
    assert mcpservers.McpServerRegistry.from_path(pfad).names() == frozenset()


def test_registry_entry_with_env_key_is_discarded(tmp_path):
    """Der harte Credential-Schnitt: ein "env"-Feld waere ein zweiter,
    ungepruefter Weg fuer Geheimnisse in den Job — der ganze Eintrag faellt,
    die uebrigen bleiben lesbar."""
    boese = dict(EINTRAG_FS, name="boese", env={"API_KEY": "klau-mich"})
    pfad = _registry_datei(tmp_path, {"version": 1, "servers": [boese, EINTRAG_FS]})
    reg = mcpservers.McpServerRegistry.from_path(pfad)
    assert reg.names() == frozenset({"filesystem"})


def test_registry_rejects_bad_names_and_relative_commands(tmp_path):
    kaputt = [
        dict(EINTRAG_FS, name="Groß"),                  # Muster: [a-z0-9-]
        dict(EINTRAG_FS, name="hat_leerzeichen x"),
        dict(EINTRAG_FS, name="x" * 33),                # Laengendeckel
        dict(EINTRAG_FS, name="rel", command="bin/fs"), # command muss absolut sein
        dict(EINTRAG_FS, name="ohne", command="", package=""),  # nichts Startbares
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "servers": [*kaputt, EINTRAG_FS]})
    reg = mcpservers.McpServerRegistry.from_path(pfad)
    assert reg.names() == frozenset({"filesystem"})


def test_registry_rejects_oversized_or_nonstring_args(tmp_path):
    kaputt = [
        dict(EINTRAG_FS, name="zulang", args=["x" * 2000]),
        dict(EINTRAG_FS, name="zahlarg", args=[1, 2]),
        dict(EINTRAG_FS, name="keinlist", args="--root /x"),
        dict(EINTRAG_FS, name="zuviele", args=["a"] * 100),
    ]
    pfad = _registry_datei(tmp_path, {"version": 1, "servers": [*kaputt, EINTRAG_FS]})
    reg = mcpservers.McpServerRegistry.from_path(pfad)
    assert reg.names() == frozenset({"filesystem"})


def test_registry_duplicate_names_first_wins(tmp_path):
    pfad = _registry_datei(tmp_path, {"version": 1, "servers": [
        EINTRAG_FS, dict(EINTRAG_FS, command="/usr/bin/anderes"),
    ]})
    reg = mcpservers.McpServerRegistry.from_path(pfad)
    assert reg.get("filesystem").command == "/usr/bin/fs-mcp"


# --- Config-Builder: reine Struktur, NIEMALS ein "env"-Schluessel -------------

def test_mcp_config_builds_exactly_the_requested_servers():
    reg = mcpservers.McpServerRegistry([
        mcpservers.McpServer(name="filesystem", command="/usr/bin/fs-mcp",
                             args=("--root", "/srv/jobs")),
        mcpservers.McpServer(name="web", command="", package="web-mcp@1.0"),
    ])
    cfg = mcpservers.mcp_config([reg.get("filesystem")])
    assert set(cfg) == {"mcpServers"}
    assert set(cfg["mcpServers"]) == {"filesystem"}
    eintrag = cfg["mcpServers"]["filesystem"]
    assert eintrag == {"command": "/usr/bin/fs-mcp", "args": ["--root", "/srv/jobs"]}
    assert "env" not in json.dumps(cfg)


def test_mcp_config_npx_fallback_when_command_empty():
    server = mcpservers.McpServer(name="web", command="", package="web-mcp@1.0",
                                  args=("--port", "9"))
    cfg = mcpservers.mcp_config([server])
    eintrag = cfg["mcpServers"]["web"]
    assert eintrag == {"command": "npx", "args": ["web-mcp@1.0", "--port", "9"]}
    assert "env" not in eintrag


# --- Worker: generischer Pfad --------------------------------------------------

class FakeExchange:
    def __init__(self, replies): self.replies, self.sent = replies, []

    def __call__(self, path, frame, deadline):
        self.sent.append(frame)
        return self.replies.pop(0)


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


def _registry(**eintraege):
    return mcpservers.McpServerRegistry([
        mcpservers.McpServer(name=name, **felder) for name, felder in eintraege.items()
    ])


def test_worker_rejects_unknown_mcp_server_by_name(tmp_path):
    """Ein Frame traegt nur Namen — einen, den weder Registry noch Gate kennen,
    wird benannt abgelehnt, und es startet kein Job."""
    jobs = claudeworker._Jobs(worker_home=str(tmp_path))
    r = _submit(jobs, "u1", tmp_path / "job-u1", _spawn_recording({}),
                mcp_servers=["filesystem"])
    assert r["ok"] is False and r["kind"] == "invalid_request"
    assert "filesystem" in r["message"]


def test_worker_rejects_registry_server_when_gate_off(tmp_path):
    """Registry-Eintrag vorhanden, aber `TALOS_CLAUDE_WORKER_MCP_SERVERS` nennt
    ihn nicht: die zweite Schicht fehlt — benannte Ablehnung, kein stiller Lauf."""
    reg = _registry(filesystem={"command": "/usr/bin/fs-mcp", "args": ()})
    jobs = claudeworker._Jobs(worker_home=str(tmp_path), mcp_registry=reg)
    r = _submit(jobs, "g1", tmp_path / "job-g1", _spawn_recording({}),
                mcp_servers=["filesystem"])
    assert r["ok"] is False and r["kind"] == "invalid_request"
    assert "filesystem" in r["message"]


def test_worker_runs_enabled_registry_server(tmp_path):
    gesehen: dict = {}
    reg = _registry(filesystem={"command": "/usr/bin/fs-mcp",
                                "args": ("--root", "/srv/jobs")})
    jobs = claudeworker._Jobs(worker_home=str(tmp_path), mcp_registry=reg,
                              mcp_enabled=frozenset({"filesystem"}))
    ws = tmp_path / "job-m1"
    r = _submit(jobs, "m1", ws, _spawn_recording(gesehen),
                mcp_servers=["filesystem"])
    assert r["ok"] is True
    assert _warte(jobs, "m1", "done")["state"] == "done"
    datei = ws / claudeworker.MCP_CONFIG_FILE
    assert datei.is_file()
    cfg = json.loads(datei.read_text(encoding="utf-8"))
    assert cfg == {"mcpServers": {"filesystem": {
        "command": "/usr/bin/fs-mcp", "args": ["--root", "/srv/jobs"]}}}
    argv = gesehen["argv"]
    assert argv[argv.index("--mcp-config") + 1] == str(datei)
    erlaubt = argv[argv.index("--allowedTools") + 1]
    # Genau die angefragten Praefixe, nie mehr.
    assert "mcp__filesystem" in erlaubt
    assert "mcp__chrome-devtools" not in erlaubt


def test_worker_combines_legacy_browser_with_registry_server(tmp_path):
    """`browser_mcp: true` (alter Client) mappt auf chrome-devtools; ein
    zusaetzlicher Registry-Server landet in DERSELBEN einen Config-Datei."""
    gesehen: dict = {}
    reg = _registry(filesystem={"command": "/usr/bin/fs-mcp", "args": ()})
    jobs = claudeworker._Jobs(worker_home=str(tmp_path), mcp_registry=reg,
                              mcp_enabled=frozenset({"filesystem"}),
                              browser=claudeworker.BrowserMcp(enabled=True))
    ws = tmp_path / "job-m2"
    r = _submit(jobs, "m2", ws, _spawn_recording(gesehen),
                browser_mcp=True, mcp_servers=["filesystem"])
    assert r["ok"] is True
    assert _warte(jobs, "m2", "done")["state"] == "done"
    datei = ws / claudeworker.MCP_CONFIG_FILE
    cfg = json.loads(datei.read_text(encoding="utf-8"))
    assert set(cfg["mcpServers"]) == {"chrome-devtools", "filesystem"}
    assert "env" not in json.dumps(cfg)
    erlaubt = gesehen["argv"][gesehen["argv"].index("--allowedTools") + 1]
    assert "mcp__chrome-devtools" in erlaubt and "mcp__filesystem" in erlaubt


def test_registry_chrome_devtools_overrides_legacy_synthesis(tmp_path):
    """Definiert die Registry chrome-devtools selbst (z.B. fest installierte
    Binary statt npx), gilt ihr Eintrag — die Env-Synthese ist nur der
    Rueckfall fuer Bestandsinstallationen ohne Registry-Datei."""
    gesehen: dict = {}
    reg = _registry(**{"chrome-devtools": {
        "command": "/usr/local/bin/chrome-devtools-mcp",
        "args": ("--headless=true",)}})
    jobs = claudeworker._Jobs(worker_home=str(tmp_path), mcp_registry=reg,
                              mcp_enabled=frozenset({"chrome-devtools"}),
                              browser=claudeworker.BrowserMcp(enabled=True))
    ws = tmp_path / "job-m3"
    r = _submit(jobs, "m3", ws, _spawn_recording(gesehen), browser_mcp=True)
    assert r["ok"] is True
    assert _warte(jobs, "m3", "done")["state"] == "done"
    datei = ws / claudeworker.MCP_CONFIG_FILE
    cfg = json.loads(datei.read_text(encoding="utf-8"))
    server = cfg["mcpServers"]["chrome-devtools"]
    assert server["command"] == "/usr/local/bin/chrome-devtools-mcp"
    assert "chrome-devtools-mcp@latest" not in server["args"]


def test_mcp_servers_frame_field_must_be_a_name_list(tmp_path):
    jobs = claudeworker._Jobs(worker_home=str(tmp_path))
    for wert in ("filesystem", [1], [{"name": "x"}], ["Nicht-Erlaubt!"]):
        r = _submit(jobs, f"f{len(str(wert))}", tmp_path / "job-f",
                    _spawn_recording({}), mcp_servers=wert)
        assert r["ok"] is False and r["kind"] == "invalid_request", wert


def test_generated_config_never_carries_env_even_from_registry(tmp_path):
    """Adversarial: die Registry enthaelt einen Eintrag MIT "env" — der Parser
    verwirft ihn schon beim Laden; der Worker hat danach schlicht keinen
    solchen Server, und die Ablehnung ist benannt."""
    pfad = tmp_path / "mcp-servers.json"
    pfad.write_text(json.dumps({"version": 1, "servers": [
        dict(EINTRAG_FS, env={"EVIL": "1"})]}), encoding="utf-8")
    reg = mcpservers.McpServerRegistry.from_path(pfad)
    jobs = claudeworker._Jobs(worker_home=str(tmp_path), mcp_registry=reg,
                              mcp_enabled=frozenset({"filesystem"}))
    r = _submit(jobs, "e1", tmp_path / "job-e1", _spawn_recording({}),
                mcp_servers=["filesystem"])
    assert r["ok"] is False and r["kind"] == "invalid_request"


# --- Worker-Daemon: die Gates kommen aus der Env-Datei -------------------------

@pytest.fixture
def kurzer_sock():
    import shutil
    import tempfile
    pfad = tempfile.mkdtemp(prefix="ms-")
    yield str(Path(pfad) / "cw.sock")
    shutil.rmtree(pfad, ignore_errors=True)


def test_serve_reads_mcp_gate_and_registry_from_env(tmp_path, kurzer_sock):
    import threading
    import unittest.mock as mock
    pfad = _registry_datei(tmp_path, {"version": 1, "servers": [EINTRAG_FS]})
    gesehen: dict = {}
    orig = claudeworker._Jobs

    class _Spion(orig):
        def __init__(self, **kwargs):
            gesehen.update(kwargs)
            super().__init__(**kwargs)

    stop = threading.Event()
    stop.set()
    with mock.patch.object(claudeworker, "_Jobs", _Spion):
        claudeworker.serve(kurzer_sock, str(tmp_path / "fehlt.env"),
                           environ={
                               "TALOS_CLAUDE_WORKER_HOME": str(tmp_path),
                               "TALOS_CLAUDE_WORKER_MCP_SERVERS": "filesystem, web",
                               "TALOS_CLAUDE_WORKER_MCP_REGISTRY": str(pfad),
                           }, spawn=_spawn_recording({}), stop=stop)
    assert gesehen["mcp_enabled"] == frozenset({"filesystem", "web"})
    assert gesehen["mcp_registry"].names() == frozenset({"filesystem"})


def test_serve_mcp_gate_defaults_closed(tmp_path, kurzer_sock):
    import threading
    import unittest.mock as mock
    gesehen: dict = {}
    orig = claudeworker._Jobs

    class _Spion(orig):
        def __init__(self, **kwargs):
            gesehen.update(kwargs)
            super().__init__(**kwargs)

    stop = threading.Event()
    stop.set()
    with mock.patch.object(claudeworker, "_Jobs", _Spion):
        claudeworker.serve(kurzer_sock, str(tmp_path / "fehlt.env"),
                           environ={"TALOS_CLAUDE_WORKER_HOME": str(tmp_path)},
                           spawn=_spawn_recording({}), stop=stop)
    assert gesehen["mcp_enabled"] == frozenset()
    assert gesehen["mcp_registry"].names() == frozenset()


# --- Client: der Frame traegt nur Namen ----------------------------------------

def test_submit_job_frame_carries_mcp_server_names():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    claudejobs.submit_job("/s/c.sock", "j1", "p", "/w", exchange=fx,
                          mcp_servers=["filesystem", "chrome-devtools"])
    sent = json.loads(fx.sent[0])
    assert sent["mcp_servers"] == ["filesystem", "chrome-devtools"]
    assert "command" not in json.dumps(sent)   # nie ausfuehrbare Inhalte im Frame


def test_submit_job_default_frame_has_no_mcp_servers():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    claudejobs.submit_job("/s/c.sock", "j1", "p", "/w", exchange=fx)
    sent = json.loads(fx.sent[0])
    assert "mcp_servers" not in sent


# --- Agent-Runner: das Gate liegt VOR dem Socket --------------------------------

def test_delegate_code_mcp_unknown_name_refused_before_submit():
    def boom(path, frame, deadline): raise AssertionError("darf nicht anmelden")
    run = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=boom,
                                          mcp_allowed=frozenset())
    out = run(_req("delegate_code", {"prompt": "x", "mcp": ["filesystem"]}))
    assert "filesystem" in out and "nicht freigeschaltet" in out


def test_delegate_code_mcp_registry_server_not_in_env_refused():
    """Server steht in der Registry, aber der Agent-Schalter
    (`TALOS_MCP_SERVERS`) nennt ihn nicht — die Erlaubnis ist die
    Schnittmenge, und der Runner kennt nur ihr Ergebnis."""
    def boom(path, frame, deadline): raise AssertionError("darf nicht anmelden")
    run = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=boom,
                                          mcp_allowed=frozenset({"web"}))
    out = run(_req("delegate_code", {"prompt": "x", "mcp": ["filesystem"]}))
    assert "nicht freigeschaltet" in out


def test_delegate_code_mcp_allowed_forwards_names():
    fx = FakeExchange([b'{"ok": true, "state": "accepted"}\n'])
    run = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=fx,
                                          mcp_allowed=frozenset({"filesystem"}))
    out = run(_req("delegate_code", {"prompt": "x", "mcp": ["filesystem"]}))
    assert "state=accepted" in out
    assert json.loads(fx.sent[0])["mcp_servers"] == ["filesystem"]


def test_delegate_code_mcp_must_be_a_string_list():
    def boom(path, frame, deadline): raise AssertionError("darf nicht anmelden")
    run = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=boom,
                                          mcp_allowed=frozenset({"filesystem"}))
    for wert in ("filesystem", [1], [{"name": "filesystem"}]):
        out = run(_req("delegate_code", {"prompt": "x", "mcp": wert}))
        assert "Liste" in out, wert


def test_browser_true_matches_mcp_chrome_devtools_exactly():
    """Der Alias: `browser: true` und `mcp: ["chrome-devtools"]` stehen und
    fallen mit demselben Gate — beide laufen, oder beide werden benannt
    abgelehnt. Nie darf der eine Pfad offen sein, waehrend der andere es
    nicht ist."""
    def boom(path, frame, deadline): raise AssertionError("darf nicht anmelden")
    aus = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=boom,
                                          browser_enabled=False,
                                          mcp_allowed=frozenset())
    assert "abgeschaltet" in aus(_req("delegate_code",
                                      {"prompt": "x", "browser": True}))
    assert "nicht freigeschaltet" in aus(_req("delegate_code",
                                              {"prompt": "x", "mcp": ["chrome-devtools"]}))
    fx1, fx2 = (FakeExchange([b'{"ok": true, "state": "accepted"}\n']) for _ in range(2))
    an1 = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=fx1,
                                          browser_enabled=True,
                                          mcp_allowed=frozenset({"chrome-devtools"}))
    an2 = tools.make_delegate_code_runner(socket_path="/s/c.sock",
                                          work_root="/tmp/root", exchange=fx2,
                                          browser_enabled=True,
                                          mcp_allowed=frozenset({"chrome-devtools"}))
    assert "state=accepted" in an1(_req("delegate_code",
                                        {"prompt": "x", "browser": True}))
    assert "state=accepted" in an2(_req("delegate_code",
                                        {"prompt": "x", "mcp": ["chrome-devtools"]}))
    assert json.loads(fx1.sent[0]).get("browser_mcp") is True
    assert json.loads(fx2.sent[0])["mcp_servers"] == ["chrome-devtools"]


# --- Config, Schema, Protokoll ---------------------------------------------------

def test_config_mcp_servers_default_empty_and_opt_in(monkeypatch):
    monkeypatch.delenv("TALOS_MCP_SERVERS", raising=False)
    aus = config.load_config(require_channel=False)
    assert aus.mcp_servers == ()
    monkeypatch.setenv("TALOS_MCP_SERVERS", "filesystem, chrome-devtools")
    an = config.load_config(require_channel=False)
    assert an.mcp_servers == ("filesystem", "chrome-devtools")


def test_config_mcp_servers_drops_malformed_names(monkeypatch):
    monkeypatch.setenv("TALOS_MCP_SERVERS", "filesystem,Unsinn!,web")
    geladen = config.load_config(require_channel=False)
    assert geladen.mcp_servers == ("filesystem", "web")


def test_mcp_keys_declared_as_policy():
    by_name = schema.BY_NAME
    for name in ("TALOS_MCP_SERVERS", "TALOS_CLAUDE_WORKER_MCP_SERVERS",
                 "TALOS_CLAUDE_WORKER_MCP_REGISTRY"):
        assert by_name[name].kind == schema.POLICY, name
        assert not by_name[name].writable, name


def test_mcp_server_list_validator():
    pruefen = schema.BY_NAME["TALOS_MCP_SERVERS"].validate
    assert pruefen("") == ""
    assert pruefen("filesystem,chrome-devtools") == "filesystem,chrome-devtools"
    with pytest.raises(ValueError):
        pruefen("grosser Fehler!")


def test_tool_protocol_documents_mcp_and_the_browser_alias():
    from talos import reasoner
    zeile = next(zeile for zeile in reasoner.TOOL_PROTOCOL.splitlines()
                 if zeile.startswith("- delegate_code "))
    assert '"mcp"' in zeile
    assert "Alias" in zeile or "alias" in zeile


def test_main_run_computes_mcp_allowed_from_registry_and_env():
    """Die Verdrahtung: der Runner bekommt die Schnittmenge fertig gerechnet —
    Registry-Datei geschnitten mit `TALOS_MCP_SERVERS`, plus chrome-devtools,
    solange der Browser-Schalter es impliziert."""
    import ast
    from talos import __main__ as hauptmodul

    quelle = Path(hauptmodul.__file__).read_text(encoding="utf-8")
    baum = ast.parse(quelle)
    lauf = next(k for k in ast.walk(baum)
                if isinstance(k, ast.FunctionDef) and k.name == "run")
    rumpf = ast.get_source_segment(quelle, lauf)
    assert "McpServerRegistry.from_path" in rumpf
    assert "mcp_allowed=" in rumpf
