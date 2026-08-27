"""Echte Isolation fuer `run_shell` — begrenzt, was ein Kommando KANN.

Der Pfad-Floor (`policy.SHELL_FORBIDDEN_PREFIXES`) und der `command_floor` sehen einen
Shell-String. Ein String laesst sich beliebig neu zusammensetzen: `P=/etc; cat $P/passwd`
enthaelt kein einziges verbotenes Token, `eval "$(echo … | base64 -d)"` erst recht nicht.
Ein Regex ist damit ein Backstop, keine Grenze — genau so stand es als offene Luecke in
der CLAUDE.md.

**Die Sandbox ersetzt den Floor nicht, sie traegt ihn.** Beide bleiben, und wer eines von
beidem spaeter als Dopplung wegraeumt, macht das System schwaecher:

* Der Floor faengt das Offensichtliche frueh (billig, vor jeder Wirkung) und liefert dem
  Freigabe-Text seine Pfad-Einordnung (`policy.command_risk_paths`) — die Sandbox kann
  einem Menschen nichts erklaeren, sie kann nur verhindern.
* Die Sandbox begrenzt den Rest — alles, was der Floor per Konstruktion nicht sehen kann.

**Hier wird nie geraten, was ein Kommando vorhat.** Kein Parsen, kein Erraten, keine
Allowlist von Programmen. Nur Begrenzung: wo geschrieben werden darf, ob es ein Netz gibt,
welche Umgebung sichtbar ist, wie lange und wie viel.

Umsetzungen, ausgewaehlt nach Plattform und echter Verfuegbarkeit:

* **Linux — `bubblewrap` (`bwrap`):** eigener Mount-/PID-/Netz-Namensraum, Wurzel
  read-only, nur der Arbeitsbereich beschreibbar, `--unshare-all` (also auch ohne Netz).
* **macOS — `sandbox-exec`:** eng gefasstes Seatbelt-Profil.
* **`none`:** keine Isolation. Wird NUR benutzt, wenn der Betreiber sie ausdruecklich
  abschaltet (`TALOS_SANDBOX_ALLOW_UNCONFINED=1`). Ohne diesen Schalter wird der Aufruf
  verweigert statt still ungeschuetzt ausgefuehrt — fail-closed.

Die Maskenliste ist die Liste des Floors selbst (`SHELL_FORBIDDEN_PREFIXES`), nicht eine
zweite daneben: zwei Listen driften auseinander, und die stille Haelfte gewinnt. Preis
dieser Strenge auf Linux: `/etc` ist im Kind leer, also gibt es dort drin keine
Benutzernamen-Aufloesung, kein `resolv.conf` und keine CA-Wurzeln. Das ist kein Versehen,
sondern die Konsequenz daraus, dass der Floor `/etc` ohnehin fuer tabu erklaert — die
Sandbox macht die Erklaerung nur wahr.
"""
from __future__ import annotations

import atexit
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .policy import INSTALL_DIR, SHELL_FORBIDDEN_PREFIXES, WORKSPACE_DIR

# Der Schalter heisst absichtlich lang und unangenehm. Ein Betreiber soll ihn bewusst
# setzen koennen — aber nie versehentlich in einer Zeile mitschleppen, die er fuer
# Bequemlichkeit haelt.
UNCONFINED_ENV = "TALOS_SANDBOX_ALLOW_UNCONFINED"

SHELL_BIN = "bash"

# Einmal pro Prozess erzeugt (siehe `_identity_files`), danach wiederverwendet.
_IDENTITY_CACHE: tuple[str, str] | None = None

# Positivliste, keine Verbotsliste. Eine Verbotsliste vergisst immer die naechste
# Variable — und die naechste Variable ist erfahrungsgemaess die mit dem Token drin.
# Was hier nicht steht, sieht das Kind nicht: kein ANTHROPIC_*, OPENAI_*, TELEGRAM_*,
# WHATSAPP_*, AWS_*, GITHUB_* und auch nichts, was es davon morgen erst geben wird.
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"}
)

