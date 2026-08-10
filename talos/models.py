"""Die Modellliste eines Anbieters — live geholt, aber nie blind uebernommen.

Bis hierher war der Katalog handkuratiert: was ein Anbieter neu herausbringt, musste
jemand eintragen. Hermes fragt stattdessen `/v1/models` ab, und das ist der bessere
Weg — aber nur mit vier Vorbehalten, die hier den Code bestimmen.

⚠️ **Es wird ERGAeNZT, nie ersetzt.** Faellt die Abfrage aus, antwortet der Anbieter mit
einer leeren Liste oder liefert er Unsinn, bleibt der kuratierte Katalog stehen. Der
umgekehrte Weg — live gewinnt — macht einen erreichbaren Fremdserver zu dem, der
bestimmt, womit dieser Agent denkt.

⚠️ **Nichts passiert beim Start.** Ein Netzaufruf im Hochfahren macht aus einer
Stoerung beim Anbieter eine Stoerung hier. Geholt wird auf Ansage (`talos models
--refresh`); dazwischen liest der Katalog einen Zwischenspeicher von der Platte.

⚠️ **Die Antwort ist fremder Text.** Sie landet in einer Auswahlliste und in
Telegram-Callback-Daten, und die sind auf 64 Byte begrenzt — ein Modellname von 300
Zeichen zerlegt den Picker, nicht die Sicherheit, aber kaputt ist kaputt. Deshalb
Form, Laenge und Anzahl begrenzt.

⚠️ **Ohne Schluessel kein Abruf.** Ein Anbieter, fuer den keine Zugangsdaten hinterlegt
sind, wird nicht gefragt — sonst wandert eine Anfrage an einen fremden Server, ohne
dass jemand dafuer etwas hinterlegt haette.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# Ein Tag. Modelle erscheinen nicht stuendlich, und ein Zwischenspeicher, der staendig
# ablaeuft, ist keiner.
CACHE_TTL_S = 24 * 60 * 60
FETCH_TIMEOUT_S = 20
# Der Picker traegt den Namen in Telegram-Callback-Daten (64 Byte insgesamt).
MAX_NAME_CHARS = 60
MAX_MODELS = 60
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$")

# Wer wo fragt. Anthropic weicht in Kopfzeile und Version ab — das ist der ganze
# Unterschied, deshalb eine Tabelle statt zweier Codewege.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "anthropic-api": ("https://api.anthropic.com/v1", "x-api-key"),
    "openai-api": ("https://api.openai.com/v1", "bearer"),
}
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Fetched:
    slug: str
    models: tuple[str, ...]
    fetched_at: float
    error: str = ""


def clean_names(raw: Iterable[object]) -> tuple[str, ...]:
    """Was aus einer fremden Antwort als Modellname durchgeht.

    Reihenfolge bleibt erhalten (Anbieter sortieren sinnvoll: neu zuerst), Doubletten
    fallen weg, und alles, was nicht wie ein Bezeichner aussieht, ebenfalls.
    """
    gesehen: list[str] = []
    for eintrag in raw or ():
        name = str(eintrag or "").strip()
        if not name or len(name) > MAX_NAME_CHARS or not _NAME_OK.match(name):
            continue
        if name not in gesehen:
            gesehen.append(name)
        if len(gesehen) >= MAX_MODELS:
            break
    return tuple(gesehen)


def _ids(payload: object) -> tuple[str, ...]:
    """`{"data": [{"id": …}]}` — das Format, auf das sich beide geeinigt haben."""
    if not isinstance(payload, dict):
        return ()
    eintraege = payload.get("data")
    if not isinstance(eintraege, list):
        return ()
    return clean_names(
        eintrag.get("id") for eintrag in eintraege if isinstance(eintrag, dict)
    )


def fetch(slug: str, *, api_key: str, base_url: str = "", get: Callable | None = None,
          now: Callable[[], float] = time.time) -> Fetched:
    """Holt die Liste eines Anbieters. Fehler sind ein Ergebnis, keine Ausnahme.

    Ein Anbieter, der gerade nicht antwortet, darf weder den Aufruf noch den Katalog
    umwerfen — er darf nur nichts beitragen.
    """
    vorgabe, art = ENDPOINTS.get(slug, ("", "bearer"))
    wurzel = (base_url or vorgabe).rstrip("/")
    if not wurzel:
        return Fetched(slug, (), now(), "no endpoint known for this provider")
    if not api_key.strip():
        return Fetched(slug, (), now(), "no key — not asked")

    kopf = ({"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
            if art == "x-api-key" else {"Authorization": f"Bearer {api_key}"})
    if get is None:
        import requests

        get = requests.get
    try:
        antwort = get(f"{wurzel}/models", headers=kopf, timeout=FETCH_TIMEOUT_S)
        daten = antwort.json() if callable(getattr(antwort, "json", None)) else None
    except Exception as fehler:                  # Netz, TLS, JSON — hier alles dasselbe
        return Fetched(slug, (), now(), type(fehler).__name__)
    namen = _ids(daten)
    return Fetched(slug, namen, now(), "" if namen else "no usable model names in the answer")


def load_cache(path: Path) -> dict[str, dict]:
    try:
        daten = json.loads(path.read_text(encoding="utf-8"))
        return daten if isinstance(daten, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".neu")
    temp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def fresh_models(cache: dict[str, dict], slug: str, *, ttl_s: int = CACHE_TTL_S,
                 now: Callable[[], float] = time.time) -> tuple[str, ...]:
    """Was im Zwischenspeicher steht — sofern es nicht zu alt ist."""
    eintrag = cache.get(slug)
    if not isinstance(eintrag, dict):
        return ()
    alter = now() - float(eintrag.get("fetched_at") or 0)
    if alter > ttl_s or alter < 0:               # negativ = die Uhr ist gesprungen
        return ()
    return clean_names(eintrag.get("models") or ())


def merged(registry, cache: dict[str, dict], *, ttl_s: int = CACHE_TTL_S,
           now: Callable[[], float] = time.time):
    """Katalog plus frische Namen. Der kuratierte Eintrag bleibt vorn.

    ⚠️ Der Filter aus `provider.safe_talos_registry` gilt hier erneut: eine Live-Liste
    darf kein Claude-Modell unter einem fremden Anbieter einschleusen — sonst laeuft
    ein Aufruf ueber ein Konto, ueber das niemand entschieden hat.
    """
    from .provider import Provider, ProviderRegistry, _looks_like_claude_model

    ergaenzt = []
    for anbieter in registry.providers:
        live = fresh_models(cache, anbieter.slug, ttl_s=ttl_s, now=now)
        if anbieter.slug != "claude-cli":
            live = tuple(m for m in live if not _looks_like_claude_model(m))
        neue = tuple(m for m in live if m not in anbieter.models)
        ergaenzt.append(
            Provider(anbieter.slug, anbieter.label, anbieter.models + neue)
            if neue else anbieter
        )
    return ProviderRegistry(tuple(ergaenzt))


def refresh(slugs: Iterable[str], *, keys: dict[str, str], path: Path,
            base_urls: dict[str, str] | None = None, get=None,
            now: Callable[[], float] = time.time) -> tuple[Fetched, ...]:
    """Holt die genannten Anbieter und schreibt den Zwischenspeicher fort.

    Ein Fehlschlag loescht den alten Eintrag NICHT: eine Stoerung beim Anbieter darf
    keine Liste vernichten, die gestern noch stimmte.
    """
    cache = load_cache(path)
    ergebnisse = []
    for slug in slugs:
        ergebnis = fetch(slug, api_key=keys.get(slug, ""),
                         base_url=(base_urls or {}).get(slug, ""), get=get, now=now)
        ergebnisse.append(ergebnis)
        if ergebnis.models:
            cache[slug] = {"models": list(ergebnis.models), "fetched_at": ergebnis.fetched_at}
    save_cache(path, cache)
    return tuple(ergebnisse)


def render(results: Iterable[Fetched], cache: dict[str, dict]) -> str:
    from .ux import SYM_FAIL, SYM_OK

    zeilen = [""]
    for ergebnis in results:
        gespeichert = len(clean_names((cache.get(ergebnis.slug) or {}).get("models") or ()))
        if ergebnis.models:
            zeilen.append(f"  {SYM_OK} {ergebnis.slug:16} {len(ergebnis.models)} models")
        else:
            hinweis = ergebnis.error or "nothing returned"
            nachsatz = f" — keeping {gespeichert} cached" if gespeichert else ""
            zeilen.append(f"  {SYM_FAIL} {ergebnis.slug:16} {hinweis}{nachsatz}")
    return "\n".join(zeilen) + "\n"


def _catalog():
    """Derselbe Katalog wie im laufenden Agenten — und derselbe Rueckfall.

    Ohne Hermes auf der Maschine bleiben die beiden API-Wege uebrig; `safe_talos_registry`
    haengt sie ohnehin immer an. Ein fehlender Nachbar darf diesen Befehl nicht umwerfen,
    sonst ist er ausgerechnet auf einer frischen Installation unbrauchbar.
    """
    from .config import HERMES_MODELS, HERMES_PROVIDER_CATALOG
    from .provider import HermesCatalogLoader, Provider, ProviderRegistry, safe_talos_registry

    try:
        roh = HermesCatalogLoader(HERMES_PROVIDER_CATALOG, HERMES_MODELS).load()
    except Exception:
        roh = ProviderRegistry((Provider("claude-cli", "placeholder", ("claude-fable-5",)),))
    return safe_talos_registry(roh)


def run_models(argv: list[str] | None = None, *, out=None, get=None) -> int:
    """`talos models [--refresh]` — zeigt den Katalog, holt ihn auf Wunsch neu."""
    import sys

    from .config import MODEL_CACHE, load_config

    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    pfad = Path(MODEL_CACHE)

    if "--refresh" in argumente:
        # Dieselbe Quelle wie der Denkweg: Anbieter → Schluessel UND Adresse. Vorher stand
        # hier eine zweite, handverdrahtete Liste, die nur `os.environ` las — eine
        # Installation mit Geheimnisdatei bekam damit „nichts zurueckgegeben" statt ihrer
        # Modelle, und die Adresse eines eigenen Gateways fehlte ganz.
        bestand = load_config().api_credentials
        ergebnisse = refresh(
            ENDPOINTS.keys(),
            keys={slug: route.api_key for slug, route in bestand.routes.items()},
            base_urls={slug: route.base_url for slug, route in bestand.routes.items()},
            path=pfad, get=get,
        )
        schreiben(render(ergebnisse, load_cache(pfad)))

    voll = merged(_catalog(), load_cache(pfad))
    schreiben("\n")
    for anbieter in voll.providers:
        schreiben(f"  {anbieter.slug:16} {len(anbieter.models):3} models   {anbieter.label}\n")
    schreiben(f"\n  cache: {pfad}"
              f"{' — empty, run `talos models --refresh`' if not pfad.is_file() else ''}\n\n")
    return 0


__all__ = [
    "CACHE_TTL_S",
    "ENDPOINTS",
    "MAX_MODELS",
    "MAX_NAME_CHARS",
    "Fetched",
    "clean_names",
    "fetch",
    "fresh_models",
    "load_cache",
    "merged",
    "refresh",
    "render",
    "run_models",
    "save_cache",
]
