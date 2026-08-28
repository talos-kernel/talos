"""`talos dashboard` — der Maschinenraum, von aussen, ohne Griff nach innen.

Die Doktrin (CLAUDE.md) laesst keinen eingehenden Kanal in den Agentenprozess — kein
HTTP, kein Socket, kein Webhook. Dieses Dashboard widerspricht dem nicht, es steht
daneben: ein EIGENER Prozess, der dieselben Dateien read-only oeffnet, die auch
`talos health` und `talos events` lesen. Vorbilder sind `health.py` (nur SELECT aus
Event-Log, Zeitplan-DB und Anker) und `examples/status-source/status-server.py`
(stdlib-`http.server`, feste Routen, Loopback plus Tailnet-Proxy als zwei Schranken).

Drei Regeln, und sie gelten hier ohne Ausnahme:

  1. **Beobachten, nicht eingreifen.** Es gibt nur GET. POST/PUT/DELETE bekommen 405,
     unbekannte Pfade 404. Freigaben bleiben, wo sie hingehören: im Chat des
     Betreibers. Ein Dashboard mit einem „Approve"-Knopf waere ein zweiter
     Erlaubnisweg am Kernel vorbei — genau der Kardinalfehler, gegen den der
     Kernel gebaut ist.
  2. **Read-only ist durchgesetzt, nicht behauptet.** Die Datenbanken werden als
     `file:…?mode=ro` geoeffnet und mit `PRAGMA query_only=ON` verriegelt: selbst
     ein Fehler in diesem Modul kann nichts schreiben. (Ausnahme, geerbt von
     `health.py`: `/api/status` ruft `health.collect()`, das die Hash-Kette ueber
     ein `EventLog` verifiziert — derselbe etablierte Pfad wie `talos health`.)
  3. **Kein Geheimnis, kein Betreiber-Text.** Payloads werden gedeckelt und durch
     dieselbe Redaktion wie `telegram._redact` geschickt; die Zeitplan-Prompts
     bleiben ungelesen — dieselbe Begruendung wie in `health.py:_schedules`: sie
     sind Gespraechsstoff des Betreibers und gehoeren nicht in eine Ausgabe, die
     ein Fenster fuellt.

Was „laufende Runs" und „offene Freigaben" angeht, ist das Log die einzige Quelle —
und das Log ist append-only, nicht ein Zustandsspeicher. Beides wird deshalb als
**Heuristik** ausgewiesen: `reason.started` ohne `reason.done`, `exec.intent` mit
`needs_human` ohne spaetere Antwort. Das ist ehrlich markiert statt als Kanon
verkauft; der exakte Stand lebt im fluechtigen Speicher des Agentenprozesses, zu
dem dieser Prozess bewusst keinen Draht hat.
"""
from __future__ import annotations

import http.server
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import DATA_DIR, EVENTLOG_DB, SCHEDULE_DB

BIND = "127.0.0.1"
PORT = 8810
# Deckel auf jede Antwort. Die Sektionen sind von sich aus begrenzt (feste Limits
# unten); dieser Deckel ist der zweite Riegel, falls eine Annahme darueber kippt.
MAX_BODY = 256 * 1024

EVENTS_LIMIT = 50
RUNS_DONE_LIMIT = 25
RUNS_OPEN_LIMIT = 25
APPROVALS_OPEN_LIMIT = 25
SCHEDULES_SHOWN = 5
# Ein Feld im Response ist eine Zeile im Maschinenraum, nie ein Dokument.
FIELD_CHARS = 140
PAYLOAD_KEYS = 12


class SourceError(Exception):
    """Eine Quelle fehlt oder ist keine Datenbank — ein Befund, kein Traceback."""


# --- Die Redaktion: dieselben Muster wie telegram._redact ------------------------------------
#
# ⚠️ Bewusst DUPLIZIERT statt importiert: telegram.py zieht `requests` und die halbe
# Kanal-Maschinerie herein — ein Beobachter-Prozess, der stdlib-only bleiben soll,
# importiert den Sende-Kanal nicht. Der Preis ist Drift-Gefahr; sie ist durch
# tests/test_dashboard.py::test_redaction_matches_the_telegram_pattern abgesichert.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|api[_-]?key|authorization|password|passwd|secret)\s*[:=]\s*\S+"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:sk|ghp|xox[baprs])-[A-Za-z0-9._-]{6,}")