DEFAULT_TIMEOUT_S = 60
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
# CPU-Sekunden als zweite Bremse neben der Wanduhr: eine Endlosschleife, die nichts
# ausgibt, laeuft sonst bis zur Zeitgrenze auf voller Last.
CPU_GRACE_S = 5
_READ_CHUNK = 8192
_PROBE_TIMEOUT_S = 10
# Wie lange auf die Lesefaeden gewartet wird, nachdem der Prozess weg ist. Ein Enkel,
# der die Pipe geerbt hat und ueberlebt, darf den Aufrufer nicht ewig festhalten.
_JOIN_TIMEOUT_S = 2.0


class SandboxUnavailable(RuntimeError):
    """Keine Isolation verfuegbar und der Betreiber hat nichts anderes verlangt."""


@dataclass(frozen=True)
class SandboxLimits:
    """Grenzen eines Laufs. `0` heisst jeweils: diese Grenze bleibt aus.

    `max_memory_bytes` und `max_processes` sind bewusst per Vorgabe AUS:

    * `RLIMIT_AS` laesst sich auf macOS (arm64) gar nicht setzen — der Versuch scheitert,
      und im `preexec_fn` wuerde daran der komplette Start haengen.
    * `RLIMIT_NPROC` zaehlt alle Prozesse der UID, nicht nur die eigenen Kinder. Ein
      scheinbar grosszuegiger Wert kann auf einer belebten Maschine sofort jeden `fork`
      abweisen. Wer die Grenze will, muss sie fuer seine Maschine waehlen.
    """

    timeout_s: int = DEFAULT_TIMEOUT_S
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_memory_bytes: int = 0
    max_processes: int = 0


@dataclass(frozen=True)
class SandboxResult:
    """Ergebnis eines Laufs — mit dem Namen der Umsetzung, die ihn eingesperrt hat."""

    backend: str
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False


class Sandbox(Protocol):
    """Austauschbare Umsetzung: sagt, ob sie kann, und baut die Kommandozeile.

    Die Umsetzungen bauen NUR Argumente. Prozessstart, Grenzen, Deckel und Abbruch
    liegen an genau einer Stelle (`SandboxedShell`) — sonst haette jede Plattform ihre
    eigene halb richtige Kopie davon.
    """

    name: str

    def available(self) -> bool: ...

    def argv(
        self, command: str, *, workspace: Path, allow_network: bool = False
    ) -> tuple[str, ...]: ...


def mask_targets(prefixes: Sequence[str]) -> tuple[tuple[str, bool], ...]:
    """`(realpath, ist_verzeichnis)` fuer jeden Praefix, der wirklich existiert.

    Nicht vorhandene Pfade fliegen raus: verraten koennen sie nichts, aber `bwrap` bricht
    den GANZEN Lauf ab, wenn es einen `--tmpfs`-Mountpunkt nicht anlegen kann
    („Can't mkdir parents … Read-only file system"). Eine Maske, die den Lauf killt,
    schuetzt niemanden.

    Realpath, weil ein Praefix selbst ein Symlink sein kann (macOS: `/etc` -> `/private/etc`).
    Maskiert wird das Ziel — der Link fuehrt dann ins Leere statt an der Maske vorbei.
    """
    found: dict[str, bool] = {}
    for prefix in prefixes:
        real = os.path.realpath(prefix)
        if real in found or not os.path.exists(real):
            continue
        found[real] = os.path.isdir(real)
    return tuple(found.items())


