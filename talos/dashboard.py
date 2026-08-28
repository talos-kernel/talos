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


# --- Die Seite: self-contained, dunkel, Maschinenraum ----------------------------------------
#
# Anmutung an site/console.html (dunkel/Terminal), aber ohne deren Marketing-Teil:
# diese Seite ist ein Instrument, keine Vitrine. Inline CSS/JS, keine externen
# Assets — der Beobachter darf nichts nachladen, was ein Dritter sehen koennte.
PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>talos dashboard — observing only</title>
<style>
  :root { color-scheme: dark; }
  body { background: #17140F; color: #d8d2c4; font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; padding: 24px; }
  h1 { font-size: 17px; color: #e8dfc8; margin: 0 0 4px; }
  h2 { font-size: 14px; color: #a89e88; border-bottom: 1px solid #35301f; padding-bottom: 4px; margin: 28px 0 8px; }
  .note { color: #8a8272; font-size: 12px; max-width: 72ch; }
  .ok { color: #7fb069; } .bad { color: #d1603d; }
  pre { background: #1e1a13; border: 1px solid #35301f; border-radius: 6px; padding: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
  #stamp { float: right; color: #8a8272; font-size: 12px; }
</style>
</head>
<body>
<span id="stamp"></span>
<h1>talos dashboard</h1>
<div class="note">Observing, not intervening. Read-only views of the event log, the
schedule store and the installed blueprints. Approvals stay in the operator's chat —
there is no button here, by design. Bind: loopback only; reach comes from a
tailnet proxy in front, never from this process.</div>

<h2>status</h2><pre id="status">…</pre>
<h2>runs</h2><pre id="runs">…</pre>
<h2>approvals</h2><pre id="approvals">…</pre>
<h2>schedules &amp; blueprints</h2><pre id="schedules">…</pre>
<h2>events</h2><pre id="events">…</pre>

<script>
// Die rohen Sektionen sind die Wahrheit — formatiert wird nur, was nichts verfaelscht:
// JSON mit Einzug, keine eigene Deutung daneben. Die Routen stehen ausgeschrieben
// hier, nicht zusammengebaut: was die Seite abfragt, soll im Quelltext zu sehen sein.
const SECTIONS = {
  status: "/api/status",
  runs: "/api/runs",
  approvals: "/api/approvals",
  schedules: "/api/schedules",
  events: "/api/events",
};
async function poll() {
  for (const [name, route] of Object.entries(SECTIONS)) {
    try {
      const answer = await fetch(route);
      const data = await answer.json();
      document.getElementById(name).textContent = JSON.stringify(data, null, 2);
    } catch (error) {
      document.getElementById(name).textContent = "(unreachable)";
    }
  }
  document.getElementById("stamp").textContent =
    "refreshed " + new Date().toLocaleTimeString() + " · every 5 s";
}
poll();
setInterval(poll, 5000);
</script>
</body>
</html>
"""


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
