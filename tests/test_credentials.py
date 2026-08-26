"""Zugangsdaten gehoeren ihrem Anbieter — der Befund vom 05.08., festgenagelt.

Bis dahin trug `config.api_key` EINEN Wert fuer JEDEN Anbieter, gefuellt aus
`ANTHROPIC_API_KEY or OPENAI_API_KEY`. Wer den Anbieter auf `openai-api` stellte, schickte
seinen Anthropic-Schluessel als `Bearer` an api.openai.com. Kein Test sah es, weil die
Testbestaende beide Anbieter mit DEMSELBEN Wert versorgten: mit identischen Schluesseln
ist jede Zusicherung ueber „welcher Schluessel ging raus" grün.

Deshalb hat hier jeder Anbieter einen eigenen, wiedererkennbaren Wert. Ein Test, der den
Fehler nicht sehen KANN, ist keiner.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos.api_reasoner import ApiReasoner
from talos.credentials import (
    CredentialStore,
    MissingKey,
    Route,
    base_url_var,
    from_lookup,
    key_var,
)

ANTHROPIC = "sk-ant-api03-NUR-FUER-ANTHROPIC-111"
OPENAI = "sk-proj-NUR-FUER-OPENAI-222"


def beide() -> CredentialStore:
    return CredentialStore({
        "anthropic-api": Route("anthropic-api", ANTHROPIC),
        "openai-api": Route("openai-api", OPENAI),
    })


class FakeResponse:
    def __init__(self, lines: list[str]) -> None:
        self.status_code, self.text, self._lines = 200, "", lines

    def iter_lines(self, decode_unicode: bool = False):
        yield from self._lines

    def close(self) -> None:
        pass


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, timeout, stream):  # noqa: A002 — Vertragsname
        self.calls.append({"url": url, "headers": dict(headers)})
        return FakeResponse(['data: {"choices":[{"delta":{"content":"ok"}}]}', "data: [DONE]"])


# --- Der Befund selbst ----------------------------------------------------------------


def test_the_openai_route_never_carries_the_anthropic_key() -> None:
    """Der reproduzierte Fall: Anbieter `openai-api`, Anthropic-Schluessel im Bearer.

    Das ist keine Fehlkonfiguration mit Fehlermeldung, sondern die Weitergabe eines
    Geheimnisses an ein fremdes Unternehmen — und weil OpenAI mit 401 antwortet, sieht es
    aus wie ein Tippfehler statt wie ein Leck.
    """
    http = FakeHttp()
    reasoner = ApiReasoner("openai-api", "qwen3", beide(), timeout_s=30, http=http)
    reasoner.reason("Status?")

    kopf = http.calls[-1]["headers"]["authorization"]
    assert kopf == f"Bearer {OPENAI}"
    assert ANTHROPIC not in kopf


def test_the_anthropic_route_never_carries_the_openai_key() -> None:
    """Dieselbe Grenze in der Gegenrichtung — sonst haelt sie nur zufaellig."""
    http = FakeHttp()
    reasoner = ApiReasoner("anthropic-api", "claude-opus-5", beide(), timeout_s=30, http=http)
    reasoner.reason("Status?")

    kopf = http.calls[-1]["headers"]["x-api-key"]
    assert kopf == ANTHROPIC
    assert OPENAI not in str(http.calls[-1]["headers"])


def test_only_the_anthropic_key_exists_and_openai_still_gets_nothing() -> None:
    """Die genaue Ausgangslage des Befundes: EIN Schluessel im Haus, der falsche Anbieter.

    Frueher gewann `ANTHROPIC_API_KEY` hier bedingungslos. Jetzt entsteht der Weg gar
    nicht — und zwar bevor irgendetwas das Haus verlaesst.
    """
    nur_anthropic = CredentialStore({"anthropic-api": Route("anthropic-api", ANTHROPIC)})
    http = FakeHttp()

    with pytest.raises(ValueError) as gefangen:
        ApiReasoner("openai-api", "qwen3", nur_anthropic, timeout_s=30, http=http)

    assert http.calls == []                      # nichts gesendet, nicht einmal ein Versuch
    assert "OPENAI_API_KEY" in str(gefangen.value)
    assert ANTHROPIC not in str(gefangen.value)  # der Name, nie der Wert


# --- Aufloesung beim Aufruf, nicht beim Bauen -----------------------------------------


def test_the_key_is_resolved_per_call_not_frozen_at_construction() -> None:
    """`/model` wechselt den Anbieter im laufenden Prozess.

    Heute baut der Router dabei neu — diese Zusicherung haelt auch an dem Tag, an dem
    jemand den Neubau aus Geschwindigkeitsgruenden entfernt. Ein Schluessel im Objekt
    gehoert dem Anbieter von damals; ein aufgeloester gehoert dem, an den gerade
    gesprochen wird.
    """
    bestand = beide()
    http = FakeHttp()
    reasoner = ApiReasoner("openai-api", "qwen3", bestand, timeout_s=30, http=http)

    # Der Schluessel wird nachtraeglich ausgetauscht — ein eingefrorener Wert wuerde den
    # alten weiterschicken und diesen Test bestehen lassen, ohne ihn zu erfuellen.
    bestand.routes["openai-api"] = Route("openai-api", "sk-proj-NEU-333")
    reasoner.reason("Status?")

    assert http.calls[-1]["headers"]["authorization"] == "Bearer sk-proj-NEU-333"


def test_a_key_that_disappears_stops_the_next_turn_instead_of_sending_another() -> None:
    """Faellt der Schluessel weg, gibt es keinen Ersatz aus dem Nachbarfach."""
    bestand = beide()
    http = FakeHttp()
    reasoner = ApiReasoner("openai-api", "qwen3", bestand, timeout_s=30, http=http)
    del bestand.routes["openai-api"]

    with pytest.raises(MissingKey):
        reasoner.reason("Status?")
    assert http.calls == []


# --- Die Adresse ist genauso gebunden wie der Schluessel ------------------------------


def test_a_base_url_belongs_to_one_provider_only() -> None:
    """Eine Adresse fuer alle ist derselbe Fehler.

    Wer `TALOS_API_BASE_URL` fuer sein Anthropic-Gateway setzte und danach auf
    `openai-api` wechselte, schickte OpenAI-foermige Anfragen dorthin. Den Schluessel zu
    binden und die Adresse nicht, haette den Befund halbiert.
    """
    bestand = CredentialStore({
        "anthropic-api": Route("anthropic-api", ANTHROPIC, "https://gateway.example/anthropic"),
        "openai-api": Route("openai-api", OPENAI),
    })
    http = FakeHttp()
    ApiReasoner("openai-api", "qwen3", bestand, timeout_s=30, http=http).reason("Status?")

    assert http.calls[-1]["url"] == "https://api.openai.com/v1/chat/completions"
    assert "gateway.example" not in http.calls[-1]["url"]


def test_each_provider_has_its_own_base_url_variable() -> None:
    assert base_url_var("openai-api") == "TALOS_BASE_URL_OPENAI_API"
    assert base_url_var("anthropic-api") == "TALOS_BASE_URL_ANTHROPIC_API"
    assert base_url_var("openai-api") != base_url_var("anthropic-api")


def test_the_variable_names_come_from_the_catalogue() -> None:
    """Eine zweite Liste waere eine Liste, die driftet."""
    assert key_var("openai-api") == "OPENAI_API_KEY"
    assert key_var("anthropic-api") == "ANTHROPIC_API_KEY"
    assert key_var("claude-cli") == ""            # kein Schluesselweg, kein Name


# --- Maskierung -----------------------------------------------------------------------


def test_every_stored_key_is_masked_not_just_the_active_one() -> None:
    """Ein fremder Server spiegelt den Header zurueck, den er bekommen hat.

    Genau der Fall, gegen den `credentials.py` gebaut ist, waere sonst der eine, der
    ungeschwaerzt im Log steht — der falsch geroutete Schluessel.
    """
    class Spiegel:
        def post(self, url, *, headers, json, timeout, stream):  # noqa: A002
            raise OSError(f"upstream said: {ANTHROPIC} and {OPENAI}")

    reasoner = ApiReasoner("openai-api", "qwen3", beide(), timeout_s=30, http=Spiegel())
    antwort = reasoner.reason("Status?")
    assert ANTHROPIC not in antwort
    assert OPENAI not in antwort


# --- Aus der Umgebung gebaut ----------------------------------------------------------


def test_from_lookup_keeps_the_keys_apart() -> None:
    werte = {"ANTHROPIC_API_KEY": ANTHROPIC, "OPENAI_API_KEY": OPENAI}
    bestand = from_lookup(lambda name: werte.get(name, ""))

    assert bestand.route("anthropic-api").api_key == ANTHROPIC
    assert bestand.route("openai-api").api_key == OPENAI


def test_a_local_provider_gets_a_route_without_a_key() -> None:
    """Ollama braucht keinen Schluessel — seine Route ist eine reine Adresse.

    Frueher warf `route()` hier MissingKey, weil es nur Schluessel kannte; ein lokaler
    Anbieter ohne Schluessel ist keine Luecke, sondern der Normalfall.
    """
    bestand = from_lookup(lambda name: "")
    route = bestand.route("ollama")
    assert route.api_key == ""
    assert route.base_url == "http://localhost:11434/v1"
    assert not bestand.has("ollama")  # „hat einen Schluessel" bleibt wahrheitsgemäss


def test_a_local_providers_address_is_overridable() -> None:
    werte = {"TALOS_BASE_URL_OLLAMA": "http://andere-kiste:11434/v1/"}
    bestand = from_lookup(lambda name: werte.get(name, ""))
    assert bestand.route("ollama").base_url == "http://andere-kiste:11434/v1"


def test_a_keyed_provider_without_a_key_still_has_no_route() -> None:
    """Fail-closed gilt unveraendert: nvidia-nim ohne NVIDIA_API_KEY gibt keine Route."""
    bestand = from_lookup(lambda name: "")
    with pytest.raises(MissingKey) as gefangen:
        bestand.route("nvidia-nim")
    assert "NVIDIA_API_KEY" in str(gefangen.value)


def test_a_local_provider_without_any_entry_still_raises() -> None:
    """Ein handgebauter Bestand, der den lokalen Anbieter gar nicht kennt, ratet nicht."""
    with pytest.raises(MissingKey):
        beide().route("ollama")


def test_a_provider_without_a_key_is_absent_rather_than_empty() -> None:
    werte = {"ANTHROPIC_API_KEY": ANTHROPIC}
    bestand = from_lookup(lambda name: werte.get(name, ""))

    assert bestand.has("anthropic-api")
    assert not bestand.has("openai-api")
    with pytest.raises(MissingKey):
        bestand.route("openai-api")


def test_the_legacy_base_url_stops_the_start_instead_of_being_ignored(monkeypatch) -> None:
    """Still ignorieren waere ein Empfaengerwechsel ohne Ansage.

    Wer die alte anbieterlose Adresse gesetzt hat, wollte zu EINER bestimmten Maschine
    sprechen. Sie kommentarlos gegen die Vorgabe zu tauschen hiesse, seine Anfragen
    woanders hinzuschicken — leiser als der Befund selbst und darum schlimmer.
    """
    from talos import config as config_modul

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:testtoken")
    monkeypatch.setenv("TALOS_ALLOWED_PRINCIPALS", "telegram:1")
    monkeypatch.setenv("TALOS_API_BASE_URL", "https://gateway.example/v1")
    with pytest.raises(ValueError) as gefangen:
        config_modul.load_config()
    assert "TALOS_BASE_URL_" in str(gefangen.value)


# --- Zugangsdaten im Notizspeicher ----------------------------------------------------


def test_a_credentials_folder_in_the_vault_is_secret_wherever_it_sits(tmp_path, monkeypatch) -> None:
    """Frueher stand im Floor genau EIN ausgeschriebener Pfad.

    Der schuetzte eine einzige Ablage — wer seine Zugangsdaten einen Ordner daneben
    legte, hatte keinen Floor. Und der Pfad verriet nebenbei die Ablagestruktur seines
    Autors. Jetzt entscheidet der Ordnername, auf jeder Ebene.
    """
    import subprocess
    import sys

    speicher = tmp_path / "notizen"
    for pfad in ("credentials", "tief/verschachtelt/credentials", "secrets"):
        (speicher / pfad).mkdir(parents=True)
        (speicher / pfad / "zugang.md").write_text("token", encoding="utf-8")
    (speicher / "offen").mkdir()
    (speicher / "offen" / "notiz.md").write_text("harmlos", encoding="utf-8")

    # Eigener Prozess: `policy` liest den Speicherort beim Aufruf aus der Umgebung, aber
    # die Praefix-Tupel entstehen beim Import — ein `reload` hier riss schon einmal
    # 49 fremde Tests mit sich (neue Enum-Klassen bei alten Referenzen).
    programm = (
        "import json, sys\n"
        "from talos import policy\n"
        "print(json.dumps([policy._vault_secret(p) for p in sys.argv[1:]]))\n"
    )
    ziele = [
        str(speicher / "credentials" / "zugang.md"),
        str(speicher / "tief" / "verschachtelt" / "credentials" / "zugang.md"),
        str(speicher / "secrets" / "zugang.md"),
        str(speicher / "offen" / "notiz.md"),
        str(tmp_path / "woanders" / "credentials" / "x.md"),
    ]
    ergebnis = subprocess.run(
        [sys.executable, "-c", programm, *ziele],
        capture_output=True, text=True,
        env={"TALOS_VAULT_DIR": str(speicher), "PATH": "/usr/bin:/bin",
             "HOME": str(tmp_path), "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    assert ergebnis.returncode == 0, ergebnis.stderr
    treffer = json.loads(ergebnis.stdout)

    assert treffer[:3] == [True, True, True]   # jede Ebene, beide Namen
    assert treffer[3] is False                 # eine gewoehnliche Notiz bleibt lesbar
    assert treffer[4] is False                 # ausserhalb des Speichers gilt die Regel nicht
