"""Der Claude-Worker: gebundelte Coding-Jobs hinter derselben Socket-Grenze.

Warum er existiert: Talos soll dem Modell das Delegieren kleiner, klar
umrissener Coding-Aufgaben an `claude -p` erlauben, ohne dem Agenten die
Schirmherrschaft ueber einen fremden Agenten-Prozess zu geben. Dieser Daemon
laeuft als eigener Benutzer, kennt NUR seine Env-Datei (bewusst kein
`config.py`-Import — der Worker soll WENIGER wissen als der Agent) und
spricht JSON-Lines ueber einen Unix-Socket. Zugriffskontrolle ist das
Dateisystem (Socket 0660 + Gruppe), exakt wie beim Modell-Worker — ein Token
waere nur ein weiteres Geheimnis, das aus einer Kind-Umgebung herausgehalten
werden muesste, die keine sehen darf.

Was ein Job ist: ein `claude -p --output-format stream-json` unter dem
Sandbox-Backend der Plattform. Netz AN (die Anthropic-API wird gebraucht —
die eine dokumentierte Abweichung von `run_shell`), Wurzel read-only,
beschreibbar nur der Arbeitsbereich des Jobs. Unconfined wird NIE gewaehlt,
und `TALOS_SANDBOX_ALLOW_UNCONFINED` gilt hier nicht: ein ungeschirmter
fremder Agent ist keine Degradation, sondern ein anderes Produkt.

Protokoll — JSON-Lines, eine Anfrage pro Verbindung:

    →  {"op": "submit", "job_id": "…", "prompt": "…", "workspace": "…",
        "browser_mcp": false, "mcp_servers": []}
    →  {"op": "status", "job_id": "…"}
    ←  {"ok": true, "state": "accepted"|"running"|"done"|"failed"|"timeout", …}
    ←  {"ok": false, "kind": "invalid_request"|"unknown_job"|"busy"|"unavailable",
        "message": "…"}

`browser_mcp: true` gibt dem Job chrome-devtools-mcp als MCP-Server mit auf den
Weg — die EINZige Browser-Automatisierung, die Talos kennt: sie laeuft hinter
derselben Sandbox-Wand (gleicher Workspace, gleiche Deadline, gleiches Env),
und der Kernel gated weiterhin nur die Delegation selbst. Der Worker schaltet
sie separat frei (`TALOS_CLAUDE_WORKER_BROWSER_MCP=1`, Vorgabe AUS): ein Frame,
der sie anfordert, ohne dass der Dienst sie kennt, wird ABGELEHNT statt still
ohne Browser zu laufen — eine still degradierte Erlaubnis waere eine Luege
gegenueber der Policy-Evidenz des Agenten.

`mcp_servers` ist die generische Form desselben Mechanismus: eine Liste von
NAMEN aus der operator-owned Registry (`data/mcp-servers.json`, siehe
`mcpservers.py`) — `browser_mcp: true` mappt intern auf `["chrome-devtools"]`,
und alte Clients bleiben so bedienbar. Der Frame traegt NIE command/args: der
Worker baut die MCP-Konfiguration ausschliesslich aus seiner eigenen Env
(`TALOS_CLAUDE_WORKER_MCP_SERVERS` als zweites Gate, Vorgabe leer = keiner)
und der Registry-Datei. Einen Server, den Registry ODER Gate nicht kennen,
lehnt er benannt ab — kein Job startet. Definiert die Registry
`chrome-devtools` selbst, gilt ihr Eintrag; sonst faellt der Worker fuer
genau diesen einen Namen auf die Env-Synthese (`BrowserMcp`) zurueck, damit
Bestandsinstallationen ohne Registry-Datei unveraendert weiterlaufen.

Der Submit-Frame traegt optional ein `"backend"`: fehlt es oder steht
`"claude"`, gilt der bisherige Weg Bit fuer Bit; `"agy"` waehlt das
Antigravity-Backend (`agy -p --output-format stream-json`) — gleiche
Sandbox, gleiches Env, gleiche Workspace-Ableitung, gleiche Deadline. agy
laeuft nur, wenn der Dienst es freigeschaltet hat
(`TALOS_CLAUDE_WORKER_AGY_BIN` + `TALOS_CLAUDE_WORKER_AGY_HOME`, Vorgabe:
kein agy) — ein Frame gegen ein nicht konfiguriertes Backend wird mit
`unavailable` benannt abgelehnt, kein Job startet. MCP/Browser bleibt dem
claude-Backend vorbehalten: ein agy-Frame mit `browser_mcp`/`mcp_servers`
ist `invalid_request`. Der agy-OAuth-Token wird pro Job aus dem
Worker-agy-HOME in das wegwerfbare Job-HOME kopiert (0600) — agy kennt
keinen Env-Token wie Claude, die Token-DATEI muss also mit, aber sie geht
nie weiter als bis in den Job-Workspace, und der Quellpfad taucht weder in
Env noch argv auf. Sein Ergebnis-Event meldet Fehler auch bei RC 0 (der
Auth-Fehler ist genau so einer) — darum zaehlt der Strom, nicht der
Returncode: ein `result` mit Fehler oder Nicht-OK-Status ist `failed`.

Bei "done" zusaetzlich: "summary" (aus dem `result`-Event des Streams),
"files" (Pfade relativ zum Arbeitsbereich, aus `tool_use`-Events — Beweis
kommt aus dem Stream, nie aus Prosa) und "returncode". Jede Status-Antwort
traegt das "backend" des Jobs (Vorgabe "claude").

Kontinuitaet lebt im Event-Log von Talos, nicht hier: die Job-Tabelle ist
fluechtig, und ein neu gestarteter Worker weiss von nichts — das ist Absicht.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import mcpservers, sandbox
from .mcpservers import SERVER_NAME, McpServerRegistry
from .sandbox import SandboxLimits

__all__ = [
    "DEFAULT_ENV",
    "DEFAULT_SOCKET",
    "ENV_FILE_VAR",
    "MAX_FILES",
    "MAX_FRAME_BYTES",
    "MAX_JOB_TIMEOUT_S",
    "MAX_PARALLEL",
    "MAX_PROMPT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MCP_CONFIG_FILE",
    "SOCKET_ENV_VAR",
    "AgyBackend",
    "BrowserMcp",
    "browser_mcp_config",
    "handle_frame",
    "job_backends",
    "job_env",
    "main",
    "make_spawn",
    "parse_stream_event",
    "serve",
    "stream_failure",
]

DEFAULT_SOCKET = "/run/talos/claude.sock"
DEFAULT_ENV = "/etc/talos/claude-worker.env"
SOCKET_ENV_VAR = "TALOS_CLAUDE_WORKER_SOCKET"
ENV_FILE_VAR = "TALOS_CLAUDE_WORKER_ENV"

# Prompts sind groesser als Modell-Frames — der Deckel schuetzt den Daemon
# gegen einen Client, der unbegrenzt Bytes kippt, nicht gegen echte Aufgaben.
MAX_FRAME_BYTES = 1 << 20
# Wer verbindet, aber nichts schickt, blockiert die Schleife hoechstens so lange.
READ_TIMEOUT_S = 30.0
# Gesamt-Deadline pro Job (Anti-Trickle): nicht Zeit-pro-Event, sondern die
# Wanduhr ueber allem — ein Kind, das alle 10 Sekunden ein Lebenszeichen
# tropfelt, darf den Daemon trotzdem nicht ewig belegen.
DEFAULT_JOB_TIMEOUT_S = 900
MAX_JOB_TIMEOUT_S = 3600
MAX_PARALLEL = 2
MAX_PROMPT_CHARS = 20000
MAX_SUMMARY_CHARS = 6000
MAX_ERROR_CHARS = 2000
MAX_FILES = 200

KIND_INVALID = "invalid_request"
KIND_UNKNOWN_JOB = "unknown_job"
KIND_BUSY = "busy"
KIND_UNAVAILABLE = "unavailable"

DEFAULT_BIN = "claude"
# Bewusst KEIN --dangerously-skip-permissions: die Konfinement-Grenze traegt,
# ein Selbst-Freischaftsschalter des Kindes wuerde sie von innen aushoehlen.
ALLOWED_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"

# chrome-devtools-mcp im Job: Server-Name, Datei (im Job-Workspace) und Paket.
# Der Server erbt das Job-Env unveraendert — die MCP-Konfiguration traegt
# absichtlich KEIN eigenes "env", damit keine Credential den ohnehin schon
# minimierten Kind-Rahmen erweitert.
BROWSER_MCP_SERVER = "chrome-devtools"
BROWSER_MCP_FILE = ".chrome-devtools.mcp.json"
BROWSER_MCP_PACKAGE = "chrome-devtools-mcp@latest"
BROWSER_MCP_ALLOWED_TOOLS = f"mcp__{BROWSER_MCP_SERVER}"

# Die generische MCP-Konfigurationsdatei eines Jobs (im Job-Workspace). Der
# alte Name oben bleibt fuer den reinen Legacy-Fall (nur chrome-devtools aus
# der Env-Synthese), damit sich am Bestandspfad kein Bit aendert; sobald die
# Registry beteiligt ist, traegt die Datei diesen neutralen Namen.
MCP_CONFIG_FILE = ".talos-mcp.json"
# Wie viele MCP-Server ein einzelner Frame anfordern darf — Deckel gegen ein
# Kind, das die Allowlist flutet.
MAX_MCP_SERVERS = 8

# Das agy-Backend (Antigravity CLI). Der Token-Pfad ist relativ zum agy-HOME:
# der Worker kopiert die Datei pro Job daraus in das wegwerfbare Job-HOME —
# agy kennt keinen Env-Token wie Claude (`CLAUDE_CODE_OAUTH_TOKEN`), die
# Token-DATEI muss also mit in den Workspace. Sie geht nie weiter: der
# Quellpfad taucht weder in der Job-Env noch in argv auf.
AGY_TOKEN_REL = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
# Stati, die ein agy-`result`-Event als Erfolg durchgehen laesst — tolerant
# gelesen (Grossschreibung egal); alles andere oder ein nicht-leeres
# `error`-Feld ist ein Misserfolg, und zwar auch bei RC 0 (gemessen am
# Auth-Fehler: `status: "ERROR"`, RC 0 — der Returncode luegt, der Strom nicht).
AGY_OK_STATI = frozenset({"OK", "DONE", "SUCCESS"})


@dataclass(frozen=True)
class AgyBackend:
    """Worker-seitiges agy-Gate. `bin` ist die gepinnte Binary (absolut,
    existierend), `home` das dedizierte agy-HOME mit dem OAuth-Login des
    Betreibers. Beides oder nichts: fehlt eines der beiden
    (`TALOS_CLAUDE_WORKER_AGY_BIN` / `TALOS_CLAUDE_WORKER_AGY_HOME`), gibt es
    das Backend nicht, und ein agy-Frame wird mit `unavailable` beantwortet
    statt gegen eine halbe Konfiguration zu starten."""
    bin: str
    home: str


def _agy_gate(bin_roh: str, home_roh: str) -> AgyBackend | None:
    """Env-Werte → das Gate, oder None. Prueft Form und Existenz, nicht mehr:
    ob der Login darin noch gilt, weiss erst der Job selbst."""
    if not bin_roh or not home_roh:
        return None
    binary = Path(bin_roh)
    home = Path(home_roh)
    if not binary.is_absolute() or not binary.is_file():
        return None
    if not home.is_absolute() or not home.is_dir():
        return None
    return AgyBackend(bin=str(binary), home=str(home))


def _stage_agy_token(agy_home: str, workspace: Path) -> None:
    """Kopiert den agy-OAuth-Token aus dem Worker-agy-HOME in das Job-HOME
    (Mode 0600). agy liest ihn aus `$HOME/.gemini/antigravity-cli/` — und das
    HOME des Jobs liegt IM Workspace, der einzige beschreibbare Ort. Der
    Unterschied zum claude-Backend ist ehrlich: dort betritt die Token-DATEI
    die Sandbox nie (der Wert reist in der Env), hier muss sie hinein, weil
    agy keinen Env-Token kennt. Das Exfiltrations-Risiko ist dasselbe — der
    Job hat ohnehin Netz —, aber die Kopie stirbt mit dem Wegwerf-Workspace,
    und der Quellpfad bleibt unsichtbar fuer das Kind."""
    quelle = Path(agy_home) / AGY_TOKEN_REL
    ziel = workspace / ".home" / AGY_TOKEN_REL
    ziel.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(quelle, ziel)
    os.chmod(ziel, 0o600)


@dataclass(frozen=True)
class BrowserMcp:
    """Worker-seitige Browser-MCP-Einstellung. `enabled=False` heisst: ein
    Frame, der sie anfordert, wird abgelehnt statt still ohne Browser zu
    laufen. `headless` ist Vorgabe AN — ein sichtbares Fenster auf einem
    Server ohne Display waere ohnehin nur ein zusaetzlicher Fehlermodus.
    `chrome_path` pinnt die Binary, wenn npx sie nicht selbst findet (Pi:
    /usr/bin/chromium). `command` ersetzt den npx-Aufruf durch eine fest
    installierte Binary — im Job ist der npm-Cache naemlich schreibgeschuetzt
    (Workspace-only), und ein `npx @latest` pro Job heisst: jeder Browser-Job
    laedt das Paket erneut in seinen wegwerfbaren HOME. `chrome_args` reicht
    Start-Flags an Chrome weiter (--chromeArg) — unter bubblewrap braucht
    Chrome typischerweise --no-sandbox, weil es keine eigenen Namespaces
    anlegen darf; leer heisst: keine Aufweichung."""
    enabled: bool = False
    headless: bool = True
    chrome_path: str = ""
    command: str = ""
    chrome_args: str = ""


def browser_mcp_config(settings: BrowserMcp) -> dict:
    """Die MCP-Konfiguration fuer `claude --mcp-config` — reine Struktur,
    keine Geheimnisse. Vorgabe: Start per npx; mit `command` eine fest
    installierte Binary (der Job hat Netz, aber keinen schreibbaren
    npm-Cache ausserhalb seines wegwerfbaren HOME)."""
    args = []
    if settings.headless:
        args.append("--headless=true")
    if settings.chrome_path:
        args.append(f"--executablePath={settings.chrome_path}")
    for flag in settings.chrome_args.split():
        args.append(f"--chromeArg={flag}")
    if settings.command:
        befehl = {"command": settings.command, "args": args}
    else:
        befehl = {"command": "npx", "args": [BROWSER_MCP_PACKAGE] + args}
    return {"mcpServers": {BROWSER_MCP_SERVER: befehl}}

# Ruhe-Takt des Stream-Lesers: ein schweigendes Kind darf die Deadline-Pruefung
# des Aufrufers nicht blockieren (Herzschlag = None aus events()).
_POLL_S = 0.25
_EOF: Any = object()

# Die Spawn-Naht: Tests injizieren ein Double mit konservierten stream-json-
# Zeilen; Produktion baut make_spawn() den echten, eingesperrten Popen.
Spawn = Callable[[list[str], Path, dict[str, str], SandboxLimits], Any]


def _read_env_file(path: Path) -> dict[str, str]:
    """Simple KEY=VALUE-Datei. Fehlt sie -> leer.

    Eigene dreizeilige Leseroutine statt `config._read_env_file` — denselben
    Grund wie beim Modell-Worker: die Agent-Konfiguration hereinzuholen hiesse,
    deren saemtliche Pfade und Zustaende mitzuziehen.
    """
    werte: dict[str, str] = {}
    if not path.is_file():
        return werte
    for zeile in path.read_text(encoding="utf-8").splitlines():
        s = zeile.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        name, _, wert = s.partition("=")
        werte[name.strip()] = wert.strip()
    return werte


def job_backends(platform: str) -> list[sandbox.Sandbox]:
    """Sandbox-Kandidaten fuer Jobs — OHNE den unconfined-Kandidaten.

    `default_backends` liefert ihn heute gar nicht; der Filter ist die
    schriftliche Zusage, dass sich das nie aendert. Ein ungeschirmter fremder
    Agent ist keine Degradation, die man hinnehmen koennte.
    """
    return [b for b in sandbox.default_backends(platform)
            if b.name not in ("unconfined", "none")]


def job_env(worker_home: str, workspace: Path, *, oauth_token: str = "") -> dict[str, str]:
    """Die Umgebung eines Job-Kindes — positive Allowlist, sonst nichts.

    Kein Talos-Geheimnis, kein Bridge-Token, keine Deployment-Env darf in ein
    Job gelangen. HOME liegt IM Arbeitsbereich (`.home`): der einzige Ort, an
    dem das Kind schreiben darf — Claude braucht ein beschreibbares HOME fuer
    State (`~/.claude`, `~/.claude.json`), und das Worker-HOME ist im Sandbox
    read-only (gemessen am zweiten Live-E2E: Bash starb am ro-Dateisystem).
    Der Claude-OAuth-Token kommt als Wert in die Env — er ist die EIGENE
    Credential des Jobs, kein Talos-Secret; die Token-DATEI bleibt draussen.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path(workspace) / ".home"),
        "LANG": "C.UTF-8",
        "TMPDIR": str(workspace),
        "PWD": str(workspace),
    }
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def parse_stream_event(line: dict, workspace: Path) -> tuple[str | None, str | None]:
    """(summary, datei) aus einer stream-json-Zeile.

    Die Summary kommt NUR aus einem top-level `result`-Event; Dateien NUR aus
    `tool_use`-Inputs, deren aufgeloester Pfad innerhalb des Arbeitsbereichs
    bleibt. Beweis kommt aus dem Stream, nie aus Prosa — ein behaupteter Pfad
    ausserhalb des Käfigs faellt weg, er wird nicht umgeschrieben.

    Tolerant gegenueber beiden Backends: claude kennzeichnet Events mit
    `type`, agy mit `event`, und agy verschachtelt sein Abschluss-Event
    (`result` ist ein Objekt, die Summary steht in `response`). agys
    Werkzeug-Schritte kommen als `step_update` mit `step_type: "tool"`;
    der Pfad steht in `tool_info.parameters` (gemessen am Live-Lauf:
    `write_to_file` mit `TargetFile`, Grossschreibung genau so). Jeder
    absolute Pfad-Wert darin ist ein Kandidat — was ausserhalb des
    Arbeitsbereichs zeigt, faellt weg, es wird nicht umgeschrieben.
    """
    if not isinstance(line, dict):
        return (None, None)
    typ = line.get("type") or line.get("event")
    if typ == "result":
        result = line.get("result")
        if isinstance(result, str) and result:
            return (result[:MAX_SUMMARY_CHARS], None)
        if isinstance(result, dict):
            antwort = result.get("response")
            if isinstance(antwort, str) and antwort:
                return (antwort[:MAX_SUMMARY_CHARS], None)
        return (None, None)
    if typ == "step_update":
        schritt = line.get("step_update")
        if not isinstance(schritt, dict) or schritt.get("step_type") != "tool":
            return (None, None)
        info = schritt.get("tool_info")
        if not isinstance(info, dict):
            return (None, None)
        parameter = info.get("parameters")
        if not isinstance(parameter, dict):
            return (None, None)
        basis = Path(workspace).resolve()
        for wert in parameter.values():
            if not isinstance(wert, str) or not wert.startswith("/"):
                continue  # nur absolute Pfade sind Kandidaten — kein Raten
            try:
                relativ = Path(wert).resolve().relative_to(basis)
            except (OSError, ValueError):
                continue  # ausserhalb des Käfigs: verworfen, nicht umgeschrieben
            return (None, relativ.as_posix())
        return (None, None)
    if typ in ("tool_use", "tool_call"):
        eingabe = line.get("input")
        if not isinstance(eingabe, dict):
            eingabe = line.get("args")
        if not isinstance(eingabe, dict):
            return (None, None)
        roh = eingabe.get("file_path") or eingabe.get("path")
        if not isinstance(roh, str) or not roh:
            return (None, None)
        try:
            basis = Path(workspace).resolve()
            kandidat = Path(roh)
            if not kandidat.is_absolute():
                kandidat = basis / kandidat
            relativ = kandidat.resolve().relative_to(basis)
        except (OSError, ValueError):
            return (None, None)
        return (None, relativ.as_posix())
    return (None, None)


