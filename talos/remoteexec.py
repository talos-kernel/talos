"""remote_exec — ein Kommando auf einer anderen Maschine, durch dasselbe Gate.

`run_shell` laeuft eingesperrt: keine Credentials, kein Netz, Wurzel read-only. Dieses
Werkzeug ist die bewusste, schmale Ausnahme fuer Fernwartung (Tailnet-Hosts per ssh) —
und gerade WEIL die Wirkung auf einer anderen Maschine entsteht, gelten hier vier
haerte Regeln, die `run_shell` nicht braucht:

1. **Die Sandbox schuetzt hier nur den lokalen ssh-Clienten.** Was das Kommando auf
   der fernen Maschine anrichtet, kann keine lokale Isolation begrenzen. Deshalb
   antwortet der Kernel ausnahmslos NEEDS_HUMAN (`policy._decide_remote`) — der
   `SHELL_NEEDS_HUMAN=0`-Komfort der Sandbox gilt hier ausdruecklich nicht. Stehende
   Freigaben binden an exakt (host, command), nie an den Host allein
   (`standing.action_key`): „immer ssh mac" gibt es nicht, nur „immer genau dieses
   Kommando auf genau diesem Host".
2. **Die Host-Allowlist ist Betreiberkonfiguration, nie Modellargument.** Sie kommt
   aus `TALOS_REMOTE_HOSTS` (Kernel: `policy.remote_hosts`); ein Host ausserhalb der
   Liste ist DENY, bevor ein Prozess startet. Der Host geht als blosser ssh-Alias auf
   die Kommandozeile — Alias, Nutzer, Schluessel und Zieladresse loest die ssh-Config
   des Betreibers auf, nicht dieses Modul und schon gar nicht das Modell.
3. **Der Client laeuft trotzdem eingesperrt** — mit zwei dokumentierten Abweichungen
   von `run_shell`, und genau zwei: Netz AN (ohne Netz kein ssh) und `~/.ssh` LESBAR
   (ohne Schluessel kein Login). Alles andere bleibt maskiert, die Wurzel bleibt
   read-only, die Umgebung bleibt die Positivliste. Gibt es kein einsperrendes
   Backend, wird verweigert statt ungeschuetzt gesprochen (fail-closed).
4. **Der Hardline-Floor gilt auch fern.** `rm -rf /` ist auf jeder Maschine
   systemzerstörend — DENY, unbypassbar, egal wer gerade „ja" tippen wuerde. Der
   lokale PFAD-Floor gilt dagegen nicht: `/etc/hosts` im Kommando meint die ferne
   Maschine, und lokale Secret-Pfade dort hineinzulesen waere ein Fehlalarm, der
   echte Fernwartung unmoeglich macht. Die ehrliche Grenze ist die Freigabe mit
   vollem Kommandotext — nicht ein Regex, der sich Fernes nicht vorstellen kann.

Sprache: Kommentare deutsch, ausgegebene Texte englisch — Ergebnis und Weigerungen
gehen an das Modell und in die Konsole (wie in `skills.py` und `skillwrite.py`).
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Mapping, Sequence

from . import sandbox
from .policy import SHELL_FORBIDDEN_PREFIXES, remote_hosts

MAX_COMMAND_CHARS = 4_000

# ssh soll nie interaktiv werden (ein Prompt haenge den Lauf bis zur Zeitgrenze),
# eine tote Gegenstelle soll in Sekunden statt Minuten scheitern, und ein
# halboffener Tunnel soll nicht ewig als „laeuft" stehen.
_SSH_OPTS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=2",
)

# Defense in depth hinter der Allowlist: der Host steht als blosses Token in einem
# `/bin/sh -c`-String. Ein Alias des Betreibers ist ein Slug — alles andere darf
# lexikalisch gar nicht erst auf der Kommandozeile landen.
_HOST_SLUG = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")


class RemoteExecError(ValueError):
    """Ein Fernaufruf wird nicht gestartet. Der Text ist der ehrliche Grund."""


def _validated(args: dict, environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Host und Kommando pruefen — jede Weigerung vor dem ersten Byte."""
    host = str(args.get("host") or "").strip()
    command = str(args.get("command") or "")
    hosts = remote_hosts(environ)
    if not hosts:
        raise RemoteExecError(
            "refused: no remote hosts configured — the operator sets "
            "TALOS_REMOTE_HOSTS to the ssh aliases this tool may reach"
        )
    if not host:
        raise RemoteExecError(
            f"refused: missing host — configured aliases: {', '.join(hosts)}"
        )
    if not _HOST_SLUG.fullmatch(host):
        raise RemoteExecError(f"refused: host {host!r} is not a plain ssh alias")
    if host not in hosts:
        raise RemoteExecError(
            f"refused: host {host!r} is not in the operator's allowlist "
            f"({', '.join(hosts)})"
        )
    if not command.strip():
        raise RemoteExecError("refused: command must be non-empty text")
    if len(command) > MAX_COMMAND_CHARS:
        raise RemoteExecError(
            f"refused: command longer than {MAX_COMMAND_CHARS} characters"
        )
    return host, command


