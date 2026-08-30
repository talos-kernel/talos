"""Fehler-Klassifizierung — aus Fehltext wird eine Retry-Strategie, deterministisch.

Der Anlass steht in der Checkliste der Agenten-Infrastruktur: ein Loop, der
„rc=1" liest und blind wiederholt, haemmert Rate-Limits fester zu und meldet
Verweigertes dreimal. Dieses Modul ordnet den Fehltext einer Klasse zu und
gibt dem Modell GENAU EINE kurze Zeile mit der passenden Strategie — Text,
keine Entscheidung: die Klasse aendert kein Urteil, keinen Grant, keinen
Schritt. Sie ist eine Leserichtung, die der Reasoner mit ins naechste Denken
nimmt, nichts weiter.

Drei Regeln halten das ehrlich:

1. **Klassen mit echtem Muster, sonst Schweigen.** Passt nichts, gibt es keine
   Zeile — eine erfundene Klasse ist schlechter als keine. Die Ausnahme ist
   `status=error` ohne Muster: dann lautet die ehrliche Klasse „logic", und
   ihr Rat ist „lies erst", nicht „versuch es nochmal".
2. **Reihenfolge ist Bedeutung.** Spezifisch vor allgemein (429 ist ein
   Rate-Limit, auch wenn im selben Text „error" steht), und jede Klasse nennt
   ihr Muster im Test.
3. **Deckel.** Die Zeile ist ein einziger Satz — ein Fehlerhinweis, der den
   Prompt fuellt, waere dieselbe Sorte Kontextfrass, gegen die `skills.py`
   und `recall.py` ihre Deckel haben.

Sprache: Kommentare deutsch, die Zeile an das Modell englisch (Tool-Protokoll).
"""
from __future__ import annotations

import re

# Spezifisch vor allgemein; das erste Muster entscheidet.
_MUSTER: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("rate-limit", re.compile(
        r"\b429\b|rate.?limit|too many requests|quota exceeded", re.IGNORECASE),
     "back off — wait before any retry; hammering a rate limit tightens it. "
     "Retry once after a pause, or report the limit instead of looping"),
    ("network", re.compile(
        r"timed?[\s-]?out|ETIMEDOUT|ECONNREFUSED|connection refused|"
        r"name or service not known|temporary failure in name resolution|"
        r"network is unreachable|could not resolve|no route to host", re.IGNORECASE),
     "transient network failure — one retry is reasonable; if it repeats, "
     "report it instead of retrying in a loop"),
    ("auth", re.compile(
        r"\b401\b|unauthorized|invalid (api.?key|token|credentials)|"
        r"authentication failed|bad credentials", re.IGNORECASE),
     "do not retry with the same credentials — they will fail the same way. "
     "Report the credential problem"),
    ("permission", re.compile(
        r"\b403\b|permission denied|EACCES|forbidden|read.only file system", re.IGNORECASE),
     "do not retry the same call — a refusal does not change on a second "
     "attempt. Report it, or choose a different path"),
    ("not-found", re.compile(
        r"\b404\b|no such file or directory|not found|does not exist", re.IGNORECASE),
     "do not retry the same call — check the name or path first"),
)

_FEHL_SIGNAL = re.compile(r"\brc=[1-9]|\bHTTP (?:4\d\d|5\d\d)\b")
MAX_NOTE_CHARS = 220


def classify(text: str) -> str | None:
    """Die Klasse des Fehltexts — oder None, wenn kein Muster traegt."""
    for name, muster, _rat in _MUSTER:
        if muster.search(text):
            return name
    return None


def note(status: str, text: str) -> str:
    """Die eine Hinweiszeile fuer `tool_history_entry` — oder "".

    Gehaengt wird an zwei Signale: ein gescheitertes Werkzeug (`status=error`)
    oder ein Fehlbild im Text (rc != 0, HTTP 4xx/5xx). Ein erfolgreicher Aufruf
    bekommt nie eine Zeile, auch wenn sein Text zufaellig „429" enthaelt —
    der Anlass ist der Fehlschlag, nicht das Wort.
    """
    inhalt = str(text or "")
    if status != "error" and not _FEHL_SIGNAL.search(inhalt):
        return ""
    klasse = classify(inhalt)
    if klasse is None:
        if status != "error":
            return ""
        return (
            "\n[error class: logic — read the message before deciding; "
            "do not retry blindly]"
        )
    rat = next(rat for name, _m, rat in _MUSTER if name == klasse)
    zeile = f"\n[error class: {klasse} — {rat}]"
    return zeile if len(zeile) <= MAX_NOTE_CHARS else zeile[: MAX_NOTE_CHARS - 1] + "]"
