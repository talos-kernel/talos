"""Verbrauchszaehler fuer die Denk-Laeufe — gemessen, nicht geschaetzt.

`/usage` zeigt bei vergleichbaren Agenten Token und Kosten. Dieselbe Anzeige waere hier eine Erfindung
gewesen: Talos rief die claude-CLI im Klartext-Modus auf und bekam nur die Antwort
zurueck — kein Modellname, keine Token, keine Dauer. Ein `/usage`, das daraus Zahlen
macht, zeigt Zahlen ueber sich selbst, nicht ueber den Lauf.

Deshalb zuerst die Quelle: der Reasoner laeuft jetzt mit `--output-format json` und
liest ab, was die CLI ohnehin meldet (`usage`, `modelUsage`, `duration_ms`,
`num_turns`). Der Zaehler hier summiert nur noch.

Drei Entscheidungen:

**1. Nur Summen und der letzte Lauf.** Kein Verlauf. Ein Zaehler, der jeden Lauf
aufhebt, waechst unbegrenzt und speichert nebenbei, wann the operator was gefragt hat — das
Gedaechtnis hat aus demselben Grund eine Obergrenze.

**2. Kosten sind rechnerisch, nicht abgerechnet.** Die CLI laeuft ueber Abo/OAuth.
`total_cost_usd` ist der Listenpreis derselben Anfrage ueber die API. Wer das als
Rechnung liest, taeuscht sich um Groessenordnungen — `/usage` sagt es deshalb dazu.

**3. Fehlversuche zaehlen mit.** Timeout, Abbruch und Fehlstart sind Laeufe. Ein
Zaehler, der nur die geglueckten zeigt, laesst genau das verschwinden, wonach man
sucht, wenn etwas klemmt.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace

__all__ = ["Run", "Snapshot", "UsageMeter"]


@dataclass(frozen=True)
class Run:
    """Ein einzelner Denk-Lauf. `note` ist leer, wenn nichts auffiel."""

    at: float
    ok: bool
    duration_s: float
    model: str = ""
    models: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str = ""
    note: str = ""


@dataclass(frozen=True)
class Snapshot:
    runs: int = 0
    failed: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0
    last: Run | None = None

    @property
    def cache_total(self) -> int:
        return self.cache_read + self.cache_write


class UsageMeter:
    """Thread-sicher: der Worker misst, der Poll-Thread liest (`/usage`, `/debug`)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = Snapshot()

    def record(self, run: Run) -> None:
        with self._lock:
            total = self._total
            self._total = replace(
                total,
                runs=total.runs + 1,
                failed=total.failed + (0 if run.ok else 1),
                seconds=total.seconds + max(0.0, run.duration_s),
                input_tokens=total.input_tokens + run.input_tokens,
                output_tokens=total.output_tokens + run.output_tokens,
                cache_read=total.cache_read + run.cache_read,
                cache_write=total.cache_write + run.cache_write,
                cost_usd=total.cost_usd + run.cost_usd,
                last=run,
            )

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._total


def now() -> float:
    """Eigene Funktion, damit Tests die Uhr ersetzen koennen."""
    return time.time()
