"""Was der Betreiber ueber ein Modell weiss — und der Katalog nicht.

`catalog.py` sagt, wie man einen Anbieter *erreicht*. Was ein Modell kostet, wie gross
sein Fenster ist und ob es Bilder versteht, sagt er absichtlich nicht: die Zahlen
wechseln, und eine falsche saehe aus wie Wissen. Hermes loest das mit
`model_overrides` in der Konfiguration; hier heisst es `TALOS_MODEL_OVERRIDES` — EIN
JSON-Objekt auf EINER Zeile, Schluessel = Modell-ID, Wert = die Felder, die gelten sollen:

    TALOS_MODEL_OVERRIDES={"claude-opus-5": {"context_window": 200000, "input_price": 15, "output_price": 75, "vision": true}}

Preise gelten je Million Token in USD, das Fenster in Token. Vier Entscheidungen
tragen das Modul:

⚠️ **Ein Override erfindet nie ein Modell und aendert nie den Weg dorthin.** Die
Feldliste ist geschlossen (`catalog.MODEL_INFO_FIELDS`): Kontextfenster, zwei Preise,
zwei Faehigkeiten. Anbieter, Adresse und Schluesselname sind KEINE Felder — ein
Override, der sie tragen koennte, waere ein zweiter Weg zu Rechten, am Katalog und an
`credentials.py` vorbei. Und ein Name, den kein Katalog und keine Konfiguration kennt,
faellt beim Start heraus (`reconcile`): nachgeschlagen wird nur, was laeuft.

⚠️ **Kaputtes JSON ist ein Startfehler, kein Achselzucken.** Betreiber-Konfiguration,
die still ignoriert wird, ist die schlimmste Variante: er glaubt, seine Preise gelten,
und `/usage` rechnet mit nichts. Der Fehler nennt die Variable, nie ihren Wert — der
landet sonst in Logs und Tickets.

⚠️ **Ein falsches Feld kostet das Feld, nicht den Eintrag.** Tippfehler, falscher Typ,
negative Zahl: das eine Feld faellt weg, mit Begruendung, der Rest gilt. Jeder Befund
steht in `Overrides.dropped`, wird beim Start ins Event-Log geschrieben
(`provider.resolve_fallback`) und in `talos doctor` wie `talos models` gezeigt.

⚠️ **Einmal beim Laden festgeschrieben, danach nur gelesen.** `config.load_config`
installiert die Tabelle (`install`), Zaehler und Kommandozentrale lesen sie (`lookup`).
Das ist Prozesszustand — bewusst: die Composition-Root reicht die Konfiguration nicht
an jeden Verbraucher, und ein weiterer Konstruktor-Parameter an drei Stellen machte
nichts sicherer. Die Overrides sind wie `TALOS_MODEL` eine Betreiber-Entscheidung auf
der geprueften Modell-Konfigurationsflaeche (Butch-Kontext: keine Sonderlogik, dieselbe
Ebene, derselbe Weg). Wer es explizit will, uebergibt `UsageMeter(infos=…)`; die
Tabelle selbst ist unveraenderlich und wird als Ganzes ersetzt, nie editiert.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping

from . import catalog
from .catalog import MODEL_INFO_FIELDS, ModelInfo
from .models import is_model_id

ENV_VAR = "TALOS_MODEL_OVERRIDES"
# Gebunden wie `models.MAX_MODELS`: eine Tabelle, die ein Skript fuellt, darf den Start
# nicht mit tausend Zeilen aufhalten.
MAX_ENTRIES = 64
# Obergrenzen gegen Versehen, nicht gegen Angreifer: ein Fenster von 10^12 Token oder
# ein Preis von 10^6 $ je Million ist keine Korrektur, sondern ein verrutschtes Komma.
MAX_CONTEXT_WINDOW = 100_000_000
MAX_PRICE_PER_MILLION = 100_000.0
_TOKENS_PER_PRICE_UNIT = 1_000_000
_FIELD_NAME_CHARS = 40


@dataclass(frozen=True)
class Overrides:
    """Die geparste Tabelle: was gilt (`entries`) und was herausfiel (`dropped`).

    `entries` ist ein gewoehnliches dict, aber per Vertrag unveraenderlich — es wird
    hier gebaut und danach nur gelesen. `dropped` sind Saetze fuer Menschen: jeder nennt
    Modell und Feld, keiner den Wert.
    """

    entries: Mapping[str, ModelInfo]
    dropped: tuple[str, ...] = ()

    def get(self, model: str) -> ModelInfo | None:
        return self.entries.get(model)


EMPTY = Overrides({}, ())


# --- Parsen -----------------------------------------------------------------------------


def parse(text: str, *, variable: str = ENV_VAR) -> Overrides:
    """Der Wert der Variablen als Tabelle — oder `ValueError`, die die Variable nennt.

    Nur die Form des JSON ist ein Fehler. Alles darunter (falscher Name, falsches Feld,
    falscher Typ) faellt einzeln heraus und steht in `dropped`: ein Tippfehler in einer
    Zeile darf nicht den Start kosten, aber er darf auch nicht verschwinden.
    """
    raw = _unquoted(str(text or "").strip())
    if not raw:
        return EMPTY
    try:
        data = json.loads(raw)
    except ValueError:
        # Absichtlich ohne den Wert und ohne die Position aus `json`: beides wiederholt,
        # was drinstand.
        raise ValueError(
            f"{variable} is not valid JSON — expected one object on one line, keyed by "
            "model id (the value is deliberately not repeated here)"
        ) from None
    if not isinstance(data, dict):
        raise ValueError(
            f"{variable} must be a JSON object keyed by model id, not {_json_kind(data)}"
        )
    entries: dict[str, ModelInfo] = {}
    dropped: list[str] = []
    for position, (key, fields) in enumerate(data.items(), start=1):
        if len(entries) >= MAX_ENTRIES:
            dropped.append(f"{variable}: more than {MAX_ENTRIES} entries — the rest is ignored")
            break
        name = str(key).strip()
        if not is_model_id(name):
            # Die Position statt des Namens: ein Name, der kein Bezeichner ist, kann
            # alles sein — und alles gehoert nicht in eine Meldung.
            dropped.append(f"{variable}: key #{position} is not a model id and is dropped")
            continue
        if not isinstance(fields, dict):
            dropped.append(f"{variable}: entry {name!r} must be an object of fields and is dropped")
            continue
        info, reasons = _entry(name, fields, variable)
        dropped.extend(reasons)
        if info.overridden:
            entries[name] = info
        else:
            dropped.append(f"{variable}: entry {name!r} has no usable field and is dropped")
    return Overrides(entries, tuple(dropped))


def _entry(name: str, fields: dict, variable: str) -> tuple[ModelInfo, list[str]]:
    """Ein Eintrag, Feld fuer Feld. Was nicht passt, faellt mit Begruendung heraus."""
    info = ModelInfo()
    reasons: list[str] = []
    for field_name, value in fields.items():
        feld = str(field_name)[:_FIELD_NAME_CHARS]
        if feld not in MODEL_INFO_FIELDS:
            reasons.append(
                f"{variable}: {name!r} names {feld!r}, which is not an overridable field "
                f"(allowed: {', '.join(MODEL_INFO_FIELDS)}) — dropped"
            )
            continue
        try:
            clean = _VALIDATORS[feld](value)
        except ValueError as problem:
            reasons.append(f"{variable}: {name!r}.{feld} {problem} — dropped")
            continue
        info = replace(info, **{feld: clean}, overridden=info.overridden | {feld})
    return info, reasons


def _window(value: object) -> int:
    # `bool` ist ein `int` — ohne diese Zeile waere `true` ein Fenster von einem Token.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("must be a whole number of tokens")
    if value <= 0 or value > MAX_CONTEXT_WINDOW:
        raise ValueError(f"must be between 1 and {MAX_CONTEXT_WINDOW}")
    return value


def _price(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number (USD per million tokens)")
    if not math.isfinite(value) or value < 0 or value > MAX_PRICE_PER_MILLION:
        raise ValueError(f"must be between 0 and {MAX_PRICE_PER_MILLION:g} USD per million tokens")
    return float(value)


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


_VALIDATORS: dict[str, Callable[[object], object]] = {
    "context_window": _window,
    "input_price": _price,
    "output_price": _price,
    "vision": _flag,
    "reasoning": _flag,
}


def _unquoted(raw: str) -> str:
    """`KEY='{…}'` ist Shell-Gewohnheit. Ein Objekt beginnt nie mit einem
    Anfuehrungszeichen — das Abstreifen EINES passenden Paars ist eindeutig, kein Raten."""
    if len(raw) >= 3 and raw[0] == raw[-1] and raw[0] in "'\"" and raw[1] == "{":
        return raw[1:-1]
    return raw


def _json_kind(data: object) -> str:
    if data is None:
        return "null"
    if isinstance(data, bool):
        return "a boolean"
    if isinstance(data, (int, float)):
        return "a number"
    if isinstance(data, str):
        return "a string"
    if isinstance(data, list):
        return "a list"
    return type(data).__name__


# --- Abgleich und Nachschlagen ------------------------------------------------------------


def reconcile(overrides: Overrides, known: Iterable[str]) -> Overrides:
    """Behaelt nur Eintraege fuer Modelle, die es gibt. Fuegt nie eines hinzu.

    `known` ist der Katalog, mit dem der Agent wirklich laeuft, plus das konfigurierte
    Modell (`resolve_fallback` laesst es auch ausserhalb des Katalogs zu, wenn eine
    eigene Adresse gesetzt ist). Was nicht darin steht, faellt mit Begruendung heraus —
    ein Override kann sonst nur eines: nie treffen, ohne dass es jemand merkt.
    """
    if not overrides.entries:
        return overrides
    bekannt = set(known)
    kept = {name: info for name, info in overrides.entries.items() if name in bekannt}
    unbekannt = tuple(
        f"{ENV_VAR}: {name!r} is not a model this catalog lists — dropped (check the "
        "spelling, or fetch the list with `talos models --refresh`)"
        for name in overrides.entries if name not in bekannt
    )
    if not unbekannt:
        return overrides
    return Overrides(kept, overrides.dropped + unbekannt)


def merge(shipped: ModelInfo, override: ModelInfo | None) -> ModelInfo:
    """Der Override liegt Feld fuer Feld ueber dem ausgelieferten Wert. Was der
    Betreiber nicht gesetzt hat, bleibt, wie es war — auch wenn das „unbekannt" ist."""
    if override is None or not override.overridden:
        return shipped
    werte = {feld: getattr(override, feld) for feld in override.overridden}
    return replace(shipped, **werte, overridden=frozenset(override.overridden))


