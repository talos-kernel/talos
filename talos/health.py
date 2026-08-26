"""`talos health` — der Gesundheitszustand, ohne dass der Dienst laufen muss.

Der Chat hat `/health`, aber der setzt einen laufenden Prozess voraus: Zaehler,
Warteschlange und Freigaben leben im Speicher. Dieser Befehl beantwortet die Frage von
aussen — Cron, SSH, Monatsreview — aus den einzigen Quellen, die einen Neustart
ueberleben und die ein Reasoner nicht faelschen kann: Event-Log, Zeitplan-DB und die
Anker-Datei.

Dieselben drei Regeln wie der Doktor: es wird **nichts geaendert** (nur SELECT, und die
Anker-Datei wird gelesen, nie geschrieben), es geht **nicht ins Netz**, und es zeigt
**kein Geheimnis** — hoechstens gekuerzte, bereits redigierte Fehlertexte aus dem Log.

Exit 1 gilt einem einzigen kritischen Befund: einer gebrochenen Hash-Kette. Fehler im
Tagesfenster sind eine Warnung, kein Abbruch — ein Cron-Waechter, der wegen eines
einzelnen Fehlschlags umfaellt, wird abgeschaltet statt gelesen (die Konvention steht
in `doctor.py`).
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path

from .anchor import ANCHORS_FILE, _previous
from .config import EVENTLOG_DB, SCHEDULE_DB
from .ux import SYM_FAIL, SYM_OK

DAY_S = 24 * 3600


def _zeit(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _log_stats(db: Path, *, day_ago: float) -> dict:
    """Laeufe und Fehler aus dem Event-Log — gezaehlt, nicht behauptet.

    Fehler sind das, was als `error` (oder `*.error`, etwa `schedule.error`)
    EINGETRAGEN wurde — dieselbe Lesart wie `/health` im Chat. Ein erfolgreicher Lauf
    ist ein `reason.done` mit Status `answered`; die anderen Endstaende
    (`needs_human`, `step_limit`) sind beendet, aber keine Antwort.
    """
    conn = sqlite3.connect(str(db))
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        last_ts = float(
            conn.execute("SELECT MAX(ts) FROM events").fetchone()[0] or 0.0
        )
        runs_24h = int(conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'reason.done' AND ts >= ?",
            (day_ago,),
        ).fetchone()[0])
        fehler_rows = conn.execute(
            "SELECT ts, payload_json FROM events "
            "WHERE (type = 'error' OR type LIKE '%.error') AND ts >= ? ORDER BY id",
            (day_ago,),
        ).fetchall()
        erfolge = conn.execute(
            "SELECT ts, payload_json FROM events WHERE type = 'reason.done' "
            "ORDER BY id DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    last_ok = 0.0
    for ts, payload_json in erfolge:
        try:
            payload = json.loads(payload_json)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("status") == "answered":
            last_ok = float(ts)
            break
    neueste_fehler = ""
    if fehler_rows:
        try:
            payload = json.loads(fehler_rows[-1][1])
        except ValueError:
            payload = {}
        detail = str(payload.get("error") or payload.get("stage") or "")
        neueste_fehler = " ".join(detail.split())[:120]
    return {
        "events_total": total,
        "last_event_ts": last_ts,
        "runs_24h": runs_24h,
        "errors_24h": len(fehler_rows),
        "newest_error": neueste_fehler,
        "newest_error_ts": float(fehler_rows[-1][0]) if fehler_rows else 0.0,
        "last_success_ts": last_ok,
    }


def _chain(db: Path) -> dict:
    """Das Urteil der Hash-Kette — der eine Befund, der hier kritisch ist."""
    from .eventlog import EventLog

    log = EventLog(db)
    broken = log.verify()
    return {
        "chain_ok": broken is None,
        "chain_broken_id": broken,
        "chained": log.protected_count(),
        "total": log.count(),
    }


def _schedules(db: Path) -> dict:
    """Anstehende Zeitplaene — nur gezaehlt und der naechste Termin, kein Inhalt.

    Die Prompts bleiben absichtlich ungelesen: sie sind Gespraechsstoff des Betreibers
    und gehoeren nicht in eine Ausgabe, die in ein Ticket kopiert werden darf.
    """
    if not db.is_file():
        return {"available": False, "pending": 0, "next_ts": 0.0}
    try:
        conn = sqlite3.connect(str(db))
        try:
            pending = int(conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0])
            naechster = float(
                conn.execute("SELECT MIN(next_run) FROM schedules").fetchone()[0] or 0.0
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return {"available": False, "pending": 0, "next_ts": 0.0}
    return {"available": True, "pending": pending, "next_ts": naechster}


def collect(
    *,
    db_path: Path | None = None,
    schedule_db: Path | None = None,
    anchors_path: Path | None = None,
    now: float | None = None,
) -> dict:
    """Alle Messwerte. Jede fehlende Quelle wird benannt, nicht erfunden."""
    moment = time.time() if now is None else float(now)
    db = Path(db_path) if db_path is not None else Path(EVENTLOG_DB)
    plan_db = Path(schedule_db) if schedule_db is not None else Path(SCHEDULE_DB)
    anker_datei = Path(anchors_path) if anchors_path is not None else ANCHORS_FILE

    daten: dict = {"ts": moment, "event_log": None, "chain": None}
    if db.is_file():
        try:
            daten["event_log"] = _log_stats(db, day_ago=moment - DAY_S)
            daten["chain"] = _chain(db)
        except sqlite3.Error:
            # Eine Datei, die keine Datenbank ist, ist ein Befund — kein Traceback.
            daten["event_log"] = {"unreadable": True}
    daten["schedules"] = _schedules(plan_db)
    anker = _previous(anker_datei)
    daten["anchor"] = (
        {
            "ts": float(anker.get("ts") or 0.0),
            "count": int(anker.get("count") or 0),
            "verify_ok": bool(anker.get("verify_ok")),
        }
        if isinstance(anker, dict)
        else None
    )
    daten["status"] = (
        "critical" if daten["chain"] and not daten["chain"]["chain_ok"] else "ok"
    )
    return daten


def render(daten: dict) -> str:
    """Die Konsolenfassung — Englisch, wie jede Maschinenkonsole hier."""
    zeilen = ["health"]
    log = daten.get("event_log")
    if log is None:
        zeilen.append("  events     no event log yet — it has not run")
    elif log.get("unreadable"):
        zeilen.append("  events     the event log file is not a readable database")
    else:
        zeilen.append(
            f"  events     {log['events_total']} total · last {_zeit(log['last_event_ts'])}"
        )
        erfolg = (
            _zeit(log["last_success_ts"]) if log["last_success_ts"] else "none yet"
        )
        zeilen.append(
            f"  runs       {log['runs_24h']} in the last 24h · last successful {erfolg}"
        )
        if log["errors_24h"]:
            detail = f" — newest: {log['newest_error']}" if log["newest_error"] else ""
            zeilen.append(f"  errors     {log['errors_24h']} in the last 24h{detail}")
        else:
            zeilen.append("  errors     none in the last 24h")
    kette = daten.get("chain")
    if kette is not None:
        if kette["chain_ok"]:
            zeilen.append(
                f"  chain      intact — {kette['chained']} of {kette['total']} entries chained"
            )
        else:
            zeilen.append(
                f"  chain      BROKEN — first altered entry id {kette['chain_broken_id']}"
            )
    plan = daten.get("schedules") or {}
    if not plan.get("available"):
        zeilen.append("  schedules  none")
    else:
        naechster = f" · next {_zeit(plan['next_ts'])}" if plan.get("pending") else ""
        zeilen.append(f"  schedules  {plan['pending']} pending{naechster}")
    anker = daten.get("anchor")
    if anker is None:
        zeilen.append("  anchor     none yet — `talos anchor` pins the first one")
    else:
        zeilen.append(
            f"  anchor     {_zeit(anker['ts'])} · {anker['count']} events · "
            f"verify {'ok' if anker['verify_ok'] else 'FAILED'}"
        )
    return "\n".join(zeilen)


def run_health(
    argv: list[str] | None = None,
    *,
    stdout=None,
    db_path: Path | None = None,
    schedule_db: Path | None = None,
    anchors_path: Path | None = None,
    now: float | None = None,
) -> int:
    """`talos health [--json]`. Exit 1 nur bei gebrochener Kette."""
    argumente = list(argv or [])
    schreiben = (stdout or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben(
            "  usage: talos health [--json]\n"
            "  runs, errors, schedules and the last anchor — from the logs, no network.\n"
        )
        return 0
    fremd = [a for a in argumente if a != "--json"]
    if fremd:
        schreiben(f"  unknown option: {fremd[0]} — usage: talos health [--json]\n")
        return 2

    daten = collect(
        db_path=db_path, schedule_db=schedule_db, anchors_path=anchors_path, now=now
    )
    if "--json" in argumente:
        schreiben(json.dumps(daten, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        schreiben(render(daten) + "\n")
        marke = SYM_OK if daten["status"] == "ok" else SYM_FAIL
        schreiben(f"status {daten['status']} {marke}\n")
    return 1 if daten["status"] == "critical" else 0


__all__ = ["DAY_S", "collect", "render", "run_health"]
