"""Provider/model registry, reasoner router, and server-side picker state."""
from __future__ import annotations

import importlib.util
import os
import secrets
import sys
import inspect
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable

from .channel import Button, Principal, StructuredMessage
from .eventlog import Event, EventLog, new_run_id
from .stream import OnText

MODEL_PAGE_SIZE = 8
CALLBACK_MAX_BYTES = 64


@dataclass(frozen=True)
class Provider:
    slug: str
    label: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str


class ProviderRegistry:
    """Immutable exact-match catalog. It never guesses a provider or model."""

    def __init__(self, providers: Iterable[Provider]) -> None:
        ordered = tuple(providers)
        if not ordered:
            raise ValueError("provider catalog is empty")
        slugs = [provider.slug for provider in ordered]
        if any(not slug for slug in slugs) or len(slugs) != len(set(slugs)):
            raise ValueError("provider slugs must be non-empty and unique")
        for provider in ordered:
            if not provider.label or not provider.models:
                raise ValueError(f"provider {provider.slug!r} has no label or models")
            if any(not model for model in provider.models) or len(provider.models) != len(set(provider.models)):
                raise ValueError(f"models for {provider.slug!r} must be non-empty and unique")
        self._providers = ordered
        self._by_slug = {provider.slug: provider for provider in ordered}

    @property
    def providers(self) -> tuple[Provider, ...]:
        return self._providers

    def get(self, slug: str) -> Provider:
        provider = self._by_slug.get(slug)
        if provider is None:
            raise ValueError(f"unknown provider: {slug}")
        return provider

    def selection(self, provider: str, model: str) -> ModelSelection:
        found = self.get(provider)
        if model not in found.models:
            raise ValueError(f"unknown model for {provider}: {model}")
        return ModelSelection(provider, model)


@dataclass(frozen=True)
class SwitchResult:
    ok: bool
    selection: ModelSelection
    error: str = ""


def _takes_sink(reason: Callable[..., str]) -> bool:
    """Fuehrt diese `reason`-Implementierung einen benannten `on_text`-Parameter?

    Nur der ausdruecklich benannte Parameter zaehlt: ein `**kwargs`-Wrapper wuerde
    Unterstuetzung behaupten und beim Aufruf scheitern.
    """
    try:
        return "on_text" in inspect.signature(reason).parameters
    except (TypeError, ValueError):
        return False