def build_command(host: str, command: str) -> str:
    """Die lokale Kommandozeile. `shlex.join` macht das Fernkommando zu GENAU einem
    argv-Element — ssh reicht es unverändert an die ferne Shell weiter, und die
    lokale Shell kann daraus kein zweites Kommando gewinnen."""
    return shlex.join(["ssh", *_SSH_OPTS, "--", host, command])


def _masked_without_ssh() -> tuple[str, ...]:
    """Die Floor-Maskenliste ohne `~/.ssh` — die eine dokumentierte Abweichung.

    Verglichen wird ueber realpath, weil die Liste selbst realpath-maskiert wird
    (`sandbox.mask_targets`): ein lexikalisch anderer Schreibweise desselben
    Verzeichnisses darf nicht zur Luecke in der Ausnahme werden — und ein
    versehentlich doppelt gelisteter Pfad nicht zu zwei Ausnahmen.
    """
    ssh_dir = os.path.realpath(Path.home() / ".ssh")
    return tuple(
        prefix for prefix in SHELL_FORBIDDEN_PREFIXES
        if os.path.realpath(prefix) != ssh_dir
    )


def remote_backends(platform: str = sys.platform) -> tuple[sandbox.Sandbox, ...]:
    """Einsperrende Kandidaten mit der .ssh-Ausnahme — OHNE unconfined.

    Dasselbe Versprechen wie `claudeworker.job_backends`: ein ungeschirmter
    Client ist keine Degradation, die man hinnehmen koennte. Die Klassen sind
    die der Standardliste, nur die Maskenliste weicht ab.
    """
    masked = _masked_without_ssh()
    if platform.startswith("linux"):
        return (sandbox.BubblewrapSandbox(masked=masked),)
    if platform == "darwin":
        return (sandbox.SandboxExecSandbox(masked=masked),)
    return ()


def remote_exec(req: object) -> str:
    """Der produktive Runner. Wirft nichts Unkontrolliertes: jede Weigerung ist Text."""
    args = getattr(req, "args")
    host, command = _validated(args)
    backend = sandbox.select_backend(remote_backends())
    if backend is None:
        return (
            "rc=refused\nno confined sandbox backend available for remote_exec "
            "(an unconfined ssh client is not a degradation this tool accepts)"
        )
    result = sandbox.run_sandboxed(
        build_command(host, command), allow_network=True, backend=backend
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    tail = out if not err else f"{out}\n[stderr] {err}".strip()
    note = ""
    if result.timed_out:
        note = "\n[timed out]"
    elif result.truncated:
        note = "\n[output truncated]"
    return f"rc={result.returncode} [{result.backend}] {host}\n{tail}{note}".strip()
