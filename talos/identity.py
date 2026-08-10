"""Wer der Agent ist — Name und Wesen, beides aus EINER Datei.

`SOUL.md` ist die Quelle. Der Name steht in ihrer ersten Ueberschrift; wer den Agenten
umtaufen will, aendert diese eine Zeile — und zwar nur diese. Kein zweiter Ort, an dem
„Talos" noch einmal steht und beim Umbenennen vergessen wird, und kein Neustart: die
Datei wird bei jeder Nachricht neu gelesen und inhaltlich verglichen (siehe `_fresh`).

Eigenes Modul, weil zwei sehr verschiedene Schichten den Namen brauchen: der Reasoner
(fuer den Prompt) und der Telegram-Kanal (fuer die Kopfzeile der Statusanzeige). Laege
das im Reasoner, muesste der Kanal den Reasoner importieren — eine Abhaengigkeit, die
es hier nicht geben soll.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

SOUL_FILENAME = "SOUL.md"
SOUL_PATH = Path(__file__).resolve().parent.parent / SOUL_FILENAME
# Die Datei geht in JEDEN Prompt. Ohne Deckel frisst eine versehentlich hineinkopierte
# Logdatei still das Kontextfenster.
MAX_SOUL_CHARS = 8_000
DEFAULT_NAME = "Talos"

FALLBACK_PREAMBLE = (
    "You are Talos, an autonomous guardian agent running on a Raspberry Pi. "
    "Answer in the language the operator writes in. Be brief and precise. "
    "If you are not sure, say so."
)

_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


# Name und Wesen werden bei JEDER Nachricht gebraucht — Kopfzeile, `/start`, `/status`,
# und der Preambel jedes Reasoner-Zugs. Beides einmal beim Start zu lesen war der Fehler,
# der zuletzt live sichtbar wurde: nach einer Umbenennung in SOUL.md stand in der Anzeige
# weiter der alte Name, waehrend die Quelle laengst den neuen trug — und der Agent stellte
# sich mit einem Namen vor, den es nicht mehr gab. Deshalb wird der gelesene Inhalt mit
# dem letzten Inhalt verglichen: Groesse und Zeitstempel allein reichen nicht, weil ein
# schneller Schreibvorgang beides unveraendert lassen kann.
#
# Kein Sicherheitsproblem: SOUL.md liegt im Persistenz-Floor, jede Aenderung geht durch
# die Freigabe des Betreibers. Das Nachladen macht keinen Weg auf, es nimmt nur den Neustart weg.
_cache: dict[str, tuple[bytes, str, str]] = {}


def _fresh(target: Path) -> tuple[str, str] | None:
    """(Persona, Name) — aus dem Cache, wenn der Dateiinhalt unveraendert ist."""
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fingerprint = hashlib.sha256(text.encode("utf-8")).digest()
    hit = _cache.get(str(target))
    if hit is not None and hit[0] == fingerprint:
        return hit[1], hit[2]
    soul = text.strip()[:MAX_SOUL_CHARS] or FALLBACK_PREAMBLE
    _cache[str(target)] = (fingerprint, soul, _name_from(text))
    return soul, _cache[str(target)][2]


def _name_from(text: str) -> str:
    """Der Name aus der ersten Ueberschrift — `# TALOS` wird zu `Talos`.

    Durchgehende Grossschreibung wird zu Titelform: die Ueberschrift darf schreien,
    die Kopfzeile im Chat soll es nicht. Gemischtes bleibt unangetastet, damit
    „ExampleAgent" oder „McCoy" nicht kaputtgehen.
    """
    match = _HEADING.search(text)
    if match is None:
        return DEFAULT_NAME
    # Kanal-Kopfzeilen sind einzeilig und schmal; ein Roman in der Ueberschrift
    # wuerde die Anzeige sprengen, also hart begrenzen statt hoffen.
    raw = " ".join(match.group(1).split())[:32]
    return raw.title() if raw.isupper() else (raw or DEFAULT_NAME)


def load_soul(path: Path | None = None) -> str:
    """Die Persona von der Platte. Fehlt oder bricht sie, laeuft der Agent ohne — nie gar nicht."""
    fresh = _fresh(SOUL_PATH if path is None else path)
    return FALLBACK_PREAMBLE if fresh is None else fresh[0]


def agent_name(path: Path | None = None) -> str:
    """Der Name aus der ersten Ueberschrift der SOUL, bei jedem Aufruf aktuell."""
    fresh = _fresh(SOUL_PATH if path is None else path)
    return DEFAULT_NAME if fresh is None else fresh[1]