class ModelRouter:
    """Serializes validation, persistence, swapping, reasoning, and cancellation."""

    def __init__(
        self,
        registry: ProviderRegistry,
        initial: ModelSelection,
        build: Callable[[ModelSelection], object],
        log: EventLog,
        *,
        fallback: ModelSelection | None = None,
    ) -> None:
        self._registry = registry
        self._build = build
        self._log = log
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._switching = False
        self._switch_cancelled = False
        self._active_reasoner: object | None = None
        self._validating_reasoner: object | None = None

        selected = registry.selection(initial.provider, initial.model)
        try:
            reasoner = self._build_validated(selected)
        except Exception as error:
            if fallback is None:
                raise
            safe = registry.selection(fallback.provider, fallback.model)
            if safe == selected:
                raise
            self._log.append(
                Event(
                    new_run_id(), "system", "model.restore_failed",
                    {"provider": selected.provider, "model": selected.model, "error": str(error)[:240]},
                )
            )
            selected = safe
            reasoner = self._build_validated(selected)
        self._current = selected
        self._reasoner = reasoner

    def _build_validated(self, selection: ModelSelection) -> object:
        reasoner = self._build(selection)
        validate = getattr(reasoner, "validate", None)
        if callable(validate):
            validate()
        return reasoner

    @property
    def current(self) -> ModelSelection:
        with self._lock:
            return self._current

    def can_select(self) -> bool:
        with self._lock:
            return self._active_reasoner is None and not self._switching

    def reason(self, prompt: str, on_text: OnText | None = None) -> str:
        """Reicht die Text-Senke an den aktiven Reasoner durch — falls der sie kennt.

        Der Router steht zwischen Conductor und Reasoner. Nahm er `on_text` nicht an, war
        das ganze Streaming still wirkungslos: der Conductor bot eine Senke an, die nie
        ankam, und niemand hat es gemerkt, weil die Antwort ja korrekt blieb.

        Weitergereicht wird nur an eine `reason`-Implementierung, die den Parameter
        ausdruecklich fuehrt. Hermes tut das nicht — dort laeuft es wie bisher, statt am
        unerwarteten Argument zu scheitern. Dieselbe fail-closed-Regel wie im Conductor:
        im Zweifel nicht streamen, nie die Antwort riskieren.
        """
        with self._condition:
            while self._switching:
                self._condition.wait()
            if self._active_reasoner is not None:
                raise RuntimeError("Reasoner laeuft bereits")
            reasoner = self._reasoner
            self._active_reasoner = reasoner
        try:
            # `reason_strict` zuerst: ein Reasoner, der seine Fehler klassifiziert
            # (ApiReasoner), soll sie als Ausnahme nach oben geben, damit eine davor
            # haengende Fallback-Kette die Art kennt. Ohne Kette faengt sie der Aufrufer
            # und liefert exakt denselben Text wie bisher — der Vertrag aendert sich
            # nur fuer den, der ihn brauchen kann.
            method = getattr(reasoner, "reason_strict", None) or getattr(reasoner, "reason")
            if on_text is not None and _takes_sink(method):
                return str(method(prompt, on_text=on_text))
            return str(method(prompt))
        finally:
            with self._condition:
                if self._active_reasoner is reasoner:
                    self._active_reasoner = None
                self._condition.notify_all()

    def cancel(self) -> bool:
        with self._lock:
            switching = self._switching
            if switching:
                self._switch_cancelled = True
            reasoner = self._validating_reasoner or self._active_reasoner
        if reasoner is None:
            return switching
        method = getattr(reasoner, "cancel", None)
        stopped = bool(method()) if method is not None else False
        return stopped or switching

    def select(self, provider: str, model: str, *, principal: Principal) -> SwitchResult:
        try:
            target = self._registry.selection(provider, model)
        except ValueError as error:
            return SwitchResult(False, self.current, str(error))
        with self._condition:
            if self._active_reasoner is not None or self._switching:
                return SwitchResult(False, self._current, "reasoner busy")
            self._switching = True
            self._switch_cancelled = False

        candidate: object | None = None
        try:
            candidate = self._build(target)
            with self._lock:
                self._validating_reasoner = candidate
                cancelled = self._switch_cancelled
            if cancelled:
                raise RuntimeError("model switch cancelled")
            validate = getattr(candidate, "validate", None)
            if callable(validate):
                validate()
            with self._lock:
                if self._switch_cancelled:
                    raise RuntimeError("model switch cancelled")

            # Persist first while new reasoning is blocked by _switching. Holding the
            # same lock across append+swap prevents cancel from splitting the pair.
            with self._condition:
                if self._switch_cancelled:
                    raise RuntimeError("model switch cancelled")
                self._log.append(
                    Event(
                        new_run_id(),
                        "human",
                        "model.selected",
                        {
                            "provider": target.provider,
                            "model": target.model,
                            "principal": str(principal),
                        },
                    )
                )
                self._reasoner = candidate
                self._current = target
                self._validating_reasoner = None
                self._switching = False
                self._switch_cancelled = False
                self._condition.notify_all()
            return SwitchResult(True, target)
        except Exception as error:
            return SwitchResult(False, self.current, f"reasoner unavailable: {error}")
        finally:
            with self._condition:
                if self._validating_reasoner is candidate:
                    self._validating_reasoner = None
                self._switching = False
                self._switch_cancelled = False
                self._condition.notify_all()


