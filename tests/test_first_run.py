"""Die Erstlauf-Wand — fuenf Schichten, und was von ihr bleiben muss.

Wer Talos frisch installierte und `talos ask "hallo"` tippte, bekam einen unbehandelten
Traceback. Fuenfmal hintereinander, aus fuenf verschiedenen Gruenden: fehlender
Bot-Token, leere Allowlist, ein Modell ausserhalb des Katalogs, ein Anbieter-Rueckfall auf
eine CLI, die es nicht gab, und ein fehlender API-Schluessel. Vier davon waren
ueberfluessig; der fuenfte ist echt.

⚠️ Die Haelfte dieser Tests prueft, dass die Wand da BLEIBT, wo sie hingehoert. Eine
Erstlauf-Hilfe, die den Dienst mit oeffnet, waere kein Fortschritt, sondern der Unfall.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from talos import config as config_mod
from talos.channel import Principal
from talos.eventlog import EventLog
from talos.provider import ModelSelection, resolve_fallback


def _env(tmp_path: Path, monkeypatch, **werte: str) -> None:
    """Eine Installation ohne jede Vorbelegung — so wie nach `install.sh`."""
    datei = tmp_path / "talos.env"
    datei.write_text("".join(f"{k}={v}\n" for k, v in werte.items()), encoding="utf-8")
    monkeypatch.setattr(config_mod, "LOCAL_ENV", datei)
    monkeypatch.setattr(config_mod, "SECRETS_ENV", tmp_path / "gibt-es-nicht.env")
    for name in ("TELEGRAM_BOT_TOKEN", "TALOS_ALLOWED_PRINCIPALS", "TALOS_ALLOWED_USER_IDS"):
        monkeypatch.delenv(name, raising=False)


# --- Schicht 1: der Bot-Token ------------------------------------------------------


def test_ask_does_not_need_a_messenger_token(tmp_path, monkeypatch) -> None:
    """`ask` und `chat` fassen Telegram nicht an — sie sollten ihn nie verlangt haben."""
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="cli:501")

    config = config_mod.load_config(require_channel=False)

    assert config.bot_token == ""


def test_the_service_still_demands_its_channel(tmp_path, monkeypatch) -> None:
    """Ein Dienst ohne Kanal waere ein Prozess, der auf nichts hoert."""
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="cli:501")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        config_mod.load_config()


# --- Schicht 2: die leere Allowlist ------------------------------------------------


def test_an_empty_allowlist_admits_the_local_caller_on_the_command_line(
    tmp_path, monkeypatch
) -> None:
    """Wer eine leere Liste hat, hat gerade installiert — und sitzt an der Maschine.

    Er koennte seine Kennung selbst eintragen; die Datei gehoert ihm. Die Wand forderte
    eine Zeremonie, deren Ergebnis feststand.
    """
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="")

    config = config_mod.load_config(require_channel=False)

    assert config.allowed_principals == frozenset({Principal("cli", str(os.getuid()))})


def test_the_service_still_refuses_an_empty_allowlist(tmp_path, monkeypatch) -> None:
    """⚠️ Dort holt Talos Nachrichten von aussen ab — leer hiesse wirklich offen fuer alle."""
    _env(tmp_path, monkeypatch, TELEGRAM_BOT_TOKEN="x", TALOS_ALLOWED_PRINCIPALS="")

    with pytest.raises(ValueError, match="offen für alle"):
        config_mod.load_config()


def test_a_set_allowlist_is_exhaustive_and_is_never_extended(tmp_path, monkeypatch) -> None:
    """⚠️ Steht ein Eintrag drin, hat jemand entschieden. Ergaenzen waere das Gegenteil."""
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="telegram:999")

    config = config_mod.load_config(require_channel=False)

    assert config.allowed_principals == frozenset({Principal("telegram", "999")})
    assert Principal("cli", str(os.getuid())) not in config.allowed_principals


def test_the_admitted_principal_is_the_caller_and_not_a_wildcard(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="")

    (wer,) = config_mod.load_config(require_channel=False).allowed_principals

    assert wer.channel == "cli", "die Lockerung darf keinen fremden Kanal oeffnen"
    assert wer.user_id == str(os.getuid())
    assert wer.user_id not in ("*", "", "0" * 9)


# --- Schicht 3+4: das Modell, das der Katalog nicht kennt ---------------------------


class _Anbieter:
    def __init__(self, slug: str, models: tuple[str, ...]) -> None:
        self.slug = slug
        self.models = models


class _Katalog:
    def __init__(self, *anbieter: _Anbieter) -> None:
        self._a = anbieter

    def providers(self):
        return self._a

    def get(self, slug: str):
        for a in self._a:
            if a.slug == slug:
                return a
        raise ValueError(f"unknown provider: {slug}")

    def selection(self, provider: str, model: str) -> ModelSelection:
        a = self.get(provider)
        if model not in a.models:
            raise ValueError(f"unknown model for {provider}: {model}")
        return ModelSelection(provider, model)


def test_a_known_default_is_returned_unchanged(tmp_path) -> None:
    log = EventLog(tmp_path / "ev.db")
    katalog = _Katalog(_Anbieter("anthropic-api", ("m1", "m2")))

    gewaehlt = resolve_fallback(log, katalog, ModelSelection("anthropic-api", "m2"))

    assert gewaehlt == ModelSelection("anthropic-api", "m2")


def test_an_unknown_model_falls_back_within_the_same_provider(tmp_path) -> None:
    """⚠️ Der Betreiber hat den ANBIETER gewaehlt; veraltet ist der Modellname.

    Der erste Anlauf nahm blind den ersten Anbieter im Katalog und landete auf einer
    frischen Maschine bei der Hermes-CLI, die es dort nicht gibt — der Start brach eine
    Zeile spaeter erneut ab.
    """
    log = EventLog(tmp_path / "ev.db")
    katalog = _Katalog(_Anbieter("hermes-cli", ("h1",)), _Anbieter("anthropic-api", ("m1",)))

    gewaehlt = resolve_fallback(log, katalog, ModelSelection("anthropic-api", "weg"))

    assert gewaehlt == ModelSelection("anthropic-api", "m1")


def test_an_unknown_provider_falls_back_to_the_first_known_one(tmp_path) -> None:
    log = EventLog(tmp_path / "ev.db")
    katalog = _Katalog(_Anbieter("anthropic-api", ("m1",)))

    gewaehlt = resolve_fallback(log, katalog, ModelSelection("gibt-es-nicht", "x"))

    assert gewaehlt == ModelSelection("anthropic-api", "m1")


def test_the_fallback_leaves_a_trace(tmp_path) -> None:
    """Still zurueckfallen hiesse: der Agent antwortet anders, und niemand weiss warum."""
    log = EventLog(tmp_path / "ev.db")
    katalog = _Katalog(_Anbieter("anthropic-api", ("m1",)))

    resolve_fallback(log, katalog, ModelSelection("anthropic-api", "weg"))

    eintraege = log.recent(5, ("model.fallback",))
    assert eintraege, "der Rueckfall hinterliess keine Spur"


def test_an_empty_catalog_says_so_instead_of_guessing(tmp_path) -> None:
    log = EventLog(tmp_path / "ev.db")

    with pytest.raises(ValueError, match="models --refresh"):
        resolve_fallback(log, _Katalog(), ModelSelection("anthropic-api", "m1"))


# --- Der Waechter: die Lockerung darf nicht weiterwandern ---------------------------


def test_require_channel_is_only_relaxed_for_ask_and_chat() -> None:
    """⚠️ Geprueft wird JEDER Ausdruck, nicht nur das Literal `False`.

    Der erste Entwurf dieses Waechters filterte auf `ast.Constant(False)` — und haette
    `require_channel=not (ask or chat)` glatt durchgelassen. Ein Waechter, der nur eine
    Schreibweise kennt, ist blind fuer die naechste.
    """
    import ast

    wurzel = Path(__file__).resolve().parent.parent / "talos"
    gefunden: list[tuple[str, str]] = []
    for datei in wurzel.rglob("*.py"):
        baum = ast.parse(datei.read_text(encoding="utf-8"))
        for knoten in ast.walk(baum):
            if not isinstance(knoten, ast.Call):
                continue
            for schluessel in knoten.keywords:
                if schluessel.arg == "require_channel":
                    gefunden.append((datei.name, ast.unparse(schluessel.value)))

    erlaubt = {
        ("cli.py", "False"),                     # cmd_ask
        ("__main__.py", "not (ask or chat)"),    # run(), der zweite Ladevorgang
    }
    unerwartet = [eintrag for eintrag in gefunden if eintrag not in erlaubt]
    assert not unerwartet, (
        f"require_channel wird an unerwarteter Stelle gelockert: {unerwartet}. "
        "Jede weitere Stelle muss hier bewusst eingetragen werden."
    )
    assert len(gefunden) >= 3, "die bekannten Aufrufstellen sind verschwunden"


def test_a_custom_base_url_makes_the_configured_model_authoritative(tmp_path, monkeypatch) -> None:
    """⚠️ Zeigt der Anbieter auf eine eigene Adresse, kennt kein Katalog seine Namen.

    `openai-api` gegen einen lokalen Ollama-Server ist genau dieser Fall: der eingebaute
    Katalog fuehrt OpenAIs Namen, der Server bietet `qwen3.5:27b-int4`. Ohne diese
    Ausnahme fiele die Wahl still auf ein OpenAI-Modell zurueck — und Talos spraeche mit
    dem lokalen Server unter einem Namen, den der gar nicht hat.
    """
    log = EventLog(tmp_path / "ev.db")
    katalog = _Katalog(_Anbieter("openai-api", ("gpt-5.5",)))
    monkeypatch.setenv("TALOS_BASE_URL_OPENAI_API", "http://localhost:11434/v1")

    gewaehlt = resolve_fallback(log, katalog, ModelSelection("openai-api", "qwen3.5:27b-int4"))

    assert gewaehlt == ModelSelection("openai-api", "qwen3.5:27b-int4")


def test_without_a_custom_base_url_the_catalogue_still_decides(tmp_path, monkeypatch) -> None:
    """Die Ausnahme haengt an einer bewussten Handlung — sonst gilt der Katalog."""
    log = EventLog(tmp_path / "ev.db")
    katalog = _Katalog(_Anbieter("openai-api", ("gpt-5.5",)))
    monkeypatch.delenv("TALOS_BASE_URL_OPENAI_API", raising=False)

    gewaehlt = resolve_fallback(log, katalog, ModelSelection("openai-api", "gibt-es-nicht"))

    assert gewaehlt == ModelSelection("openai-api", "gpt-5.5")
