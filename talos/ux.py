"""Zeichenvorrat der Oberflaeche — ein Zeichen, eine Bedeutung.

Warum ein eigenes Modul und keine Konstanten im Kanal: die Symbole werden vom
Telegram-Kanal UND vom kanal-neutralen Conductor gebraucht. Laegen sie in
`telegram.py`, muesste der Conductor den Kanal importieren — genau die Abhaengigkeit,
die `channel.py` vermeidet. Hier haengt niemand an niemandem.

Das ist ausdruecklich **keine** i18n-Tabelle. Es gibt einen Nutzer und eine Sprache;
eine Lookup-Mechanik waere Aufwand ohne Gegenwert. Es ist eine Konstantenliste, damit
die paar Zeichen, die the operator bei jedem Lauf sieht, nicht ueber vier Dateien verstreut sind.

Bewusst geometrisch statt der ueblichen Chatbot-Emoji (🧠🔧✅❌): Talos ist ein
gravierter Bronzeautomat, kein Assistent mit Sprechblase. Emoji leben in Statuszeilen
und Quittungen — nie in der Prosa der Antwort.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

SYM_TALOS = "◉"      # Kopfzeile eines Laufs; der Waechter ist wach
SYM_THINKING = "◈"   # Reasoner arbeitet, noch kein Werkzeug
SYM_PLAN = "≡"       # ein Ablauf wurde angekuendigt — Reihenfolge, keine Erlaubnis
SYM_TOOL = "▸"       # Werkzeug laeuft an
SYM_OK = "✓"         # sauber fertig
SYM_FAIL = "✕"       # fehlgeschlagen
SYM_GATE = "⏸"       # wartet auf des Betreibers Freigabe
SYM_BLOCKED = "⛒"    # der Kernel verbietet es
SYM_UNDO = "↩"       # zurueckgerollt

__all__ = [
    "SYM_TALOS", "SYM_THINKING", "SYM_PLAN", "SYM_TOOL", "SYM_OK", "SYM_FAIL",
    "SYM_GATE", "SYM_BLOCKED", "SYM_UNDO",
    "Style", "GEOMETRIC", "EXPRESSIVE", "style_for",
]


# --- Missions-Panel ------------------------------------------------------------------
# Eine kompakte Anzeige waehrend der Arbeit, auf Wunsch des Betreibers.
#
# Die eine Entscheidung, die es von Vorbildern unterscheidet: **es steht nichts drin, was
# Talos nicht misst.** Uebliche Panels dieser Art zeigen „Confidence 94 %" und „ETA 12 s".
# Beides kennt ein Agent nicht — ein Sprachmodell hat keine kalibrierte Zuversicht, und
# wie lange ein Lauf noch braucht, weiss es erst hinterher. Solche Zahlen sehen aus wie
# Messwerte und sind geraten; sie in eine Anzeige zu schreiben, waere genau die
# vorgetaeuschte Praezision, die SOUL.md verbietet („Nicht wissen ist eine Tatsache, die
# man berichtet, kein Fehler, den man kaschiert").
#
# Angezeigt wird deshalb nur, was der Lauf wirklich hergibt: der Schritt-Zaehler (echt,
# aus dem Agent-Loop), die verstrichene Zeit (gemessen), die Zahl gelaufener Werkzeuge
# (gezaehlt) und das letzte Ereignis (beobachtet).
PANEL_WIDTH = 34
_FULL, _EMPTY = "█", "░"


def bar(done: int, total: int, width: int = 10) -> str:
    """Ein Balken aus ECHTEN Zahlen. Ohne bekanntes Ziel gibt es keinen Balken."""
    if total <= 0:
        return _EMPTY * width
    filled = max(0, min(width, round(width * done / total)))
    return _FULL * filled + _EMPTY * (width - filled)


def mission_panel(
    *,
    step: int,
    max_steps: int,
    elapsed_s: float,
    tools_run: int,
    last_event: str = "",
    done: bool = False,
) -> str:
    """Baut das Panel. Jede Zeile ist ein gemessener Wert, keine Schaetzung."""
    lines = [f"{SYM_TALOS} MISSION"]
    if max_steps > 0:
        lines.append(f"step  {bar(step, max_steps)} {step}/{max_steps}")
    lines.append(f"time  {int(elapsed_s)}s · tools {tools_run}")
    if last_event:
        lines.append(f"last  {last_event[:PANEL_WIDTH]}")
    if done:
        lines.append("done")
    return "\n".join(lines)


# --- Statusstil ----------------------------------------------------------------------
# Zwei Ausgabestile fuer die Statuszeilen. Der Vorgabestil ist bewusst geometrisch (siehe
# oben): Talos ist ein gravierter Automat, kein Chatbot mit Sprechblase. Ein Betreiber, der
# seine eigene Instanz ausdrucksvoller haben will, schaltet den Emoji-Stil per
# `TALOS_STATUS_STYLE=expressive` frei — ohne dass sich am Vorgabeverhalten etwas aendert.
#
# Nur die ANZEIGE aendert sich, nie die Substanz: dieselben gemessenen Werte, dieselbe Regel
# „nichts, was Talos nicht misst". Kein Wort davon erreicht die Prosa der Antwort.


@dataclass(frozen=True)
class Style:
    """Zeichensatz fuer Kopf, Phasen und Werkzeuge einer Statusanzeige.

    `tool_glyphs`/`tool_verbs` sind pro Werkzeug; fehlt ein Eintrag, gelten `tool` und das
    knappe Vorgabelabel. So bleibt der geometrische Stil ein reiner Konstantensatz, waehrend
    der ausdrucksvolle Stil einzelne Werkzeuge namentlich zeichnet.
    """

    talos: str
    thinking: str
    plan: str
    tool: str
    ok: str
    fail: str
    gate: str
    blocked: str
    undo: str
    tool_glyphs: Mapping[str, str] = field(default_factory=dict)
    tool_verbs: Mapping[str, str] = field(default_factory=dict)

    def tool_symbol(self, tool: str) -> str:
        """Das Zeichen fuer ein laufendes Werkzeug — sein eigenes, sonst das Vorgabezeichen."""
        return self.tool_glyphs.get(tool, self.tool)

    def tool_label(self, tool: str, fallback: str) -> str:
        """Das Verb fuer ein Werkzeug — ausdrucksvoll ueberschrieben, sonst das knappe Label."""
        return self.tool_verbs.get(tool, fallback)


GEOMETRIC = Style(
    talos=SYM_TALOS, thinking=SYM_THINKING, plan=SYM_PLAN, tool=SYM_TOOL,
    ok=SYM_OK, fail=SYM_FAIL, gate=SYM_GATE, blocked=SYM_BLOCKED, undo=SYM_UNDO,
)

# Ausdrucksvoll: dasselbe Vokabular in gaengigen Emoji, plus ein Verb je Werkzeug. Die
# Kopfzeile bleibt die Signatur (◉) — sie benennt den Waechter, sie kommentiert nicht.
EXPRESSIVE = Style(
    talos=SYM_TALOS,
    thinking="🧠", plan="🗺️", tool="🛠️",
    ok="✅", fail="❌", gate="⏸️", blocked="⛔", undo="↩️",
    tool_glyphs={
        "read_file": "📖", "write_file": "✍️", "run_shell": "💻", "undo_last": "↩️",
        "vault_search": "🔎", "vault_get": "📖", "vault_write_note": "✍️",
        "web_fetch": "🌐", "web_search": "🔎", "browse": "🌐", "see_image": "👁️",
        "hear": "👂", "speak": "🔊",
        "grab_frame": "🎞️", "entity_status": "📡", "agent_consult": "🤝",
        "ask_operator": "🙋", "delegate": "🧭", "session_search": "🗂️",
        "delegate_code": "🛠️", "delegate_status": "🔍", "delegate_dag": "🧩",
        "delegate_agy": "🛠️", "delegate_steer": "🎯",
        "skill_write": "🎓", "remote_exec": "🛰️", "http_request": "🔗",
        "git": "📦",
    },
    tool_verbs={
        "read_file": "Reading", "write_file": "Writing", "run_shell": "Running",
        "undo_last": "Undoing", "vault_search": "Searching vault",
        "vault_get": "Reading note", "vault_write_note": "Writing note",
        "web_fetch": "Fetching", "web_search": "Searching the web", "browse": "Browsing",
        "see_image": "Looking", "hear": "Listening", "speak": "Speaking",
        "grab_frame": "Capturing", "entity_status": "Checking",
        "agent_consult": "Consulting", "ask_operator": "Asking you",
        "delegate": "Delegating", "session_search": "Searching history",
        "delegate_code": "Delegating code", "delegate_status": "Checking job",
        "delegate_dag": "Delegating task graph", "delegate_agy": "Delegating code",
        "delegate_steer": "Steering background task",
        "skill_write": "Distilling skill", "remote_exec": "Running remote",
        "http_request": "Calling API", "git": "Running git",
    },
)


def style_for(name: str) -> Style:
    """Waehlt den Stil ueber seinen Namen. Alles ausser `expressive` bleibt geometrisch —
    ein unbekannter Wert darf nie die Vorgabe kippen."""
    return EXPRESSIVE if str(name or "").strip().lower() == "expressive" else GEOMETRIC