def stream_failure(line: dict) -> str | None:
    """Fehlertext aus einem agy-`result`-Event — oder None.

    Der Returncode luegt: der gemessene Auth-Fehler kommt als
    `{"event": "result", "result": {"status": "ERROR", …}}` mit RC 0. Darum
    ist ein `result` mit nicht-leerem `error` oder einem Status ausserhalb
    der OK-Menge ein Misserfolg, egal was der Exit-Code behauptet.
    Claude-Result-Events (Text statt Objekt) liefern hier bewusst None —
    ihr Misserfolg bleibt der RC wie bisher.
    """
    if not isinstance(line, dict):
        return None
    if (line.get("type") or line.get("event")) != "result":
        return None
    result = line.get("result")
    if not isinstance(result, dict):
        return None
    fehler = result.get("error")
    if isinstance(fehler, str) and fehler.strip():
        return fehler.strip()[-MAX_ERROR_CHARS:]
    status = str(result.get("status") or "").strip()
    if status and status.upper() not in AGY_OK_STATI:
        return f"stream reports status {status}"[:MAX_ERROR_CHARS]
    return None


def _job_argv(prompt: str, *, mcp_config: Path | None = None,
              mcp_servers: tuple[str, ...] = ()) -> list[str]:
    """Die Argumente NACH dem Binary. Das Binary selbst gehoert der
    Worker-Konfiguration (`make_spawn`) — kein Frame der Leitung darf es waehlen.
    Die MCP-Konfiguration (nur bei MCP-Jobs) hat der Worker selbst in den
    Arbeitsbereich geschrieben; auch ihre WERKZEUGE stehen in der Allowlist,
    sonst stuenden sie ungenutzt hinter dem Berechtigungs-Prompt. Pro Server
    genau ein `mcp__<name>`-Praefix — nie mehr als angefragt wurde."""
    erlaubt = ALLOWED_TOOLS
    if mcp_config is not None:
        namen = mcp_servers or (BROWSER_MCP_SERVER,)
        erlaubt = f"{erlaubt}," + ",".join(f"mcp__{name}" for name in namen)
    argv = [
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", erlaubt,
    ]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config)]
    return argv