def _redact(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _SECRET_TOKEN.sub("[REDACTED]", text)
    return text[:240]


def _ro(path: Path) -> sqlite3.Connection:
    """Eine Verbindung, die nicht schreiben KANN — URI mode=ro plus query_only.

    Zwei Riegel statt einer: `mode=ro` verweigert das Schreiben auf Datei-Ebene,
    `query_only=ON` jede schreibende Anweisung. Fehlt die Datei oder ist sie keine
    Datenbank, fliegt ein benannter Fehler — die Sektion meldet dann Leere, nie 500.
    """
    pfad = Path(path)
    if not pfad.is_file():
        raise SourceError(f"missing: {pfad.name}")
    try:
        conn = sqlite3.connect(f"file:{pfad}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("SELECT 1")  # zwingt das Oeffnen jetzt, nicht spaeter im Handler
        return conn
    except sqlite3.Error as fehler:
        raise SourceError(f"unreadable: {pfad.name} ({type(fehler).__name__})") from None


def _zeit(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _cap(value: object) -> object:
    """Ein Payload-Feld, gekuerzt und redigiert. Verschachteltes wird verflacht —
    der Maschinenraum zeigt Umrisse, keine Dokumente."""
    if isinstance(value, str):
        return _redact(value)[:FIELD_CHARS]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_cap(v) for v in list(value)[:5]]
    return _redact(str(value))[:FIELD_CHARS]


def _cap_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {"value": _cap(payload)}
    return {str(k): _cap(v) for k, v in list(payload.items())[:PAYLOAD_KEYS]}


def _load(raw: str) -> dict:
    try:
        wert = json.loads(raw)
    except ValueError:
        return {}
    return wert if isinstance(wert, dict) else {}


@dataclass(frozen=True)
class Collector:
    """Die Pfade zu den Quellen. Fehlende Dateien sind erlaubt — die Sektionen
    melden dann benannte Leere statt zu sterben (der Befund IST die Antwort)."""

    eventlog_db: Path = EVENTLOG_DB
    schedule_db: Path = SCHEDULE_DB
    blueprint_state: Path = DATA_DIR / "blueprints.json"
    anchors_path: Path | None = None  # None = Vorgabe von health.collect()

    # --- /api/status ----------------------------------------------------------------
    def status(self) -> dict:
        """Der Gesundheitsstand — wiederverwendet `health.collect()` statt eine
        zweite, aehnliche Wahrheit zu erfinden."""
        from . import health

        daten = health.collect(
            db_path=self.eventlog_db,
            schedule_db=self.schedule_db,
            anchors_path=self.anchors_path,
        )
        daten["version"] = __version__
        return daten

    # --- /api/runs -------------------------------------------------------------------
    def runs(self) -> dict:
        """Laufende und abgeschlossene Laeufe, aus dem Log approximiert.

        ⚠️ Heuristik, ehrlich gelabelt: „laufend" ist `reason.started` ohne
        folgendes `reason.done`. Ein Lauf, dessen Prozess starb, bevor `done`
        geschrieben wurde, bleibt hier sichtbar — mit seinem Alter, damit die
        Leiche von einem lebenden Lauf zu unterscheiden ist.
        """
        try:
            conn = _ro(self.eventlog_db)
        except SourceError as fehler:
            return {"available": False, "reason": str(fehler), "running": [], "completed": []}
        try:
            gestartet = dict(conn.execute(
                "SELECT run_id, MIN(ts) FROM events WHERE type = 'reason.started' "
                "GROUP BY run_id"
            ).fetchall())
            beendet: dict[str, tuple[float, dict]] = {}
            for run_id, ts, roh in conn.execute(
                "SELECT run_id, ts, payload_json FROM events "
                "WHERE type = 'reason.done' ORDER BY id"
            ).fetchall():
                beendet[run_id] = (float(ts), _load(roh))
            werkzeuge = dict(conn.execute(
                "SELECT run_id, COUNT(*) FROM events WHERE type = 'exec.result' "
                "GROUP BY run_id"
            ).fetchall())
        except sqlite3.Error as fehler:
            conn.close()
            return {"available": False, "reason": f"query failed ({type(fehler).__name__})",
                    "running": [], "completed": []}
        conn.close()

        jetzt = time.time()
        laufend = [
            {
                "run_id": run_id,
                "started_ts": float(ts),
                "started": _zeit(float(ts)),
                "age_s": round(jetzt - float(ts), 1),
                "tool_calls": int(werkzeuge.get(run_id, 0)),
            }
            for run_id, ts in gestartet.items()
            if run_id not in beendet
        ]
        laufend.sort(key=lambda r: -r["started_ts"])
        fertig = []
        for run_id, (ts, payload) in sorted(
            beendet.items(), key=lambda eintrag: -eintrag[1][0]
        )[:RUNS_DONE_LIMIT]:
            start = float(gestartet.get(run_id, ts))
            fertig.append({
                "run_id": run_id,
                "status": str(payload.get("status") or ""),
                "done_ts": ts,
                "done": _zeit(ts),
                "duration_s": round(ts - start, 1),
                "tool_calls": int(werkzeuge.get(run_id, 0)),
            })
        return {
            "available": True,
            "basis": "heuristic — reason.started without reason.done counts as running",
            "running": laufend[:RUNS_OPEN_LIMIT],
            "completed": fertig,
        }

    # --- /api/approvals ----------------------------------------------------------------
    def approvals(self) -> dict:
        """Stehende Freigaben exakt, offene als Heuristik — getrennt gelabelt.

        Die stehenden Regeln spielen dieselben zwei Event-Typen nach wie
        `standing.restore()` — nur ohne `EventLog`, denn dessen Konstruktor setzt
        Schreib-Pragmas, und dieser Prozess oeffnet die Datei read-only.
        `_from_payload` bleibt die eine Stelle, die entscheidet, was als Regel
        durchgeht: keine zweite, mildere Lesart daneben.
        """
        leer = {"basis": "heuristic — needs_human without a recorded answer",
                "items": []}
        try:
            conn = _ro(self.eventlog_db)
        except SourceError as fehler:
            return {"available": False, "reason": str(fehler),
                    "standing": [], "open": leer}
        from .standing import RESTORE_LIMIT, _from_payload

        try:
            roh = conn.execute(
                "SELECT type, payload_json FROM events "
                "WHERE type IN ('approval.standing', 'approval.standing_revoked') "
                "ORDER BY id DESC LIMIT ?",
                (RESTORE_LIMIT,),
            ).fetchall()
            regeln: dict[tuple, object] = {}
            for typ, payload_json in reversed(roh):  # chronologisch, wie restore()
                regel = _from_payload(_load(payload_json))
                if regel is None:
                    continue
                schluessel = (regel.conversation, regel.principal, regel.key)
                if typ == "approval.standing":
                    regeln[schluessel] = regel
                else:
                    regeln.pop(schluessel, None)

            intents = conn.execute(
                "SELECT id, ts, run_id, payload_json FROM events "
                "WHERE type = 'exec.intent' AND payload_json LIKE '%needs_human%' "
                "ORDER BY id"
            ).fetchall()
            ergebnisse = conn.execute(
                "SELECT id, run_id, payload_json FROM events "
                "WHERE type = 'exec.result' ORDER BY id"
            ).fetchall()
            beschlossen = conn.execute(
                "SELECT run_id, MIN(id) FROM events WHERE type IN "
                "('approval.denied', 'approval.refused', 'approval.stale') GROUP BY run_id"
            ).fetchall()
        except sqlite3.Error as fehler:
            conn.close()
            return {"available": False, "reason": f"query failed ({type(fehler).__name__})",
                    "standing": [], "open": leer}
        conn.close()

        stehend = [
            {
                "tool": r.tool,
                "label": _redact(r.label)[:FIELD_CHARS],
                "conversation": r.conversation,
                "principal": r.principal,
                "created_ts": r.created_at,
                "created": _zeit(r.created_at),
            }
            for r in sorted(regeln.values(), key=lambda r: (r.created_at, r.key))
        ]

        # Letztes exec.result je (run, tool): status needs_human heisst geparkt,
        # alles andere ist beantwortet. Ein Deny/Stale beendet die Wartung ebenfalls.
        jetzt = time.time()
        letztes: dict[tuple[str, str], tuple[int, str]] = {}
        for eid, run_id, payload_json in ergebnisse:
            payload = _load(payload_json)
            letztes[(run_id, str(payload.get("tool") or ""))] = (
                int(eid), str(payload.get("status") or ""))
        verweigert = {run_id: int(eid) for run_id, eid in beschlossen}
        offen = []
        for eid, ts, run_id, payload_json in intents:
            payload = _load(payload_json)
            werkzeug = str(payload.get("tool") or "")
            stand = letztes.get((run_id, werkzeug))
            if stand is not None and stand[1] != "needs_human":
                continue  # beantwortet (ausgefuehrt oder endgueltig abgelehnt)
            if run_id in verweigert and verweigert[run_id] > int(eid):
                continue
            offen.append({
                "run_id": run_id,
                "tool": werkzeug,
                "since_ts": float(ts),
                "since": _zeit(float(ts)),
                "age_s": round(jetzt - float(ts), 1),
                "targets": _cap(payload.get("targets") or []),
            })
        offen.sort(key=lambda e: -e["since_ts"])
        return {
            "available": True,
            "standing": stehend,
            "open": {"basis": leer["basis"], "items": offen[:APPROVALS_OPEN_LIMIT]},
        }

    # --- /api/events -------------------------------------------------------------------
    def events(self) -> dict:
        """Die letzten Ereignisse — gedeckelt, redigiert, run_id gekuerzt."""
        try:
            conn = _ro(self.eventlog_db)
        except SourceError as fehler:
            return {"available": False, "reason": str(fehler), "count": 0, "events": []}
        try:
            roh = conn.execute(
                "SELECT id, ts, run_id, actor, type, payload_json FROM events "
                "ORDER BY id DESC LIMIT ?",
                (EVENTS_LIMIT,),
            ).fetchall()
        except sqlite3.Error as fehler:
            conn.close()
            return {"available": False, "reason": f"query failed ({type(fehler).__name__})",
                    "count": 0, "events": []}
        conn.close()
        eintraege = [
            {
                "id": int(eid),
                "ts": float(ts),
                "time": _zeit(float(ts)),
                "run_id": str(run_id)[:8],
                "actor": str(actor)[:40],
                "type": str(typ)[:60],
                "payload": _cap_payload(_load(payload_json)),
            }
            for eid, ts, run_id, actor, typ, payload_json in reversed(roh)
        ]
        return {"available": True, "count": len(eintraege), "events": eintraege}

    # --- /api/schedules -----------------------------------------------------------------
    def schedules(self) -> dict:
        """Anzahl, naechste Fälligkeiten, Blueprints — NIE ein Prompt.

        Dieselbe Grenze wie `health.py:_schedules`: die Prompts sind Text des
        Betreibers und bleiben ungelesen. Gezeigt werden Rhythmus und Termin —
        das, was man braucht, um „laeuft der Waechter" zu beantworten.
        """
        antwort: dict = {"available": False, "count": 0, "upcoming": [],
                         "blueprints": {}}
        # Der Blueprint-Stand ist eine JSON-Datei des Betreibers — lesbar auch ohne
        # Zeitplan-DB. Namen sind betreiber-gewaehlt und zeigbar (blueprints.py).
        try:
            roh = json.loads(Path(self.blueprint_state).read_text(encoding="utf-8"))
            if isinstance(roh, dict):
                antwort["blueprints"] = {
                    str(name): {
                        "enabled": bool(eintrag.get("enabled")),
                        "task_id": str(eintrag.get("task_id") or ""),
                    }
                    for name, eintrag in roh.items()
                    if isinstance(eintrag, dict)
                }
        except (OSError, ValueError):
            pass  # kaputt oder fehlend heisst: nichts installiert (fail-open)

        try:
            conn = _ro(self.schedule_db)
        except SourceError as fehler:
            antwort["reason"] = str(fehler)
            return antwort
        try:
            antwort["count"] = int(conn.execute(
                "SELECT COUNT(*) FROM schedules").fetchone()[0])
            faellig = conn.execute(
                "SELECT id, next_run, interval_s, cron, once FROM schedules "
                "ORDER BY next_run LIMIT ?",
                (SCHEDULES_SHOWN,),
            ).fetchall()
        except sqlite3.Error as fehler:
            conn.close()
            antwort["reason"] = f"query failed ({type(fehler).__name__})"
            return antwort
        conn.close()
        antwort["available"] = True
        naechste: dict[str, float] = {}
        antwort["upcoming"] = []
        for task_id, next_run, interval_s, cron, once in faellig:
            naechste[str(task_id)] = float(next_run)
            antwort["upcoming"].append({
                "task_id": str(task_id),
                "next_run_ts": float(next_run),
                "next_run": _zeit(float(next_run)),
                "interval_s": int(interval_s),
                "cron": str(cron or ""),
                "once": bool(once),
            })
        for eintrag in antwort["blueprints"].values():
            termin = naechste.get(eintrag["task_id"])
            if termin is not None:
                eintrag["next_run_ts"] = termin
                eintrag["next_run"] = _zeit(termin)
        return antwort


# --- Die Seite: self-contained, im Look von talos-agent.ch --------------------------------------
#
# Dieselbe Designsprache wie die oeffentliche Site (VT323 + Sometype Mono, die
# oklch-Tokens, Scanline-Overlay, spec-row, Stempel) — aber als Instrument, nicht
# als Vitrine: jede Zahl kommt aus den /api-Routen, gerendert wird ausschliesslich
# ueber textContent (Daten beruehren nie innerHTML). Fonts und Sigil sind base64-
# eingebettet (dashboard_assets): kein Request verlaesst den Prozess, kein Dritter
# sieht einen Abruf — die Self-contained-Regel gilt unverkuerzt.
from .dashboard_assets import (
    FONT_SOMETYPE_MONO_WOFF2_B64,
    FONT_VT323_WOFF2_B64,
    SIGIL_PNG_B64,
)


def _asset(text: str) -> str:
    """base64 ohne die Umbrueche des generierten Moduls."""
    return "".join(text.split())


_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>TALOS — machine room</title>
<meta name="theme-color" content="#17140F">
<style>
@font-face { font-family:"VT323"; src:url("data:font/woff2;base64,__FONT_VT323__") format("woff2"); font-weight:400; font-display:swap; }
@font-face { font-family:"Sometype Mono"; src:url("data:font/woff2;base64,__FONT_MONO__") format("woff2"); font-weight:400 700; font-style:normal; font-display:swap; }

/* Dieselben Tokens wie talos-agent.ch — ein System, ein Look. */
:root{
  --bg:    oklch(0.115 0.012 75);
  --bg2:   oklch(0.145 0.016 78);
  --amber: oklch(0.80 0.155 80);
  --hot:   oklch(0.92 0.13 85);
  --dim:   oklch(0.56 0.09 80);
  --faint: oklch(0.38 0.055 80);
  --line:  oklch(0.28 0.04 80);
  --deny:  oklch(0.64 0.19 32);
  --allow: oklch(0.74 0.14 145);
  --crt:   "VT323", monospace;
  --mono:  "Sometype Mono", monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth; scroll-padding-top:80px}
body{
  background:var(--bg); color:var(--amber);
  font-family:var(--mono); font-size:14px; line-height:1.7;
  font-variant-numeric:tabular-nums;
}
::selection{background:var(--amber); color:var(--bg)}

/* Scanlines + Vignette, dezent dosiert wie im Manual. */
.crt-fx{position:fixed; inset:0; pointer-events:none; z-index:90}
.crt-fx::before{
  content:""; position:absolute; inset:0;
  background:repeating-linear-gradient(0deg, oklch(0 0 0/.13) 0 1px, transparent 1px 3px);
}
.crt-fx::after{
  content:""; position:absolute; inset:0;
  background:radial-gradient(ellipse at 50% 40%, transparent 62%, oklch(0 0 0/.34) 100%);
}

.wrap{max-width:1100px; margin:0 auto; padding:0 clamp(18px,4vw,52px)}

nav{position:fixed; inset:0 0 auto 0; z-index:50; border-bottom:1px solid var(--line);
  background:color-mix(in oklch, var(--bg) 88%, transparent); backdrop-filter:blur(8px)}
nav .wrap{display:flex; gap:24px; align-items:baseline; padding-top:13px; padding-bottom:13px}
.brand{font-family:var(--crt); font-size:25px; color:var(--hot); letter-spacing:.1em;
  text-shadow:0 0 10px oklch(0.8 0.155 80/.6);
  display:inline-flex; align-items:center; gap:10px}
.brand .mark{width:24px; height:24px; display:block}
nav a.lnk{font-size:11px; color:var(--dim); text-decoration:none; letter-spacing:.16em;
  text-transform:uppercase}
nav a.lnk:hover{color:var(--hot)}
nav .sp{flex:1}
.live{font-size:11px; color:var(--faint); letter-spacing:.12em; white-space:nowrap}
.live i{color:var(--allow); font-style:normal; animation:pulse 2s infinite}
@keyframes pulse{50%{opacity:.3}}
@media(max-width:820px){nav a.lnk{display:none}}

main{padding-top:58px}

.hero{padding:clamp(36px,6vh,64px) 0 26px}
.doc-line{font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; color:var(--dim);
          display:flex; gap:24px; flex-wrap:wrap; margin-bottom:22px}
.doc-line::before{content:"● "; color:var(--allow)}
h1{font-family:var(--crt); font-weight:400; font-size:clamp(40px,5.5vw,68px); line-height:.98;
   color:var(--hot); text-shadow:0 0 18px oklch(0.8 0.155 80/.5), 0 0 60px oklch(0.8 0.155 80/.22)}
.hero .lede{margin-top:20px; max-width:66ch; color:var(--dim); font-size:13.5px}
.hero .lede strong{color:var(--hot); font-weight:600}

.spec-row{display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:1px;
          background:var(--line); border:1px solid var(--line); margin-top:30px}
.spec{background:var(--bg); padding:18px 20px; font-size:11px; color:var(--dim); line-height:1.55;
      letter-spacing:.08em; text-transform:uppercase}
.spec b{display:block; font-family:var(--crt); font-weight:400; font-size:clamp(26px,3vw,36px);
        color:var(--hot); line-height:1.1; letter-spacing:.03em; text-transform:none;
        text-shadow:0 0 14px oklch(0.8 0.155 80/.45)}
.spec b.bad{color:var(--deny); text-shadow:0 0 14px oklch(0.64 0.19 32/.5)}
.spec b.good{color:var(--allow); text-shadow:0 0 14px oklch(0.74 0.14 145/.4)}

section{padding:34px 0 8px; scroll-margin-top:76px}
h2{font-family:var(--crt); font-weight:400; font-size:clamp(28px,3.4vw,40px); line-height:1.05;
   color:var(--hot); text-shadow:0 0 14px oklch(0.8 0.155 80/.4); margin-bottom:4px}
h2 .n{display:block; font-size:15px; letter-spacing:.14em; color:var(--dim);
      text-shadow:none; margin-bottom:6px}
h2 .n::before{content:"┌─[ "}
h2 .n::after{content:" ]"}
h3{font-family:var(--crt); font-weight:400; font-size:19px; color:var(--hot);
   margin:22px 0 6px; text-shadow:0 0 10px oklch(0.8 0.155 80/.3)}
.basis{font-size:11px; color:var(--faint); letter-spacing:.05em; margin:2px 0 10px}

.note{border:1px solid var(--line); border-left:3px solid var(--faint);
      background:var(--bg2); padding:12px 15px; margin:14px 0; max-width:70ch;
      font-size:12.5px; color:var(--amber)}
.note.warn{border-left-color:var(--deny)}
.note.good{border-left-color:var(--allow)}
.note b{display:block; font-size:10px; letter-spacing:.16em; text-transform:uppercase;
        color:var(--faint); margin-bottom:5px; font-weight:700}
.note.warn b{color:var(--deny)}
.note.good b{color:var(--allow)}

.kv{display:grid; grid-template-columns:minmax(170px,250px) 1fr; border:1px solid var(--line);
    margin:14px 0; font-size:12.5px}
.kv>div{padding:7px 13px; border-bottom:1px solid var(--line); word-break:break-word}
.kv>div:nth-child(odd){color:var(--dim); border-right:1px solid var(--line); background:var(--bg2)}
.kv>div:nth-last-child(-n+2){border-bottom:none}

.cards{display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:1px;
       background:var(--line); border:1px solid var(--line); margin:14px 0}
.card{background:var(--bg); padding:13px 15px; font-size:12.5px; color:var(--dim); line-height:1.6}
.card b{display:block; font-family:var(--crt); font-weight:400; font-size:18px;
        color:var(--hot); letter-spacing:.04em; margin-bottom:2px}
.card.warn{background:color-mix(in oklch, var(--bg) 92%, var(--deny))}

.tw{overflow-x:auto; margin:14px 0; border:1px solid var(--line); background:var(--bg)}
table{border-collapse:collapse; width:100%; font-size:12px; min-width:560px}
th,td{text-align:left; padding:8px 13px; border-bottom:1px solid var(--line); vertical-align:top}
th{font-size:10px; letter-spacing:.15em; text-transform:uppercase; color:var(--dim);
   font-weight:400; background:var(--bg2)}
tr:last-child td{border-bottom:none}
tbody tr:nth-child(even){background:color-mix(in oklch, var(--bg) 94%, var(--amber))}
td.d{color:var(--dim)}
td.mono{font-size:11.5px; word-break:break-word}

.pill{display:inline-block; border:1.5px solid currentColor; padding:0 8px; font-size:10px;
      letter-spacing:.13em; text-transform:uppercase; white-space:nowrap}
.pill.good{color:var(--allow)} .pill.bad{color:var(--deny)}
.pill.warn{color:var(--amber)} .pill.dim{color:var(--dim)}

.chips{display:flex; flex-wrap:wrap; gap:8px; margin:14px 0}
.chip{border:1px solid var(--line); background:var(--bg2); padding:6px 12px;
      font-size:12px; color:var(--dim)}
.chip .on{color:var(--allow); font-style:normal}
.chip .off{color:var(--deny); font-style:normal}
.chip .t{color:var(--hot)}

footer{border-top:1px solid var(--line); margin-top:44px; padding:30px 0 44px;
       font-size:11.5px; color:var(--faint)}
footer .wrap{display:flex; gap:22px; flex-wrap:wrap}
footer .sp{flex:1}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important; transition:none!important}}
</style>
</head>
<body>

