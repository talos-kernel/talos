"""Small terminal presentation helpers. No dependencies, animation or hidden state."""
from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap

_COLORS = {"bronze": "38;5;180", "muted": "90", "ok": "32", "warn": "33", "fail": "31"}
_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def clean(text: str) -> str:
    text = _ESCAPES.sub("", str(text))
    return "".join(c for c in text if c in "\n\t" or
                   (ord(c) >= 32 and not 127 <= ord(c) < 160))


def is_terminal(out=None) -> bool:
    try:
        return bool((out or sys.stdout).isatty())
    except (AttributeError, OSError, ValueError):
        return False


def paint(text: str, tone: str = "bronze", *, out=None, bold: bool = False) -> str:
    text = clean(text)
    if not is_terminal(out) or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return text
    code = ("1;" if bold else "") + _COLORS[tone]
    return f"\x1b[{code}m{text}\x1b[0m"


def heading(title: str, subtitle: str = "", *, out=None) -> str:
    width = max(20, min(76, shutil.get_terminal_size((80, 24)).columns - 4))
    title = " ".join(clean(title).split())
    lines = ["", paint("  ◉  " + title, out=out, bold=True)]
    if subtitle:
        lines.extend(paint("     " + line, "muted", out=out)
                     for line in textwrap.wrap(" ".join(clean(subtitle).split()), width - 3))
    lines.append(paint("  " + "─" * width, "muted", out=out))
    return "\n".join(lines) + "\n"


def help_text(text: str, *, out=None) -> str:
    """Accent commands, retain complete copyable text in logs and narrow terminals."""
    width = max(24, shutil.get_terminal_size((80, 24)).columns - 2)
    lines = []
    for line in text.splitlines():
        match = re.match(r"^(  [a-z][\w-]*(?: [^ ]+)?)\s{3,}(.*)$", line)
        if match and len(line) > width:
            command, description = re.split(r"\s{3,}", line.strip(), maxsplit=1)
            lines.extend(paint(part, out=out) for part in textwrap.wrap(
                command, max(20, width), initial_indent="  ", subsequent_indent="    "))
            lines.extend("    " + part for part in textwrap.wrap(description, max(16, width - 4)))
        elif line.strip().isupper() and line.strip():
            lines.append(paint(line, out=out, bold=True))
        elif match:
            lines.append(paint(match.group(1), out=out) + line[len(match.group(1)):])
        else:
            lines.append(clean(line))
    return "\n".join(lines) + "\n"
