"""Was der Lauf wirklich getan hat — aus dem Protokoll, nicht aus der Erzaehlung.

Der Anlass ist gemessen, nicht erdacht. Auf die Bitte, eine Notiz anzulegen, antwortete
eine laufende Installation:

    „Die Vault-Notiz wurde unter `gotchas/…md` angelegt.
     Eine Notiz geschrieben; die zwei unvollstaendigen Varianten blieben verworfen."

Die Datei existierte nicht. Das Protokoll desselben Laufs zeigte zwei Schreibversuche,
beide `error`, und keinen dritten. Das Modell hatte die Fehlschlaege sogar bemerkt — und
daraus fabuliert, ein weiterer sei gelungen.

⚠️ **Das ist die gefaehrlichere Fehlerklasse, und kein Gate faengt sie.** Der Kernel
arbeitete fehlerfrei: Urteil erteilt, Token ausgegeben, der Runner setzte seine Regel
durch und meldete `error`. Alles, wogegen dieses Projekt gebaut ist, funktionierte. Der
Schaden entstand danach — in der *Zusammenfassung*. Ein Betreiber, der die Antwort liest,
glaubt an eine Wirkung, die es nicht gab, und merkt es erst, wenn er nachsieht.

Deshalb steht die Tatsache jetzt neben der Erzaehlung. Zwei Entscheidungen tragen das:

⚠️ **Es wird nicht geraten, ob die Antwort etwas Falsches behauptet.** Das waere
Textdeutung — unzuverlaessig in beide Richtungen und ausgerechnet bei der Formulierung
angreifbar, die das Modell selbst gewaehlt hat. Gemeldet wird stattdessen die nackte
Tatsache: in diesem Lauf ist ein Werkzeug gescheitert. Was der Text darueber sagt, darf
der Betreiber selbst vergleichen.

⚠️ **Die Quelle ist der `run_id`, nicht die History des Laufs.** Was im Agent-Loop
mitwandert, hat das Modell schon einmal gelesen und koennte es in seiner Antwort
umdeuten. Das Ereignisprotokoll kann es nicht faelschen — dort schreibt der Executor.

Ein Fehlschlag, der spaeter im selben Lauf gelang, wird nicht gemeldet: er hat den Lauf
nicht gekostet, und eine Warnung ueber etwas bereits Behobenes ist dieselbe Sorte
Moebel, gegen die `lessons.py` und `review.py` anschreiben.
"""
from __future__ import annotations

__all__ = ["failed_tools", "note"]

MAX_LISTED = 3
DETAIL_CHARS = 90
# Bewusst nuechtern und ohne Vorwurf: die Zeile stellt eine Tatsache neben eine Aussage,
# sie behauptet nicht, dass die Aussage falsch ist.
HEADER_ONE = "⚠ One tool call failed in this run and did not succeed afterwards:"
HEADER_MANY = "⚠ {n} tool calls failed in this run and did not succeed afterwards:"


def _payload(event) -> dict:
    return (event.get("payload") or {}) if isinstance(event, dict) else {}


def failed_tools(entries) -> tuple[tuple[str, str], ...]:
    """(Werkzeug, Grund) fuer alles, was in diesem Lauf scheiterte und nicht mehr gelang.

    `entries` sind die Ereignisse EINES Laufs, chronologisch — `EventLog.by_run`.
    """
    zuletzt_gut: dict[str, int] = {}
    for i, e in enumerate(entries):
        if e.get("type") != "exec.result":
            continue
        if str(_payload(e).get("status") or "").upper() == "DONE":
            zuletzt_gut[str(_payload(e).get("tool") or "?")] = i

    gefunden: list[tuple[str, str]] = []
    for i, e in enumerate(entries):
        if e.get("type") != "exec.result":
            continue
        status = str(_payload(e).get("status") or "").upper()
        if status in ("", "DONE"):
            continue
        werkzeug = str(_payload(e).get("tool") or "?")
        if zuletzt_gut.get(werkzeug, -1) > i:
            continue                     # danach gelungen — hat den Lauf nichts gekostet
        grund = " ".join(str(_payload(e).get("detail") or status).split())[:DETAIL_CHARS]
        paar = (werkzeug, grund)
        if paar not in gefunden:
            gefunden.append(paar)
    return tuple(gefunden)


def note(entries) -> str:
    """Die Zeile unter die Antwort — leer, wenn jedes Werkzeug durchlief.

    Leer heisst leer: eine Quittung, die unter jeder Antwort „alles gut" meldet, wird
    nach dreissig Wiederholungen ueberlesen, und dann auch die eine, die zaehlt.
    """
    gescheitert = failed_tools(entries)
    if not gescheitert:
        return ""
    kopf = (HEADER_ONE if len(gescheitert) == 1
            else HEADER_MANY.format(n=len(gescheitert)))
    zeilen = [kopf] + [f"  {werkzeug} — {grund}" for werkzeug, grund in gescheitert[:MAX_LISTED]]
    if len(gescheitert) > MAX_LISTED:
        zeilen.append(f"  … and {len(gescheitert) - MAX_LISTED} more (`talos events`)")
    return "\n".join(zeilen)