<div class="crt-fx" aria-hidden="true"></div>

<nav>
  <div class="wrap">
    <span class="brand"><img class="mark" src="data:image/png;base64,__SIGIL__" alt="" width="24" height="24">TALOS</span>
    <a class="lnk" href="#status">Status</a>
    <a class="lnk" href="#runs">Runs</a>
    <a class="lnk" href="#approvals">Approvals</a>
    <a class="lnk" href="#schedules">Schedules</a>
    <a class="lnk" href="#events">Events</a>
    <span class="sp"></span>
    <span class="live"><i>●</i> OBSERVING · <span id="rev">…</span></span>
  </div>
</nav>

<main>
<div class="wrap">

  <div class="hero">
    <div class="doc-line">
      <span>Dashboard · read-only</span>
      <span>Feed · event log + schedule store</span>
      <span id="stamp">connecting…</span>
    </div>
    <h1>The machine room.</h1>
    <p class="lede"><strong>Observing, not intervening.</strong> This page reads the same
    files <code>talos health</code> and <code>talos events</code> read — opened read-only and
    locked that way. Approvals stay in the operator&rsquo;s chat: there is no button here,
    by design. Bind is loopback; reach comes from a tailnet proxy in front, never from
    this process.</p>
    <div class="spec-row">
      <div class="spec"><b id="spec-version">…</b>agent version</div>
      <div class="spec"><b id="spec-chain">…</b>hash chain</div>
      <div class="spec"><b id="spec-runs">…</b>runs · 24 h</div>
      <div class="spec"><b id="spec-errors">…</b>errors · 24 h</div>
      <div class="spec"><b id="spec-running">…</b>running now</div>
    </div>
  </div>

  <section id="status">
    <h2><span class="n">01</span>Status</h2>
    <div id="status-body"><div class="note">…</div></div>
  </section>

  <section id="runs">
    <h2><span class="n">02</span>Runs</h2>
    <div class="basis" id="runs-basis"></div>
    <div id="runs-body"><div class="note">…</div></div>
  </section>

  <section id="approvals">
    <h2><span class="n">03</span>Approvals</h2>
    <div class="basis" id="approvals-basis"></div>
    <div id="approvals-body"><div class="note">…</div></div>
  </section>

  <section id="schedules">
    <h2><span class="n">04</span>Schedules &amp; blueprints</h2>
    <div id="schedules-body"><div class="note">…</div></div>
  </section>

  <section id="events">
    <h2><span class="n">05</span>Event log</h2>
    <div class="basis">the last entries — capped, redacted, run ids shortened</div>
    <div id="events-body"><div class="note">…</div></div>
  </section>