def resolve_fallback(log: EventLog, registry, wanted: ModelSelection) -> ModelSelection:
    """Macht aus der EINGESTELLTEN Vorgabe eine, die dieser Katalog wirklich kennt.

    ⚠️ Warum das noetig ist: auf einer FRISCHEN Installation warf `talos ask` beim
    allerersten Aufruf `unknown model for anthropic-api: claude-opus-5` — als
    unbehandelter Traceback. Der Katalog kommt aus Hermes-Dateien; ohne sie bleibt der
    kleine eingebaute, und der kennt die Vorgabe nicht. Die Sicherung, die den Start
    retten sollte, war das, was ihn verhinderte.

    ⚠️ Und es gehoert HIERHIN, an eine Stelle. Der erste Anlauf flickte es in
    `restore_selection` — und lief prompt in dieselbe Zeile in `ModelRouter.__init__`.
    Wer die Vorgabe an zwei Orten heilt, heilt sie am dritten nicht.

    Der Rueckfall ist belegt, nicht still: wer seine Modellwahl bewusst gesetzt hat, soll
    im Protokoll finden, warum eine andere lief.
    """
    # ⚠️ Zeigt der Anbieter auf eine EIGENE Adresse, kann kein Katalog seine Modellnamen
    # kennen. `openai-api` gegen einen lokalen Ollama-Server ist genau dieser Fall: der
    # eingebaute Katalog fuehrt OpenAIs Namen, der Server bietet `qwen3.5:27b-int4`.
    # Ohne diese Ausnahme fiele die Wahl still auf ein OpenAI-Modell zurueck — und Talos
    # spraeche mit dem lokalen Server unter einem Namen, den der gar nicht hat. Ein
    # stiller Rueckfall auf das FALSCHE Modell ist schlimmer als ein lauter Abbruch.
    #
    # Die Ausnahme haengt an einer bewussten Handlung des Betreibers: er hat die Adresse
    # gesetzt. Sie gibt kein Recht und umgeht keinen Kernel — sie sagt nur, dass hier der
    # Katalog nicht mehr die Wahrheit ueber die verfuegbaren Namen ist.
    from .credentials import base_url_var

    # Dieselbe Stelle, dasselbe Argument: hier trifft die Modell-Konfiguration des
    # Betreibers zum ersten Mal auf den Katalog, mit dem der Agent wirklich laeuft —
    # und auf das Log. TALOS_MODEL_OVERRIDES gehoert zu dieser Konfiguration.
    _reconcile_model_overrides(log, registry, wanted)

    if wanted.model and os.environ.get(base_url_var(wanted.provider), "").strip():
        return wanted
    try:
        return registry.selection(wanted.provider, wanted.model)
    except ValueError as fehler:
        # ⚠️ ZUERST derselbe Anbieter. Der Betreiber hat den Anbieter gewaehlt; veraltet
        # ist der Modellname. Der erste Anlauf nahm blind „erster Anbieter im Katalog"
        # und landete auf einer frischen Maschine bei der Hermes-CLI — die es dort nicht
        # gibt, weshalb der Start eine Zeile spaeter erneut abbrach. Ein Rueckfall, der
        # den Anbieter wechselt, beantwortet eine andere Frage als die gestellte.
        ersatz = None
        try:
            anbieter = registry.get(wanted.provider)
            if anbieter.models:
                ersatz = ModelSelection(wanted.provider, anbieter.models[0])
        except ValueError:
            ersatz = None
        if ersatz is None:
            ersatz = _first_known(registry)
        if ersatz is None:
            raise ValueError(
                f"{fehler} — und dieser Katalog kennt ueberhaupt kein Modell. "
                "Hol die Liste mit `talos models --refresh`."
            ) from None
        log.append(Event("boot", "provider", "model.fallback", {
            "wanted": f"{wanted.provider}/{wanted.model}",
            "used": f"{ersatz.provider}/{ersatz.model}",
            "reason": "configured default is not in this catalog",
        }))
        return ersatz