def _agy_argv(prompt: str, timeout_s: int) -> list[str]:
    """Die agy-Argumente NACH dem Binary — dasselbe Grundgeruest wie beim
    claude-Backend (`-p`, stream-json), gemessen an der Binary 1.1.22.

    Zwei ehrliche Unterschiede: `--dangerously-skip-permissions` ist hier
    Pflicht, wo es beim claude-Backend bewusst fehlt — agy kann im
    Print-Modus nicht rueckfragen, ohne den Schalter hinge jeder Job am
    Berechtigungs-Prompt. Die Konfinement-Grenze aendert das nicht: sie
    traegt die Sandbox (gleiches Backend, gleicher Workspace, gleiche
    Deadline), nicht das Berechtigungssystem des Kindes. Und
    `--print-timeout` spiegelt die Job-Deadline in das Kind, damit es selbst
    aufhoert, bevor die Wanduhr die Prozessgruppe toetet."""
    return [
        "-p", prompt,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout", f"{timeout_s}s",
    ]


class _Busy(Exception):
    """Das Parallel-Limit ist erreicht — der dritte Job wartet nicht, er faellt um."""


class _Unavailable(Exception):
    """Das angeforderte Backend ist auf diesem Worker nicht fahrbar — ein
    benannter `unavailable`-Fehler, und es startet kein Job."""


