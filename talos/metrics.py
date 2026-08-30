"""Metriken — Latenz und Erfolgsquoten aus dem Protokoll, nicht aus der Erinnerung.

Der Anlass steht in der Checkliste der Agenten-Infrastruktur: wer Engpaesse sucht,
braucht Time-to-First-Token und die Erfolgsquote der Werkzeugaufrufe. Dieses Modul
rechnet sie — aus dem Event-Log, nie aus einer Zustandsdatei, die daneben driften
koennte (die outcome.py-Doktrin: das Protokoll schreibt der Executor, es ist die
einzige Quelle, die das Modell nicht umdeuten kann).

Drei Reihen, mehr behauptet das Modul nicht:

1. **Reasoner-Zuege** (`reason.started` -> `reason.done`): wie lange denkt ein Zug.
2. **TTFT** (`reason.started` -> `reason.first_token`): wie schnell der erste
   sichtbare Token kommt. Fehlt das Ereignis (aeltere Laeufe, nicht gestreamte
   Antworten), wird ehrlich gezaehlt, wie viele Zuege KEINE Messung haben —
   eine erfundene Zahl waere schlimmer als eine fehlende.
3. **Werkzeuge** (`exec.result`): Aufrufe und Erfolgsquote je Werkzeug.
   `done` zaehlt als Erfolg, alles andere mit seinem Statusnamen — ein DENY ist
   kein Werkzeugfehler, aber er gehoert in die Bilanz, weil eine Quote ohne ihn
   schoener laege als die Wirklichkeit.

Fail-open wie jede Quittung hier: ein unlesbares Log ergibt einen leeren Bericht,
nie einen Fehler. Gerechnet wird mit exakten Zeitstempeln aus dem Log; gerundet
wird erst beim Schreiben.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Ein Zug ohne Ende (Absturz mitten im Lauf) verfaelscht keine Dauer: er fehlt
# einfach — und fehlt damit ehrlich, statt eine halbe Messung zu sein.
def _paare(entries: list[dict], start: str, ende: str) -> list[float]:
    """Dauern zwischen zwei Ereignisarten, pro run_id in Log-Reihenfolge gepaart."""
    offen: dict[str, float] = {}
    dauern: list[float] = []
    for entry in entries:
        run_id = entry.get("run_id", "")
        typ = entry.get("type")
        ts = float(entry.get("ts", 0) or 0)
        if typ == start:
            offen[run_id] = ts
        elif typ == ende and run_id in offen:
            dauern.append(max(0.0, ts - offen.pop(run_id)))
    return dauern


def _ttft(entries: list[dict]) -> tuple[list[float], int]:
    """TTFT pro Lauf: started -> first_token. Rueckgabe: (dauern, ohne_messung)."""
    gestartet: dict[str, float] = {}
    gemessen: set[str] = set()
    dauern: list[float] = []
    zuege: set[str] = set()
    for entry in entries:
        run_id = entry.get("run_id", "")
        typ = entry.get("type")
        ts = float(entry.get("ts", 0) or 0)
        if typ == "reason.started":
            gestartet[run_id] = ts
            zuege.add(run_id)
        elif typ == "reason.first_token" and run_id in gestartet and run_id not in gemessen:
            gemessen.add(run_id)
            dauern.append(max(0.0, ts - gestartet[run_id]))
    return dauern, len(zuege - gemessen)


def _quantile(werte: list[float], q: float) -> float:
    if not werte:
        return 0.0
    geordnet = sorted(werte)
    index = min(len(geordnet) - 1, max(0, round(q * (len(geordnet) - 1))))
    return geordnet[index]


@dataclass(frozen=True)
class _Reihe:
    n: int
    avg: float
    p50: float
    p95: float


def _reihe(werte: list[float]) -> _Reihe:
    if not werte:
        return _Reihe(0, 0.0, 0.0, 0.0)
    return _Reihe(
        len(werte),
        sum(werte) / len(werte),
        _quantile(werte, 0.5),
        _quantile(werte, 0.95),
    )


@dataclass(frozen=True)
class Metrics:
    """Die drei Reihen. Unveraenderlich — gerechnet, nicht gepflegt."""

    reasoner: _Reihe
    ttft: _Reihe
    ttft_ohne_messung: int
    werkzeuge: tuple[tuple[str, int, int], ...] = field(default_factory=tuple)
    # (name, aufrufe, davon done) — sortiert nach Aufrufzahl.
    fenster_s: float = 0.0


def collect(entries: list[dict], *, fenster_s: float = 0.0) -> Metrics:
    """Aus Log-Eintraegen (dicts, wie `EventLog.recent` sie liefert) rechnen."""
    werkzeuge: dict[str, list[int]] = {}
    for entry in entries:
        if entry.get("type") != "exec.result":
            continue
        payload = entry.get("payload") or {}
        name = str(payload.get("tool") or "?")
        zaehler = werkzeuge.setdefault(name, [0, 0])
        zaehler[0] += 1
        if payload.get("status") == "done":
            zaehler[1] += 1
    ttft_werte, ohne = _ttft(entries)
    return Metrics(
        reasoner=_reihe(_paare(entries, "reason.started", "reason.done")),
        ttft=_reihe(ttft_werte),
        ttft_ohne_messung=ohne,
        werkzeuge=tuple(
            sorted(((n, z[0], z[1]) for n, z in werkzeuge.items()),
                   key=lambda z: (-z[1], z[0]))
        ),
        fenster_s=fenster_s,
    )


def render(stats: Metrics) -> str:
    """Die Konsolenform: drei Reihen plus Werkzeugtabelle, ehrlich bei Leere."""
    if stats.reasoner.n == 0 and not stats.werkzeuge:
        return "no events in this window — nothing to measure"
    zeilen = ["metrics from the event log" + (
        f" (window {stats.fenster_s / 3600:.0f}h)" if stats.fenster_s else ""
    )]
    r = stats.reasoner
    zeilen.append(
        f"reasoner: {r.n} turns · avg {r.avg:.1f}s · p50 {r.p50:.1f}s · p95 {r.p95:.1f}s"
    )
    t = stats.ttft
    fussnote = (
        f" ({stats.ttft_ohne_messung} turns without a stream — no measurement)"
        if stats.ttft_ohne_messung else ""
    )
    zeilen.append(
        f"ttft:     {t.n} streams · avg {t.avg:.1f}s · p50 {t.p50:.1f}s · p95 {t.p95:.1f}s{fussnote}"
    )
    gesamt = sum(z[1] for z in stats.werkzeuge)
    ok = sum(z[2] for z in stats.werkzeuge)
    if gesamt:
        zeilen.append(f"tools:    {gesamt} calls · {100 * ok / gesamt:.0f}% ok")
        breite = max(len(z[0]) for z in stats.werkzeuge)
        for name, aufrufe, erfolge in stats.werkzeuge:
            quote = 100 * erfolge / aufrufe if aufrufe else 0.0
            zeilen.append(f"  {name:<{breite}}  {aufrufe:>4} · {quote:.0f}% ok")
    return "\n".join(zeilen)


def run_metrics(argv: list[str] | None = None, *, out=None, db=None) -> int:
    """`talos metrics [--since 24h]` — Latenz und Erfolgsquoten aus dem Protokoll."""
    from .config import EVENTLOG_DB
    from .eventlog import EventLog
    from .eventscli import _dauer

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben("  usage: talos metrics [--since 24h]\n")
        return 0
    seit = ""
    if "--since" in argumente and argumente.index("--since") + 1 < len(argumente):
        seit = argumente[argumente.index("--since") + 1]
    fenster = _dauer(seit) if seit else None
    if seit and fenster is None:
        schreiben(f"  --since wants a duration like 30m, 4h or 2d — got {seit!r}\n")
        return 2

    pfad = Path(db) if db is not None else Path(EVENTLOG_DB)
    try:
        log = EventLog(pfad)
        try:
            # grosszuegig gelesen, exakt gefiltert: das Fenster schneidet die
            # Zeitstempel, nicht die Zeilenanzahl.
            roh = log.recent(10_000)
        finally:
            log.close()
    except Exception:
        roh = []
    if fenster is not None:
        grenze = time.time() - fenster
        roh = [e for e in roh if float(e.get("ts", 0) or 0) >= grenze]
    schreiben(render(collect(roh, fenster_s=fenster or 0.0)) + "\n")
    return 0
