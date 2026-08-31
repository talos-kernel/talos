"""Command-Floor für exec-Tools — modelliert nach Hermes' `tools/approval.py`.

Zwei Ebenen (Hermes' Philosophie, „nimm Hermes als Beispiel"):
- **Hardline:** katastrophal, ohne Recovery-Pfad → NIE ausführbar, unbypassbar.
  Bewusst winzig: Root-/Systemverzeichnis-Löschung, mkfs, dd auf Blockdevice, Fork-Bomb,
  kill -1, shutdown/reboot.
- **Dangerous:** rückrollbar-aber-riskant (curl|sh, chmod -R, git reset --hard, rm -rf im tmp).
  Bei Hermes lässt yolo das durch; Talos ist autonom über Telegram → wir routen es zu
  NEEDS_HUMAN (den Betreiber fragen), statt still auszuführen.

Regex-Ansatz wie Hermes (Backup-Ebene). Strukturelles Parsen (tree-sitter/bashlex) kommt in M3.
"""
from __future__ import annotations

import re

_FLAGS = re.IGNORECASE | re.DOTALL

# Startposition eines Kommandos: Zeilenanfang, nach Separator, Subshell, sudo/env-Wrapper.
_CMDPOS = (
    r"(?:^|[\n`;|&]|\$\()\s*"
    r"(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+(?:\w+=\S*\s+)*)?"
    r"(?:(?:exec|nohup|setsid|time)\s+)*\s*"
)
_RM = _CMDPOS + r"rm\s+(?:-\S*\s+)*"

HARDLINE_PATTERNS: tuple[tuple[str, str], ...] = (
    (_RM + r"""["']?/(?:\s|["']|$|\*)""", "recursive delete of the root filesystem"),
    # /home ist zweigeteilt: die Wurzel und User-Homes (erste Ebene) sind
    # katastrophal — TIEFERE Pfade (/home/<user>/.cache/...) sind keine
    # Systemverzeichnisse und gehoeren auf die Freigabe-Ebene (dangerous),
    # sonst blockiert die Totalsperre gewoehnlichen Cache-Cleanup.
    (_RM + r"""["']?(?:/root|/etc|/usr|/var|/bin|/sbin|/boot|/lib)(?:/\*)?\b""",
     "recursive delete of a system directory"),
    (_RM + r"""["']?/home(?:/[^\s/"';|&]+)?(?:/\*?|/)?["']?(?:\s|$)""",
     "recursive delete of a home directory"),
    (_RM + r"""["']?(?:~|\$\{?HOME\}?)(?:/\*)?["']?(?:\s|$)""", "recursive delete of the home directory"),
    (r"\bmkfs(\.[a-z0-9]+)?\b", "formatting a filesystem (mkfs)"),
    (r"\bdd\b[^\n]*\bof=/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*", "dd onto a raw block device"),
    (r">\s*/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "redirect onto a raw block device"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "Fork-Bomb"),
    (r"\bkill\s+(?:-\S+\s+)*-1\b", "killing every process (kill -1)"),
    (_CMDPOS + r"(?:shutdown|reboot|halt|poweroff)\b", "system shutdown/reboot"),
    (_CMDPOS + r"systemctl\s+(?:poweroff|reboot|halt|kexec)\b", "systemctl poweroff/reboot"),
)

DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bcurl\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", "piping the network into a shell (curl|sh)"),
    (r"\bwget\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b", "piping the network into a shell (wget|sh)"),
    (_CMDPOS + r"rm\s+(?:-\S*\s+)*-\S*r", "recursive delete"),
    (r"\bchmod\s+(?:-\S+\s+)*-R\b", "recursive chmod"),
    (r"\bchown\s+(?:-\S+\s+)*-R\b", "recursive chown"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+-\S*[fx]", "git clean -fx"),
    (r"\bgit\s+push\b[^\n]*--force", "git push --force"),
)

_HARDLINE = tuple((re.compile(p, _FLAGS), d) for p, d in HARDLINE_PATTERNS)
_DANGEROUS = tuple((re.compile(p, _FLAGS), d) for p, d in DANGEROUS_PATTERNS)


def detect_hardline(command: str) -> tuple[bool, str | None]:
    """Katastrophales Kommando? Dann unbypassbar blockiert."""
    for rx, desc in _HARDLINE:
        if rx.search(command):
            return True, desc
    return False, None


def detect_dangerous(command: str) -> tuple[bool, str | None]:
    """Riskant-aber-rückrollbar? Dann menschliche Freigabe einholen."""
    for rx, desc in _DANGEROUS:
        if rx.search(command):
            return True, desc
    return False, None