class _Job:
    """Ein Job in der fluechtigen Tabelle. Alles unter einem eigenen Schloss,
    damit `status` nie einen halb geschriebenen Zustand liest."""

    def __init__(self, job_id: str, prompt: str, workspace: str, *,
                 backend: str = "claude", agy_home: str = "",
                 mcp_eintraege: dict[str, dict] | None = None,
                 legacy_browser: bool = False) -> None:
        self.job_id = job_id
        self.prompt = prompt
        self.workspace = workspace
        # Welcher Motor den Job faehrt ("claude" | "agy") — der Frame waehlt
        # das Backend, nie das Ziel; die Status-Antwort sagt es ehrlich mit.
        self.backend = backend
        # Nur fuer agy-Jobs: das Worker-agy-HOME, AUS dem der Token kommt.
        # Der Pfad gehoert der Worker-Konfiguration und taucht weder in der
        # Job-Env noch in argv auf.
        self.agy_home = agy_home
        # Vom WORKER aufgeloeste MCP-Konfigurationseintraege (Name →
        # {"command": …, "args": […]}) — der Frame lieferte nur Namen.
        self.mcp_eintraege = mcp_eintraege or {}
        # Reiner Legacy-Fall: genau chrome-devtools aus der Env-Synthese —
        # die Datei behaelt dann den alten Namen (BROWSER_MCP_FILE).
        self.legacy_browser = legacy_browser
        self.state = "accepted"
        self.summary = ""
        self.files: list[str] = []
        self.returncode = -1
        self.error = ""
        self.schloss = threading.Lock()

    def beenden(self, state: str, *, returncode: int = -1, fehler: str = "") -> None:
        with self.schloss:
            self.state = state
            self.returncode = returncode
            if fehler:
                # Ein `failed` ohne Spur ist un-debuggbar — gemessen am ersten
                # Live-E2E, als stderr im Nichts landete und nur raten blieb.
                self.error = fehler[-MAX_ERROR_CHARS:]

    def merke(self, summary: str | None, datei: str | None) -> None:
        with self.schloss:
            if summary is not None:
                self.summary = summary
            if (datei is not None and datei not in self.files
                    and len(self.files) < MAX_FILES):
                self.files.append(datei)


