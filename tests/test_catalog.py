from __future__ import annotations

import dataclasses

import pytest

from talos import catalog
from talos.api_reasoner import SUPPORTED_PROVIDERS
from talos.catalog import AUTH_KINDS, PROVIDERS, WIRE_KINDS, ProviderInfo

# Namentlich zugesagte Anbieter. Verschwindet einer beim Umbenennen, faellt es hier auf
# und nicht erst beim Nutzer, der ihn in seiner Konfiguration stehen hat.
# Nur die Anbieter, bei denen ein Drittanbieter-OAuth tatsaechlich zulaessig ist.
# Anthropic, Copilot, Codex, Gemini und Qwen standen hier ebenfalls — sie sind in den
# CLI-Weg verschoben, weil es untersagt, undokumentiert oder abgeschaltet ist. Warum
# genau, steht in `FORBIDDEN_OAUTH` weiter unten und in den `notes` der Eintraege.
PROMISED_OAUTH = (
    "nous-portal",
    "xai-grok-oauth",
    "minimax-oauth",
)
PROMISED_KEYED = (
    "openrouter", "openai-api", "anthropic-api", "google-gemini-api", "deepseek",
    "xai", "novita", "zai-glm", "kimi-moonshot", "kimi-china", "minimax",
    "minimax-china", "alibaba-dashscope", "alibaba-coding-plan", "tencent-tokenhub",
    "xiaomi-mimo", "nvidia-nim", "aws-bedrock", "azure-ai-foundry", "huggingface",
    "arcee-ai", "gmi-cloud", "stepfun", "kilo-code", "opencode-zen", "opencode-go",
    "ollama-cloud", "perplexity", "mistral", "cohere", "groq",
    "cloudflare-workers-ai", "together-ai", "fireworks-ai", "cerebras", "sambanova",
    "google-vertex-ai",
)
PROMISED_LOCAL = ("ollama", "lm-studio", "custom")
PROMISED_CLI = ("claude-cli", "hermes")


def test_slugs_are_unique() -> None:
    slugs = catalog.slugs()
    assert len(slugs) == len(set(slugs))
    assert len(slugs) == len(PROVIDERS)


def test_slugs_are_kebab_case() -> None:
    for provider in PROVIDERS:
        assert provider.slug == provider.slug.lower()
        assert provider.slug.replace("-", "").isalnum(), provider.slug


def test_auth_values_are_known() -> None:
    for provider in PROVIDERS:
        assert provider.auth in AUTH_KINDS, provider.slug


def test_wire_is_a_known_protocol_or_empty_for_cli() -> None:
    """Nur bei einer Hersteller-CLI darf das Protokoll fehlen — sie besitzt die Leitung."""
    for provider in PROVIDERS:
        if provider.wire:
            assert provider.wire in WIRE_KINDS, provider.slug
        else:
            assert provider.auth == "cli", provider.slug


def test_api_key_providers_name_an_env_variable() -> None:
    for provider in catalog.by_auth("api-key"):
        assert provider.env_key, provider.slug
        assert provider.env_key == provider.env_key.upper()


def test_oauth_local_and_cli_carry_no_env_key() -> None:
    """Ein Schluesselname bei OAuth/lokal/CLI waere eine Einladung, dort einen zu setzen."""
    for auth in ("oauth-device", "oauth-pkce", "local", "cli"):
        for provider in catalog.by_auth(auth):
            assert not provider.env_key, provider.slug


def test_base_urls_have_a_usable_scheme() -> None:
    """Eine falsche URL ist schlimmer als eine fehlende — mindestens das Schema muss stimmen."""
    for provider in PROVIDERS:
        if not provider.base_url:
            continue
        assert provider.base_url.startswith(("https://", "http://")), provider.slug
        assert not provider.base_url.endswith("/"), provider.slug
        assert " " not in provider.base_url, provider.slug


def test_only_local_providers_may_use_plain_http() -> None:
    for provider in PROVIDERS:
        if provider.base_url.startswith("http://"):
            assert provider.auth == "local", provider.slug


def test_every_provider_tells_the_user_what_is_needed() -> None:
    for provider in PROVIDERS:
        assert provider.notes.strip(), provider.slug
        assert provider.label.strip(), provider.slug


def test_get_returns_none_for_unknown_slug() -> None:
    assert catalog.get("does-not-exist") is None
    assert catalog.get("") is None
    assert catalog.get("OPENROUTER") is None


def test_get_returns_the_entry_for_a_known_slug() -> None:
    provider = catalog.get("openrouter")
    assert provider is not None
    assert provider.slug == "openrouter"
    assert provider.base_url == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("slug", PROMISED_OAUTH)
