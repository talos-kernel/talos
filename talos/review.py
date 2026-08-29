"""Der Selbstreview — was diese Installation an sich selbst aendern sollte.

`lessons.py` gibt dem MODELL mit, was schiefging, damit es im naechsten Zug klueger
vorschlaegt. Das ist Reflex, nicht Verbesserung: die Wand bleibt stehen, der Agent
laeuft nur nicht mehr dagegen. Dieses Modul richtet sich an den BETREIBER und stellt
die andere Frage — was muesste sich aendern, damit die Wand verschwindet.

Vier Befunde, alle aus dem eigenen Protokoll, alle abzaehlbar:

1. **Dieselbe Wand mehrfach.** Ein Werkzeug scheitert wiederholt am selben Grund und
   hat seitdem nie funktioniert. Das kostet jedes Mal einen Lauf.
2. **Abgenutzte Rueckfragen.** Dieselbe Handlung wurde drei Mal freigegeben. Die vierte
   Rueckfrage schuetzt niemanden mehr — sie erzieht zum Wegklicken, und ein Betreiber,
   der wegklickt, ist gefaehrlicher als eine Regel, die er bewusst angelegt haette.
3. **Wiederholt abgelehnte Vorschlaege.** Das Modell schlaegt immer wieder etwas vor,
   das der Kernel jedes Mal ablehnt. Das ist ein Befund ueber die AUFGABE, nicht ueber
   das Modell: entweder ist sie falsch gestellt, oder die Regel ist enger als gemeint.
4. **Luecken, die schon etwas gekostet haben.** Dass `ddgs` fehlt, weiss der Doktor.
   Interessant wird es erst zusammen mit dem Protokoll: dass es vier Laeufe gekostet hat.
   Diese Verknuepfung ist der eigentliche Grund fuer dieses Modul — beide Haelften lagen
   schon da, nur hat sie nie jemand nebeneinandergelegt.

⚠️ **Der Review aendert nichts und darf nichts aendern.** Er schlaegt vor; angelegt wird
von Hand. Ein Agent, der aus seiner eigenen Geschichte Rechte ableitet, hat einen zweiten
Erlaubnisweg neben dem Kernel — und der Unterschied zwischen einem Vorschlag und einer
automatischen Freigabe ist der ganze Kernel. Dieses Modul importiert `policy` nicht und
kann keine Regel schreiben; ein Test haelt das fest, weil gute Absicht das nicht tut.

⚠️ **Und er misst sich selbst.** Frueher gemeldete Befunde stehen als `review.reported`
im Protokoll; taucht einer zum wiederholten Mal auf, sagt der Bericht das dazu. Ein
Review, der jede Woche dieselbe Liste schickt und das nicht bemerkt, ist ein Ritual —
und Rituale liest niemand mehr, auch der Betreiber nicht.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Finding", "due", "render", "render_compact", "run_review", "survey"]

# Zwei ist Zufall, drei ist ein Muster — ausser bei Fehlschlaegen, die Zeit kosten:
# dort ist schon die Wiederholung teuer genug, um sie zu melden.
REPEAT_FAILURE_AT = 2
REPEAT_REFUSAL_AT = 3
WORN_APPROVAL_AT = 3
# Nichts davon darf zum Roman werden: der Bericht geht an einen Menschen, und ein
# Bericht, den man wegwischt, hat dieselbe Wirkung wie keiner.
MAX_FINDINGS = 8
NOTE_CHARS = 120
# ⚠️ Wie weit zurueck ueberhaupt gezaehlt wird. Gefunden am eigenen Bericht: der erste
# Lauf gegen ein echtes Protokoll meldete 17 abgelehnte `run_shell` mit der Begruendung
# „Shell ohne Sandbox" — allesamt vom 1. August, aus der Zeit VOR der Einrichtung von
# bubblewrap. Fuenf Tage spaeter war das keine Baustelle mehr, sondern Geschichte.
#
# Die Lehre daraus ist nicht, dass Ablehnungen verjaehren sollten (eine erlaubte Datei
# sagt nichts darueber, ob der gesperrte Pfad daneben offen waere) — sondern dass manche
# Ablehnung selbst auf einem ZUSTAND beruht und nicht auf einer Regel. Diese beiden
# maschinell zu trennen ist nicht moeglich; das Alter zu messen schon. Ein Muster von vor
# zwei Wochen ist kein Muster von heute, und das gilt fuer alle vier Befundarten.
MAX_AGE_S = 14 * 24 * 60 * 60

HEADER = "Self-review — what this installation should change"
FOOTER = (
    "Proposals only. Nothing here has been applied, and nothing here can apply itself: "
    "rules are created by hand, as always."
)


@dataclass(frozen=True)
class Finding:
    """Ein Befund. `count` ist die Begruendung — ohne Zahl ist es eine Meinung."""

    kind: str
    subject: str
    count: int
    note: str = ""
    seen_before: int = 0
    # Wann es zuletzt vorkam. ⚠️ Der Grund steht im Bericht selbst: „17×" allein liest
    # sich wie ein Zustand von heute. Der erste Lauf gegen ein echtes Protokoll meldete
    # genau so 17 abgelehnte `run_shell` — alle aus der Zeit vor der Sandbox-Einrichtung.
    # Ob eine Ablehnung auf einer Regel oder einem behobenen Zustand beruht, kann keine
    # Maschine entscheiden; wann sie zuletzt auftrat, schon. Also wird es gemessen und
    # hingeschrieben, statt es zu raten.
    last_ts: float = 0.0

    @property
    def key(self) -> str:
        """Der Wiedererkennungsschluessel ueber Berichte hinweg — Art plus Gegenstand,
        NICHT die Zahl: dieselbe Wand ein viertes Mal ist derselbe Befund, nicht ein neuer."""
        return f"{self.kind}:{self.subject}"


def _payload(event) -> dict:
    return (event.get("payload") or {}) if isinstance(event, dict) else {}


def _ts(event) -> float:
    try:
        return float(event.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


class _Tally(dict):
    """Zaehlt und merkt sich, wann es zuletzt vorkam.

    Beides an einer Stelle, weil beides zusammen gehoert: die Zahl sagt, wie oft — der
    Zeitpunkt sagt, ob es noch gilt. Getrennt gefuehrt haetten sie frueher oder spaeter
    verschiedene Schluessel.
    """

    def add(self, key, ts: float) -> None:
        anzahl, zuletzt = self.get(key, (0, 0.0))
        self[key] = (anzahl + 1, max(zuletzt, ts))


def _tool(event) -> str:
    return str(_payload(event).get("tool") or "?")


def _survey_failures(entries) -> list[Finding]:
    """Werkzeuge, die wiederholt an derselben Sache scheitern — und seitdem nie liefen.

    ⚠️ Dieselbe Verjaehrung wie in `lessons.py`, aus demselben Anlass: ein Fehlschlag,
    der spaeter behoben wurde, ist keine Baustelle mehr. Ihn zu melden hiesse, den
    Betreiber auf eine Reparatur zu schicken, die schon stattgefunden hat.
    """
    zuletzt_gut: dict[str, int] = {}
    for i, e in enumerate(entries):
        if e.get("type") == "exec.result" and str(_payload(e).get("status") or "").upper() == "DONE":
            zuletzt_gut[_tool(e)] = i

    zaehler = _Tally()
    for i, e in enumerate(entries):
        if e.get("type") != "exec.result":
            continue
        status = str(_payload(e).get("status") or "").upper()
        if status in ("", "DONE"):
            continue
        werkzeug = _tool(e)
        if zuletzt_gut.get(werkzeug, -1) > i:
            continue
        grund = " ".join(str(_payload(e).get("detail") or status).split())[:NOTE_CHARS]
        zaehler.add((werkzeug, grund), _ts(e))

    return [
        Finding("repeat-failure", werkzeug, anzahl, f"fails on: {grund}", last_ts=zuletzt)
        for (werkzeug, grund), (anzahl, zuletzt) in zaehler.items()
        if anzahl >= REPEAT_FAILURE_AT
    ]


def _survey_refusals(entries) -> list[Finding]:
    """Was das Modell immer wieder vorschlaegt und der Kernel jedes Mal ablehnt.

    Gezaehlt werden ausschliesslich Felder, die der Kernel selbst geschrieben hat.
    """
    zaehler = _Tally()
    for e in entries:
        if e.get("type") != "exec.intent":
            continue
        urteil = str(_payload(e).get("verdict") or "").lower()
        if urteil in ("", "allow"):
            continue
        grund = " ".join(str(_payload(e).get("reason") or urteil).split())[:NOTE_CHARS]
        zaehler.add((_tool(e), grund), _ts(e))

    return [
        Finding("repeat-refusal", werkzeug, anzahl,
                f"refused every time: {grund} — either the task is aimed wrong, "
                f"or the rule is tighter than intended", last_ts=zuletzt)
        for (werkzeug, grund), (anzahl, zuletzt) in zaehler.items()
        if anzahl >= REPEAT_REFUSAL_AT
    ]


def _survey_approvals(entries) -> list[Finding]:
    """Handlungen, die so oft freigegeben wurden, dass die Rueckfrage abnutzt.

    Gezaehlt wird der Fingerabdruck der HANDLUNG, nie der Werkzeugname: „du erlaubst
    `run_shell` staendig" waere eine Aussage ueber ein Wort. Genau diese Verwechslung
    ist der Grund, warum Dauerrechte an Werkzeugnamen aus diesem Projekt entfernt wurden.
    """
    zaehler = _Tally()
    beschriftung: dict[str, str] = {}
    for e in entries:
        if e.get("type") != "grant.issued":
            continue
        fp = str(_payload(e).get("action_fp") or "")
        if not fp:
            continue
        zaehler.add(fp, _ts(e))
        beschriftung.setdefault(fp, _tool(e))

    return [
        Finding("worn-approval", f"{beschriftung.get(fp, '?')} ({fp[:12]})", anzahl,
                "approved every time — a standing rule would be more honest than a "
                "prompt you always click through", last_ts=zuletzt)
        for fp, (anzahl, zuletzt) in zaehler.items()
        if anzahl >= WORN_APPROVAL_AT
    ]


def _survey_gaps(entries, gaps) -> list[Finding]:
    """Fehlende Faehigkeiten, die im Protokoll tatsaechlich etwas gekostet haben.

    Der Doktor beschriftet nach Werkzeug („web_search (ddgs)"); davor steht der Name,
    den auch das Protokoll fuehrt. Nur ueber diese Bruecke laesst sich sagen, ob eine
    Luecke stoert oder bloss existiert — und eine Luecke, die niemanden aufhaelt, ist
    keine Baustelle, sondern eine Fussnote.
    """
    kosten = _Tally()
    for e in entries:
        if e.get("type") != "exec.result":
            continue
        if str(_payload(e).get("status") or "").upper() in ("", "DONE"):
            continue
        kosten.add(_tool(e), _ts(e))

    gefunden = []
    for label, weg in gaps:
        werkzeug = str(label).split(" (")[0].strip()
        anzahl, zuletzt = kosten.get(werkzeug, (0, 0.0))
        if anzahl:
            gefunden.append(Finding("gap-cost", werkzeug, anzahl,
                                    str(weg)[:NOTE_CHARS], last_ts=zuletzt))
    return gefunden


def _reported_before(entries) -> dict[str, int]:
    """Wie oft ein Befund in frueheren Berichten schon stand."""
    zaehler: dict[str, int] = {}
    for e in entries:
        if e.get("type") != "review.reported":
            continue
        for schluessel in _payload(e).get("keys") or ():
            schluessel = str(schluessel)
            zaehler[schluessel] = zaehler.get(schluessel, 0) + 1
    return zaehler


def _recent(entries, now: float | None) -> list:
    """Nur was jung genug ist, um noch ein Muster zu sein.

    ⚠️ Ein Eintrag OHNE Zeitstempel bleibt drin. Das ist Absicht: fehlende Zeit heisst
    „unbekannt", und einen Befund wegen fehlender Angabe verschwinden zu lassen waere die
    falsche Richtung — der Bericht wuerde stiller, ohne dass jemand etwas repariert haette.
    """
    if now is None:
        return list(entries)
    grenze = now - MAX_AGE_S
    return [e for e in entries if float((e.get("ts") or grenze) if isinstance(e, dict) else grenze) >= grenze]


def survey(entries, *, gaps=(), now: float | None = None) -> tuple[Finding, ...]:
    """Alle Befunde, das Teuerste zuerst.

    Die Reihenfolge ist keine Kosmetik: ein Bericht wird von oben gelesen, und was
    unten steht, wird nicht gelesen. Sortiert wird nach Haeufigkeit, weil das die
    einzige Zahl ist, die hier wirklich gemessen wurde.

    `now` schaltet das Altersfenster ein. Ohne Angabe wird alles gezaehlt — dann liegt
    die Auswahl beim Aufrufer, und Tests brauchen keine Uhr.
    """
    eintraege = _recent(entries, now)
    gefunden = (
        _survey_failures(eintraege)
        + _survey_gaps(eintraege, gaps)
        + _survey_refusals(eintraege)
        + _survey_approvals(eintraege)
    )
    frueher = _reported_before(eintraege)
    gefunden = [
        Finding(f.kind, f.subject, f.count, f.note, frueher.get(f.key, 0), f.last_ts)
        for f in gefunden
    ]
    gefunden.sort(key=lambda f: (-f.count, f.kind, f.subject))
    return tuple(gefunden[:MAX_FINDINGS])


def _age(last_ts: float, now: float | None) -> str:
    """„last 5d ago" — oder nichts, wenn es keine brauchbare Zeit gibt.

    ⚠️ Der Grund, warum diese Angabe da ist: „17×" liest sich wie ein Zustand von heute.
    Beim ersten Lauf gegen ein echtes Protokoll waren es 17 Ablehnungen aus der Zeit vor
    der Sandbox-Einrichtung — richtig gezaehlt und trotzdem irrefuehrend. Ob eine
    Ablehnung auf einer Regel oder auf einem behobenen Zustand beruht, kann die Maschine
    nicht wissen; wann sie zuletzt vorkam, schon. Also steht es dabei, und der Betreiber
    entscheidet.
    """
    if not last_ts or now is None:
        return ""
    tage = int((now - last_ts) // 86_400)
    if tage >= 1:
        return f" · last {tage}d ago"
    stunden = int((now - last_ts) // 3_600)
    return f" · last {stunden}h ago" if stunden >= 1 else " · just now"


def render(findings, *, now: float | None = None) -> str:
    """Der Bericht — leer, wenn es nichts zu verbessern gibt.

    Leer heisst leer. Ein Selbstreview, der woechentlich „alles in Ordnung" meldet,
    trainiert genau das Wegklicken an, gegen das der zweite Befund oben anschreibt.
    """
    if not findings:
        return ""
    zeilen = [HEADER, ""]
    for f in findings:
        wieder = f" · reported {f.seen_before}× before" if f.seen_before else ""
        zeilen.append(f"• {f.subject} — {f.count}×{_age(f.last_ts, now)}{wieder}")
        if f.note:
            zeilen.append(f"  {f.note}")
    zeilen += ["", FOOTER]
    return "\n".join(zeilen)


_KIND_LABEL = {
    "repeat-failure": "repeat failures",
    "repeat-refusal": "repeat refusals",
    "worn-approval": "worn approvals",
    "gap-cost": "costly gaps",
}


def render_compact(findings, *, now: float | None = None) -> str:
    """Die Chat-Fassung: eine Zeile — die begruendete Liste bleibt einen Befehl entfernt.

    ⚠️ Warum zwei Fassungen: der ausfuehrliche Bericht begruendet jeden Befund, und das
    soll er. Als Nachricht im laufenden Chat las ihn trotzdem niemand mehr — und ein
    Bericht, den man wegwischt, hat dieselbe Wirkung wie keiner (die Begruendung steht
    oben bei MAX_FINDINGS). Die Zeile sagt WIE VIELE es sind und WOVON das meiste
    handelt; wer die Begruendung will, holt sie mit `talos review`. Leer bleibt leer.
    """
    if not findings:
        return ""
    arten: dict[str, int] = {}
    for f in findings:
        beschriftet = _KIND_LABEL.get(f.kind, f.kind)
        arten[beschriftet] = arten.get(beschriftet, 0) + 1
    aufteilung = ", ".join(
        f"{anzahl} {art if anzahl > 1 else art.rstrip('s')}"
        for art, anzahl in sorted(arten.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    groesster = findings[0]
    return (
        f"🔎 Self-review: {len(findings)} findings ({aufteilung}) — "
        f"biggest: {groesster.subject} {groesster.count}×. "
        "Full list: `talos review`. Proposals only — nothing self-applies."
    )


def run_review(argv: list[str] | None = None, *, out=None, db=None) -> int:
    """`talos review [--window <n>]` — derselbe Bericht, nur jetzt und auf stdout.

    Der Befehl ist Komfort, nicht der Mechanismus: der Review laeuft ohnehin nach jedem
    Lauf, sobald er faellig ist (`conductor._maybe_review`). Hier steht er fuer den
    Betreiber, der nicht bis morgen warten will — und fuer einen Cron, der ihn woanders
    hin schicken moechte.

    ⚠️ Anders als der automatische Weg schreibt dieser Befehl **kein** `review.reported`
    ins Protokoll. Sonst verschoebe ein Blick auf den Bericht den naechsten echten um
    einen Tag — und ein Diagnosebefehl, der den Zustand aendert, ist keiner (dieselbe
    Regel wie in `doctor.py`).
    """
    import sys

    from .config import EVENTLOG_DB
    from .eventlog import EventLog

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben("  usage: talos review [--window <n>]\n")
        return 0

    fenster = 1_500
    if "--window" in argumente:
        stelle = argumente.index("--window") + 1
        if stelle < len(argumente):
            try:
                fenster = max(1, int(argumente[stelle]))
            except ValueError:
                schreiben("  --window wants a number\n")
                return 2

    log = EventLog(Path(db) if db is not None else Path(EVENTLOG_DB))
    try:
        eintraege = log.recent(
            fenster, types=("exec.intent", "exec.result", "grant.issued", "review.reported")
        )
    finally:
        log.close()

    luecken: tuple[tuple[str, str], ...] = ()
    try:
        from . import doctor, remedy
        from .config import load_config

        luecken = remedy.gaps(doctor.collect(load_config(), online=False))
    except Exception:
        # Ohne ladbare Konfiguration faellt genau ein Befund weg, nicht der Bericht.
        pass

    import time

    jetzt = time.time()
    text = render(survey(eintraege, gaps=luecken, now=jetzt), now=jetzt)
    schreiben((text or "  nothing to improve that the log can see\n").rstrip() + "\n")
    return 0


def due(last_ts: float | None, now: float, interval_s: float) -> bool:
    """Faellig? Ohne vorherigen Lauf: ja.

    Der erste Review soll stattfinden, nicht ein Intervall lang auf sich warten lassen —
    eine frische Installation ist genau der Moment, in dem eine fehlende Bibliothek noch
    billig zu beheben ist.
    """
    if last_ts is None:
        return True
    return (now - last_ts) >= interval_s
