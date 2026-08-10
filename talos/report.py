"""`talos report` — der Beleg, den man jemand anderem vorlegen kann.

Das Ereignisprotokoll enthaelt die Wahrheit ueber jeden Lauf, aber in einer Form, die nur
liest, wer dieses Projekt kennt: 2218 Zeilen JSONL mit `exec.intent` und `grant.issued`.
Ein Auditor, ein Vorgesetzter, ein Versicherer liest das nicht — und ein Datenbestand, den
niemand lesen kann, ist kein Beweis, sondern eine Behauptung mit mehr Zeichen.

Deshalb dieses Modul. Es erfindet nichts hinzu; es ordnet, was ohnehin dasteht, und
uebersetzt es in Saetze, die ohne Kenntnis von Talos verstaendlich sind.

**Der wertvollste Teil ist das Abgelehnte.** Was ein Agent getan hat, behauptet jedes
Werkzeug. Was er NICHT tun durfte und trotzdem vorgeschlagen hat, steht nur dort, wo ein
Kernel vor der Wirkung urteilt — und genau diese Differenz ist der Grund, warum jemand
diesem Agenten eine Shell in die Hand gibt. Im Protokoll ist sie sichtbar als der Abstand
zwischen `exec.intent` (vorgeschlagen) und `grant.issued` (erlaubt).

⚠️ **Was der Bericht beweist und was nicht** — das steht auch IM Bericht, nicht nur hier.
Wer ein Beweisstueck ausstellt, muss dessen Grenzen mitliefern, sonst ist es Werbung:

* Jeder Eintrag wurde VOR der Wirkung geschrieben, nicht danach (`executor` schreibt
  `exec.intent`, bevor er den Runner ruft). Ein Lauf, der mitten in der Wirkung stirbt,
  hinterlaesst darum eine Absicht ohne Ergebnis — sichtbar, statt spurlos.
* Der Fingerabdruck bindet **diesen Auszug**: er laeuft als Kette ueber genau die
  Ereignisse, die im Bericht stehen, in ihrer Reihenfolge. Wer danach eine Zeile aendert,
  entfernt oder umsortiert, bekommt beim naechsten Auszug einen anderen Fingerabdruck.
* Er beweist **nicht**, dass niemand VOR dem ersten Auszug am Protokoll war. Dafuer
  braeuchte es eine Kette in der Datenbank selbst, und die gibt es hier nicht. Das ist
  eine bekannte Luecke und keine verschwiegene — sie steht im Bericht.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .vault import redact_secrets

__all__ = ["Entry", "collect", "digest", "render", "run_report"]

# Wieviel von einer Nachricht oder einem Kommando im Bericht steht. Genug, um zu erkennen,
# worum es ging — nicht genug, um den Bericht zum zweiten Datenbestand zu machen.
SNIPPET_CHARS = 160
DEFAULT_RUNS = 20

# Uebersetzung der Ereignistypen in Saetze fuer jemanden, der Talos nicht kennt.
# Was hier fehlt, taucht im Bericht mit seinem rohen Namen auf — lieber ein technischer
# Name zu viel als eine Zeile, die stillschweigend verschwindet.
LABELS: dict[str, str] = {
    "task.received": "asked",
    "task.rejected": "refused at the door",
    "reason.started": "thinking",
    "reason.done": "thought",
    "exec.intent": "proposed",
    "grant.issued": "authorised",
    "exec.result": "carried out",
    "approval.parked": "held for a human",
    "approval.granted": "approved by a human",
    "approval.denied": "declined by a human",
    "approval.standing": "standing rule created",
    "approval.standing_used": "standing rule applied",
    "approval.standing_revoked": "standing rule revoked",
    "approval.stale": "approval expired unused",
    "snapshot.taken": "snapshot taken before the write",
    "control.rejected": "control command refused",
    "schedule.refused": "scheduled run refused",
    "model.selected": "model changed",
    "autonomy.set": "autonomy level changed",
    "reply.sent": "answered",
    "done": "finished",
}

# Ereignisse des Transports, nicht des Agenten. Sie gehoeren in den Bericht — ein Auszug,
# der Stoerungen verschweigt, ist geschoent — aber gebuendelt: 1034 identische
# Telegram-Konflikte im Protokoll haetten jeden Beleg unlesbar gemacht, und
# unlesbar ist fuer ein Beweisstueck dasselbe wie nicht vorhanden.
NOISE_TYPES = frozenset({"channel.error"})


@dataclass(frozen=True)
class Entry:
    """Eine Zeile des Protokolls, so wie der Bericht sie braucht."""

    rowid: int
    ts: float
    run_id: str
    actor: str
    type: str
    payload: dict


def _rows(path: Path, runs: int, run_id: str) -> list[Entry]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if run_id:
            cursor = conn.execute(
                "SELECT id, ts, run_id, actor, type, payload_json FROM events "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        else:
            # Die letzten N Laeufe, aber vollstaendig: ein halber Lauf im Bericht waere
            # genau die Sorte Auszug, die mehr verschweigt als zeigt.
            gewaehlt = [r[0] for r in conn.execute(
                "SELECT run_id FROM events GROUP BY run_id ORDER BY MAX(id) DESC LIMIT ?",
                (runs,),
            )]
            if not gewaehlt:
                return []
            platzhalter = ",".join("?" * len(gewaehlt))
            cursor = conn.execute(
                "SELECT id, ts, run_id, actor, type, payload_json FROM events "
                f"WHERE run_id IN ({platzhalter}) ORDER BY id",
                gewaehlt,
            )
        gesammelt = []
        for rowid, ts, rid, actor, typ, roh in cursor:
            try:
                nutzlast = json.loads(roh)
            except (TypeError, ValueError):
                nutzlast = {"unparsable": str(roh)[:200]}
            gesammelt.append(Entry(rowid, float(ts), str(rid), str(actor), str(typ),
                                   nutzlast if isinstance(nutzlast, dict) else {"value": nutzlast}))
        return gesammelt
    finally:
        conn.close()


def collect(db_path, *, runs: int = DEFAULT_RUNS, run_id: str = "") -> tuple[Entry, ...]:
    """Die Ereignisse, ueber die berichtet wird — vollstaendige Laeufe, in Reihenfolge."""
    pfad = Path(db_path)
    if not pfad.is_file():
        return ()
    return tuple(_rows(pfad, max(1, int(runs)), run_id.strip()))


def digest(entries) -> str:
    """Ein Fingerabdruck als KETTE ueber genau diese Ereignisse, in dieser Reihenfolge.

    Kette statt Summe: eine Summe ueber eine Menge bliebe gleich, wenn jemand zwei
    Ereignisse vertauscht — und die Reihenfolge ist hier die halbe Aussage („erst
    geurteilt, dann ausgefuehrt").
    """
    haken = hashlib.sha256(b"talos-report-v1")
    for e in entries:
        haken.update(
            json.dumps(
                [e.rowid, round(e.ts, 6), e.run_id, e.actor, e.type, e.payload],
                sort_keys=True, default=str, ensure_ascii=False,
            ).encode("utf-8")
        )
        haken.update(haken.hexdigest().encode("ascii"))
    return haken.hexdigest()


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _kurz(wert, laenge: int = SNIPPET_CHARS) -> str:
    text = redact_secrets(" ".join(str(wert).split()))
    return text[:laenge] + ("…" if len(text) > laenge else "")


def _ziel(payload: dict) -> str:
    """Was der Aufruf angefasst haette — Ziele zuerst, sonst die Argumente.

    Der Kernel leitet `targets` aus den echten Argumenten ab; sie sind damit die
    ehrlichste Kurzfassung dessen, worum es ging.
    """
    ziele = payload.get("targets")
    if isinstance(ziele, (list, tuple)) and ziele:
        return _kurz(", ".join(str(z) for z in ziele))
    argumente = payload.get("args")
    if isinstance(argumente, dict) and argumente:
        for schluessel in ("command", "path", "url", "query", "text"):
            if argumente.get(schluessel):
                return _kurz(argumente[schluessel])
        return _kurz(json.dumps(argumente, ensure_ascii=False, default=str))
    return ""


def _describe(entry: Entry) -> str:
    """Der Satz zu einem Ereignis — je Typ, nicht generisch.

    ⚠️ Die erste Fassung suchte in jeder Nutzlast nach `text`/`command`/`path` und fand
    bei den WICHTIGSTEN Typen nichts: `exec.intent` traegt `args`, `targets` und
    `verdict`, `task.received` traegt ueberhaupt keinen Text. Der Bericht sah dadurch
    vollstaendig aus und war an genau den Stellen leer, auf die es ankommt.
    """
    p = entry.payload
    typ = entry.type

    if typ == "task.received":
        wer = p.get("principal") or "unknown"
        wo = p.get("conversation") or ""
        # ⚠️ Der Wortlaut der Anfrage steht NICHT im Protokoll. Das ist eine
        # Entscheidung des Protokolls, keine Luecke dieses Berichts — und sie gehoert
        # hingeschrieben, statt als leere Spalte zu erscheinen.
        return f"by {wer}" + (f" in {wo}" if wo else "") + "  (wording not recorded)"
    if typ == "exec.intent":
        urteil = str(p.get("verdict") or "").upper()
        grund = p.get("reason")
        satz = f"{p.get('tool', '?')}  {_ziel(p)}".rstrip()
        if urteil:
            satz += f"   → kernel: {urteil}"
        if grund and urteil not in ("ALLOW", ""):
            satz += f" ({_kurz(grund, 70)})"
        return satz
    if typ == "grant.issued":
        vom_menschen = p.get("human_approved")
        return f"{p.get('tool', '?')}" + ("  after a human approved" if vom_menschen else "")
    if typ == "exec.result":
        status = str(p.get("status") or "").upper()
        return f"{p.get('tool', '?')}  {status}  {_kurz(p.get('detail') or '', 90)}".rstrip()
    if typ in ("approval.parked", "approval.granted", "approval.denied", "approval.stale"):
        return f"{p.get('tool', '?')}  {_ziel(p)}".rstrip()
    if typ == "model.selected":
        return f"{p.get('provider', '?')}/{p.get('model', '?')}"

    for schluessel in ("text", "reason", "detail", "error", "command", "path"):
        wert = p.get(schluessel)
        if isinstance(wert, str) and wert.strip():
            return _kurz(wert)
    return ""


def _counts(entries) -> dict[str, int]:
    zahlen: dict[str, int] = {}
    for e in entries:
        zahlen[e.type] = zahlen.get(e.type, 0) + 1
    return zahlen


def _identities(entries) -> list[str]:
    wer = set()
    for e in entries:
        for schluessel in ("principal", "identity", "who"):
            wert = e.payload.get(schluessel)
            if isinstance(wert, str) and wert.strip():
                wer.add(wert.strip())
    return sorted(wer)


PROOF = """WHAT THIS RECORD PROVES, AND WHAT IT DOES NOT
  Every line below was written BEFORE the effect it describes, not after: the executor
  records what it intends, then acts. A run that dies mid-effect therefore leaves an
  intent with no result — visible, rather than gone.
  The fingerprint is a chain over exactly the events printed here, in this order. Change,
  remove or reorder one line and the next export carries a different fingerprint.
  It does NOT prove that nobody edited the log before this export was taken. That would
  need a chain inside the database itself, and there is none. Stated here rather than
  left for someone to discover."""


def render(entries, *, agent: str = "Talos", version: str = "",
           source: str = "", now: float | None = None) -> str:
    """Der Bericht als Text — lesbar ohne jede Kenntnis dieses Projekts."""
    jetzt = time.time() if now is None else now
    if not entries:
        return (f"{agent} — record of what was done and what was refused\n\n"
                "  No events in the log for the requested range. Nothing has run, or the "
                "log was not found.\n")

    zahlen = _counts(entries)
    laeufe = sorted({e.run_id for e in entries})
    vorgeschlagen = zahlen.get("exec.intent", 0)
    erlaubt = zahlen.get("grant.issued", 0)
    gehalten = zahlen.get("approval.parked", 0)
    zeilen = [
        f"{agent} — record of what was done and what was refused",
        "=" * 78,
        f"  Version      {version or 'unknown'}",
        f"  Source       {source or 'event log'}",
        f"  Covering     {_when(entries[0].ts)} – {_when(entries[-1].ts)} UTC",
        f"  Exported     {_when(jetzt)} UTC",
        f"  Events       {len(entries)} across {len(laeufe)} run(s)",
        f"  Commanded by {', '.join(_identities(entries)) or 'not recorded'}",
        f"  Fingerprint  sha256:{digest(entries)}",
        "",
        PROOF,
        "",
        "SUMMARY",
        f"  {vorgeschlagen:5} tool call(s) proposed by the model",
        f"  {erlaubt:5} authorised by the kernel",
        f"  {gehalten:5} held for a human decision",
        # ⚠️ Die Differenz wird BERECHNET, nicht gezaehlt: es gibt kein Ereignis „abgelehnt".
        # Eine Ablehnung ist das AUSBLEIBEN einer Erlaubnis, und genau so steht es da.
        f"  {max(0, vorgeschlagen - erlaubt):5} proposed but never authorised — the kernel "
        "or a human said no",
        "",
    ]

    for lauf in sorted(laeufe, key=lambda r: min(e.rowid for e in entries if e.run_id == r)):
        gehoerig = [e for e in entries if e.run_id == lauf]
        laut = [e for e in gehoerig if e.type not in NOISE_TYPES]
        leise = len(gehoerig) - len(laut)
        zeilen.append(f"RUN {lauf}   {_when(gehoerig[0].ts)} UTC")
        for e in laut:
            beschriftung = LABELS.get(e.type, e.type)
            text = _describe(e)
            zeilen.append(f"  {_when(e.ts)[11:]}  {beschriftung:<28} {text}".rstrip())
        if leise:
            # Gezaehlt, nicht geloescht: die Zahl steht da, die tausend Zeilen nicht.
            zeilen.append(f"  {'':8}  {'transport errors':<28} {leise} suppressed "
                          "(counted, not removed — see the fingerprint)")
        zeilen.append("")
    return "\n".join(zeilen) + "\n"


def run_report(argv: list[str] | None = None, *, out=None, db=None) -> int:
    """`talos report [--run <id>] [--runs <n>] [--out <datei>]`."""
    import sys

    from .config import EVENTLOG_DB
    from .identity import agent_name

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write

    def _wert(name: str, vorgabe: str = "") -> str:
        return argumente[argumente.index(name) + 1] if name in argumente and \
            argumente.index(name) + 1 < len(argumente) else vorgabe

    if "--help" in argumente or "-h" in argumente:
        schreiben("  usage: talos report [--run <id>] [--runs <n>] [--out <file>]\n")
        return 0

    pfad = Path(db) if db is not None else Path(EVENTLOG_DB)
    eintraege = collect(pfad, runs=int(_wert("--runs", str(DEFAULT_RUNS)) or DEFAULT_RUNS),
                        run_id=_wert("--run"))
    from . import __version__

    text = render(eintraege, agent=agent_name(), version=__version__, source=str(pfad))

    ziel = _wert("--out")
    if ziel:
        # 0600 ab dem ersten Byte: der Bericht traegt Kommandos und Nachrichten des
        # Betreibers. Wer ihn weitergibt, soll das entscheiden — nicht der Dateimodus.
        import os

        fd = os.open(ziel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as datei:
            datei.write(text)
        schreiben(f"  written to {ziel} ({len(text)} bytes, mode 600)\n")
        return 0
    schreiben(text)
    return 0