def test_oauth_providers_are_present(slug: str) -> None:
    provider = catalog.get(slug)
    assert provider is not None
    assert provider.auth in ("oauth-device", "oauth-pkce")


@pytest.mark.parametrize("slug", PROMISED_KEYED)
def test_api_key_providers_are_present(slug: str) -> None:
    provider = catalog.get(slug)
    assert provider is not None
    assert provider.auth == "api-key"


@pytest.mark.parametrize("slug", PROMISED_LOCAL)
def test_local_providers_are_present(slug: str) -> None:
    provider = catalog.get(slug)
    assert provider is not None
    assert provider.auth == "local"


@pytest.mark.parametrize("slug", PROMISED_CLI)
def test_cli_providers_are_present(slug: str) -> None:
    provider = catalog.get(slug)
    assert provider is not None
    assert provider.auth == "cli"


def test_by_auth_covers_every_provider_exactly_once() -> None:
    seen = [p.slug for kind in AUTH_KINDS for p in catalog.by_auth(kind)]
    assert sorted(seen) == sorted(catalog.slugs())


def test_by_auth_is_empty_for_an_unknown_kind() -> None:
    assert catalog.by_auth("magic") == ()


def test_by_wire_splits_the_two_protocols() -> None:
    anthropic = catalog.by_wire("anthropic")
    openai = catalog.by_wire("openai")
    assert {p.slug for p in anthropic} & {p.slug for p in openai} == set()
    # Der Grund fuer die Laenge des Katalogs: fast alles spricht OpenAI.
    assert len(openai) > len(anthropic)
    assert "anthropic-api" in {p.slug for p in anthropic}


def test_providers_without_models_are_marked_not_guessed() -> None:
    """Fehlende Modelle bleiben leer und sind abfragbar — nie mit erfundenen IDs gefuellt."""
    gaps = catalog.without_models()
    assert gaps, "der Katalog verschweigt seine Luecken"
    for provider in gaps:
        assert provider.models == ()
        assert not provider.has_models
    for provider in PROVIDERS:
        assert provider.has_models is bool(provider.models)


def test_known_gaps_stay_empty_rather_than_invented() -> None:
    for slug in ("tencent-tokenhub", "cloudflare-workers-ai", "custom", "ollama"):
        provider = catalog.get(slug)
        assert provider is not None
        assert provider.models == (), slug


def test_model_ids_are_non_empty_and_unique_per_provider() -> None:
    for provider in PROVIDERS:
        assert all(model.strip() for model in provider.models), provider.slug
        assert len(set(provider.models)) == len(provider.models), provider.slug


def test_anthropic_entries_use_current_model_ids() -> None:
    provider = catalog.get("anthropic-api")
    assert provider is not None
    assert provider.wire == "anthropic"
    assert "claude-opus-5" in provider.models
    for model in provider.models:
        assert model.startswith("claude-"), model


def test_slugs_of_the_two_api_paths_match_the_reasoner() -> None:
    """Der Katalog muss `ApiReasoner` speisen koennen, ohne dass dort etwas geaendert wird."""
    for slug in SUPPORTED_PROVIDERS:
        provider = catalog.get(slug)
        assert provider is not None, slug
        assert provider.wire in WIRE_KINDS


def test_entries_are_immutable() -> None:
    provider = catalog.get("deepseek")
    assert provider is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        provider.base_url = "https://evil.example/v1"  # type: ignore[misc]


def test_validation_rejects_a_duplicate_slug() -> None:
    entry = ProviderInfo("dupe", "Dupe", "local", "openai", notes="n")
    with pytest.raises(ValueError, match="duplicate slug"):
        catalog._check_all((entry, entry))


def test_validation_rejects_an_unknown_auth() -> None:
    entry = ProviderInfo("x", "X", "password", "openai", notes="n")
    with pytest.raises(ValueError, match="unknown auth"):
        catalog._check_all((entry,))


def test_validation_rejects_an_unknown_wire() -> None:
    entry = ProviderInfo("x", "X", "local", "grpc", notes="n")
    with pytest.raises(ValueError, match="unknown wire"):
        catalog._check_all((entry,))


def test_validation_rejects_a_missing_wire_outside_cli() -> None:
    entry = ProviderInfo("x", "X", "local", "", notes="n")
    with pytest.raises(ValueError, match="wire protocol"):
        catalog._check_all((entry,))