class _Jobs:
    """In-memory Job-Tabelle. Zustaende: accepted → running → done|failed|timeout."""

    def __init__(self, *, max_parallel: int = MAX_PARALLEL, worker_home: str = "",
                 browser: BrowserMcp | None = None,
                 mcp_registry: McpServerRegistry | None = None,
                 mcp_enabled: frozenset[str] = frozenset(),
                 agy: AgyBackend | None = None) -> None:
        self._max_parallel = max_parallel
        self._worker_home = worker_home
        self._browser = browser if browser is not None else BrowserMcp()
        # Registry (operator-owned Datei) und das Worker-Gate: ein Server ist
        # nur fahrbar, wenn BEIDE ihn kennen — die Datei allein ist eine
        # Beschreibung, das Gate allein ein Versprechen ohne Inhalt.
        self._mcp_registry = (mcp_registry if mcp_registry is not None
                              else McpServerRegistry())
        self._mcp_enabled = frozenset(mcp_enabled)
        # Das agy-Gate: None heisst, dieser Worker kennt das Backend nicht —
        # ein agy-Frame wird dann benannt abgelehnt, nicht gegen eine halbe
        # Konfiguration gefahren.
        self._agy = agy
        self._schloss = threading.Lock()
        self._jobs: dict[str, _Job] = {}

    def _loese_mcp(self, namen: list[str]) -> tuple[dict[str, dict], bool]:
        """Namen → Konfigurationseintraege, ausschliesslich aus Gate + Registry
        (+ der Legacy-Env-Synthese fuer chrome-devtools). Wirft ValueError mit
        benanntem Grund — der Aufrufer macht daraus den Fehler-Frame."""
        eintraege: dict[str, dict] = {}
        legacy = False
        for name in namen:
            freigegeben = (name in self._mcp_enabled
                           or (name == BROWSER_MCP_SERVER and self._browser.enabled))
            if not freigegeben:
                raise ValueError(f"mcp server {name!r} is not enabled on this worker")
            server = self._mcp_registry.get(name)
            if server is not None:
                eintraege[name] = server.config_entry()
            elif name == BROWSER_MCP_SERVER:
                # Bestandsinstallation ohne Registry-Datei: die Env-Synthese
                # (headless, chrome_path, command, chrome_args) baut denselben
                # Eintrag wie bisher — Bit fuer Bit.
                eintraege[name] = browser_mcp_config(
                    self._browser)["mcpServers"][name]
                legacy = True
            else:
                raise ValueError(
                    f"mcp server {name!r} is not in the worker registry")
        return eintraege, legacy and len(eintraege) == 1

    def submit(self, job_id: str, prompt: str, workspace: str, *,
               spawn: Spawn, limits: SandboxLimits,
               browser_mcp: bool = False,
               mcp_servers: Sequence[str] = (),
               backend: str = "claude",
               agy_spawn: Spawn | None = None) -> str:
        with self._schloss:
            if job_id in self._jobs:
                raise ValueError(f"job_id {job_id!r} is already known")
            aktive = sum(1 for j in self._jobs.values()
                         if j.state in ("accepted", "running"))
            if aktive >= self._max_parallel:
                raise _Busy
            agy_home = ""
            lauf_spawn = spawn
            if backend == "agy":
                # Beide Gates stehen, oder es gibt keinen Job: das Backend
                # selbst (Binary + agy-HOME) und der Login darin. Der
                # Fehler heisst `unavailable` — es fehlt Infrastruktur,
                # nicht ein gueltiger Frame.
                if self._agy is None:
                    raise _Unavailable(
                        "agy backend is not configured on this worker "
                        "(TALOS_CLAUDE_WORKER_AGY_BIN / "
                        "TALOS_CLAUDE_WORKER_AGY_HOME)")
                if browser_mcp or mcp_servers:
                    raise ValueError(
                        "mcp servers are only available on the claude backend")
                if not (Path(self._agy.home) / AGY_TOKEN_REL).is_file():
                    raise _Unavailable(
                        "agy oauth token is missing — log in as the worker "
                        "user first (agy under the configured agy home)")
                agy_home = self._agy.home
                # Der agy-Spawn traegt die agy-Binary; Tests injizieren nur
                # einen Spawn — der gilt dann fuer beide Backends.
                lauf_spawn = agy_spawn if agy_spawn is not None else spawn
            elif browser_mcp and not self._browser.enabled:
                raise ValueError("browser mcp is not enabled on this worker")
            # `browser_mcp: true` mappt auf ["chrome-devtools"] — der alte
            # Client und der neue sprechen denselben Mechanismus an.
            namen = list(dict.fromkeys(
                ([BROWSER_MCP_SERVER] if browser_mcp else []) + list(mcp_servers)))
            eintraege: dict[str, dict] = {}
            legacy_browser = False
            if namen:
                eintraege, legacy_browser = self._loese_mcp(namen)
            job = _Job(job_id, prompt, workspace, backend=backend,
                       agy_home=agy_home, mcp_eintraege=eintraege,
                       legacy_browser=legacy_browser)
            self._jobs[job_id] = job
        deadline = time.monotonic() + limits.timeout_s
        thread = threading.Thread(
            target=_run_job,
            args=(job, self._worker_home, lauf_spawn, limits, deadline),
            daemon=True,
        )
        thread.start()
        return "accepted"

    def status(self, job_id: str) -> dict[str, Any]:
        with self._schloss:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with job.schloss:
            antwort: dict[str, Any] = {"ok": True, "state": job.state,
                                       "backend": job.backend}
            if job.state == "done":
                antwort["summary"] = job.summary
                antwort["files"] = list(job.files)
                antwort["returncode"] = job.returncode
            if job.state in ("failed", "timeout"):
                antwort["returncode"] = job.returncode
                if job.error:
                    antwort["error"] = job.error
            return antwort


def _lies_oauth_token(worker_home: str) -> str:
    """Den Claude-OAuth-Token frisch aus dem Worker-HOME — pro Job gelesen,
    damit ein ausserhalb erneuerter Token sofort gilt. Leer, wenn nichts da
    ist: der Job startet dann ohne Credential und scheitert ehrlich am API-
    Aufruf, statt dass der Daemon ratet."""
    try:
        return (Path(worker_home) / ".claude" / "oauth-token").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


