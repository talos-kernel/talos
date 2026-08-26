"""Das Morgen-Briefing — der Stand der Installation, aus haltbaren Quellen.

Ein laufender Agent weiss, wie es ihm geht — aber nur im Speicher, und nur solange er
laeuft. Dieser Befehl beantwortet die Morgenfrage („ist alles in Ordnung?") aus den
einzigen Quellen, die einen Neustart ueberleben und die ein Reasoner nicht faelschen
kann: Event-Log, Zeitplan-DB und Anker-Datei. Dieselbe Quellenlage wie `health.py`,
dessen `collect()` hier die Messwerte liefert; dazu kommt der Blick auf offene
Freigaben, die im Event-Log ihre Spur hinterlassen (`approval.parked` ohne spaeteren
Entscheid, innerhalb der TTL aus `approval.py`).

Drei Regeln wie bei `health` und `anchor`: es wird **nichts geaendert** (nur SELECT),
es geht **nicht ins Netz** (ausser `--send` verlangt es ausdruecklich), und es zeigt
**kein Geheimnis**. Ein gebrochenes oder unlesbares Log macht das Briefing nicht
leiser, sondern lauter: der Befund steht im Text — ein Waechter, der bei Schaden
schweigt, ist schlimmer als keiner.

**Kein Tool-Gebrauch, send-only.** Das Briefing laeuft nicht durch den Agenten und
seine Werkzeuge; es setzt Text zusammen und sendet ihn. `--send` geht an denselben
Owner-Chat wie `anchor --send` — dieselbe Empfaenger-Aufloesung (`anchor._build_sender`),
weil zwei Aufloesungen zwei Wahrheiten darueber waeren, wer der Betreiber ist.

**`--install`** legt den taeglichen Eintrag in der Zeitplan-DB an — ueber
`schedule.ScheduleStore`, also den bestehenden Pfad. Was dort steht, ist ein Auftrag
wie einer aus dem Chat: beim Faelligwerden laeuft er durch den Kernel und unter
`UnattendedCeiling` — `NEEDS_HUMAN` wird zu `DENY`, nichts wird geparkt. Das Installieren
selbst erteilt kein einziges Recht; es schreibt eine Zeile in eine Datenbank, die ohnehin
nur anstoesst. ⚠️ Der deterministische Versandweg (dieser Befehl mit `--send`, ohne
Modell dazwischen) ist der systemd-Timer unter `deploy/`; der Zeitplan-Eintrag speist
einen Bericht-Auftrag in den Ticker, der wie jeder unbeaufsichtigte Lauf WENIGER darf
als ein getippter.

Exit-Codes folgen `anchor`: 0 ok, 1 kritisch (gebrochene Kette, fehlgeschlagener
Versand, verweigerte Installation), 2 falsche Benutzung.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

from .approval import TTL_SECONDS
from .config import EVENTLOG_DB, SCHEDULE_DB
from .ux import SYM_FAIL, SYM_TALOS

# Taeglich um 07:00 — ein Briefing ist ein Kalendertermin, kein Intervall.
BRIEFING_CRON = "0 7 * * *"
# Der Auftrag, der im Zeitplan landet. Formuliert wie ein Auftrag des Betreibers im
# Chat — denn genau so wird der Ticker ihn behandeln: als Nachricht, die durch Kernel
# und unbeaufsichtigte Decke geht. „Nur berichten" steht bewusst drin: der Text ist
# die einzige Stelle, an der der Auftrag seine Absicht festhaelt.
BRIEFING_PROMPT = (
    "Morgen-Briefing: Berichte aus dem Event-Log den Stand der Installation — "
    "Laeufe und Fehler der letzten 24 h, Hash-Kette, offene Freigaben, Alter des "
    "letzten Ankers. Nur berichten, nichts ausfuehren."
)

_APPROVAL_TYPES = ("approval.parked", "approval.granted", "approval.denied", "approval.stale")


def _zeit(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _pending_approvals(db: Path, *, now: float) -> list[str]:
    """Offene Freigaben, aus dem Log gelesen: geparkt, nicht entschieden, nicht abgelaufen.

    Der `ApprovalStore` selbst lebt im Speicher — seine haltbare Spur sind die
    Ereignisse. Ein `approval.parked` gilt als offen, wenn kein spaeterer Entscheid
    (granted/denied/stale) zum selben Werkzeug folgt und die TTL noch laeuft. Wer nach
    Ablauf der TTL nicht entschieden wurde, ist faktisch tot — ihn als „offen" zu
    melden, wuerde den Betreiber nach einem Geist suchen lassen.
    """
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT ts, type, payload_json FROM events WHERE type IN "
            "('approval.parked', 'approval.granted', 'approval.denied', 'approval.stale')"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    geparkt: list[tuple[float, str]] = []
    entscheide: list[tuple[float, str]] = []
    for ts, typ, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except ValueError:
            payload = {}
        werkzeug = str(payload.get("tool") or "") if isinstance(payload, dict) else ""
        if typ == "approval.parked":
            geparkt.append((float(ts), werkzeug))
        else:
            entscheide.append((float(ts), werkzeug))
    offen: list[str] = []
    for ts, werkzeug in geparkt:
        if ts + TTL_SECONDS <= now:
            continue
        erledigt = any(
            e_ts >= ts and (not e_tool or not werkzeug or e_tool == werkzeug)
            for e_ts, e_tool in entscheide
        )
        if not erledigt:
            offen.append(werkzeug or "(unbekanntes Werkzeug)")
    return offen


def compose(
    *,
    db_path: Path | None = None,
    schedule_db: Path | None = None,
    anchors_path: Path | None = None,
    now: float | None = None,
) -> tuple[str, bool]:
    """Der Briefing-Text und ob er einen kritischen Befund enthaelt (gebrochene Kette).

    Jede fehlende oder kaputte Quelle wird im Text benannt — ein Briefing, das eine
    Luecke still ueberspringt, sieht aus wie „alles ok" und ist damit eine falsche
    Nachricht, keine fehlende.
    """
    from .health import collect as health_collect

    moment = time.time() if now is None else float(now)
    db = Path(db_path) if db_path is not None else Path(EVENTLOG_DB)
    daten = health_collect(
        db_path=db, schedule_db=schedule_db, anchors_path=anchors_path, now=moment
    )

    zeilen = [f"{SYM_TALOS} briefing {_zeit(moment)}"]
    log = daten.get("event_log")
    if log is None:
        zeilen.append("events: no event log yet — the installation has not run")
    elif log.get("unreadable"):
        zeilen.append("events: the event log file is NOT a readable database")
    else:
        zeilen.append(
            f"runs: {log['runs_24h']} in the last 24h"
            + (f" · last successful {_zeit(log['last_success_ts'])}"
               if log["last_success_ts"] else " · none successful yet")
        )
        if log["errors_24h"]:
            detail = f" — newest: {log['newest_error']}" if log["newest_error"] else ""
            zeilen.append(f"errors: {log['errors_24h']} in the last 24h{detail}")
        else:
            zeilen.append("errors: none in the last 24h")

    kette = daten.get("chain")
    if kette is None:
        zeilen.append("chain: not checked — no readable event log")
    elif kette["chain_ok"]:
        zeilen.append(
            f"chain: intact — {kette['chained']} of {kette['total']} entries chained"
        )
    else:
        zeilen.append(
            f"chain: BROKEN — first altered entry id {kette['chain_broken_id']}"
        )

    if log is not None and not log.get("unreadable"):
        offen = _pending_approvals(db, now=moment)
        zeilen.append(
            "approvals: none pending" if not offen
            else f"approvals: {len(offen)} pending — {', '.join(offen)}"
        )

    anker = daten.get("anchor")
    if anker is None:
        zeilen.append("anchor: none yet — `talos anchor` pins the first one")
    else:
        alter_h = max(0.0, (moment - anker["ts"]) / 3600)
        zeilen.append(
            f"anchor: {_zeit(anker['ts'])} ({alter_h:.0f}h ago) · {anker['count']} events · "
            f"verify {'ok' if anker['verify_ok'] else 'FAILED'}"
        )

    zeilen.append(f"status: {daten['status']}")
    return "\n".join(zeilen), daten["status"] == "critical"


def _owner_principal(config):
    """Der Owner: erster Telegram-Eintrag der Allowlist — dieselbe Lesart wie `anchor`."""
    owner = next(
        (p for p in sorted(config.allowed_principals, key=str) if p.channel == "telegram"),
        None,
    )
    if owner is None:
        raise ValueError(
            "no telegram principal in TALOS_ALLOWED_PRINCIPALS — no owner chat to send to"
        )
    return owner


def _install(*, schreiben, config, schedules) -> int:
    """Legt den taeglichen Briefing-Eintrag in der Zeitplan-DB an. Idempotent.

    Idempotent, weil ein Installer, der bei jedem Lauf eine Zeile mehr schreibt,
    frueher oder spaeter an `MAX_TASKS` scheitert — und ein Cron, der `--install`
    absichern soll, wuerde genau das tun.
    """
    try:
        owner = _owner_principal(config)
    except ValueError as fehler:
        schreiben(f"{SYM_FAIL} {fehler}\n")
        return 1
    conversation = f"telegram:{owner.user_id}"
    if not schedules.available:
        grund = getattr(schedules, "reason", "") or "store unavailable"
        schreiben(f"{SYM_FAIL} schedule store not writable: {grund}\n")
        return 1
    for task in schedules.list_for(conversation):
        if task.prompt == BRIEFING_PROMPT and task.cron == BRIEFING_CRON:
            schreiben(f"already installed: {task.describe()}\n")
            return 0
    try:
        task = schedules.add(
            conversation=conversation,
            principal=str(owner),
            prompt=BRIEFING_PROMPT,
            cron=BRIEFING_CRON,
        )
    except ValueError as fehler:
        schreiben(f"{SYM_FAIL} install refused: {fehler}\n")
        return 1
    if task is None:
        schreiben(f"{SYM_FAIL} install failed — the schedule store refused the entry\n")
        return 1
    schreiben(
        f"installed: {task.describe()}\n"
        "It fires as an unattended run: UnattendedCeiling applies — anything that\n"
        "would need approval is reported, never performed. Nothing was granted here.\n"
    )
    return 0


def run_briefing(
    argv: list[str] | None = None,
    *,
    stdout=None,
    db_path: Path | None = None,
    schedule_db: Path | None = None,
    anchors_path: Path | None = None,
    now: float | None = None,
    sender: Callable[[str], None] | None = None,
    config=None,
    schedules=None,
) -> int:
    """`talos briefing [--send] [--install]`. Liest nur; sendet nur auf Verlangen."""
    argumente = list(argv or [])
    schreiben = (stdout or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben(
            "  usage: talos briefing [--send] [--install]\n"
            "  morning status from the persistent sources: health, chain, pending\n"
            "  approvals, yesterday's errors, anchor age. --send delivers it to the\n"
            "  owner chat; --install adds the daily entry to the schedule DB.\n"
        )
        return 0
    fremd = [a for a in argumente if a not in ("--send", "--install")]
    if fremd:
        schreiben(
            f"  unknown option: {fremd[0]} — usage: talos briefing [--send] [--install]\n"
        )
        return 2

    if "--install" in argumente:
        if config is None:
            from .config import load_config

            config = load_config(require_channel=False)
        eigener_store = schedules is None
        if eigener_store:
            from .schedule import ScheduleStore

            schedules = ScheduleStore(Path(SCHEDULE_DB))
        try:
            return _install(schreiben=schreiben, config=config, schedules=schedules)
        finally:
            if eigener_store:
                schedules.close()

    text, kritisch = compose(
        db_path=db_path, schedule_db=schedule_db, anchors_path=anchors_path, now=now
    )
    # Der Befund steht IMMER auf stdout — auch mit --send. Ein Zustellweg, der die
    # einzige Anzeige waere, macht den Cron-Lauf blind, sobald das Netz fehlt.
    schreiben(f"{text}\n")
    if "--send" in argumente:
        try:
            if sender is None:
                from .anchor import _build_sender

                sender = _build_sender()
            sender(text)
        except Exception as fehler:
            # Ein Versandfehler ist kritisch fuer diesen Aufruf: wer --send in einen
            # Timer schreibt, will das Briefing AUS der Maschine heraus haben.
            schreiben(f"{SYM_FAIL} send failed: {fehler}\n")
            return 1
        schreiben("sent owner chat\n")
    return 1 if kritisch else 0


__all__ = [
    "BRIEFING_CRON",
    "BRIEFING_PROMPT",
    "compose",
    "run_briefing",
]
