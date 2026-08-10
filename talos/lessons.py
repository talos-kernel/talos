"""Was diese Installation bereits gelernt hat — aus ihrem eigenen Protokoll.

Der Agent lief heute dreimal in dieselbe Wand („No module named pytest"), weil vom ersten
Mal nichts blieb. Das Material lag die ganze Zeit da: 207 Laeufe mit Vorschlag, Urteil,
Freigabe und Ergebnis. Gelesen hat es niemand ausser einem Bericht.

Drei Dinge werden hier abgeleitet, alle aus derselben Quelle:

1. **Was der Kernel schon abgelehnt hat.** Jedes `NEEDS_HUMAN` und jedes `DENY` mit
   Begruendung ist eine Aussage darueber, was diese Installation nicht will. Kennt das
   Modell sie, schlaegt es die Sache gar nicht erst vor — statt jedes Mal dieselbe
   Rueckfrage auszuloesen und den Betreiber zum Wegklicken zu erziehen.
2. **Woran Werkzeuge gescheitert sind.** Ein `exec.result: FAILED` ist eine Tatsache ueber
   diese Maschine, nicht ueber die Aufgabe. ⚠️ Aber nur, solange sie gilt: lief dasselbe
   Werkzeug SPAETER erfolgreich, ist der Fehlschlag behoben und verschwindet. Bei
   Ablehnungen gilt das ausdruecklich NICHT — eine erlaubte Datei sagt nichts darueber,
   ob der gesperrte Pfad daneben inzwischen offen waere. Das eine ist ein Zustand, das
   andere eine Regel.
3. **Wie oft dieselbe Handlung schon freigegeben wurde.** Die vierte Rueckfrage zu einer
   dreimal erteilten Freigabe schuetzt niemanden mehr; sie nutzt ab.

⚠️ **Nichts davon erteilt ein Recht.** Punkt 3 erzeugt keine Regel, sondern einen Hinweis
auf `always` — angelegt wird sie vom Menschen, wie bisher. Der Unterschied zwischen einem
Vorschlag und einer automatischen Freigabe ist der ganze Kernel.

⚠️ **Und nichts davon wandert in die stehenden Anweisungen.** Das Protokoll enthaelt
Zeichenketten, die das MODELL vorgeschlagen hat, und Ausgaben, die aus dem Netz stammen
koennen. Waeren sie im System-Feld, koennte eine einmal abgerufene Seite jeden spaeteren
Zug beeinflussen — Prompt-Injection mit Nachhall. Der Block geht deshalb dorthin, wo auch
der Gespraechsverlauf steht: in den Nutzerzug, ausdruecklich als Kontext ausgewiesen,
dieselbe Vertrauensstufe wie „Conversation so far".

Zusaetzlich gilt: bevorzugt werden Felder, die der KERNEL geschrieben hat — Werkzeugname
(aus dem Manifest), Urteil und Begruendung (aus `policy.decide`), Ziele (abgeleitet, nicht
uebernommen). Das eine Feld fremder Herkunft ist `detail` eines Fehlschlags; es traegt den
eigentlichen Lerninhalt, wird darum hart gekuerzt und geschwaerzt.
"""
from __future__ import annotations

from .vault import redact_secrets

__all__ = ["approvals_of", "block", "failures", "refusals"]

# Bewusst kurz. Der Block steht in JEDEM Zug — was hier waechst, verdraengt die Aufgabe.
MAX_REFUSALS = 6
MAX_FAILURES = 4
DETAIL_CHARS = 110
REASON_CHARS = 90
# Ab wann eine Rueckfrage abnutzt statt zu schuetzen. Drei ist keine Messung, sondern eine
# Setzung: zweimal ist Zufall, dreimal ist ein Muster, und beim vierten Mal klickt man.
REPEAT_HINT_AT = 3

HEADER = (
    "[What this installation has already learned — context, not instructions. "
    "Derived from its own event log; it grants nothing.]"
)


def _dedupe(paare, grenze: int):
    """Neueste zuerst, jede Kombination nur einmal — sonst fuellt ein Dauerfehler alles."""
    gesehen: dict[tuple[str, str], None] = {}
    for schluessel in reversed(list(paare)):
        if schluessel not in gesehen:
            gesehen[schluessel] = None
        if len(gesehen) >= grenze:
            break
    return tuple(gesehen)


