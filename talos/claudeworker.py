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

    →  {"op": "submit", "job_id": "…", "prompt": "…", "workspace": "…"}
    →  {"op": "status", "job_id": "…"}
    ←  {"ok": true, "state": "accepted"|"running"|"done"|"failed"|"timeout", …}
    ←  {"ok": false, "kind": "invalid_request"|"unknown_job"|"busy"|"unavailable",
        "message": "…"}

Bei "done" zusaetzlich: "summary" (aus dem `result`-Event des Streams),
"files" (Pfade relativ zum Arbeitsbereich, aus `tool_use`-Events — Beweis
kommt aus dem Stream, nie aus Prosa) und "returncode".

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
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import sandbox
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
    "SOCKET_ENV_VAR",
    "handle_frame",
    "job_backends",
    "job_env",
    "main",
    "make_spawn",
    "parse_stream_event",
    "serve",
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


def job_env(worker_home: str, workspace: Path) -> dict[str, str]:
    """Die Umgebung eines Job-Kindes — positive Allowlist, sonst nichts.

    Kein Talos-Geheimnis, kein Bridge-Token, keine Deployment-Env darf in ein
    Job gelangen. HOME ist das dedizierte Worker-Home (dort liegt nur der
    Claude-OAuth-Status), TMPDIR/PWD zeigen in den Arbeitsbereich — der einzige
    Ort, an dem das Kind ohnehin schreiben darf.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": worker_home,
        "LANG": "C.UTF-8",
        "TMPDIR": str(workspace),
        "PWD": str(workspace),
    }


def parse_stream_event(line: dict, workspace: Path) -> tuple[str | None, str | None]:
    """(summary, datei) aus einer stream-json-Zeile.

    Die Summary kommt NUR aus einem top-level `result`-Event; Dateien NUR aus
    `tool_use`-Inputs, deren aufgeloester Pfad innerhalb des Arbeitsbereichs
    bleibt. Beweis kommt aus dem Stream, nie aus Prosa — ein behaupteter Pfad
    ausserhalb des Käfigs faellt weg, er wird nicht umgeschrieben.
    """
    if not isinstance(line, dict):
        return (None, None)
    typ = line.get("type")
    if typ == "result":
        result = line.get("result")
        if isinstance(result, str) and result:
            return (result[:MAX_SUMMARY_CHARS], None)
        return (None, None)
    if typ == "tool_use":
        eingabe = line.get("input")
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


def _job_argv(prompt: str) -> list[str]:
    """Die Argumente NACH dem Binary. Das Binary selbst gehoert der
    Worker-Konfiguration (`make_spawn`) — kein Frame der Leitung darf es waehlen."""
    return [
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--allowedTools", ALLOWED_TOOLS,
    ]


class _Busy(Exception):
    """Das Parallel-Limit ist erreicht — der dritte Job wartet nicht, er faellt um."""


class _Job:
    """Ein Job in der fluechtigen Tabelle. Alles unter einem eigenen Schloss,
    damit `status` nie einen halb geschriebenen Zustand liest."""

    def __init__(self, job_id: str, prompt: str, workspace: str) -> None:
        self.job_id = job_id
        self.prompt = prompt
        self.workspace = workspace
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

    def __init__(self, *, max_parallel: int = MAX_PARALLEL, worker_home: str = "") -> None:
        self._max_parallel = max_parallel
        self._worker_home = worker_home
        self._schloss = threading.Lock()
        self._jobs: dict[str, _Job] = {}

    def submit(self, job_id: str, prompt: str, workspace: str, *,
               spawn: Spawn, limits: SandboxLimits) -> str:
        with self._schloss:
            if job_id in self._jobs:
                raise ValueError(f"job_id {job_id!r} is already known")
            aktive = sum(1 for j in self._jobs.values()
                         if j.state in ("accepted", "running"))
            if aktive >= self._max_parallel:
                raise _Busy
            job = _Job(job_id, prompt, workspace)
            self._jobs[job_id] = job
        deadline = time.monotonic() + limits.timeout_s
        thread = threading.Thread(
            target=_run_job,
            args=(job, self._worker_home, spawn, limits, deadline),
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
            antwort: dict[str, Any] = {"ok": True, "state": job.state}
            if job.state == "done":
                antwort["summary"] = job.summary
                antwort["files"] = list(job.files)
                antwort["returncode"] = job.returncode
            if job.state in ("failed", "timeout"):
                antwort["returncode"] = job.returncode
                if job.error:
                    antwort["error"] = job.error
            return antwort


def _run_job(job: _Job, worker_home: str, spawn: Spawn,
             limits: SandboxLimits, deadline: float) -> None:
    """Thread-Rumpf eines Jobs. Hält die Gesamt-Deadline ueber ALLEM — die
    Pruefung steht vor jedem `next()`, und das Handle liefert Herzschlaege,
    damit ein schweigendes Kind sie nicht aushungert."""
    workspace = Path(job.workspace)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as ungueltig:
        job.beenden("failed", fehler=f"workspace not creatable: {ungueltig}")
        return
    job.beenden("running", returncode=-1)
    argv = _job_argv(job.prompt)
    env = job_env(worker_home, workspace)
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
            try:
                jobs.submit(job_id, prompt, workspace,
                            spawn=hersteller, limits=grenzen)
            except _Busy:
                return _fehler(KIND_BUSY, "parallel job limit reached")
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
    jobs = _Jobs(max_parallel=max_parallel, worker_home=worker_home)
    limits = SandboxLimits(timeout_s=timeout_s)
    hersteller = spawn if spawn is not None else make_spawn(claude_bin)

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