def _run_job(job: _Job, worker_home: str, spawn: Spawn,
             limits: SandboxLimits, deadline: float) -> None:
    """Thread-Rumpf eines Jobs. Hält die Gesamt-Deadline ueber ALLEM — die
    Pruefung steht vor jedem `next()`, und das Handle liefert Herzschlaege,
    damit ein schweigendes Kind sie nicht aushungert."""
    workspace = Path(job.workspace)
    mcp_pfad: Path | None = None
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".home").mkdir(exist_ok=True)
        if job.backend == "agy":
            # Die agy-Credential NUR als Kopie im wegwerfbaren Job-HOME —
            # der Quellpfad bleibt fuer das Kind unsichtbar (nie Env, nie argv).
            _stage_agy_token(job.agy_home, workspace)
        if job.mcp_eintraege:
            # Der WORKER schreibt die MCP-Konfiguration, bevor das Kind startet:
            # das Kind findet sie fertig vor und kann ihren Inhalt nie waehlen.
            datei = BROWSER_MCP_FILE if job.legacy_browser else MCP_CONFIG_FILE
            mcp_pfad = workspace / datei
            mcp_pfad.write_text(
                json.dumps({"mcpServers": job.mcp_eintraege}), encoding="utf-8")
            os.chmod(mcp_pfad, 0o600)
    except OSError as ungueltig:
        job.beenden("failed", fehler=f"workspace not creatable: {ungueltig}")
        return
    job.beenden("running", returncode=-1)
    if job.backend == "agy":
        argv = _agy_argv(job.prompt, limits.timeout_s)
        # Kein Claude-OAuth in der Env eines agy-Jobs: dessen Credential liegt
        # als Kopie im Job-HOME; die andere gehoert hier nicht hinein.
        env = job_env(worker_home, workspace)
    else:
        argv = _job_argv(job.prompt, mcp_config=mcp_pfad,
                         mcp_servers=tuple(job.mcp_eintraege))
        env = job_env(worker_home, workspace,
                      oauth_token=_lies_oauth_token(worker_home))
    try:
        handle = spawn(argv, workspace, env, limits)
    except Exception as ungueltig:
        # Kein Backend, kein Binary, kein Start — der Job ist gescheitert,
        # der Daemon nicht. Der Grund landet im Record, nicht im Nichts.
        job.beenden("failed", fehler=f"spawn failed: {ungueltig}")
        return
    try:
        events: Iterator[Any] = handle.events()
        while True:
            if time.monotonic() >= deadline:
                toeten = getattr(handle, "kill", None)
                if callable(toeten):
                    try:
                        toeten()
                    except Exception:
                        pass
                job.beenden("timeout", fehler="overall job deadline reached")
                return
            try:
                event = next(events)
            except StopIteration as ende:
                rc = ende.value if isinstance(ende.value, int) else -1
                spur = str(getattr(handle, "stderr_tail", "") or "")
                job.beenden("done" if rc == 0 else "failed",
                            returncode=rc,
                            fehler="" if rc == 0 else (spur or f"exit code {rc}, no stderr"))
                return
            if event is None:
                continue  # Herzschlag — nur die Uhr lief weiter
            summary, datei = parse_stream_event(event, workspace)
            job.merke(summary, datei)
            strom_fehler = stream_failure(event)
            if strom_fehler is not None:
                # Der Strom meldet den Misserfolg (agy: auch bei RC 0 — der
                # Auth-Fall) — das Kind wird beendet, der Job ist `failed`,
                # egal welchen Exit-Code es noch behaupten wuerde.
                toeten = getattr(handle, "kill", None)
                if callable(toeten):
                    try:
                        toeten()
                    except Exception:
                        pass
                job.beenden("failed", fehler=strom_fehler)
                return
    except Exception as ungueltig:
        job.beenden("failed", fehler=f"job loop failed: {ungueltig}")