def test_validation_rejects_an_api_key_provider_without_env_key() -> None:
    entry = ProviderInfo("x", "X", "api-key", "openai", notes="n")
    with pytest.raises(ValueError, match="env_key"):
        catalog._check_all((entry,))


def test_validation_rejects_an_env_key_on_a_local_provider() -> None:
    entry = ProviderInfo("x", "X", "local", "openai", env_key="X_KEY", notes="n")
    with pytest.raises(ValueError, match="must not carry an env_key"):
        catalog._check_all((entry,))


def test_validation_rejects_a_schemeless_base_url() -> None:
    entry = ProviderInfo("x", "X", "local", "openai", base_url="api.example.com", notes="n")
    with pytest.raises(ValueError, match="scheme"):
        catalog._check_all((entry,))


def test_validation_rejects_an_empty_model_id() -> None:
    entry = ProviderInfo("x", "X", "local", "openai", models=("",), notes="n")
    with pytest.raises(ValueError, match="empty model id"):
        catalog._check_all((entry,))


def test_validation_rejects_a_repeated_model_id() -> None:
    entry = ProviderInfo("x", "X", "local", "openai", models=("a", "a"), notes="n")
    with pytest.raises(ValueError, match="twice"):
        catalog._check_all((entry,))


def test_validation_rejects_a_missing_note() -> None:
    entry = ProviderInfo("x", "X", "local", "openai")
    with pytest.raises(ValueError, match="note"):
        catalog._check_all((entry,))


def test_validation_rejects_an_empty_catalog() -> None:
    with pytest.raises(ValueError, match="empty"):
        catalog._check_all(())


def test_catalog_touches_no_network_or_filesystem() -> None:
    """Der Katalog ist Daten. Ein Import darf nichts oeffnen, nichts anmelden, nichts holen."""
    source = (catalog.__file__ or "")
    assert source.endswith("catalog.py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("import requests", "import os", "open(", "urllib", "subprocess"):
        assert forbidden not in text, forbidden


# --- Nutzungsbedingungen: die Regel gehoert in den Code, nicht in ein Gedaechtnis ---

FORBIDDEN_OAUTH = {
    "anthropic-max": (
        "Anthropic erlaubt Drittanbietern ausdruecklich nicht, Anfragen ueber Free-, Pro- "
        "oder Max-Zugaenge zu leiten. OpenCode hat seine Plugins deshalb entfernt."
    ),
    "github-copilot": (
        "Der rohe Endpunkt braucht unveroeffentlichte APIs, die Copilots "
        "Entwicklerrichtlinie namentlich verbietet."
    ),
    "openai-codex": (
        "Der Abo-Endpunkt steht in keiner oeffentlichen Doku, und OpenAI hat die Frage "
        "nach der Zulaessigkeit ausdruecklich nicht beantwortet."
    ),
    "google-gemini-oauth": "Googles Verbraucher-Pfad wurde am 2026-06-18 abgeschaltet.",
    "qwen-portal": "Die kostenlose Qwen-OAuth-Stufe endete am 2026-04-15.",
}


def test_subscription_routes_that_forbid_third_party_oauth_stay_on_the_cli() -> None:
    """Diese fuenf duerfen NIE `oauth-*` sein — und der Test sagt bei jedem, warum.

    Sie stehen alle in der Liste, die der Betreiber uns gegeben hat, und sie sehen dort
    wie OAuth-Anbieter aus. Sind sie aber nicht: bei vieren ist es untersagt oder
    abgeschaltet, beim fuenften hat der Hersteller die Frage offengelassen. Wer eine
    dieser Zeilen „ergaenzt", riskiert nicht das Projekt, sondern das Abo jedes Nutzers,
    der es einschaltet — deshalb steht die Regel hier und nicht nur in einer Notiz.
    """
    for slug, reason in FORBIDDEN_OAUTH.items():
        entry = catalog.get(slug)
        assert entry is not None, f"{slug} fehlt im Katalog"
        assert not entry.auth.startswith("oauth"), f"{slug}: {reason}"
        assert entry.auth == "cli", f"{slug} muss den Hersteller-CLI-Weg gehen: {reason}"


def test_the_forbidden_entries_say_why_in_their_own_notes() -> None:
    """Der Grund muss am Eintrag stehen, nicht nur im Test — sonst liest ihn niemand."""
    for slug in FORBIDDEN_OAUTH:
        notes = catalog.get(slug).notes.lower()
        assert any(word in notes for word in ("forbid", "not permit", "declined", "discontinued", "stopped")), (
            f"{slug} nennt den Grund nicht in seinen notes"
        )