</div>
</main>

<footer>
  <div class="wrap">
    <span>TALOS dashboard — observing only</span>
    <span class="sp"></span>
    <span>GET only · POST/PUT/DELETE → 405 · unknown → 404 · no approve endpoint, by design</span>
  </div>
</footer>

<script>
// Jeder Wert auf dieser Seite kommt aus den /api-Routen und wird ausschliesslich
// per textContent gesetzt — Daten beruehren nie innerHTML. Die Routen stehen
// ausgeschrieben hier, nicht zusammengebaut: was die Seite abfragt, ist sichtbar.
"use strict";

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function $(id) { return document.getElementById(id); }
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function fmtAge(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return Math.round(s) + " s";
  if (s < 3600) return Math.round(s / 60) + " m";
  if (s < 86400) return (s / 3600).toFixed(1) + " h";
  return (s / 86400).toFixed(1) + " d";
}
function fmtInterval(seconds) {
  const s = Number(seconds) || 0;
  if (s >= 86400 && s % 86400 === 0) return "every " + (s / 86400) + " d";
  if (s >= 3600 && s % 3600 === 0) return "every " + (s / 3600) + " h";
  if (s >= 60 && s % 60 === 0) return "every " + (s / 60) + " m";
  return "every " + s + " s";
}
function statusPill(status) {
  const s = String(status || "").toLowerCase();
  let cls = "pill dim";
  if (["answered", "done", "ok", "completed", "success"].includes(s)) cls = "pill good";
  else if (["failed", "error", "critical", "broken"].includes(s)) cls = "pill bad";
  else if (["needs_human", "step_limit", "denied", "refused"].includes(s)) cls = "pill warn";
  return el("span", cls, status || "—");
}
function noteBox(kind, label, text) {
  const box = el("div", "note" + (kind ? " " + kind : ""));
  if (label) box.appendChild(el("b", "", label));
  box.appendChild(document.createTextNode(text));
  return box;
}
function unavailable(body, data) {
  clear(body);
  body.appendChild(noteBox("warn", "unavailable", String((data && data.reason) || "source missing")));
}
function unreachable(body) {
  clear(body);
  body.appendChild(noteBox("warn", "unreachable", "the route did not answer — agent down or proxy flapping"));
}