class _ProcHandle:
    """Das laufende `claude`-Kind. `events()` liefert geparste stream-json-
    Objekte — oder `None`-Herzschlaege, damit ein schweigendes Kind die
    Deadline-Pruefung des Aufrufers nicht blockiert (Anti-Trickle)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._zeilen: queue.Queue = queue.Queue()
        self._fehler: list[str] = []
        self._pumpe = threading.Thread(target=self._pumpen, daemon=True)
        self._pumpe.start()
        self._fehlerpumpe = threading.Thread(target=self._fehler_pumpen, daemon=True)
        self._fehlerpumpe.start()

    def _fehler_pumpen(self) -> None:
        # stderr laeuft mit, begrenzt auf die letzten Zeilen: ein `failed`
        # ohne Spur ist un-debuggbar (gemessen am ersten Live-E2E — bwrap
        # starb stumm, und nur raten blieb). Auch hier immer weiterlesen,
        # damit kein Kind an einer vollen Pipe haengt.
        if self._proc.stderr is None:
            return
        with self._proc.stderr:
            for roh in self._proc.stderr:
                try:
                    zeile = roh.decode("utf-8", "replace").rstrip()
                except AttributeError:
                    zeile = str(roh)
                self._fehler.append(zeile)
                if len("".join(self._fehler)) > MAX_ERROR_CHARS * 2:
                    self._fehler = ["".join(self._fehler)[-MAX_ERROR_CHARS:]]

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._fehler)[-MAX_ERROR_CHARS:]

    def _pumpen(self) -> None:
        # Immer weiterlesen, auch wenn niemand konsumiert: ein Kind an einer
        # vollen Pipe blockiert — und dann erschiesst die Uhr einen Lauf,
        # der laengst fertig war (derselbe Grund wie sandbox._OutputPump).
        with self._proc.stdout:
            for roh in self._proc.stdout:
                self._zeilen.put(roh)
        self._zeilen.put(_EOF)

    def events(self) -> Iterator[dict | None]:
        while True:
            try:
                zeile = self._zeilen.get(timeout=_POLL_S)
            except queue.Empty:
                yield None
                continue
            if zeile is _EOF:
                break
            try:
                parsed = json.loads(zeile)
            except ValueError:
                continue  # keine stream-json-Zeile — kein Beweis, weg damit
            if isinstance(parsed, dict):
                yield parsed
        self._proc.wait()
        return self._proc.returncode

    def kill(self) -> None:
        # Die ganze Gruppe, nicht nur das Kind — ein Enkel ueberlebt sonst.
        sandbox._kill_group(self._proc)


def make_spawn(claude_bin: str = DEFAULT_BIN, *, platform: str = sys.platform) -> Spawn:
    """Der Produktions-Spawn: waehlt ein CONFINED Backend (nie unconfined),
    wickelt das Kommando hinein und startet es mit Netz AN.

    Netz ist die eine dokumentierte Abweichung von `run_shell`: das Kind muss
    die Anthropic-API erreichen. Gibt es kein einsperrendes Backend, gibt es
    keinen Job — fail-closed, kein stiller Direktlauf.
    """

    def spawn(argv: list[str], workspace: Path,
              env: dict[str, str], limits: SandboxLimits) -> _ProcHandle:
        backend = sandbox.select_backend(job_backends(platform))
        if backend is None:
            raise sandbox.SandboxUnavailable(
                "no confined sandbox backend available for claude jobs"
            )
        kommando = shlex.join([claude_bin, *argv])
        voll = backend.argv(kommando, workspace=workspace, allow_network=True)
        proc = subprocess.Popen(
            list(voll),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(workspace),
            env=dict(env),
            start_new_session=True,
        )
        return _ProcHandle(proc)

    return spawn


def _invalid(detail: str) -> dict[str, Any]:
    return {"ok": False, "kind": KIND_INVALID,
            "message": f"(Claude worker: invalid request — {detail[:200]})"}


def _fehler(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "kind": kind, "message": message}


def handle_frame(raw: bytes, jobs: _Jobs, *, spawn: Spawn | None = None,
                 limits: SandboxLimits | None = None) -> dict[str, Any]:
    """Eine Anfrage-Zeile → der Antwort-Frame. Wirft NIE: die Verbindung
    traegt den Fehler, der Daemon ueberlebt jeden Frame.

    Nur die benannten Felder werden gelesen — alles andere im Frame wird
    verworfen, nicht geglaubt. Ein `admin`-Feld des Callers existiert hier nicht.
    """
    try:
        frame = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _invalid("unreadable JSON")
    if not isinstance(frame, dict):
        return _invalid("frame is not an object")
    op = frame.get("op")
    hersteller = spawn if spawn is not None else make_spawn(
        os.environ.get("TALOS_CLAUDE_WORKER_BIN", DEFAULT_BIN))
    grenzen = limits if limits is not None else SandboxLimits(
        timeout_s=DEFAULT_JOB_TIMEOUT_S)
    try:
        if op == "submit":
            job_id = frame.get("job_id")
            prompt = frame.get("prompt")
            workspace = frame.get("workspace")
            if not isinstance(job_id, str) or not job_id.strip():
                return _invalid("job_id missing")
            if not isinstance(prompt, str) or not prompt.strip():
                return _invalid("prompt missing")
            if len(prompt) > MAX_PROMPT_CHARS:
                return _invalid("prompt exceeds the size cap")
            if not isinstance(workspace, str) or not os.path.isabs(workspace):
                return _invalid("workspace missing or not absolute")
            browser_mcp = frame.get("browser_mcp", False)
            if not isinstance(browser_mcp, bool):
                return _invalid("browser_mcp must be a boolean")
            mcp_roh = frame.get("mcp_servers", [])
            if (not isinstance(mcp_roh, list) or len(mcp_roh) > MAX_MCP_SERVERS
                    or not all(isinstance(n, str) and SERVER_NAME.fullmatch(n)
                               for n in mcp_roh)):
                return _invalid("mcp_servers must be a list of server names")
            backend = frame.get("backend", "claude")
            if not isinstance(backend, str) or backend not in ("claude", "agy"):
                return _invalid(f"unknown backend {backend!r}")
            if backend == "agy" and (browser_mcp or mcp_roh):
                return _invalid(
                    "mcp servers are only available on the claude backend")
            agy_hersteller: Spawn | None = None
            if backend == "agy" and spawn is None and jobs._agy is not None:
                # Der Produktions-Spawn fuer agy traegt die agy-Binary aus dem
                # Worker-Gate — kein Frame der Leitung waehlt sie.
                agy_hersteller = make_spawn(jobs._agy.bin)
            try:
                jobs.submit(job_id, prompt, workspace,
                            spawn=hersteller, limits=grenzen,
                            browser_mcp=browser_mcp, mcp_servers=mcp_roh,
                            backend=backend, agy_spawn=agy_hersteller)
            except _Busy:
                return _fehler(KIND_BUSY, "parallel job limit reached")
            except _Unavailable as fehlend:
                return _fehler(KIND_UNAVAILABLE, str(fehlend))
            except ValueError as ungueltig:
                return _invalid(str(ungueltig))
            return {"ok": True, "state": "accepted"}
        if op == "status":
            job_id = frame.get("job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                return _invalid("job_id missing")
            try:
                return jobs.status(job_id)
            except KeyError:
                return _fehler(KIND_UNKNOWN_JOB, f"unknown job {job_id!r}")
        return _invalid("unknown op")
    except Exception:
        # Der letzte Fang: ein kaputter Frame kostet die Verbindung, nie den Daemon.
        return _fehler(KIND_UNAVAILABLE, "worker-internal error")


class _RahmenZuGross(Exception):
    """Die Anfrage-Zeile ueberschreitet MAX_FRAME_BYTES ohne Zeilenende."""


def _lese_zeile(verbindung: socket.socket) -> bytes | None:
    """Eine Zeile vom Client. `None`: Timeout oder Verbindung ohne Inhalt."""
    puffer = bytearray()
    while True:
        if len(puffer) > MAX_FRAME_BYTES:
            raise _RahmenZuGross
        try:
            stueck = verbindung.recv(65536)
        except socket.timeout:
            return None
        if not stueck:
            return bytes(puffer) if puffer else None
        puffer += stueck
        if b"\n" in puffer:
            return bytes(puffer.split(b"\n", 1)[0])


def _bediene(verbindung: socket.socket, jobs: _Jobs,
             spawn: Spawn, limits: SandboxLimits) -> None:
    """Eine Verbindung: eine Zeile rein, eine Zeile raus. Jeder Fehler wird
    zur Antwort — der Accept-Loop sieht davon nichts."""
    try:
        verbindung.settimeout(READ_TIMEOUT_S)
        try:
            roh = _lese_zeile(verbindung)
        except _RahmenZuGross:
            antwort: dict[str, Any] = _invalid("frame exceeds the size cap")
        else:
            if roh is None:
                return  # Verbindung ohne Inhalt — nichts zu beantworten
            antwort = handle_frame(roh, jobs, spawn=spawn, limits=limits)
    except Exception:
        antwort = _fehler(KIND_UNAVAILABLE, "worker-internal error")
    try:
        verbindung.sendall(json.dumps(antwort).encode("utf-8") + b"\n")
    except OSError:
        pass


def _best_effort_owner(pfad: Path) -> None:
    """Socket `talos:talos-claude`, WENN die Rechte reichen — sonst still.
    Die Sicherheit traegt die Installation, nicht dieser Versuch."""
    try:
        shutil.chown(pfad, user="talos", group="talos-claude")
    except (OSError, LookupError):
        pass


def _positive(roh: str, default: int) -> int:
    try:
        wert = int(roh)
    except (TypeError, ValueError):
        return default
    return wert if wert > 0 else default


def serve(socket_path: str = DEFAULT_SOCKET, env_path: str = DEFAULT_ENV, *,
          environ: Mapping[str, str] | None = None,
          spawn: Spawn | None = None,
          stop: threading.Event | None = None) -> None:
    """Der Daemon: binden, Rechte setzen, Anfragen nacheinander bedienen.

    Sequentieller Accept-Loop wie beim Modell-Worker; die Jobs selbst laufen
    in eigenen Daemon-Threads unter der Tabelle. `stop` erlaubt Tests ein
    sauberes Ende; im Dienst laeuft die Schleife, bis systemd den Prozess
    beendet.
    """
    quelle = os.environ if environ is None else environ
    werte = _read_env_file(Path(env_path))

    def cfg(name: str, default: str = "") -> str:
        return quelle.get(name) or werte.get(name, default)

    worker_home = cfg("TALOS_CLAUDE_WORKER_HOME")
    if not worker_home:
        # Ohne dediziertes HOME laege der OAuth-Status im beschreibbaren
        # Arbeitsbereich eines Jobs — lieber gar nicht starten.
        raise RuntimeError("TALOS_CLAUDE_WORKER_HOME is not configured")
    claude_bin = cfg("TALOS_CLAUDE_WORKER_BIN", DEFAULT_BIN)
    max_parallel = _positive(cfg("TALOS_CLAUDE_WORKER_MAX_PARALLEL"), MAX_PARALLEL)
    timeout_s = min(
        _positive(cfg("TALOS_CLAUDE_WORKER_JOB_TIMEOUT"), DEFAULT_JOB_TIMEOUT_S),
        MAX_JOB_TIMEOUT_S,
    )
    # Das Browser-Gate des Dienstes: Vorgabe AUS. Es ist ein ZWEITES Schloss
    # neben dem agent-seitigen Schalter — der Worker vertraut keinem Frame,
    # und ein Versehen auf Agent-Seite oeffnet hier nichts.
    browser = BrowserMcp(
        enabled=cfg("TALOS_CLAUDE_WORKER_BROWSER_MCP") == "1",
        headless=cfg("TALOS_CLAUDE_WORKER_BROWSER_HEADLESS", "1") != "0",
        chrome_path=cfg("TALOS_CLAUDE_WORKER_BROWSER_CHROME"),
        command=cfg("TALOS_CLAUDE_WORKER_BROWSER_CMD"),
        chrome_args=cfg("TALOS_CLAUDE_WORKER_BROWSER_CHROME_ARGS"),
    )
    # Das generische MCP-Gate des Dienstes: Vorgabe LEER = kein Server. Es ist
    # die zweite Schicht neben der Registry-Datei — der Worker vertraut keinem
    # Frame, und ein Versehen auf Agent-Seite oeffnet hier nichts. Ungueltige
    # Namen in der Liste fallen still heraus; sie wuerden ohnehin nie matchen.
    mcp_enabled = frozenset(
        teil for teil in (t.strip() for t in
                          cfg("TALOS_CLAUDE_WORKER_MCP_SERVERS").split(","))
        if SERVER_NAME.fullmatch(teil))
    mcp_registry_pfad = cfg("TALOS_CLAUDE_WORKER_MCP_REGISTRY")
    mcp_registry = (McpServerRegistry.from_path(Path(mcp_registry_pfad))
                    if mcp_registry_pfad else McpServerRegistry())
    # Das agy-Gate des Dienstes: Vorgabe KEIN agy. Es ist ein eigenes Schloss
    # neben dem agent-seitigen — der Worker vertraut keinem Frame, und ein
    # Versehen auf Agent-Seite oeffnet hier nichts. Fehlt Binary oder agy-HOME
    # (oder zeigt eines ins Leere), gibt es das Backend nicht.
    agy = _agy_gate(cfg("TALOS_CLAUDE_WORKER_AGY_BIN"),
                    cfg("TALOS_CLAUDE_WORKER_AGY_HOME"))
    jobs = _Jobs(max_parallel=max_parallel, worker_home=worker_home,
                 browser=browser, mcp_registry=mcp_registry,
                 mcp_enabled=mcp_enabled, agy=agy)
    limits = SandboxLimits(timeout_s=timeout_s)
    # Kein Default-Spawn hier: handle_frame baut ihn selbst — und zwar pro
    # Backend (claude wie agy). Wer hier einen einzigen vorgefertigten Spawn
    # durchreicht, schickt die agy-Argv an die claude-Binary (gemessen am
    # ersten agy-Live-E2E: "unknown option '--print-timeout'"). Ein injizierter
    # Spawn gilt bewusst fuer beide Backends (Testpfad).
    hersteller = spawn

    pfad = Path(socket_path)
    if pfad.exists():
        # Nur ein liegengebliebener Socket wird ersetzt. Jede andere Datei
        # unter diesem Namen gehoert jemandem.
        if not stat.S_ISSOCK(pfad.stat().st_mode):
            raise RuntimeError(f"{pfad} exists and is not a socket")
        pfad.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(pfad))
        # 0660: Besitzer und Gruppe duerfen sprechen, der Rest der Maschine nicht.
        os.chmod(pfad, 0o660)
        _best_effort_owner(pfad)
        server.listen(8)
        server.settimeout(0.25)
        while stop is None or not stop.is_set():
            try:
                verbindung, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                continue  # z.B. EMFILE: haesslich, aber kein Grund zu sterben
            with verbindung:
                _bediene(verbindung, jobs, hersteller, limits)
    finally:
        server.close()
        try:
            if pfad.is_socket():
                pfad.unlink()
        except OSError:
            pass


def main() -> None:
    """Einstieg fuer `python -m talos.claudeworker` — alles Weitere ist Env."""
    serve(
        os.environ.get(SOCKET_ENV_VAR) or DEFAULT_SOCKET,
        os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV,
    )


if __name__ == "__main__":
    main()