_ACTIVE: Overrides = EMPTY


def install(overrides: Overrides) -> None:
    """Die Tabelle des Prozesses ersetzen — als Ganzes, nie editiert."""
    global _ACTIVE
    _ACTIVE = overrides


def active() -> Overrides:
    return _ACTIVE


def lookup(model: str) -> ModelInfo:
    """Katalog plus installierte Overrides fuer dieses Modell. Nie `None`."""
    return merge(catalog.model_info(model), _ACTIVE.get(model))


# --- Rechnen und Zeigen ---------------------------------------------------------------------


def cost_usd(info: ModelInfo, input_tokens: int, output_tokens: int) -> float:
    """Listenpreis nach den Preisen in `info`. Cache-Token bleiben absichtlich
    unbepreist: ihr Tarif ist Anbietersache (Anthropic rechnet 10 % und 125 %), und
    eine geratene Quote ist schlechter als eine benannte Luecke."""
    return (
        max(0, input_tokens) * info.input_price + max(0, output_tokens) * info.output_price
    ) / _TOKENS_PER_PRICE_UNIT


def format_tokens(count: int) -> str:
    """`200000` -> `200k`, `1500000` -> `1.5M`. Exakt, nicht gerundet — ein Fenster ist
    eine Zahl, die jemand eingetragen hat, und die soll er wiedererkennen."""
    if count < 1_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1_000:g}k"
    return f"{count / 1_000_000:g}M"


def describe(info: ModelInfo, *, mark: bool = True) -> str:
    """Eine Zeile fuer die Maschinenkonsole (englisch). `mark` haengt an, welche Felder
    vom Betreiber stammen — damit niemand den Katalog fuer die Quelle haelt."""
    teile: list[str] = []
    if info.context_window:
        teile.append(f"context {format_tokens(info.context_window)}")
    if info.has_prices:
        teile.append(f"in ${info.input_price:g}/M")
        teile.append(f"out ${info.output_price:g}/M")
    if info.vision:
        teile.append("vision")
    if info.reasoning:
        teile.append("reasoning")
    text = " · ".join(teile) if teile else "nothing known"
    if mark and info.overridden:
        felder = ", ".join(feld for feld in MODEL_INFO_FIELDS if feld in info.overridden)
        text += f"  (override: {felder})"
    return text


__all__ = [
    "EMPTY",
    "ENV_VAR",
    "MAX_ENTRIES",
    "Overrides",
    "active",
    "cost_usd",
    "describe",
    "format_tokens",
    "install",
    "lookup",
    "merge",
    "parse",
    "reconcile",
]