// --- 01 status -------------------------------------------------------------
function renderStatus(data) {
  if (data.version) { $("rev").textContent = "REV " + String(data.version).toUpperCase(); }
  if (data.version) { $("spec-version").textContent = data.version; }
  const chain = data.chain || {};
  const chainOk = chain.chain_ok === true;
  const sc = $("spec-chain");
  sc.textContent = chain.chain_ok === undefined ? "—" : (chainOk ? "OK" : "BROKEN");
  sc.className = chainOk ? "good" : "bad";
  const log = data.event_log || {};
  $("spec-runs").textContent = log.runs_24h !== undefined ? log.runs_24h : "—";
  const se = $("spec-errors");
  se.textContent = log.errors_24h !== undefined ? log.errors_24h : "—";
  se.className = (log.errors_24h || 0) > 0 ? "bad" : "good";

  const body = $("status-body");
  clear(body);
  const kv = el("div", "kv");
  function row(k, v) { kv.appendChild(el("div", "", k)); kv.appendChild(el("div", "", v)); }
  const health = el("span");
  health.appendChild(statusPill(data.status || "?"));
  kv.appendChild(el("div", "", "health"));
  const healthCell = el("div");
  healthCell.appendChild(health);
  kv.appendChild(healthCell);
  row("measured at", (data.ts !== undefined ? new Date(Number(data.ts) * 1000).toLocaleString() : "—"));
  row("events total", log.total !== undefined ? log.total : "—");
  row("last event", log.last_ts ? new Date(Number(log.last_ts) * 1000).toLocaleString() : "—");
  row("chain", (chain.chained !== undefined ? chain.chained + " chained / " + chain.total + " total" : "—"));
  if (!chainOk && chain.chain_broken_id) row("chain broken at", "event id " + chain.chain_broken_id);
  const anchor = data.anchor || {};
  if (anchor.count !== undefined) row("anchor", anchor.count + " entries · verify " + (anchor.verify_ok ? "ok" : "FAILED"));
  const sched = data.schedules || {};
  if (sched.count !== undefined) row("schedules", sched.count);
  body.appendChild(kv);
}

