"""Zugangsdaten, an ihren Anbieter gebunden — Schluessel und Adresse in einem Stueck.

Der Anlass ist ein Befund aus der Pruefung vom 05.08.: `config.api_key` war EIN Feld fuer
JEDEN Anbieter, gefuellt mit `ANTHROPIC_API_KEY or OPENAI_API_KEY`. Wer den Anbieter auf
`openai-api` stellte, schickte damit seinen Anthropic-Schluessel als `Bearer` an OpenAI.
Aus einem Konfigurationsfehler wurde die Weitergabe eines Geheimnisses an einen Dritten —
und weil der fremde Anbieter mit 401 antwortet, faellt es nicht einmal als Stoerung auf.

Vier Entscheidungen halten das geschlossen:

⚠️ **Schluessel und Basis-Adresse sind EIN Stueck.** Getrennt sind sie derselbe Fehler
zweimal: eine Adresse ohne Anbieterbindung schickt OpenAI-Anfragen an Anthropics Basis,
oder an eine Maschine, die jemand anders gewaehlt hat. Wer nur den Schluessel bindet, hat
den Fehler halbiert, nicht behoben.

⚠️ **Aufgeloest wird beim AUFRUF, nicht beim Bauen.** Der Betreiber wechselt den Anbieter
im laufenden Prozess (`/model`). Der Router baut dabei heute jedes Mal neu — die Aufloesung
am Verbrauchspunkt macht die Fehlerklasse trotzdem unmoeglich statt bloss unwahrscheinlich,
und sie ueberlebt den Tag, an dem jemand den Neubau aus Geschwindigkeitsgruenden entfernt.

⚠️ **Fehlt der Schluessel, gibt es keinen Ersatz.** Kein Rueckfall auf einen anderen
Anbieter: der Betreiber hat einen Empfaenger seiner Daten BENANNT, und ihn still
auszutauschen ist dieselbe Fehlerklasse wie der Befund selbst, nur leiser.

⚠️ **Die Meldung nennt den Namen der Variablen, nie ihren Wert.** Ein „kein Schluessel
hinterlegt" ist eine Bauanleitung; ein Schluessel im Fehlertext ist ein zweites Leck.

Die Namen kommen aus `catalog.ProviderInfo.env_key` — der Katalog fuehrt sie ohnehin
schon, sie waren nur nie verdrahtet. Eine zweite Liste waere eine Liste, die driftet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import catalog

__all__ = [
    "MissingKey",
    "Route",
    "CredentialStore",
    "WORKER_ENV_VAR",
    "base_url_var",
    "key_var",
    "from_lookup",
    "parse_worker_socket",
    "worker_scope_names",
]

# Der Praefix, unter dem eine anbietergebundene Basis-Adresse steht. Mechanisch aus dem
# Slug abgeleitet statt handgepflegt: eine Tabelle, die jemand beim naechsten Anbieter
# vergisst, waere genau der stille Fehler, gegen den diese Datei gebaut ist.
BASE_URL_PREFIX = "TALOS_BASE_URL_"

# Die alte, anbieterlose Adresse. Sie wird nicht mehr gelesen — aber sie wird auch nicht
# ignoriert: wer sie gesetzt hat, wollte zu einer BESTIMMTEN Maschine sprechen, und die
# still gegen die Vorgabe zu tauschen hiesse, seine Daten woanders hinzuschicken.
LEGACY_BASE_URL = "TALOS_API_BASE_URL"

LEGACY_MESSAGE = (
    f"{LEGACY_BASE_URL} is set but no longer read: one base url for every provider is "
    "how a key ends up at the wrong company. Name the provider — for example "
    f"{BASE_URL_PREFIX}OPENAI_API — and remove the old variable."
)

# Die Transport-Naht des Reasoners: `TALOS_MODEL_WORKER=socket:///run/talos/model.sock`
# verlagert das Denken in den UID-getrennten Modell-Worker (`modelworker.py`). Leer
# heisst: Direktweg, wie bisher — die Vorgabe auf jeder Dev-Maschine.
WORKER_ENV_VAR = "TALOS_MODEL_WORKER"
WORKER_SCHEME = "socket://"


def parse_worker_socket(value: str) -> str:
    """Den Socket-Pfad aus `TALOS_MODEL_WORKER` — oder "" fuer den Direktweg.

    Fail-closed bei unbekannten Werten: wer `sock://…` tippt und still auf dem
    Direktweg landet, haelt den Schluessel wieder im Agenten-Prozess — genau der
    Zustand, den der Worker abschaffen soll, nur diesmal unbemerkt. Ein gesetzter,
    unlesbarer Wert ist deshalb ein Fehler, kein Rueckfall.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(WORKER_SCHEME):
        pfad = text[len(WORKER_SCHEME):].strip()
        if pfad:
            return pfad
    raise ValueError(
        f"{WORKER_ENV_VAR} kennt nur die Form 'socket:///pfad/zum.sock' — gelesen "
        f"wurde {text[:40]!r}. Es wurde nichts gewaehlt; der Direktweg waere hier "
        "ein stiller Rueckfall."
    )