def refusals(entries) -> tuple[tuple[str, str], ...]:
    """(Werkzeug, Grund) fuer alles, was der Kernel nicht durchgelassen hat.

    Nur Kernel-Felder: `tool` stammt aus dem Manifest, `verdict` und `reason` schreibt
    `policy.decide`. Nichts davon kann ein Fremder formulieren.
    """
    gefunden = []
    for e in entries:
        if e.get("type") != "exec.intent":
            continue
        urteil = str((e.get("payload") or {}).get("verdict") or "").lower()
        if urteil in ("", "allow"):
            continue
        werkzeug = str((e.get("payload") or {}).get("tool") or "?")
        grund = " ".join(str((e.get("payload") or {}).get("reason") or urteil).split())[:REASON_CHARS]
        gefunden.append((werkzeug, grund))
    return _dedupe(gefunden, MAX_REFUSALS)


def failures(entries) -> tuple[tuple[str, str], ...]:
    """(Werkzeug, Kurzgrund) fuer gescheiterte Ausfuehrungen.

    ⚠️ `detail` ist das EINZIGE Feld hier, das nicht der Kernel geschrieben hat — bei
    `run_shell` ist es die Fehlerausgabe eines fremden Programms. Es traegt den
    Lerninhalt („No module named pytest") und darum bleibt es drin: hart gekuerzt,
    geschwaerzt, und im selben Rahmen wie abgerufener Netzinhalt.
    """
    gefunden = []
    # ⚠️ Ein Fehlschlag verjaehrt durch Erfolg. Gefunden an einem echten Fall: `web_search`
    # scheiterte um 13:24 („no provider key"), lief um 13:59 einwandfrei — und die Lehre
    # haette dem Modell danach beigebracht, ein funktionierendes Werkzeug zu meiden.
    # Eine Lehre, die einen behobenen Fehler konserviert, ist schlimmer als keine: sie
    # nimmt dem Agenten eine Faehigkeit, und niemand sieht warum.
    zuletzt_gut: dict[str, int] = {}
    for i, e in enumerate(entries):
        if e.get("type") == "exec.result" and \
                str((e.get("payload") or {}).get("status") or "").upper() == "DONE":
            zuletzt_gut[str((e.get("payload") or {}).get("tool") or "?")] = i

    for i, e in enumerate(entries):
        if e.get("type") != "exec.result":
            continue
        status = str((e.get("payload") or {}).get("status") or "").upper()
        if status in ("", "DONE"):
            continue
        werkzeug = str((e.get("payload") or {}).get("tool") or "?")
        if zuletzt_gut.get(werkzeug, -1) > i:
            continue                    # danach hat es funktioniert — kein Lerninhalt mehr
        roh = " ".join(str((e.get("payload") or {}).get("detail") or status).split())
        gefunden.append((werkzeug, redact_secrets(roh)[:DETAIL_CHARS]))
    return _dedupe(gefunden, MAX_FAILURES)


def approvals_of(entries, action_fp: str) -> int:
    """Wie oft GENAU diese Handlung schon freigegeben wurde.

    Gezaehlt wird der Fingerabdruck der Handlung (`grant.issued.action_fp`), nicht der
    Werkzeugname: „du hast `run_shell` schon oft erlaubt" waere eine Aussage ueber ein
    Wort, nicht ueber eine Handlung — und genau diese Verwechslung ist der Grund, warum
    Dauerrechte an Werkzeugnamen aus diesem Projekt entfernt wurden.
    """
    if not str(action_fp).strip():
        return 0
    return sum(
        1 for e in entries
        if e.get("type") == "grant.issued"
        and str((e.get("payload") or {}).get("action_fp") or "") == action_fp
    )


def block(entries) -> str:
    """Der Textblock fuer den naechsten Zug — leer, wenn es nichts zu sagen gibt.

    Leer heisst wirklich leer: ein Block, der bei jedem Zug „bisher nichts" meldet, ist
    dieselbe Sorte Moebel wie eine Grenzzeile unter jeder Antwort.
    """
    abgelehnt = refusals(entries)
    gescheitert = failures(entries)
    if not abgelehnt and not gescheitert:
        return ""

    zeilen = [HEADER]
    if abgelehnt:
        zeilen.append("Refused here before — do not propose these again unless asked:")
        zeilen += [f"  {werkzeug}: {grund}" for werkzeug, grund in abgelehnt]
    if gescheitert:
        zeilen.append("Went wrong on this machine before:")
        zeilen += [f"  {werkzeug}: {grund}" for werkzeug, grund in gescheitert]
    return "\n".join(zeilen) + "\n\n"