// --- 02 runs ---------------------------------------------------------------
function renderRuns(data) {
  $("spec-running").textContent = (data.running || []).length;
  const basis = $("runs-basis");
  basis.textContent = data.basis || "";
  const body = $("runs-body");
  clear(body);
  const running = data.running || [];
  const done = data.completed || [];
  if (running.length) {
    body.appendChild(el("h3", "", "running"));
    const grid = el("div", "cards");
    for (const r of running) {
      const card = el("div", "card");
      card.appendChild(el("b", "", String(r.run_id || "").slice(0, 8)));
      card.appendChild(el("span", "", "for " + fmtAge(r.age_s) + " · " + (r.tool_calls || 0) + " tool calls · since " + (r.started || "?")));
      grid.appendChild(card);
    }
    body.appendChild(grid);
  } else {
    body.appendChild(noteBox("good", "quiet", "nothing running right now"));
  }
  if (done.length) {
    body.appendChild(el("h3", "", "completed"));
    const tw = el("div", "tw");
    const table = el("table");
    const head = el("tr");
    for (const h of ["run", "status", "duration", "tools", "finished"]) head.appendChild(el("th", "", h));
    const thead = el("thead"); thead.appendChild(head); table.appendChild(thead);
    const tbody = el("tbody");
    for (const r of done) {
      const tr = el("tr");
      tr.appendChild(el("td", "d", String(r.run_id || "").slice(0, 8)));
      const st = el("td"); st.appendChild(statusPill(r.status)); tr.appendChild(st);
      tr.appendChild(el("td", "d", fmtAge(r.duration_s)));
      tr.appendChild(el("td", "d", r.tool_calls !== undefined ? r.tool_calls : "—"));
      tr.appendChild(el("td", "d", r.done || ""));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody); tw.appendChild(table); body.appendChild(tw);
  }
}