def worker_scope_names() -> tuple[str, ...]:
    """Die Namen aller Provider-Schluessel laut Katalog.

    Im Worker-Modus gehoeren ihre WERTE in die Worker-Env (`/etc/talos/model.env`,
    UID `talos-model`) — nicht in die Umgebung des Agenten. Aus dem Katalog
    abgeleitet statt handgepflegt: eine zweite Liste driftet beim naechsten
    Anbieter, und eine vergessene Zeile hiesse hier „Schluessel im Agent-Env
    erwartet, obwohl der Worker laeuft".
    """
    return tuple(
        info.env_key for info in catalog.PROVIDERS if info.needs_key and info.env_key
    )


class MissingKey(ValueError):
    """Kein Schluessel fuer diesen Anbieter. Traegt den Variablennamen, nie den Wert."""


@dataclass(frozen=True)
class Route:
    """Wohin gesprochen wird und womit man sich dabei ausweist. Untrennbar."""

    provider: str
    api_key: str
    # Leer heisst: die Vorgabe des Protokolls gilt (`ApiReasoner` setzt sie ein).
    base_url: str = ""


def _slug_part(provider: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")


def base_url_var(provider: str) -> str:
    """`TALOS_BASE_URL_OPENAI_API` — der Name, unter dem diese Adresse steht."""
    return f"{BASE_URL_PREFIX}{_slug_part(provider)}"


def key_var(provider: str) -> str:
    """Der Name der Schluesselvariablen laut Katalog; leer, wenn der Anbieter keinen will."""
    info = catalog.get(provider)
    return info.env_key if info is not None else ""


@dataclass(frozen=True)
class CredentialStore:
    """Anbieter → Route. Kennt nur, was tatsaechlich hinterlegt ist."""

    routes: dict[str, Route] = field(default_factory=dict)

    def has(self, provider: str) -> bool:
        return bool((self.routes.get(provider) or Route(provider, "")).api_key)

    def route(self, provider: str) -> Route:
        """Die Route dieses Anbieters — oder `MissingKey`. Nie die eines anderen."""
        eintrag = self.routes.get(provider)
        if eintrag is not None and eintrag.api_key:
            return eintrag
        # ⚠️ Ein LOKALER Anbieter (auth="local", etwa Ollama) hat bewusst keinen
        # Schluessel: die Route ist nur eine Adresse, und das ist keine Luecke. Nur bei
        # ihm darf eine Route ohne Schluessel das Haus verlassen — schluesselpflichtige
        # Anbieter bleiben fail-closed. `from_lookup` legt solche Routen mit leerem
        # Schluessel an; ein handgebauter Bestand ohne Eintrag landet weiter unten.
        info = catalog.get(provider)
        if eintrag is not None and info is not None and not info.needs_key:
            return eintrag
        name = key_var(provider)
        gesucht = name or f"a key for {provider}"
        raise MissingKey(
            f"no {gesucht} configured — provider {provider!r} was not used. "
            "Nothing was sent, and no other provider's key stands in for it."
        )

    def all_keys(self) -> tuple[str, ...]:
        """Jeder hinterlegte Schluessel — zum Maskieren, nicht zum Benutzen.

        Bewusst ALLE, nicht nur der aktive: ein fremder Server, der einen Header in seinen
        Fehlertext zurueckspiegelt, spiegelt den, den er bekommen hat. Genau der Fall,
        den diese Datei verhindert, waere sonst der eine, der ungeschwaerzt im Log steht.
        """
        return tuple(sorted({r.api_key for r in self.routes.values() if r.api_key}))


def from_lookup(lookup) -> CredentialStore:
    """Baut den Bestand aus einer Namensauflösung (Umgebung + Geheimnisdatei).

    Gelesen wird bei schluesselpflichtigen Anbietern nur, was der Katalog als solchen
    fuehrt. Ein Anbieter ohne hinterlegten Schluessel taucht gar nicht erst auf —
    „vorhanden, aber leer" ist ein Zustand, den niemand braucht.

    Lokale Anbieter (auth="local", etwa Ollama) bekommen eine Route OHNE Schluessel:
    ihre Route ist eine reine Adresse, und genau dafuer ist sie da. Wer keine Adresse
    kennt (weder Katalog-Vorgabe noch `TALOS_BASE_URL_<PROVIDER>`), taucht ebenfalls
    nicht auf — eine Route ohne Ziel waere eine Einladung zum Raten.
    """
    routen: dict[str, Route] = {}
    for info in catalog.PROVIDERS:
        if info.needs_key and info.env_key:
            schluessel = str(lookup(info.env_key) or "").strip()
            if not schluessel:
                continue
            adresse = str(lookup(base_url_var(info.slug)) or "").strip()
            routen[info.slug] = Route(
                info.slug, schluessel, (adresse or info.base_url).rstrip("/")
            )
        elif info.auth == "local":
            adresse = str(lookup(base_url_var(info.slug)) or "").strip()
            ziel = (adresse or info.base_url).rstrip("/")
            if ziel:
                routen[info.slug] = Route(info.slug, "", ziel)
    return CredentialStore(routen)