def _identity_line(path: str, uid_or_gid: int) -> str:
    """Die EINE Zeile aus `/etc/passwd` bzw. `/etc/group`, die uns selbst beschreibt.

    Gelesen wird die echte Datei, nicht `pwd`/`grp`: auf Systemen mit LDAP/SSSD kaeme
    ueber die Bibliothek ein Eintrag, den es in der Datei gar nicht gibt — und im Kind
    steht am Ende trotzdem nur eine Datei.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split(":")
                if len(parts) > 2 and parts[2].strip() == str(uid_or_gid):
                    return line if line.endswith("\n") else line + "\n"
    except OSError:
        pass
    return ""


def _identity_files() -> tuple[str, str] | None:
    """Legt eine minimale `passwd`/`group` an — NUR die eigene Zeile, sonst nichts.

    Warum das noetig ist: `/etc` wird im Kind als leeres tmpfs ueberdeckt, damit
    `cat /etc/shadow` ins Leere greift. Dabei verschwindet aber auch die Zuordnung
    `uid -> Name`, und die braucht mehr, als man denkt. `whoami` scheitert, `git`
    verweigert den Commit ohne Autor, und **`ssh` bricht ab, bevor es ueberhaupt eine
    Verbindung versucht** (`No user exists for uid …`) — der Fehler sieht dann nach
    einem kaputten Zielrechner aus, obwohl er rein lokal ist. Genau das ist einmal
    passiert, als der Agent den Status eines entfernten Rechners holen sollte.

    Warum es keine Lockerung ist: das Kind kennt seine eigene UID ohnehin (`id -u`),
    und mehr steht hier nicht drin. Die ECHTE `/etc/passwd` bleibt unerreichbar —
    kein `root`, keine Dienstkonten, keine Shells anderer Nutzer. Ein Red-Team-Fall
    haelt genau das fest.

    Die Dateien liegen in einem 0700-Verzeichnis ausserhalb des Arbeitsbereichs: die
    Wurzel ist im Kind read-only gebunden, also kann kein Lauf seine eigene Identitaet
    fuer den naechsten Lauf faelschen. Im Arbeitsbereich waere genau das moeglich.
    """
    global _IDENTITY_CACHE
    if _IDENTITY_CACHE is not None:
        return _IDENTITY_CACHE
    if not hasattr(os, "getuid"):
        return None
    passwd = _identity_line("/etc/passwd", os.getuid())
    group = _identity_line("/etc/group", os.getgid())
    if not passwd:
        # Ohne echte Zeile wird nichts erfunden: ein ausgedachter Name waere schlimmer
        # als ein fehlender, weil er sich durch jede Ausgabe zieht.
        return None
    try:
        directory = Path(tempfile.mkdtemp(prefix="talos-identity-"))
        passwd_file = directory / "passwd"
        group_file = directory / "group"
        passwd_file.write_text(passwd, encoding="utf-8")
        group_file.write_text(group or "", encoding="utf-8")
        passwd_file.chmod(0o444)
        group_file.chmod(0o444)
    except OSError:
        return None
    atexit.register(shutil.rmtree, directory, True)
    _IDENTITY_CACHE = (str(passwd_file), str(group_file))
    return _IDENTITY_CACHE


# Steht in der reduzierten Umgebung JEDES Sandbox-Laufs. `askcli` liest ihn: koennte
# der Agent in seiner eigenen Shell `talos ask` starten, haette er einen Weg, sich
# selbst Auftraege zu geben — ohne Kanal, ohne fremde Kennung, ohne Leser.
MARKER = "TALOS_SANDBOX"


def sandbox_env(workspace: Path) -> dict[str, str]:
    """Die gesaeuberte Umgebung des Kindes — Positivliste, plattformunabhaengig.

    Bewusst hier und nicht in den Umsetzungen: die Zusicherung „keine Schluessel im Kind"
    darf nicht davon abhaengen, welches Backend gerade greift.

    `TMPDIR` zeigt in den Arbeitsbereich, weil es sonst der einzige Ort waere, an dem ein
    Programm ausserhalb schreiben wollte — und dann an der Sandbox scheitert, obwohl es
    voellig harmlos ist (Python auf macOS tut genau das beim Start).
    """
    env = {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}
    env.setdefault("PATH", os.defpath)
    env["TMPDIR"] = str(workspace)
    env["PWD"] = str(workspace)
    # ⚠️ GESETZT, nicht durchgereicht. `PYTHONPATH` steht bewusst nicht in der
    # `ENV_ALLOWLIST`: geerbt truege er den Wert des Elternprozesses in die Sandbox, und
    # ein Pfad, der irgendwo hinzeigt, ist genau der Weg, auf dem fremder Code in jedes
    # `python` im Sandkasten geraet. Hier steht ein fester, nur lesbar eingehaengter Pfad.
    #
    # Warum ueberhaupt: die Shell startet im Arbeitsbereich (`--chdir`), und von dort war
    # das eigene Paket nicht importierbar — `python -m pytest tests/` endete mit
    # „No module named 'talos'". Der Agent konnte damit ueber seinen eigenen Zustand
    # nichts beweisen, obwohl genau das der Punkt dieses Projekts ist.
    #
    # Es erweitert seine Macht nicht: die Grenze ist der Sandkasten, nicht die
    # Importierbarkeit. Wer die Runner von Hand aufruft, sitzt weiterhin in einer
    # Umgebung mit nur lesbarer Wurzel, ohne Netz und mit maskierten Geheimnispfaden —
    # und der Kernel, der Rechte vergibt, laeuft in einem ANDEREN Prozess.
    env["PYTHONPATH"] = str(INSTALL_DIR)
    # Und der Deutersatz dazu. `PYTHONPATH` allein reichte nicht: der Agent griff zum
    # `python3` aus dem PATH — dem System-Interpreter, in dem `pytest` gar nicht liegt —
    # und meldete „No module named pytest", waehrend zwei Verzeichnisse weiter eine
    # vollstaendige Umgebung stand. Der halbe Fix sah aus wie ein Fehlschlag der Sache.
    #
    # Die eigene `.venv` liegt unter `INSTALL_DIR` und ist im Sandkasten NUR LESBAR
    # eingehaengt: der Agent kann dort nichts hinlegen, was er danach als Programm
    # aufruft. Vorangestellt statt angehaengt, damit `python` in seinem eigenen Baum
    # auch wirklich seinen eigenen Interpreter meint.
    venv_bin = INSTALL_DIR / ".venv" / "bin"
    if venv_bin.is_dir():
        env["PATH"] = f"{venv_bin}{os.pathsep}{env['PATH']}"
    env[MARKER] = "1"
    return env


def _probe(argv: Sequence[str]) -> bool:
    """Echter Probelauf statt `which`.

    `bwrap` liegt auf vielen Systemen herum und scheitert trotzdem, weil der Kernel keine
    unprivilegierten User-Namespaces erlaubt. Ein vorhandenes Programm ist keine
    vorhandene Isolation — nur ein geglueckter Lauf ist einer.
    """
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class BubblewrapSandbox:
    """Linux: eigener Namensraum, Wurzel read-only, nur der Arbeitsbereich beschreibbar.

    `--unshare-all` nimmt Mount-, PID-, IPC-, UTS-, Cgroup- UND Netz-Namensraum. Netz
    kommt nur zurueck, wenn der Aufrufer es ausdruecklich verlangt (`--share-net`).
    `--die-with-parent` sorgt dafuer, dass nichts uebrig bleibt, wenn Talos stirbt.
    """

    name = "bubblewrap"

    def __init__(
        self,
        binary: str = "bwrap",
        masked: Sequence[str] = SHELL_FORBIDDEN_PREFIXES,
    ) -> None:
        self._binary = binary
        self._masked = tuple(masked)
        self._probed: bool | None = None

    def available(self) -> bool:
        if self._probed is None:
            self._probed = _probe(
                (self._binary, "--unshare-all", "--ro-bind", "/", "/", "--", "/bin/true")
            )
        return self._probed

    def argv(
        self, command: str, *, workspace: Path, allow_network: bool = False
    ) -> tuple[str, ...]:
        """Reihenfolge ist Bedeutung: spaetere Bindungen ueberdecken fruehere.

        Erst die Wurzel read-only, dann die Masken darueber, ganz zuletzt der
        Arbeitsbereich — so bleibt er beschreibbar, selbst wenn er unter einem
        maskierten Pfad laege.
        """
        workspace_path = str(workspace)
        args = [self._binary, "--die-with-parent", "--new-session", "--unshare-all"]
        if allow_network:
            args.append("--share-net")
        # Bewusst KEIN `--tmpfs /tmp`. Das sieht nach Hygiene aus, reisst aber ein Loch:
        # ein tmpfs ist BESCHREIBBAR, also gaebe es ausserhalb des Arbeitsbereichs
        # ploetzlich wieder einen Ort zum Schreiben — und alles, was unter /tmp liegt
        # (auf manchen Maschinen auch der Python-Interpreter), verschwaende zusaetzlich
        # aus der Sicht des Kindes. Ueber die read-only Wurzel ist /tmp bereits gesperrt;
        # Programme, die Temporaeres brauchen, finden es ueber TMPDIR im Arbeitsbereich.
        args += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for path, is_dir in mask_targets(self._masked):
            # Verzeichnis -> leeres tmpfs darueber. Datei -> /dev/null darueber; ein Lesen
            # scheitert dann mit EACCES, statt den Inhalt zu zeigen.
            args += ["--tmpfs", path] if is_dir else ["--ro-bind", "/dev/null", path]
        # NACH den Masken, weil spaetere Bindungen frueherer ueberdecken: das leere
        # `/etc` bekommt seine EINE passwd/group-Zeile zurueck. Ohne sie kennt das Kind
        # den eigenen Benutzernamen nicht, und Programme scheitern an etwas, das mit
        # ihrer Aufgabe nichts zu tun hat (`ssh` bricht ab, bevor es das Netz anfasst).
        identity = _identity_files()
        if identity is not None:
            passwd_file, group_file = identity
            args += ["--ro-bind", passwd_file, "/etc/passwd"]
            args += ["--ro-bind", group_file, "/etc/group"]
        if allow_network:
            # Netz ohne DNS ist kein Netz: `/etc` liegt unter der leeren tmpfs-Maske,
            # also kommen die Resolver-Dateien einzeln zurueck (spaetere Bindung
            # ueberdeckt fruehere). Gemessen am ersten 0.11-E2E: ein `claude`-Job
            # scheiterte mit `Unable to connect to API`, weil `/etc/resolv.conf` fehlte.
            # realpath, weil resolv.conf gern ein Symlink nach /run ist — der Link
            # selbst wuerde ins Leere zeigen.
            for name in ("resolv.conf", "nsswitch.conf", "hosts"):
                quelle = f"/etc/{name}"
                if os.path.exists(quelle):
                    args += ["--ro-bind", os.path.realpath(quelle), quelle]
        args += ["--bind", workspace_path, workspace_path, "--chdir", workspace_path]
        args += ["--", SHELL_BIN, "-c", command]
        return tuple(args)


def _sb_string(value: str) -> str:
    """Zeichenkette fuers Seatbelt-Profil. Ein Pfad mit `"` darf das Profil nicht sprengen."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class SandboxExecSandbox:
    """macOS: `sandbox-exec` mit einem eng gefassten Seatbelt-Profil.

    **Apple fuehrt `sandbox-exec` seit OS X 10.10 ausdruecklich als veraltet** (die
    Manpage sagt es in der ersten Zeile). Es ist trotzdem die einzige eingebaute
    Isolation ohne Fremdabhaengigkeit, und der Kernel-Unterbau (Seatbelt) ist unveraendert
    in Benutzung. Faellt das Programm irgendwann weg, faellt `available()` durch, und der
    Aufruf wird verweigert — die Veralterung fuehrt zu einem Nein, nie zu einem stillen Ja.

    Profil-Logik: `(deny default)` sperrt alles, danach wird gezielt geoeffnet. Spaetere
    Regeln gewinnen, deshalb steht das `deny file-read*` der geschuetzten Praefixe hinter
    dem allgemeinen `allow file-read*`.
    """

    name = "sandbox-exec"

    def __init__(
        self,
        binary: str = "/usr/bin/sandbox-exec",
        masked: Sequence[str] = SHELL_FORBIDDEN_PREFIXES,
    ) -> None:
        self._binary = binary
        self._masked = tuple(masked)
        self._probed: bool | None = None

    def available(self) -> bool:
        if self._probed is None:
            self._probed = _probe((self._binary, "-p", "(version 1)(allow default)", "/usr/bin/true"))
        return self._probed

    def profile(self, workspace: Path, *, allow_network: bool = False) -> str:
        """Das Seatbelt-Profil als Text — rein, damit es sich pruefen laesst."""
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec*)",
            "(allow process-fork)",
            "(allow sysctl-read)",
            "(allow signal (target self))",
            "(allow mach-lookup)",
            "(allow file-read*)",
        ]
        if self._masked:
            subpaths = " ".join(f"(subpath {_sb_string(p)})" for p in self._masked)
            lines.append(f"(deny file-read* {subpaths})")
        lines += [
            f"(allow file-write* (subpath {_sb_string(str(workspace))}))",
            '(allow file-write-data (literal "/dev/null") (literal "/dev/zero")'
            ' (literal "/dev/random") (literal "/dev/urandom")'
            ' (literal "/dev/dtracehelper") (literal "/dev/tty"))',
            "(allow network*)" if allow_network else "(deny network*)",
        ]
        return "\n".join(lines)

    def argv(
        self, command: str, *, workspace: Path, allow_network: bool = False
    ) -> tuple[str, ...]:
        profile = self.profile(workspace, allow_network=allow_network)
        return (self._binary, "-p", profile, SHELL_BIN, "-c", command)