// --- 03 approvals ----------------------------------------------------------
function renderApprovals(data) {
  const basis = $("approvals-basis");
  basis.textContent = (data.open && data.open.basis) || "";
  const body = $("approvals-body");
  clear(body);
  const open = (data.open && data.open.items) || [];
  const standing = data.standing || [];
  if (open.length) {
    body.appendChild(el("h3", "", "waiting for a human"));
    const grid = el("div", "cards");
    for (const item of open) {
      const card = el("div", "card warn");
      card.appendChild(el("b", "", item.tool || "?"));
      const targets = Array.isArray(item.targets) ? item.targets.join(", ") : String(item.targets || "");
      card.appendChild(el("span", "", targets || "(no target)"));
      card.appendChild(el("br"));
      card.appendChild(el("span", "", "waiting " + fmtAge(item.age_s) + " · since " + (item.since || "?")));
      grid.appendChild(card);
    }
    body.appendChild(grid);
    body.appendChild(noteBox("", "by design", "approve or deny in the operator's chat — this page watches, it cannot answer."));
  } else {
    body.appendChild(noteBox("good", "nothing pending", "no approval is waiting for a human"));
  }
  if (standing.length) {
    body.appendChild(el("h3", "", "standing rules"));
    const tw = el("div", "tw");
    const table = el("table");
    const head = el("tr");
    for (const h of ["tool", "rule", "conversation", "granted"]) head.appendChild(el("th", "", h));
    const thead = el("thead"); thead.appendChild(head); table.appendChild(thead);
    const tbody = el("tbody");
    for (const r of standing) {
      const tr = el("tr");
      tr.appendChild(el("td", "", r.tool || ""));
      tr.appendChild(el("td", "mono", r.label || ""));
      tr.appendChild(el("td", "d", r.conversation || ""));
      tr.appendChild(el("td", "d", r.created || ""));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody); tw.appendChild(table); body.appendChild(tw);
  }
}

// --- 04 schedules & blueprints ----------------------------------------------
function renderSchedules(data) {
  const body = $("schedules-body");
  clear(body);
  const names = Object.keys(data.blueprints || {});
  if (names.length) {
    body.appendChild(el("h3", "", "blueprints"));
    const chips = el("div", "chips");
    for (const name of names.sort()) {
      const bp = data.blueprints[name];
      const chip = el("span", "chip");
      chip.appendChild(el("i", bp.enabled ? "on" : "off", bp.enabled ? "● " : "○ "));
      chip.appendChild(el("span", "t", name));
      if (bp.next_run) chip.appendChild(el("span", "", " · next " + bp.next_run));
      chips.appendChild(chip);
    }
    body.appendChild(chips);
  }
  const upcoming = data.upcoming || [];
  if (upcoming.length) {
    body.appendChild(el("h3", "", "next due (" + (data.count !== undefined ? data.count : upcoming.length) + " total)"));
    const tw = el("div", "tw");
    const table = el("table");
    const head = el("tr");
    for (const h of ["task", "next run", "rhythm", "once"]) head.appendChild(el("th", "", h));
    const thead = el("thead"); thead.appendChild(head); table.appendChild(thead);
    const tbody = el("tbody");
    for (const s of upcoming) {
      const tr = el("tr");
      tr.appendChild(el("td", "d", String(s.task_id || "").slice(0, 8)));
      tr.appendChild(el("td", "", s.next_run || ""));
      tr.appendChild(el("td", "d", s.cron ? "cron " + s.cron : fmtInterval(s.interval_s)));
      tr.appendChild(el("td", "d", s.once ? "once" : ""));
      tbody.appendChild(tr);
    }
    table.appendChild(tbody); tw.appendChild(table); body.appendChild(tw);
  } else if (!names.length) {
    body.appendChild(noteBox("", "empty", "no schedules, no blueprints installed"));
  }
}

// --- 05 events ---------------------------------------------------------------
function renderEvents(data) {
  const body = $("events-body");
  clear(body);
  const events = data.events || [];
  if (!events.length) {
    body.appendChild(noteBox("", "empty", "no events"));
    return;
  }
  const tw = el("div", "tw");
  const table = el("table");
  const head = el("tr");
  for (const h of ["time", "type", "actor", "run", "payload"]) head.appendChild(el("th", "", h));
  const thead = el("thead"); thead.appendChild(head); table.appendChild(thead);
  const tbody = el("tbody");
  for (const e of events) {
    const tr = el("tr");
    tr.appendChild(el("td", "d", String(e.time || "").slice(11)));
    tr.appendChild(el("td", "", e.type || ""));
    tr.appendChild(el("td", "d", e.actor || ""));
    tr.appendChild(el("td", "d", e.run_id || ""));
    const payload = e.payload || {};
    const parts = [];
    for (const k of Object.keys(payload)) {
      const v = payload[k];
      parts.push(k + "=" + (typeof v === "object" ? JSON.stringify(v) : String(v)));
    }
    let line = parts.join(" · ");
    if (line.length > 160) line = line.slice(0, 157) + "…";
    tr.appendChild(el("td", "mono", line));
    tbody.appendChild(tr);
  }
  table.appendChild(tbody); tw.appendChild(table); body.appendChild(tw);
}

