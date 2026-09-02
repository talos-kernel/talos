from __future__ import annotations

from pathlib import Path
import threading

import pytest

from talos.channel import CallbackQuery, Principal
from talos.eventlog import EventLog
from talos.provider import (
    HermesCatalogLoader,
    ModelPicker,
    ModelRouter,
    ModelSelection,
    SwitchResult,
    Provider,
    ProviderRegistry,
    restore_selection,
    resolve_fallback,
    safe_talos_registry,
    with_local_provider,
)

OWNER = Principal("telegram", "7")
CHAT = "telegram:7"


class FakeReasoner:
    def __init__(self, selection: ModelSelection) -> None:
        self.selection = selection
        self.cancelled = False

    def reason(self, prompt: str) -> str:
        return f"{self.selection.provider}/{self.selection.model}: {prompt}"

    def cancel(self) -> bool:
        self.cancelled = True
        return True


def registry() -> ProviderRegistry:
    return ProviderRegistry(
        (
            Provider("alpha", "Alpha", tuple(f"model-{n}" for n in range(10))),
            Provider("beta", "Beta", ("small", "large")),
            Provider("gamma", "Gamma", ("one",)),
        )
    )


def test_registry_validates_exact_provider_and_model() -> None:
    reg = registry()
    assert reg.selection("alpha", "model-0") == ModelSelection("alpha", "model-0")
    with pytest.raises(ValueError):
        reg.selection("Alpha", "model-0")
    with pytest.raises(ValueError):
        reg.selection("alpha", "missing")


def test_registry_rejects_duplicates_and_empty_models() -> None:
    with pytest.raises(ValueError):
        ProviderRegistry((Provider("p", "P", ("m",)), Provider("p", "Other", ("x",))))
    with pytest.raises(ValueError):
        ProviderRegistry((Provider("p", "P", ()),))


def test_catalog_loader_executes_real_dataclass_style_modules(tmp_path: Path) -> None:
    catalog = tmp_path / "provider_catalog.py"
    models = tmp_path / "models.py"
    catalog.write_text(
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class Entry:\n    slug: str\n    label: str\n"
        "def provider_catalog(): return [Entry('alpha', 'Alpha')]\n",
        encoding="utf-8",
    )
    models.write_text(
        "def provider_model_ids(slug): return ['m1', 'm2'] if slug == 'alpha' else []\n",
        encoding="utf-8",
    )
    loaded = HermesCatalogLoader(catalog, models).load()
    assert loaded.selection("alpha", "m2") == ModelSelection("alpha", "m2")


def test_catalog_loader_without_hermes_files_is_absent_not_fatal(tmp_path: Path) -> None:
    """Gemessen 02.09.: eine frische Installation ohne Hermes-Checkout starb bei jedem
    Start an „catalog helper not found". Die Vorgabe-Pfade duerfen fehlen — nur
    `load()` (der Weg fuer eine vom Betreiber gesetzte Datei) bleibt laut."""
    loader = HermesCatalogLoader(tmp_path / "nein" / "provider_catalog.py", tmp_path / "nein" / "models.py")
    assert loader.load_if_present() is None
    with pytest.raises(ValueError, match="catalog helper not found"):
        loader.load()


def test_safe_registry_without_hermes_still_has_the_built_in_ways() -> None:
    """Die API-Wege und die Claude-CLI stehen auch ohne Hermes-Katalog im Katalog —
    das war immer die Absicht, kam aber nie an die Reihe, weil der Loader vorher warf."""
    registry = safe_talos_registry(None)
    slugs = {provider.slug for provider in registry.providers}
    assert {"claude-cli", "anthropic-api", "openai-api"} <= slugs
    assert registry.get("openai-api").models