class UnconfinedSandbox:
    """KEINE Isolation — das Kommando laeuft mit allen Rechten des Agenten.

    Existiert nur, damit der Ausnahmefall einen Namen hat, der in jeder Quittung
    auftaucht (`SandboxResult.backend == "none"`). Ausgewaehlt wird er ausschliesslich,
    wenn der Betreiber `TALOS_SANDBOX_ALLOW_UNCONFINED=1` gesetzt hat.
    """

    name = "none"

    def available(self) -> bool:
        return True

    def argv(
        self, command: str, *, workspace: Path, allow_network: bool = False
    ) -> tuple[str, ...]:
        return (SHELL_BIN, "-c", command)


def default_backends(platform: str = sys.platform) -> tuple[Sandbox, ...]:
    """Kandidaten fuer diese Plattform, in der Reihenfolge der Bevorzugung."""
    if platform.startswith("linux"):
        return (BubblewrapSandbox(),)
    if platform == "darwin":
        return (SandboxExecSandbox(),)
    return ()


def select_backend(candidates: Sequence[Sandbox]) -> Sandbox | None:
    """Die erste Umsetzung, die einen Probelauf uebersteht. Sonst `None`."""
    for candidate in candidates:
        if candidate.available():
            return candidate
    return None


def unconfined_allowed(env: Mapping[str, str] | None = None) -> bool:
    """Hat der Betreiber die Isolation ausdruecklich abgeschaltet?"""
    source = os.environ if env is None else env
    return str(source.get(UNCONFINED_ENV, "")).strip() == "1"


