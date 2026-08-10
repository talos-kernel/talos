"""Verifier — TOCTOU-Bindung vor Ausführung + Ergebnisprüfung danach.

Vorbild ist OpenClaws Exec-Approval-Binding (aus der Codebasen-Analyse): der Zustand einer
Zieldatei wird zum Entscheidungszeitpunkt als realpath + sha256 gebunden und **unmittelbar vor
dem Ausführen erneut geprüft**. Weicht er ab, wird fail-closed abgebrochen — so kann zwischen
Freigabe und Spawn nichts untergeschoben werden.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

_ABSENT = "absent"


def _digest(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return _ABSENT
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class Binding:
    original: str
    realpath: str
    sha256: str


def bind(targets: tuple[str, ...]) -> tuple[Binding, ...]:
    """Bindet jeden Zielpfad an (realpath, sha256) zum Entscheidungszeitpunkt."""
    return tuple(Binding(t, os.path.realpath(t), _digest(t)) for t in targets)


def recheck(bindings: tuple[Binding, ...]) -> bool:
    """True nur, wenn JEDE Bindung unverändert ist. Sonst fail-closed."""
    for b in bindings:
        if os.path.realpath(b.original) != b.realpath:
            return False
        if _digest(b.original) != b.sha256:
            return False
    return True


def verify_result(expected: object, actual: object) -> bool:
    """Deterministischer Erwartet-vs-Tatsächlich-Vergleich."""
    return expected == actual