def test_with_local_provider_makes_the_configured_local_model_known() -> None:
    """`ollama` mit dem einen Modell, das der Betreiber genannt hat — ohne Hermes."""
    registry = safe_talos_registry(None)
    with pytest.raises(ValueError, match="unknown provider"):
        registry.get("ollama")

    seeded = with_local_provider(registry, ModelSelection("ollama", "qwen3.5:0.8b"))
    assert seeded.get("ollama").models == ("qwen3.5:0.8b",)
    assert seeded.selection("ollama", "qwen3.5:0.8b") == ModelSelection("ollama", "qwen3.5:0.8b")
    # Die eingebauten Wege bleiben davor, unveraendert.
    assert [p.slug for p in seeded.providers][:-1] == [p.slug for p in registry.providers]


def test_with_local_provider_changes_nothing_for_hosted_or_known_or_empty() -> None:
    registry = safe_talos_registry(None)
    # Ein gehosteter Anbieter ausserhalb des Katalogs wird NICHT erfunden.
    assert with_local_provider(registry, ModelSelection("openrouter", "x")) is registry
    # Ohne Modellnamen gibt es nichts, was bekannt sein koennte.
    assert with_local_provider(registry, ModelSelection("ollama", "")) is registry
    # Kennt der Katalog (etwa aus Hermes) den Anbieter schon, bleibt seine Liste.
    known = ProviderRegistry((*registry.providers, Provider("ollama", "Ollama (local)", ("a", "b"))))
    assert with_local_provider(known, ModelSelection("ollama", "c")) is known


def test_resolve_fallback_keeps_the_configured_local_model(tmp_path: Path) -> None:
    """Der Weg von `ollama launch talos` / `llmman launch talos`: Anbieter ollama,
    Modell aus der Konfiguration, kein Hermes — die Wahl bleibt, kein Rueckfall."""
    log = EventLog(tmp_path / "events.db")
    wanted = ModelSelection("ollama", "docker.io/ai/qwen3.5:0.8b")
    registry = with_local_provider(safe_talos_registry(None), wanted)
    assert resolve_fallback(log, registry, wanted) == wanted
    assert not log.recent(1, ("model.fallback",))


