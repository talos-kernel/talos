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