// --- poll loop ---------------------------------------------------------------
async function poll() {
  const jobs = [
    ["/api/status", $("status-body"), renderStatus],
    ["/api/runs", $("runs-body"), renderRuns],
    ["/api/approvals", $("approvals-body"), renderApprovals],
    ["/api/schedules", $("schedules-body"), renderSchedules],
    ["/api/events", $("events-body"), renderEvents],
  ];
  for (const [route, body, render] of jobs) {
    try {
      const answer = await fetch(route);
      const data = await answer.json();
      if (data && data.available === false) { unavailable(body, data); continue; }
      render(data);
    } catch (error) {
      unreachable(body);
    }
  }
  $("stamp").textContent = "refreshed " + new Date().toLocaleTimeString() + " · every 5 s";
}
poll();
setInterval(poll, 5000);
</script>
</body>
</html>
"""

PAGE = (
    _PAGE_TEMPLATE
    .replace("__FONT_VT323__", _asset(FONT_VT323_WOFF2_B64))
    .replace("__FONT_MONO__", _asset(FONT_SOMETYPE_MONO_WOFF2_B64))
    .replace("__SIGIL__", _asset(SIGIL_PNG_B64))
)


# --- HTTP: feste Routen, kein Input in der Ausfuehrung ----------------------------------------

class _Server(http.server.HTTPServer):
    def __init__(self, addr, handler, collector: Collector) -> None:
        super().__init__(addr, handler)
        self.collector = collector


class Handler(http.server.BaseHTTPRequestHandler):
    """Nur GET, nur feste Routen. Es gibt nichts zu injizieren: keine Pfade aus der
    Anfrage, kein Query-String, der Verhalten aendert, kein Request-Body."""

    server_version = f"talos-dashboard/{__version__}"
    ROUTES = {
        "/api/status": Collector.status,
        "/api/runs": Collector.runs,
        "/api/approvals": Collector.approvals,
        "/api/events": Collector.events,
        "/api/schedules": Collector.schedules,
    }

    def do_GET(self) -> None:  # noqa: N802
        pfad = self.path.split("?")[0].split("#")[0]
        if pfad == "/":
            self._send(200, "text/html; charset=utf-8", PAGE)
            return
        bauer = self.ROUTES.get(pfad)
        if bauer is None:
            self._send_json(404, {"error": "unknown route"})
            return
        try:
            daten = bauer(self.server.collector)
        except Exception as fehler:  # nie ein Traceback nach aussen
            self._send_json(500, {"error": f"section failed ({type(fehler).__name__})"})
            return
        self._send_json(200, daten)

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"error": "observing only — this endpoint changes nothing"})

    do_POST = _method_not_allowed  # noqa: N815
    do_PUT = _method_not_allowed  # noqa: N815
    do_DELETE = _method_not_allowed  # noqa: N815

    def _send_json(self, code: int, daten: dict) -> None:
        body = json.dumps(daten, ensure_ascii=False, sort_keys=True)
        if len(body.encode("utf-8")) > MAX_BODY:
            code, body = 500, '{"error":"response exceeded the size cap"}'
        self._send(code, "application/json; charset=utf-8", body)

    def _send(self, code: int, typ: str, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Still. Zugriffe stehen im Proxy-Log, doppelt waere nur Rauschen."""


def make_server(
    *,
    eventlog_db: Path = EVENTLOG_DB,
    schedule_db: Path = SCHEDULE_DB,
    blueprint_state: Path = DATA_DIR / "blueprints.json",
    anchors_path: Path | None = None,
    port: int = PORT,
    bind: str = BIND,
) -> _Server:
    """Der Server, bereit zum `serve_forever()`. Port 0 ist erlaubt (Tests)."""
    collector = Collector(
        eventlog_db=Path(eventlog_db),
        schedule_db=Path(schedule_db),
        blueprint_state=Path(blueprint_state),
        anchors_path=anchors_path,
    )
    return _Server((bind, int(port)), Handler, collector)


USAGE = (
    f"  usage: talos dashboard\n"
    f"  live view: running runs, open approvals, event log, schedules —\n"
    f"  observing, not intervening. Listens on {BIND}:{PORT} (loopback only);\n"
    f"  put a tailnet proxy in front (`tailscale serve`) to watch from away.\n"
    f"  Approvals stay in the operator's chat — there is no endpoint for them.\n"
)


def run_dashboard(argv: list[str] | None = None, *, out=None) -> int:
    """`talos dashboard` — blockiert im Vordergrund, Ctrl-C beendet.

    Laeuft VOR `load_config()` wie jeder Unterbefehl: die Pfade kommen aus den
    Konstanten in `config.py`, fehlende Datenbanken sind benannte Leere, kein
    Abbruch — ein Dashboard, das nur bei laufender Installation startet, zeigt
    gerade dann nichts, wenn man es braucht.
    """
    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben(USAGE)
        return 0
    if argumente:
        schreiben(f"  unknown option: {argumente[0]} — usage: talos dashboard\n")
        return 2
    server = make_server()
    schreiben(f"  talos {__version__} dashboard — observing only, "
              f"http://{BIND}:{PORT} (Ctrl-C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        schreiben("\n")
    finally:
        server.server_close()
    return 0


__all__ = [
    "BIND", "PORT", "MAX_BODY", "Collector", "Handler", "SourceError",
    "make_server", "run_dashboard",
]