def test_router_switches_actual_reasoner_and_logs_selection(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    built: list[ModelSelection] = []

    def build(selection: ModelSelection) -> FakeReasoner:
        built.append(selection)
        return FakeReasoner(selection)

    router = ModelRouter(registry(), ModelSelection("beta", "small"), build, log)
    assert router.reason("hello").startswith("beta/small")

    result = router.select("alpha", "model-3", principal=OWNER)

    assert result.ok is True
    assert router.reason("hello").startswith("alpha/model-3")
    assert built[-1] == ModelSelection("alpha", "model-3")
    event = log.recent(1, ("model.selected",))[0]
    assert event["payload"]["provider"] == "alpha"
    assert event["payload"]["model"] == "model-3"
    assert event["payload"]["principal"] == str(OWNER)


def test_running_reasoner_cannot_be_swapped_and_cancel_hits_that_exact_instance(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    built: list[FakeReasoner] = []

    class BlockingReasoner(FakeReasoner):
        def reason(self, prompt: str) -> str:
            started.set()
            release.wait(2)
            return "cancelled" if self.cancelled else prompt

        def cancel(self) -> bool:
            self.cancelled = True
            release.set()
            return True

    def build(selection: ModelSelection) -> BlockingReasoner:
        value = BlockingReasoner(selection)
        built.append(value)
        return value

    router = ModelRouter(registry(), ModelSelection("beta", "small"), build, EventLog(tmp_path / "e.db"))
    run = threading.Thread(target=lambda: router.reason("work"))
    run.start()
    assert started.wait(1)
    switched = router.select("alpha", "model-1", principal=OWNER)
    assert switched.ok is False
    assert "busy" in switched.error
    assert router.cancel() is True
    run.join(1)
    assert not run.is_alive()
    assert built[0].cancelled is True
    assert router.current == ModelSelection("beta", "small")


def test_cancel_reaches_candidate_validation_and_prevents_swap(tmp_path: Path) -> None:
    validating = threading.Event()
    release = threading.Event()

    class Candidate(FakeReasoner):
        def validate(self) -> None:
            validating.set()
            release.wait(2)

        def cancel(self) -> bool:
            self.cancelled = True
            release.set()
            return True

    def build(selection: ModelSelection) -> FakeReasoner:
        return Candidate(selection) if selection.model == "large" else FakeReasoner(selection)

    router = ModelRouter(registry(), ModelSelection("beta", "small"), build, EventLog(tmp_path / "e.db"))
    result: list[object] = []
    switching = threading.Thread(
        target=lambda: result.append(router.select("beta", "large", principal=OWNER))
    )
    switching.start()
    assert validating.wait(1)
    assert router.cancel() is True
    switching.join(1)
    assert not switching.is_alive()
    assert result and result[0].ok is False  # type: ignore[union-attr]
    assert router.current == ModelSelection("beta", "small")


def test_persistence_failure_keeps_old_runtime_selection() -> None:
    class FailingLog:
        def append(self, event) -> bool:
            raise OSError("disk full")

    router = ModelRouter(
        registry(), ModelSelection("beta", "small"), FakeReasoner, FailingLog()  # type: ignore[arg-type]
    )
    result = router.select("alpha", "model-2", principal=OWNER)
    assert result.ok is False
    assert "disk full" in result.error
    assert router.current == ModelSelection("beta", "small")
    assert router.reason("still old").startswith("beta/small")


def test_invalid_restored_reasoner_validates_and_falls_back_at_startup(tmp_path: Path) -> None:
    class Probe(FakeReasoner):
        def validate(self) -> None:
            if self.selection == ModelSelection("alpha", "model-1"):
                raise RuntimeError("expired oauth")

    log = EventLog(tmp_path / "e.db")
    router = ModelRouter(
        registry(),
        ModelSelection("alpha", "model-1"),
        Probe,
        log,
        fallback=ModelSelection("beta", "small"),
    )
    assert router.current == ModelSelection("beta", "small")
    event = log.recent(1, ("model.restore_failed",))[0]
    assert event["payload"]["model"] == "model-1"


def test_failed_validation_is_a_noop(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")

    class BrokenProbe(FakeReasoner):
        def validate(self) -> None:
            if self.selection.model == "large":
                raise RuntimeError("auth failed")

    router = ModelRouter(registry(), ModelSelection("beta", "small"), BrokenProbe, log)
    result = router.select("beta", "large", principal=OWNER)
    assert result.ok is False
    assert router.current == ModelSelection("beta", "small")
    assert log.recent(1, ("model.selected",)) == []


def test_failed_or_invalid_switch_is_a_noop(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")

    def build(selection: ModelSelection) -> FakeReasoner:
        if selection.model == "large":
            raise RuntimeError("backend unavailable")
        return FakeReasoner(selection)

    router = ModelRouter(registry(), ModelSelection("beta", "small"), build, log)
    assert not router.select("beta", "missing", principal=OWNER).ok
    assert not router.select("beta", "large", principal=OWNER).ok
    assert router.current == ModelSelection("beta", "small")
    assert log.recent(1, ("model.selected",)) == []


def test_latest_valid_selection_is_restored_and_invalid_event_falls_back(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    fallback = ModelSelection("beta", "small")
    router = ModelRouter(registry(), fallback, FakeReasoner, log)
    assert router.select("alpha", "model-1", principal=OWNER).ok
    assert router.select("alpha", "model-2", principal=OWNER).ok
    assert restore_selection(log, registry(), fallback) == ModelSelection("alpha", "model-2")

    # A stale catalog entry fails closed to the configured fallback.
    log.append_model_selection_for_test = None  # type: ignore[attr-defined]
    from talos.eventlog import Event
    log.append(Event("bad", "human", "model.selected", {"provider": "gone", "model": "x"}))
    assert restore_selection(log, registry(), fallback) == fallback


def test_picker_provider_grid_and_paginated_model_list(tmp_path: Path) -> None:
    now = [100.0]
    router = ModelRouter(registry(), ModelSelection("beta", "small"), FakeReasoner, EventLog(tmp_path / "e.db"))
    picker = ModelPicker(registry(), router, ttl_s=60, clock=lambda: now[0], token_factory=lambda: "tok")

    top = picker.open(principal=OWNER, conversation=CHAT)
    assert [len(row) for row in top.keyboard[:-1]] == [2, 1]
    assert top.keyboard[-1][0].label.endswith("Cancel")
    assert all(len(button.data.encode()) <= 64 for row in top.keyboard for button in row)

    models = picker.handle(top.keyboard[0][0].data, principal=OWNER, conversation=CHAT)
    assert len([button for row in models.keyboard[:4] for button in row]) == 8
    assert "1–8 of 10" in models.text
    next_button = next(b for row in models.keyboard for b in row if "Next" in b.label)
    page_two = picker.handle(next_button.data, principal=OWNER, conversation=CHAT)
    assert "9–10 of 10" in page_two.text
    assert any(b.label.endswith("Back") for row in page_two.keyboard for b in row)


def test_picker_releases_state_lock_during_model_validation() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingRouter:
        current = ModelSelection("beta", "small")

        def select(self, provider: str, model: str, *, principal: Principal) -> SwitchResult:
            started.set()
            assert release.wait(2)
            self.current = ModelSelection(provider, model)
            return SwitchResult(True, self.current)

    tokens = iter(("tok1", "tok2"))
    picker = ModelPicker(registry(), BlockingRouter(), token_factory=lambda: next(tokens))  # type: ignore[arg-type]
    top = picker.open(principal=OWNER, conversation=CHAT)
    page = picker.handle(top.keyboard[0][0].data, principal=OWNER, conversation=CHAT)
    selection: dict[str, object] = {}
    selecting = threading.Thread(
        target=lambda: selection.setdefault(
            "message",
            picker.handle(page.keyboard[0][0].data, principal=OWNER, conversation=CHAT),
        )
    )
    selecting.start()
    assert started.wait(1)

    opened = threading.Event()
    navigation = threading.Thread(
        target=lambda: (picker.open(principal=OWNER, conversation=CHAT), opened.set())
    )
    navigation.start()
    assert opened.wait(1), "picker lock remained held during model validation"

    release.set()
    selecting.join(2)
    navigation.join(2)
    assert not selecting.is_alive()
    assert not navigation.is_alive()


def test_picker_selection_switches_and_expired_or_foreign_callback_does_not(tmp_path: Path) -> None:
    now = [100.0]
    router = ModelRouter(registry(), ModelSelection("beta", "small"), FakeReasoner, EventLog(tmp_path / "e.db"))
    picker = ModelPicker(registry(), router, ttl_s=10, clock=lambda: now[0], token_factory=lambda: "tok")
    top = picker.open(principal=OWNER, conversation=CHAT)
    model_page = picker.handle(top.keyboard[0][0].data, principal=OWNER, conversation=CHAT)
    stranger = Principal("telegram", "8")
    selected = model_page.keyboard[0][0].data

    denied = picker.handle(selected, principal=stranger, conversation=CHAT)
    assert "expired" in denied.text.lower() or "invalid" in denied.text.lower()
    assert router.current == ModelSelection("beta", "small")

    now[0] = 111.0
    expired = picker.handle(selected, principal=OWNER, conversation=CHAT)
    assert "expired" in expired.text.lower()
    assert router.current == ModelSelection("beta", "small")


def test_picker_refuses_switch_while_reasoner_is_busy(tmp_path: Path) -> None:
    router = ModelRouter(registry(), ModelSelection("beta", "small"), FakeReasoner, EventLog(tmp_path / "e.db"))
    picker = ModelPicker(registry(), router, can_select=lambda: False, token_factory=lambda: "tok")
    typed = picker.select_typed("alpha model-2", principal=OWNER)
    assert "running" in typed.text.lower()
    top = picker.open(principal=OWNER, conversation=CHAT)
    page = picker.handle(top.keyboard[0][0].data, principal=OWNER, conversation=CHAT)
    selected = picker.handle(page.keyboard[0][0].data, principal=OWNER, conversation=CHAT)
    assert "running" in selected.text.lower()
    assert router.current == ModelSelection("beta", "small")


def test_picker_typed_switch_requires_exact_two_arguments(tmp_path: Path) -> None:
    router = ModelRouter(registry(), ModelSelection("beta", "small"), FakeReasoner, EventLog(tmp_path / "e.db"))
    picker = ModelPicker(registry(), router)
    bad = picker.select_typed("alpha model-2 extra", principal=OWNER)
    assert "usage" in bad.text.lower()
    assert router.current == ModelSelection("beta", "small")
    good = picker.select_typed("alpha model-2", principal=OWNER)
    assert "model-2" in good.text
    assert router.current == ModelSelection("alpha", "model-2")


def test_safe_registry_routes_claude_only_through_cli_and_blocks_antigravity() -> None:
    raw = ProviderRegistry((
        Provider("openai-codex", "Codex", ("gpt",)),
        Provider("anthropic", "Anthropic", ("claude-fable-5", "claude-sonnet-5")),
        Provider("openrouter", "OpenRouter", ("openai/gpt-5", "anthropic/claude-opus-4-8")),
        Provider("bedrock", "Bedrock", ("anthropic.claude-sonnet-v1",)),
        Provider("copilot", "Copilot", ("gpt-5", "claude-haiku-4-5")),
        Provider("google-antigravity", "Antigravity", ("gemini",)),
    ))
    safe = safe_talos_registry(raw)
    slugs = tuple(provider.slug for provider in safe.providers)
    assert "anthropic" not in slugs
    assert "google-antigravity" not in slugs
    assert "bedrock" not in slugs
    assert safe.get("openrouter").models == ("openai/gpt-5",)
    assert safe.get("copilot").models == ("gpt-5",)
    # Kein aus dem Hermes-Katalog uebernommener Anbieter fuehrt Claude-Modelle: die
    # gehen ueber die gehaertete CLI-Anmeldung, damit nichts still ueber fremdes
    # API-Guthaben laeuft. `anthropic-api` ist die eine ausdrueckliche Ausnahme und
    # kommt nicht aus dem Katalog, sondern wird bewusst ergaenzt — mit dem EIGENEN
    # Schluessel des Betreibers. Ohne gesetzten Schluessel wirft der Reasoner beim Bauen,
    # es gibt also keinen stillen Weg in eine Abrechnung.
    assert all(
        not any(marker in model.casefold() for marker in ("anthropic", "claude", "sonnet", "opus", "haiku", "fable"))
        for provider in safe.providers
        if provider.slug not in {"claude-cli", "anthropic-api"}
        for model in provider.models
    )
    assert safe.get("anthropic-api").models == safe.get("claude-cli").models
    assert safe.get("claude-cli") == Provider(
        "claude-cli",
        "Anthropics Max (CLI OAuth)",
        ("claude-fable-5", "claude-sonnet-5"),
    )


def test_safe_registry_survives_a_catalog_that_already_knows_openai_api() -> None:
    """Neuere Hermes-Kataloge fuehren `openai-api` selbst. Der blinde Anhang erzeugte
    dann einen doppelten Slug — und `ProviderRegistry.__init__` liess den AGENTEN-START
    daran scheitern. Ein gewoehnliches Hermes-Update haette den Waechter getoetet; auf
    dem Mac ist genau das beim e2e-Lauf passiert. Der Katalog-Eintrag gewinnt, weil er
    die Modelle des tatsaechlich installierten Hermes kennt."""
    raw = ProviderRegistry((
        Provider("openai-codex", "Codex", ("gpt-5.6-sol",)),
        Provider("openai-api", "OpenAI API (from catalog)", ("gpt-5.2", "o4-mini")),
    ))
    safe = safe_talos_registry(raw)
    slugs = [provider.slug for provider in safe.providers]
    assert slugs.count("openai-api") == 1
    assert safe.get("openai-api").models == ("gpt-5.2", "o4-mini")
    # Die feste Ergaenzung bleibt fuer Kataloge OHNE den Eintrag bestehen.
    bare = safe_talos_registry(ProviderRegistry((Provider("openai-codex", "Codex", ("gpt-5.6-sol",)),)))
    assert bare.get("openai-api") is not None
    assert bare.get("anthropic-api") is not None


def test_the_api_route_refuses_to_exist_without_a_key() -> None:
    """Der API-Weg darf nie still in eine Abrechnung fuehren.

    Claude-Modelle liefen bisher ausschliesslich ueber die CLI-Anmeldung, damit kein
    Aufruf unbemerkt fremdes API-Guthaben verbraucht. Der neue `anthropic-api`-Weg ist die
    bewusste Ausnahme fuer die oeffentliche Fassung — tragbar nur, weil er ohne
    ausdruecklich gesetzten Schluessel gar nicht erst entsteht.
    """
    import pytest

    from talos.api_reasoner import ApiReasoner
    from talos.credentials import CredentialStore

    with pytest.raises(ValueError):
        ApiReasoner("anthropic-api", "claude-opus-5", CredentialStore(), timeout_s=30)


def test_a_silent_model_switch_leaves_a_trace(tmp_path) -> None:
    """Faellt die Modellwahl zurueck, muss es im Log stehen — sonst merkt es niemand.

    Der Rueckfall selbst ist richtig: eine Auswahl, die der Katalog nicht mehr kennt,
    darf den Start nicht verhindern. Still darf er nicht sein. Verschwindet ein
    Modellname (Hermes-Update, Katalog nicht ladbar), lief der Agent danach mit einem
    ANDEREN Modell weiter — ohne Meldung und ohne Spur. Wer seine Wahl bewusst
    festgelegt hat, haette es erst gemerkt, wenn die Antworten sich anders anfuehlen.
    """
    from talos.eventlog import Event, EventLog
    from talos.provider import ModelSelection, Provider, ProviderRegistry, restore_selection

    log = EventLog(tmp_path / "ev.db")
    log.append(Event("r1", "human", "model.selected",
                     {"provider": "alpha", "model": "verschwundenes-modell"}))
    registry = ProviderRegistry((Provider("alpha", "Alpha", ("bleibt",)),))

    used = restore_selection(log, registry, ModelSelection("alpha", "bleibt"))

    assert used.model == "bleibt"
    traces = [e for e in log.recent(20) if e["type"] == "model.restore_failed"]
    assert traces, "der Wechsel passierte still"
    assert "verschwundenes-modell" in traces[0]["payload"]["wanted"]
    assert traces[0]["payload"]["used"] == "alpha/bleibt"


def test_a_restored_selection_that_still_exists_leaves_no_alarm(tmp_path) -> None:
    """Kein Fehlalarm im Normalfall — sonst gewoehnt man sich den Beleg ab."""
    from talos.eventlog import Event, EventLog
    from talos.provider import ModelSelection, Provider, ProviderRegistry, restore_selection

    log = EventLog(tmp_path / "ev.db")
    log.append(Event("r1", "human", "model.selected", {"provider": "alpha", "model": "bleibt"}))
    registry = ProviderRegistry((Provider("alpha", "Alpha", ("bleibt", "andere")),))

    used = restore_selection(log, registry, ModelSelection("alpha", "andere"))

    assert used.model == "bleibt"
    assert not [e for e in log.recent(20) if e["type"] == "model.restore_failed"]


def test_an_empty_event_log_does_not_change_the_model_without_a_trace(tmp_path) -> None:
    """Der Fall nach einem Update, bei dem `data/` nicht mitkam: ohne Eintrag greift die
    Vorgabe — und eine bewusst eingestellte Installation liefe danach mit einem anderen
    Modell. Der Rueckfall bleibt moeglich, aber nie stumm."""
    from talos.eventlog import EventLog
    from talos.provider import ModelSelection, restore_selection

    log = EventLog(tmp_path / "leer.db")
    kat = registry()
    gewaehlt = restore_selection(log, kat, ModelSelection("beta", "large"))

    assert gewaehlt.model == "large"
    belege = log.recent(5, ("model.restore_failed",))
    assert belege, "der Rueckfall wurde nicht belegt"
    assert "no model.selected" in belege[-1]["payload"]["error"]