def unavailable_message(platform: str = sys.platform) -> str:
    """Sagt, was fehlt und wie man es bekommt — nicht nur, dass etwas fehlt."""
    if platform.startswith("linux"):
        missing = (
            "bubblewrap is missing or the kernel refuses unprivileged user namespaces "
            "(Debian/Raspberry Pi OS: sudo apt install bubblewrap)"
        )
    elif platform == "darwin":
        missing = "sandbox-exec is missing or refused to start"
    else:
        missing = f"no sandbox implementation exists for platform {platform!r}"
    return (
        f"Refusing to run a shell command without isolation: {missing}. "
        f"Set {UNCONFINED_ENV}=1 to run shell commands with the agent's full rights instead."
    )


def _set_limit(which: int | None, value: int) -> None:
    """Eine Grenze setzen, best effort. Jede einzeln gekapselt — siehe `_rlimit_hook`."""
    if which is None:
        return
    try:
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass


def _rlimit_hook(limits: SandboxLimits) -> Callable[[], None]:
    """Grenzen im Kind setzen, bevor `exec` laeuft.

    Jede Grenze ist einzeln gekapselt, weil eine Ausnahme im `preexec_fn` den kompletten
    Start scheitern laesst: auf macOS/arm64 ist `RLIMIT_AS` nicht setzbar — ohne die
    Kapselung koennte dort ueberhaupt kein Kommando mehr starten.

    `RLIMIT_CPU` zaehlt pro Prozess, nicht pro Gruppe. Es ist damit eine Bremse, kein
    Riegel — der Riegel ist die Wanduhr in `SandboxedShell._collect`.
    """
    cpu_seconds = max(1, int(limits.timeout_s) + CPU_GRACE_S)

    def apply() -> None:
        _set_limit(resource.RLIMIT_CPU, cpu_seconds)
        if limits.max_memory_bytes > 0:
            _set_limit(getattr(resource, "RLIMIT_AS", None), limits.max_memory_bytes)
        if limits.max_processes > 0:
            _set_limit(getattr(resource, "RLIMIT_NPROC", None), limits.max_processes)

    return apply


