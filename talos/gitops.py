"""git — die Netz-Ops von git, durch dasselbe Gate.

Alles Lokale (status, log, diff, add, commit, branch) kann `run_shell` bereits:
kein Netz noetig, die Sandbox reicht. Dieses Werkzeug deckt genau den Rest ab,
den die Sandbox per Bauart verweigert — clone, fetch, pull, push — und gerade
WEIL dabei Credentials (`~/.ssh`) und entfernte, sichtbare Wirkung (push) im
Spiel sind, gelten vier haerte Regeln:

1. **Jede Op fragt.** Der erste Kontakt mit einem Remote ist eine
   Vertrauensentscheidung (woher kommt Code, wohin geht er), und ein push ist
   oeffentlich sichtbare Wirkung. Der Kernel antwortet darum ausnahmslos
   NEEDS_HUMAN (`policy._decide_git`); `outward` im Manifest schliesst die
   Attended-Auto-Freigabe per Bauart aus. Erleichterung nur als stehende Regel
   auf exakt (op, repo, url) — „clone von X" deckt „push nach X" nie.
2. **Die URL besteht dieselbe Tuer wie bei web_fetch.** https-Remotes gehen
   durch `web.guard_url` (kein Loopback, kein RFC 1918, kein CGNAT ohne
   operator-benannte Adresse). ssh-Remotes kann keine URL-Pruefung einordnen —
   sie werden gebaut, nie frei formuliert (Host-Slug, kein Whitespace), und
   bleiben ohnehin hinter der Freigabe aus Regel 1.
3. **Repos leben im Arbeitsbereich.** fetch/pull/push schreiben `.git` — die
   Sandbox laesst Schreiben nur dort zu, und dieses Modul prueft es realpath-
   basiert, bevor ein Prozess startet. Ein Repo ausserhalb ist eine ehrliche
   Weigerung, kein Sandbox-Fehler.
4. **Das Kommando wird gebaut, nie uebernommen.** Das Modell nennt op, Pfade
   und eine URL — Flags, Refspecs und Protokoll-Tricks (`--upload-pack`,
   `ext::`) kommen lexikalisch nicht auf die Kommandozeile. `pull` laeuft als
   `--ff-only`: ein Agent, der mergen darf, loest Konflikte, die niemand
   bestellt hat.

Der Client laeuft eingesperrt mit den zwei dokumentierten Abweichungen aus
`remoteexec.py`: Netz an, `~/.ssh` lesbar. Gibt es kein einsperrendes Backend,
wird verweigert (fail-closed).

Sprache: Kommentare deutsch, ausgegebene Texte englisch (Haus-Regel).
"""
from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Mapping

from . import remoteexec, sandbox, web
from .policy import WORKSPACE_DIR
from .web import UrlRefusedError, guard_url, parse_allowed_addresses

OPS = frozenset({"clone", "fetch", "pull", "push"})
# Was an eine Remote-Bezeichnung oder einen Branch-Namen darf: ein Token, kein
# Satz. Damit kommen weder Flags noch Shell-Metazeichen auf die Kommandozeile.
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
# ssh-Remote-Formen: [user@]host:pfad oder ssh://[user@]host/pfad. Der Host ist
# ein Slug — alles andere (Whitespace, Metazeichen) ist keine Remote, sondern
# ein Kommandoversuch.
_SSH_REMOTE = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9._/~%-]+$"
)
_SSH_URL = re.compile(
    r"^ssh://(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9.-]*(?::\d+)?/[A-Za-z0-9._/~%-]+$"
)
MAX_URL_CHARS = 500


class GitOpsError(ValueError):
    """Eine git-Op wird nicht gestartet. Der Text ist der ehrliche Grund."""


def _checked_remote(url: str, *, allowed_addresses: frozenset[str]) -> str:
    """Eine Remote-URL einordnen: https durch die guard_url-Tuer, ssh lexikalisch."""
    if not url or len(url) > MAX_URL_CHARS:
        raise GitOpsError(f"refused: remote url is empty or longer than {MAX_URL_CHARS}")
    if any(ch.isspace() for ch in url):
        raise GitOpsError("refused: remote url contains whitespace")
    if url.startswith(("https://", "http://")):
        # Dieselbe Tuer wie web_fetch — sie wirft UrlRefusedError mit Grund.
        guard_url(url, allow_http=False, allowed_addresses=allowed_addresses)
        return url
    if url.startswith("git://"):
        # Unverschluesselt und unauthentifiziert — dafuer gibt es keinen legitimen
        # Fall mehr, der nicht https oder ssh besser kann.
        raise GitOpsError("refused: git:// is unauthenticated — use https or ssh")
    if _SSH_REMOTE.fullmatch(url) or _SSH_URL.fullmatch(url):
        return url
    raise GitOpsError(
        f"refused: remote {url[:60]!r} is neither an https URL nor a plain ssh remote"
    )