def _reconcile_model_overrides(log: EventLog, registry, wanted: ModelSelection) -> None:
    """Haelt die installierten Overrides (`modelinfo`) an den echten Katalog und belegt,
    was herausfaellt.

    Bekannt ist, was dieser Katalog listet, plus das konfigurierte Modell: das laesst
    `resolve_fallback` auch ausserhalb des Katalogs zu (eigene Adresse, lokaler
    Server), und dann ist es das laufende Modell — sein Override darf nicht als
    „unbekannt" verschwinden. Jeder Befund, auch die aus dem Parsen, wird hier ins
    Event-Log geschrieben: `load_config` hat kein Log, und ein Override, der nie
    trifft, hinterliesse sonst keine Spur. Ein Override fuegt dem Katalog NIE etwas
    hinzu — `registry` wird gelesen, nicht veraendert.
    """
    from . import modelinfo

    installiert = modelinfo.active()
    if not installiert.entries and not installiert.dropped:
        return
    bekannt = {model for anbieter in _providers_of(registry) for model in anbieter.models}
    if wanted.model:
        bekannt.add(wanted.model)
    bereinigt = modelinfo.reconcile(installiert, bekannt)
    for grund in bereinigt.dropped:
        log.append(Event("boot", "provider", "model.override_dropped", {"reason": grund}))
    modelinfo.install(bereinigt)


def _providers_of(registry) -> tuple:
    """`providers` ist je nach Registry eine Methode ODER eine Eigenschaft (siehe
    `_first_known`). Nachgesehen, nicht geraten."""
    anbieterliste = registry.providers
    return tuple(anbieterliste() if callable(anbieterliste) else anbieterliste)


def _first_known(registry) -> ModelSelection | None:
    """Das erste Modell, das dieser Katalog wirklich kennt — oder `None`.

    Deterministisch (erster Anbieter, erstes Modell), damit zwei Starts auf derselben
    Maschine dieselbe Wahl treffen. Eine zufaellige Auswahl waere hier das Schlimmste:
    der Agent antwortete mal so, mal anders, und niemand koennte sagen warum.
    """
    # ⚠️ `providers` ist je nach Registry eine Methode ODER eine Eigenschaft — beide
    # Formen liegen im Baum, und der erste Anlauf fiel prompt ueber `'tuple' object is
    # not callable`. Hier wird nicht geraten, sondern nachgesehen.
    anbieterliste = registry.providers
    if callable(anbieterliste):
        anbieterliste = anbieterliste()
    for anbieter in anbieterliste:
        for modell in anbieter.models:
            return ModelSelection(anbieter.slug, modell)
    return None


def restore_selection(
    log: EventLog, registry: ProviderRegistry, fallback: ModelSelection
) -> ModelSelection:
    """Restore only the latest event; malformed/stale state falls to a known-safe default.

    Der Rueckfall ist noetig — eine Auswahl, die der Katalog nicht mehr kennt, darf den
    Start nicht verhindern. Aber er darf nicht STILL passieren. Genau das war der Fall:
    verschwand ein Modellname aus dem Katalog (etwa nach einem Hermes-Update, oder weil
    der Katalog gar nicht geladen werden konnte), lief der Agent danach mit einem anderen
    Modell weiter, ohne Fehlermeldung und ohne Spur. Wer seine Modellwahl bewusst
    festgelegt hat, haette es erst gemerkt, wenn die Antworten sich anders anfuehlen.

    Deshalb wird der Rueckfall belegt. Der Beleg gehoert ins Event-Log und nicht auf
    stdout: das Log ist die Quelle, aus der die Wahl ohnehin wiederhergestellt wird.
    """
    # ⚠️ Der Rueckfall selbst war nicht abgesichert. Genau diese Zeile warf auf einer
    # FRISCHEN Installation `unknown model for anthropic-api: claude-opus-5` — als
    # unbehandelter Traceback, beim allerersten `talos ask`. Grund: die Registry kommt aus
    # Hermes-Dateien; ohne sie bleibt der kleine eingebaute Katalog, und der kennt das
    # voreingestellte Modell nicht. Die Sicherung, die den Start retten sollte, war das,
    # was ihn verhinderte.
    safe = registry.selection(fallback.provider, fallback.model)
    wanted = ""
    try:
        rows = log.recent(1, ("model.selected",))
        if not rows:
            # Kein Eintrag heisst NICHT „nie gewaehlt". Es heisst auch: das Log ist neu,
            # leer oder bei einem Update nicht mitgekommen — und dann laeuft eine bewusst
            # eingestellte Installation ploetzlich mit dem Vorgabemodell weiter. Der
            # Rueckfall bleibt (ein Start muss moeglich sein), aber er hinterlaesst eine
            # Spur, damit die Frage „warum antwortet er anders" beantwortbar ist.
            _record_fallback(log, "", safe, "no model.selected in the event log")
            return safe
        payload = rows[-1]["payload"]
        wanted = f"{payload.get('provider', '')}/{payload.get('model', '')}"
        return registry.selection(str(payload.get("provider", "")), str(payload.get("model", "")))
    except (KeyError, TypeError, ValueError) as error:
        _record_fallback(log, wanted, safe, str(error))
        return safe


