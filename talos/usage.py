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
Meldet der Reasoner KEINEN Preis (der API-Weg kennt nur Token), rechnet der Zaehler
mit den Preisen, die der Betreiber in `TALOS_MODEL_OVERRIDES` hinterlegt hat — und
merkt sich am Lauf, dass die Zahl daher stammt (`cost_source`). Ohne Preis bleibt es
bei 0: eine Null, die „unbekannt" heisst, ist ehrlicher als ein geratener Tarif.

**3. Fehlversuche zaehlen mit.** Timeout, Abbruch und Fehlstart sind Laeufe. Ein
Zaehler, der nur die geglueckten zeigt, laesst genau das verschwinden, wonach man
sucht, wenn etwas klemmt.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Callable

from . import modelinfo
from .catalog import ModelInfo

__all__ = ["Run", "Snapshot", "UsageMeter"]


@dataclass(frozen=True)
class Run:
    """Ein einzelner Denk-Lauf. `note` ist leer, wenn nichts auffiel.

    `cost_source` sagt, woher `cost_usd` stammt: leer = vom Reasoner gemeldet (oder
    kein Preis), `override` = nach Betreiber-Preisen gerechnet, `catalog` = nach
    Katalog-Preisen. Eine gerechnete Zahl darf nie wie eine gemeldete aussehen.
    """

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
    cost_source: str = ""


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
    # Der Teil von `cost_usd`, der nach Betreiber-Preisen gerechnet wurde — `/usage`
    # nennt ihn getrennt, sonst laese jemand einen selbst eingetragenen Tarif als Messung.
    cost_override_usd: float = 0.0
    last: Run | None = None

    @property
    def cache_total(self) -> int:
        return self.cache_read + self.cache_write


class UsageMeter:
    """Thread-sicher: der Worker misst, der Poll-Thread liest (`/usage`, `/debug`).

    `infos` liefert die Eckdaten eines Modells (Preise). Vorgabe ist die beim Laden
    installierte Tabelle (`modelinfo.lookup`) — so ist der Zaehler in `__main__`
    verdrahtet; Tests uebergeben ihre eigene Funktion.
    """

    def __init__(self, *, infos: Callable[[str], ModelInfo] | None = None) -> None:
        self._lock = threading.Lock()
        self._total = Snapshot()
        self._infos = infos or modelinfo.lookup

    def record(self, run: Run) -> None:
        run = self._priced(run)
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
                cost_override_usd=total.cost_override_usd
                + (run.cost_usd if run.cost_source == "override" else 0.0),
                last=run,
            )

    def _priced(self, run: Run) -> Run:
        """Ein gemeldeter Preis bleibt; nur ein fehlender wird gerechnet — und nur, wenn
        es Token UND einen belegten Preis gibt. Sonst bleibt es bei 0 = unbekannt."""
        if run.cost_usd > 0 or not (run.input_tokens or run.output_tokens) or not run.model:
            return run
        info = self._infos(run.model)
        if not info.has_prices:
            return run
        quelle = "override" if {"input_price", "output_price"} & info.overridden else "catalog"
        return replace(
            run,
            cost_usd=modelinfo.cost_usd(info, run.input_tokens, run.output_tokens),
            cost_source=quelle,
        )

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._total


def now() -> float:
    """Eigene Funktion, damit Tests die Uhr ersetzen koennen."""
    return time.time()
