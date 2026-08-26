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

⚠️ **Die Fehlschlag-Liste allein hat ein Loch: den erfundenen Erfolg.** Ein Werkzeug,
das nie aufgerufen wurde, hinterlaesst kein `error`-Ereignis — eine Antwort, die drei
Aktionen beschreibt, waehrend das Protokoll einen einzigen Aufruf zeigt, erzeugt keine
einzige Detail-Zeile. Deshalb steht ueber der Liste die nackte Zahl: N Aufrufe, davon
M ohne spaeteren Erfolg gescheitert. Ob die Erzaehlung zu N passt, darf wieder der
Betreiber vergleichen — gezaehlt wird nur, nicht gedeutet.
"""
from __future__ import annotations

__all__ = ["failed_tools", "note"]

MAX_LISTED = 3
DETAIL_CHARS = 90
# Bewusst nuechtern und ohne Vorwurf: die Zeile stellt eine Tatsache neben eine Aussage,
# sie behauptet nicht, dass die Aussage falsch ist.
HEADER_ONE = "⚠ One tool call failed in this run and did not succeed afterwards:"
HEADER_MANY = "⚠ {n} tool calls failed in this run and did not succeed afterwards:"
# Die Totals-Zeile traegt bewusst kein ⚠: „0 failed" darf nicht wie eine Warnung
# aussehen, sonst wird genau sie zum Moebel. Und ein Glyph aus `ux.py` steht hier
# nicht zur Verfuegung — dieses Modul importiert nichts (s. Test zur Quelle).
TOTALS_ONE = "1 tool call, {m} failed"
TOTALS_MANY = "{n} tool calls, {m} failed"


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


def _count_calls(entries) -> tuple[int, int]:
    """(N, M) — N: alle Werkzeugaufrufe des Laufs, M: davon ohne spaeteren Erfolg
    gescheitert.

    Gezaehlt werden `exec.result`-Ereignisse: der Executor schreibt genau eines pro
    Aufruf, auch fuer abgelehnte. M zaehlt jeden Fehlschlag einzeln — anders als die
    Detail-Liste, die gleiche Befunde zusammenfasst. Eine gekuerzte Zahl waere die
    gleiche Sorte falsche Quittung, gegen die dieses Modul existiert.
    """
    zuletzt_gut: dict[str, int] = {}
    fehlschlaege: list[tuple[int, str]] = []
    n = 0
    for i, e in enumerate(entries):
        if e.get("type") != "exec.result":
            continue
        n += 1
        status = str(_payload(e).get("status") or "").upper()
        werkzeug = str(_payload(e).get("tool") or "?")
        if status == "DONE":
            zuletzt_gut[werkzeug] = i
        elif status:
            fehlschlaege.append((i, werkzeug))
    m = sum(1 for i, werkzeug in fehlschlaege if zuletzt_gut.get(werkzeug, -1) <= i)
    return n, m


def note(entries) -> str:
    """Die Zeile unter die Antwort — leer nur, wenn der Lauf gar kein Werkzeug rief.

    Leer heisst leer: eine Quittung unter einer tool-freien Antwort waere Moebel und
    wuerde nach dreissig Wiederholungen ueberlesen — und dann auch die eine, die
    zaehlt.
    """
    n, m = _count_calls(entries)
    if n == 0:
        return ""
    zeilen = [(TOTALS_ONE if n == 1 else TOTALS_MANY).format(n=n, m=m)]
    gescheitert = failed_tools(entries)
    if gescheitert:
        kopf = (HEADER_ONE if len(gescheitert) == 1
                else HEADER_MANY.format(n=len(gescheitert)))
        zeilen.append(kopf)
        zeilen.extend(f"  {werkzeug} — {grund}" for werkzeug, grund in gescheitert[:MAX_LISTED])
        if len(gescheitert) > MAX_LISTED:
            zeilen.append(f"  … and {len(gescheitert) - MAX_LISTED} more (`talos events`)")
    return "\n".join(zeilen)