def _record_fallback(
    log: EventLog, wanted: str, used: ModelSelection, error: str
) -> None:
    """Der Beleg darf selbst nicht der Grund sein, warum der Start scheitert."""
    try:
        log.append(Event(
            "boot",
            "provider",
            "model.restore_failed",
            {"wanted": wanted, "used": f"{used.provider}/{used.model}", "error": error[:200]},
        ))
    except Exception:
        pass


@dataclass
class _PickerState:
    principal: Principal
    conversation: str
    expires_at: float
    selected_provider: int | None = None
    selecting: bool = False


class ModelPicker:
    """Hermes-style two-step picker with all identity/model data held server-side."""

    def __init__(
        self,
        registry: ProviderRegistry,
        router: ModelRouter,
        *,
        ttl_s: int = 300,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
        can_select: Callable[[], bool] = lambda: True,
    ) -> None:
        self.registry = registry
        self.router = router
        self._ttl_s = max(1, int(ttl_s))
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(9))
        self._can_select = can_select
        self._states: dict[str, _PickerState] = {}
        self._lock = threading.Lock()

    def open(self, *, principal: Principal, conversation: str) -> StructuredMessage:
        token = self._token_factory()
        with self._lock:
            self._states[token] = _PickerState(
                principal=principal,
                conversation=conversation,
                expires_at=self._clock() + self._ttl_s,
            )
        return self._provider_view(token)

    def select_typed(self, raw: str, *, principal: Principal) -> StructuredMessage:
        parts = raw.split()
        if len(parts) != 2:
            return StructuredMessage("Usage: /model <provider> <model>. Nothing changed.")
        if not self._can_select():
            return StructuredMessage("Model not changed: another task is running.")
        result = self.router.select(parts[0], parts[1], principal=principal)
        if not result.ok:
            return StructuredMessage(f"Model not changed: {result.error}")
        return StructuredMessage(
            f"Model switched to {result.selection.model}\nProvider: {result.selection.provider}"
        )

    def handle(self, data: str, *, principal: Principal, conversation: str) -> StructuredMessage:
        parsed = self._parse(data)
        if parsed is None:
            return StructuredMessage("Picker invalid or expired. Nothing changed.")
        token, operation, value = parsed
        with self._lock:
            state = self._states.get(token)
            if state is None:
                return StructuredMessage("Picker expired. Nothing changed.")
            if state.expires_at <= self._clock():
                self._states.pop(token, None)
                return StructuredMessage("Picker expired. Nothing changed.")
            if state.principal != principal or state.conversation != conversation:
                return StructuredMessage("Picker invalid or expired. Nothing changed.")
            if state.selecting:
                return StructuredMessage("Model not changed: selection already running.")

            if operation == "x":
                self._states.pop(token, None)
                return StructuredMessage("Model selection cancelled.")
            if operation == "b":
                state.selected_provider = None
                return self._provider_view(token)
            if operation == "n":
                return self._model_view(token, state, value)
            if operation == "p":
                if value < 0 or value >= len(self.registry.providers):
                    return StructuredMessage("Picker invalid or expired. Nothing changed.")
                state.selected_provider = value
                return self._model_view(token, state, 0)
            if operation == "g":
                return self._model_view(token, state, value)
            if operation != "m" or state.selected_provider is None:
                return StructuredMessage("Picker invalid or expired. Nothing changed.")
            provider = self.registry.providers[state.selected_provider]
            if value < 0 or value >= len(provider.models):
                return StructuredMessage("Picker invalid or expired. Nothing changed.")
            target = ModelSelection(provider.slug, provider.models[value])
            state.selecting = True

        # Validation can call a real model and must never hold the picker lock: model
        # navigation and `/model` remain responsive in the Telegram poll thread.
        if not self._can_select():
            result = SwitchResult(False, self.router.current, "another task is running")
        else:
            result = self.router.select(target.provider, target.model, principal=principal)

        with self._lock:
            current = self._states.get(token)
            if current is state:
                if result.ok:
                    self._states.pop(token, None)
                else:
                    state.selecting = False
        if not result.ok:
            return StructuredMessage(f"Model not changed: {result.error}")
        return StructuredMessage(
            f"Model switched to {result.selection.model}\nProvider: {result.selection.provider}"
        )

    def _provider_view(self, token: str) -> StructuredMessage:
        current = self.router.current
        buttons = []
        for index, provider in enumerate(self.registry.providers):
            mark = "✓ " if provider.slug == current.provider else ""
            buttons.append(Button(f"{mark}{provider.label} ({len(provider.models)})", self._data(token, "p", index)))
        rows = [tuple(buttons[index:index + 2]) for index in range(0, len(buttons), 2)]
        rows.append((Button("✗ Cancel", self._data(token, "x")),))
        return StructuredMessage(
            "⚙ Model Configuration\n\n"
            f"Current model: {current.model}\nProvider: {current.provider}\n\n"
            "Select a provider:",
            tuple(rows),
        )

    def _model_view(self, token: str, state: _PickerState, page: int) -> StructuredMessage:
        if state.selected_provider is None:
            return self._provider_view(token)
        provider = self.registry.providers[state.selected_provider]
        total = len(provider.models)
        pages = max(1, (total + MODEL_PAGE_SIZE - 1) // MODEL_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * MODEL_PAGE_SIZE
        end = min(start + MODEL_PAGE_SIZE, total)
        buttons = []
        current = self.router.current
        for index in range(start, end):
            model = provider.models[index]
            short = model.rsplit("/", 1)[-1]
            if len(short) > 38:
                short = short[:35] + "..."
            mark = "✓ " if current == ModelSelection(provider.slug, model) else ""
            buttons.append(Button(mark + short, self._data(token, "m", index)))
        rows = [tuple(buttons[index:index + 2]) for index in range(0, len(buttons), 2)]
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(Button("◀ Prev", self._data(token, "g", page - 1)))
            nav.append(Button(f"{page + 1}/{pages}", self._data(token, "n", page)))
            if page < pages - 1:
                nav.append(Button("Next ▶", self._data(token, "g", page + 1)))
            rows.append(tuple(nav))
        rows.append((Button("◀ Back", self._data(token, "b")), Button("✗ Cancel", self._data(token, "x"))))
        page_info = f" ({start + 1}–{end} of {total})" if pages > 1 else ""
        return StructuredMessage(
            f"⚙ Model Configuration\n\nProvider: {provider.label}{page_info}\nSelect a model:",
            tuple(rows),
        )

    @staticmethod
    def _parse(data: str) -> tuple[str, str, int] | None:
        parts = data.split(":")
        if len(parts) not in (3, 4) or parts[0] != "tm":
            return None
        token, operation = parts[1], parts[2]
        if not token or operation not in {"p", "m", "g", "n", "b", "x"}:
            return None
        if operation in {"b", "x"}:
            if len(parts) != 3:
                return None
            return token, operation, 0
        if len(parts) != 4:
            return None
        try:
            return token, operation, int(parts[3])
        except ValueError:
            return None

    @staticmethod
    def _data(token: str, operation: str, value: int | None = None) -> str:
        data = f"tm:{token}:{operation}" + (f":{value}" if value is not None else "")
        if len(data.encode("utf-8")) > CALLBACK_MAX_BYTES:
            raise ValueError("picker callback data exceeds Telegram's 64-byte limit")
        return data


class HermesCatalogLoader:
    """Load Hermes' catalog through configurable helper module paths.

    The loader is deliberately separate from the registry so tests and other
    deployments can inject a catalog without importing Hermes or touching its
    credential/configuration files.
    """

    def __init__(self, provider_catalog_path: Path, models_path: Path) -> None:
        self.provider_catalog_path = Path(provider_catalog_path)
        self.models_path = Path(models_path)

    def load(self) -> ProviderRegistry:
        catalog_module = _load_helper(self.provider_catalog_path, "talos_hermes_provider_catalog")
        models_module = _load_helper(self.models_path, "talos_hermes_models")
        catalog_fn = getattr(catalog_module, "provider_catalog", None)
        models_fn = getattr(models_module, "provider_model_ids", None)
        if not callable(catalog_fn) or not callable(models_fn):
            raise ValueError("Hermes catalog helpers do not expose required functions")
        providers = []
        for entry in catalog_fn():
            slug = str(getattr(entry, "slug", "") or (entry.get("slug", "") if isinstance(entry, dict) else ""))
            label = str(getattr(entry, "label", "") or (entry.get("label", "") if isinstance(entry, dict) else "") or slug)
            models = tuple(dict.fromkeys(str(model) for model in models_fn(slug) if str(model)))
            if slug and models:
                providers.append(Provider(slug, label, models))
        return ProviderRegistry(providers)

    def load_if_present(self) -> ProviderRegistry | None:
        """Wie `load()` — aber ohne Hermes gibt es KEINEN Katalog statt eines Abbruchs.

        ⚠️ Gemessen am 02.09. mit einer frischen Installation auf einer Maschine ohne
        Hermes-Checkout: `talos ask` endete bei jedem Start in „catalog helper not
        found" — vor dem ersten Modellzug. Der Hermes-Katalog ist ein Zusatz (er kennt
        die Modelle einer lokal installierten Hermes-CLI); die eingebauten Wege in
        `safe_talos_registry` waren immer als Ersatz gedacht, kamen aber nie an die
        Reihe, weil diese Zeile vorher warf. Ein fehlender Zusatz ist ein Mangel, kein
        Urteil — er kostet die Hermes-Modelle, nie den Start.

        Nur die VORGABE-Pfade duerfen fehlen. Wer `TALOS_HERMES_PROVIDER_CATALOG` selbst
        gesetzt hat, meint eine Datei; die fehlt dann laut — ueber `load()`, das der
        Aufrufer in diesem Fall nimmt (`config.hermes_catalog_configured`).
        """
        if not self.provider_catalog_path.is_file() or not self.models_path.is_file():
            return None
        return self.load()


def safe_talos_registry(registry: ProviderRegistry | None) -> ProviderRegistry:
    """Expose only routes Talos can invoke without bypassing the operator's auth policy.

    Native ``anthropic`` can consume API/extra-usage credentials and Antigravity is
    an isolated worker, never a main reasoner. Claude models are therefore exposed
    through the already hardened Claude CLI OAuth runtime.

    `None` heisst: kein Hermes-Katalog auf dieser Maschine (`load_if_present`). Dann
    bleiben genau die eingebauten Wege — der Fall, fuer den sie unten seit jeher
    angehaengt werden.
    """
    blocked = {"anthropic", "antigravity", "google-antigravity", "claude-cli"}
    hermes_providers = registry.providers if registry is not None else ()
    native_anthropic = next((p for p in hermes_providers if p.slug == "anthropic"), None)
    claude_models = native_anthropic.models if native_anthropic is not None else (
        "claude-fable-5", "claude-sonnet-5", "claude-opus-4-8",
        "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
    )
    providers: list[Provider] = []
    for provider in hermes_providers:
        if provider.slug in blocked:
            continue
        safe_models = tuple(
            model for model in provider.models if not _looks_like_claude_model(model)
        )
        if safe_models:
            providers.append(Provider(provider.slug, provider.label, safe_models))
    # `claude-cli` steht in `blocked` und kann deshalb nie schon in `providers` sein.
    providers.append(Provider("claude-cli", "Anthropics Max (CLI OAuth)", tuple(claude_models)))
    # Die beiden API-Wege stehen IMMER im Katalog, auch ohne Hermes und ohne claude-CLI.
    # Ohne sie hat eine frische oeffentliche Installation gar keinen waehlbaren Anbieter:
    # beide CLI-Wege setzen eine lokale Anmeldung voraus, die ein Fremder nicht hat, und
    # der Agent scheiterte an jeder Nachricht statt an der Einrichtung. Der Schluessel
    # gehoert dem Betreiber — kein fremdes Abo, keine Impersonation.
    #
    # ABER: nur anhaengen, was der Hermes-Katalog nicht selbst schon fuehrt. Neuere
    # Hermes-Staende kennen `openai-api` bereits — der blinde Anhang erzeugte dann einen
    # doppelten Slug, und `ProviderRegistry.__init__` liess den AGENTEN-START daran
    # scheitern. Ein Katalog-Update haette den Waechter getoetet. Der Katalog-Eintrag
    # gewinnt, weil er die Modelle des tatsaechlich installierten Hermes kennt.
    existing = {provider.slug for provider in providers}
    if "anthropic-api" not in existing:
        providers.append(Provider("anthropic-api", "Anthropic API (your own key)", tuple(claude_models)))
    if "openai-api" not in existing:
        providers.append(Provider("openai-api", "OpenAI-compatible API (your own key)", OPENAI_API_MODELS))
    return ProviderRegistry(tuple(providers))


# Bewusst kurz und handkuratiert: der Katalog eines beliebigen OpenAI-kompatiblen
# Anbieters laesst sich nicht raten. Wer etwas anderes fahren will, traegt den Namen im
# Einrichtungs-Assistenten von Hand ein — geraten wird hier nichts.
OPENAI_API_MODELS: tuple[str, ...] = (
    "gpt-5.6-sol", "gpt-5.2", "gpt-5-codex", "o4-mini",
)


def with_local_provider(registry: ProviderRegistry, wanted: ModelSelection) -> ProviderRegistry:
    """Der EINGESTELLTE lokale Anbieter (ollama, lm-studio, custom) steht im Katalog —
    mit dem einen Modell, das der Betreiber genannt hat.

    Kein ausgelieferter Katalog kann die Modelle eines lokalen Servers kennen: was dort
    liegt, hat der Betreiber gezogen. Bisher kannte Talos `ollama` nur, wenn ein
    Hermes-Katalog es fuehrte — auf einer Maschine ohne (oder mit einem aelteren)
    Hermes hiess `TALOS_MODEL_PROVIDER=ollama` „unknown provider", und die Einrichtung
    ueber `ollama launch talos` oder `llmman launch talos` starb am Start. Gemessen am
    02.09. gegen eine frische Installation.

    Der Name kommt aus der Konfiguration des Betreibers, nicht aus Modelltext; die
    Adresse bleibt Sache von `credentials.py`; die uebrigen Namen ergaenzt
    `talos models --refresh` ueber den Zwischenspeicher (`models.merged`). Ein
    bekannter Name ist kein Recht: jeder Zug geht weiter durch denselben Kernel.
    """
    from . import catalog

    info = catalog.get(wanted.provider)
    if info is None or info.auth != "local" or not wanted.model:
        return registry
    if any(provider.slug == wanted.provider for provider in registry.providers):
        return registry
    return ProviderRegistry(
        (*registry.providers, Provider(wanted.provider, info.label, (wanted.model,)))
    )


def _looks_like_claude_model(model: str) -> bool:
    normalized = model.casefold()
    return any(marker in normalized for marker in (
        "anthropic", "claude", "sonnet", "opus", "haiku", "fable",
    ))


def _load_helper(path: Path, name: str) -> ModuleType:
    if not path.is_file():
        raise ValueError(f"catalog helper not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load catalog helper: {path}")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and some import helpers resolve the module through sys.modules while
    # it executes. A bare exec_module() leaves that lookup empty and crashes real Hermes.
    previous = sys.modules.get(name)
    sys.modules[name] = module
    # Hermes helper files import their package by absolute name. Their package
    # root is configurable together with the file rather than assumed globally.
    root = path.parent.parent if path.parent.name == "hermes_cli" else path.parent
    inserted = str(root) not in sys.path
    if inserted:
        sys.path.insert(0, str(root))
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        if inserted:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
    return module
