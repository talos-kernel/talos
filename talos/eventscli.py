"""`talos events` und `talos why` — gegen die Blindheit, nicht gegen fehlende Verben.

Die Beobachtung, die zu diesem Modul fuehrte, stammt aus einer Beratung: ein Agent fuehlt
sich nicht deshalb duenn an, weil ihm Befehle fehlen, sondern weil man ihm nicht ansieht,
was er getan hat und warum. Talos protokolliert seit jeher jedes Urteil mit Begruendung —
lesbar war das bisher nur als Bericht am Stueck (`report`) oder als letzte Zeile
(`status`).

Zwei Sichten, beide **ausschliesslich lesend**:

    events   was ist passiert — filterbar nach Lauf, Typ und Werkzeug
    why      warum wurde GENAU DAS erlaubt oder abgelehnt, und was folgte daraus

⚠️ **Kein Befehl hier bewirkt etwas.** Das ist keine Bescheidenheit, sondern die Regel,
unter der sie ueberhaupt entstehen durften: entweder read-only, oder durch Kernel,
Capability-Token und die unbeaufsichtigte Decke. Es gibt hier keinen dritten Fall.

⚠️ **`undo` fehlt hier mit Absicht.** Es liegt nahe, es danebenzustellen — es gibt es ja
schon als `/undo`. Aber dann haette dieselbe Handlung zwei Wege: einen durch die
Kommando-Kette des Conductors und einen daneben. Seit `talos chat` ist `/undo` von der
Kommandozeile aus erreichbar, ueber genau den Weg, den auch der Messenger nimmt. Ein
zweiter waere reine Bequemlichkeit gegen die Doktrin dieses Projekts.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

__all__ = ["run_events", "run_why"]

DEFAULT_LIMIT = 25
MAX_LIMIT = 500
SUMMARY_CHARS = 84


def _stamp(ts: object) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "??-?? ??:??:??"


def _flag(name: str, argv: list[str], default: str = "") -> str:
    if name not in argv:
        return default
    stelle = argv.index(name) + 1
    return argv[stelle] if stelle < len(argv) else default


def _one_line(text: object, limit: int = SUMMARY_CHARS) -> str:
    """Alles auf eine Zeile. ⚠️ Dieselbe Regel wie in `remedy.py`: ein Zeilenumbruch aus
    fremdem Text koennte in einer Liste eine Zeile vortaeuschen, die es nicht gibt."""
    return " ".join(str(text or "").split())[:limit]


def _summary(event: dict) -> str:
    """Die eine Zeile, die den Unterschied macht: bei einem Urteil das Urteil."""
    p = event.get("payload") or {}
    werkzeug = p.get("tool")
    urteil = p.get("verdict") or p.get("status")
    teile = [str(t) for t in (werkzeug, urteil) if t]
    grund = p.get("reason") or p.get("detail")
    if grund:
        teile.append(_one_line(grund))
    if teile:
        return " · ".join(teile)
    # Kein bekanntes Feld: lieber die rohen Schluessel zeigen als eine leere Zeile —
    # eine Liste mit unerklaerten Luecken sieht aus wie ein Fehler im Werkzeug.
    return _one_line(", ".join(sorted(p)) if p else "")


def run_events(argv: list[str] | None = None, *, out=None, db=None) -> int:
    """`talos events [--limit n] [--type t] [--tool t] [--run id]`."""
    from .config import EVENTLOG_DB
    from .eventlog import EventLog

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben("  usage: talos events [--limit n] [--type t] [--tool t] [--run id]\n")
        return 0

    try:
        limit = min(MAX_LIMIT, max(1, int(_flag("--limit", argumente, str(DEFAULT_LIMIT)))))
    except ValueError:
        schreiben("  --limit wants a number\n")
        return 2

    typ, werkzeug, lauf = _flag("--type", argumente), _flag("--tool", argumente), _flag("--run", argumente)
    log = EventLog(Path(db) if db is not None else Path(EVENTLOG_DB))
    try:
        # Bei einem Filter wird weiter zurueck gelesen — sonst faende `--tool run_shell`
        # in den letzten 25 Zeilen nichts und behauptete, es sei nie vorgekommen.
        roh = log.by_run(lauf) if lauf else log.recent(
            limit * 20 if (typ or werkzeug) else limit,
            types=(typ,) if typ else (),
        )
    finally:
        log.close()

    if werkzeug:
        roh = [e for e in roh if str((e.get("payload") or {}).get("tool") or "") == werkzeug]
    gezeigt = roh[-limit:]
    if not gezeigt:
        schreiben("  nothing matched\n")
        return 0

    for e in gezeigt:
        schreiben(f"  {e['id']:>7}  {_stamp(e.get('ts'))}  {str(e.get('type') or ''):<16} "
                  f"{_summary(e)}\n")
    if len(roh) > len(gezeigt):
        # ⚠️ Eine stille Kuerzung liest sich wie Vollstaendigkeit.
        schreiben(f"  … {len(roh) - len(gezeigt)} older match(es) not shown (--limit)\n")
    schreiben(f"  `talos why <id>` explains one of them\n")
    return 0


def _verdict_lines(event: dict) -> list[str]:
    """Was der Kernel entschieden hat — und woran.

    Nur Felder, die der KERNEL geschrieben hat. Dieselbe Regel wie in `lessons.py`:
    was das Modell formuliert hat, taugt nicht als Beleg dafuer, was entschieden wurde.
    """
    p = event.get("payload") or {}
    zeilen = []
    for etikett, schluessel in (("tool", "tool"), ("verdict", "verdict"), ("status", "status"),
                                ("reason", "reason"), ("effect", "effect")):
        if p.get(schluessel):
            zeilen.append(f"  {etikett:<9} {_one_line(p[schluessel], 200)}")
    ziele = p.get("targets") or p.get("derived_targets")
    if ziele:
        # Die Ziele sind ABGELEITET, nie uebernommen — das ist der Satz, an dem der
        # ganze Kernel haengt, und er gehoert in die Erklaerung.
        zeilen.append(f"  targets   {_one_line(', '.join(str(z) for z in ziele), 200)}")
        zeilen.append("            (derived from the real arguments, never taken from them)")
    return zeilen


def run_why(argv: list[str] | None = None, *, out=None, db=None) -> int:
    """`talos why <event-id>` — warum genau das erlaubt oder abgelehnt wurde."""
    from .config import EVENTLOG_DB
    from .eventlog import EventLog

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    zahlen = [a for a in argumente if a.lstrip("-").isdigit() and not a.startswith("-")]
    if "--help" in argumente or "-h" in argumente or not zahlen:
        schreiben("  usage: talos why <event-id>      (ids come from `talos events`)\n")
        return 0 if ("--help" in argumente or "-h" in argumente) else 2

    log = EventLog(Path(db) if db is not None else Path(EVENTLOG_DB))
    try:
        ereignis = log.by_id(int(zahlen[0]))
        umfeld = log.by_run(str(ereignis.get("run_id") or "")) if ereignis else []
    finally:
        log.close()

    if ereignis is None:
        schreiben(f"  no event {zahlen[0]} in this log\n")
        return 1

    schreiben(f"  event {ereignis['id']}  ·  {_stamp(ereignis.get('ts'))}  ·  "
              f"{ereignis.get('type')}  ·  actor {ereignis.get('actor')}\n")
    zeilen = _verdict_lines(ereignis)
    if zeilen:
        schreiben("\n".join(zeilen) + "\n")
    else:
        schreiben(f"  {_summary(ereignis) or '(no fields)'}\n")

    # Der Zusammenhang: ein Urteil allein sagt nicht, was daraus wurde. Genau diese
    # Luecke — „abgelehnt, und dann?" — ist der Grund, warum ein Protokoll oft ungelesen
    # bleibt.
    andere = [e for e in umfeld if e["id"] != ereignis["id"]]
    if andere:
        schreiben(f"\n  the same run ({ereignis.get('run_id')}):\n")
        for e in andere:
            schreiben(f"    {e['id']:>7}  {str(e.get('type') or ''):<16} {_summary(e)}\n")
    return 0