def _checked_repo(path: str) -> Path:
    """Das Repo muss IM Arbeitsbereich liegen — realpath, fail-closed.

    `~` wird VOR der Pruefung aufgeloest: die Shell im Sandkasten expandiert ein
    gequotetes Tilde nicht (es bliebe ein harmloses Verzeichnis namens `~`), aber
    der Aufrufer MEINT sein Home — und eine Pruefung, die an der Meinung vorbei
    entscheidet, ist keine.
    """
    if not path or any(ch in path for ch in ("\x00",)):
        raise GitOpsError("refused: repo path must be non-empty text")
    root = os.path.realpath(WORKSPACE_DIR)
    roh = os.path.expanduser(path) if path.startswith("~") else path
    ziel = os.path.realpath(Path(root) / roh if not os.path.isabs(roh) else roh)
    if ziel != root and not ziel.startswith(root + os.sep):
        raise GitOpsError(
            f"refused: repo {path!r} is outside the workspace — git ops write, "
            "and writing is only possible in the workspace"
        )
    return Path(ziel)


def build_command(op: str, repo: str, url: str, branch: str) -> tuple[str, str]:
    """Die gebaute Kommandozeile und ihr Repo-Pfad — NIE frei formuliert."""
    if op not in OPS:
        raise GitOpsError(f"refused: op {op!r} — one of {', '.join(sorted(OPS))}")
    ziel = _checked_repo(repo)
    argv: list[str]
    if op == "clone":
        if not url:
            raise GitOpsError("refused: clone needs a remote url")
        argv = ["git", "clone", "--", url, str(ziel)]
    else:
        if not ziel.is_dir():
            raise GitOpsError(f"refused: {repo!r} is not a directory in the workspace")
        argv = ["git", "-C", str(ziel)]
        if op == "fetch":
            argv += ["fetch"] + (["--", url] if url else [])
        elif op == "pull":
            argv += ["pull", "--ff-only"] + (["--", url] if url else [])
        else:
            if branch and not url:
                # `git push -- <name>` liest den Namen als REMOTE, nicht als Branch.
                # Ein Branch ohne Remote ist mehrdeutig — lieber verweigern als raten.
                raise GitOpsError("refused: push with a branch needs the remote too")
            argv += ["push"]
            if url:
                argv += ["--", url]
            if branch:
                if not _SLUG.fullmatch(branch):
                    raise GitOpsError(f"refused: branch {branch!r} is not a plain name")
                argv += [branch]
    return shlex.join(argv), str(ziel)


def make_git_runner(
    *,
    allowed_addresses: frozenset[str] | None = None,
    platform: str = sys.platform,
):
    """Baut den `git`-Runner. `allowed_addresses`: operator-benannte Adressen
    (TALOS_WEB_ALLOWED_ADDRESSES), etwa der eigene Server im Tailnet."""

    def git(req: object) -> str:
        args = getattr(req, "args")
        op = str(args.get("op") or "").strip().lower()
        repo = str(args.get("repo") or "").strip()
        url = str(args.get("url") or "").strip()
        branch = str(args.get("branch") or "").strip()
        adressen = (
            allowed_addresses
            if allowed_addresses is not None
            else parse_allowed_addresses(os.environ.get("TALOS_WEB_ALLOWED_ADDRESSES", ""))
        )
        if url:
            url = _checked_remote(url, allowed_addresses=adressen)
        kommando, _ziel = build_command(op, repo, url, branch)
        backend = sandbox.select_backend(remoteexec.remote_backends(platform))
        if backend is None:
            return (
                "rc=refused\nno confined sandbox backend available for git "
                "(an unconfined client with credentials is not a degradation this tool accepts)"
            )
        result = sandbox.run_sandboxed(kommando, allow_network=True, backend=backend)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        tail = out if not err else f"{out}\n[stderr] {err}".strip()
        note = ""
        if result.timed_out:
            note = "\n[timed out]"
        elif result.truncated:
            note = "\n[output truncated]"
        return f"rc={result.returncode} [{result.backend}] git {op}\n{tail}{note}".strip()

    return git


# Der produktive Runner loest seine Freigabe-Adressen pro Aufruf selbst auf —
# die __main__-Verdrahtung ersetzt ihn durch den config-gebauten Runner.
def git(req: object) -> str:
    return make_git_runner()(req)