def _kill_group(proc: subprocess.Popen) -> None:
    """Die ganze Prozessgruppe beenden — ein Kind ueberlebt seinen Vater sonst muehelos.

    Gleiches Muster wie `reasoner._kill_group`: `start_new_session=True` beim Start,
    `killpg` beim Abbruch. Nur `proc.kill()` erwischt den Enkel nicht, und `/stop` waere
    dann eine Behauptung statt einer Tatsache.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class _OutputPump(threading.Thread):
    """Liest einen Stream mit Deckel: gelesen wird alles, behalten nur der Anfang.

    Warum weiterlesen statt aufhoeren, wenn der Deckel voll ist: wer aufhoert zu lesen,
    laesst das Kind an einer vollen Pipe blockieren. Dann laeuft nicht der Speicher voll,
    sondern die Uhr — und die Zeitgrenze erschiesst ein Kommando, das laengst fertig war.
    """

    def __init__(self, stream, cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = max(0, int(cap))
        self._chunks: list[bytes] = []
        self._kept = 0
        self.truncated = False

    def run(self) -> None:
        with self._stream as stream:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    return
                room = self._cap - self._kept
                if len(chunk) > room:
                    self.truncated = True
                if room <= 0:
                    continue
                self._chunks.append(chunk[:room])
                self._kept += min(len(chunk), room)

    @property
    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


class SandboxedShell:
    """Fuehrt Shell-Kommandos isoliert aus. Fail-closed, abbrechbar, gedeckelt.

    Ein Objekt haelt hoechstens einen laufenden Prozess; `cancel()` schiesst dessen ganze
    Gruppe ab. Die Wahl der Umsetzung passiert beim ersten Lauf und nicht im Konstruktor,
    damit ein Probelauf nicht schon beim Hochfahren von Talos Zeit kostet.
    """

    def __init__(
        self,
        *,
        backend: Sandbox | None = None,
        limits: SandboxLimits | None = None,
        workspace: Path | str = WORKSPACE_DIR,
        platform: str = sys.platform,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._explicit = backend
        self._limits = limits or SandboxLimits()
        # Realpath, sonst passt die Schreiberlaubnis auf die falsche Zeichenkette:
        # auf macOS ist `/var` ein Symlink auf `/private/var`, ein Arbeitsbereich unter
        # `/var/folders/...` war damit im Seatbelt-Profil ein Pfad, den es nicht gibt —
        # der Agent konnte in seinem eigenen Arbeitsbereich nichts schreiben. Dieselbe
        # Falle wie `policy._both_forms`, nur von der anderen Seite.
        self._workspace = Path(os.path.realpath(workspace))
        self._platform = platform
        self._env = env
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    @property
    def workspace(self) -> Path:
        return self._workspace

    def backend(self) -> Sandbox:
        """Die Umsetzung fuer diese Maschine — oder ein Nein.

        Ein ausdruecklich uebergebenes Backend wird NICHT gegen ein anderes getauscht,
        wenn es nicht kann: „nimm bwrap" heisst bwrap, nicht „irgendwas".
        """
        candidates = (self._explicit,) if self._explicit is not None else default_backends(self._platform)
        chosen = select_backend(candidates)
        if chosen is not None:
            return chosen
        if unconfined_allowed(self._env):
            return UnconfinedSandbox()
        raise SandboxUnavailable(unavailable_message(self._platform))

    def run(self, command: str, *, allow_network: bool = False) -> SandboxResult:
        """Ein Kommando isoliert ausfuehren. Wirft `SandboxUnavailable`, wenn nichts sperrt."""
        chosen = self.backend()
        self._workspace.mkdir(parents=True, exist_ok=True)
        argv = chosen.argv(str(command), workspace=self._workspace, allow_network=allow_network)
        with self._lock:
            # Popen unter dem Schloss: sonst kann ein `cancel()` genau zwischen Start und
            # Ablage stattfinden und laeuft ins Leere, waehrend der Prozess weiterlaeuft.
            self._cancelled = False
            proc = subprocess.Popen(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._workspace),
                env=sandbox_env(self._workspace),
                start_new_session=True,
                preexec_fn=_rlimit_hook(self._limits),
            )
            self._proc = proc
        return self._collect(proc, chosen.name)

    def _collect(self, proc: subprocess.Popen, backend_name: str) -> SandboxResult:
        out = _OutputPump(proc.stdout, self._limits.max_output_bytes)
        err = _OutputPump(proc.stderr, self._limits.max_output_bytes)
        out.start()
        err.start()
        timed_out = False
        try:
            proc.wait(timeout=self._limits.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        finally:
            out.join(_JOIN_TIMEOUT_S)
            err.join(_JOIN_TIMEOUT_S)
            with self._lock:
                cancelled = self._cancelled
                self._proc = None
        return SandboxResult(
            backend=backend_name,
            returncode=proc.returncode,
            stdout=out.text,
            stderr=err.text,
            truncated=out.truncated or err.truncated,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def cancel(self) -> bool:
        """True, wenn wirklich ein Lauf abgeschossen wurde. False heisst: es lief nichts."""
        with self._lock:
            proc = self._proc
            if proc is None:
                return False
            self._cancelled = True
        _kill_group(proc)
        return True


def run_sandboxed(
    command: str,
    *,
    workspace: Path | str = WORKSPACE_DIR,
    limits: SandboxLimits | None = None,
    allow_network: bool = False,
    backend: Sandbox | None = None,
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
) -> SandboxResult:
    """Einmal-Aufruf ohne eigenes Objekt — fuer alles, was nicht abbrechbar sein muss."""
    shell = SandboxedShell(
        backend=backend, limits=limits, workspace=workspace, platform=platform, env=env
    )
    return shell.run(command, allow_network=allow_network)
