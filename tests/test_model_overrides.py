"""TALOS_MODEL_OVERRIDES — der Betreiber korrigiert Eckdaten einzelner Modelle.

Der Katalog sagt, wie man einen Anbieter *erreicht*. Was ein Modell kostet, wie gross
sein Fenster ist und was es kann, sagt er absichtlich nicht (Preise wechseln im
Monatsrhythmus, und eine falsche Zahl sieht aus wie Wissen). Wer diese Zahlen braucht,
traegt sie selbst ein — und genau drei Dinge muessen dabei stimmen:

  - Ein Override erfindet NIE ein Modell und aendert NIE Anbieter, Adresse oder
    Schluessel. Sonst waere er ein zweiter Weg zu Rechten, am Katalog vorbei.
  - Kaputtes JSON ist ein Startfehler, der die Variable nennt — nie ihren Wert.
    Betreiber-Konfiguration, die still ignoriert wird, ist die schlimmste Variante.
  - Was herausfaellt (unbekannter Name, falsches Feld, falscher Typ), hinterlaesst
    eine Spur: im Event-Log beim Start, in `talos doctor`, in `talos models`.
"""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from talos import catalog, configcli, doctor, modelinfo, models, schema
from talos import config as config_mod
from talos.approval import ApprovalStore
from talos.capability import CapabilityMint
from talos.catalog import ModelInfo
from talos.channel import Principal
from talos.commands import CommandCenter
from talos.eventlog import EventLog
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel
from talos.provider import ModelSelection, Provider, ProviderRegistry, resolve_fallback
from talos.usage import Run, UsageMeter

VAR = "TALOS_MODEL_OVERRIDES"
OPUS = json.dumps({
    "claude-opus-5": {
        "context_window": 200_000, "input_price": 15, "output_price": 75, "vision": True,
    },
})
OWNER = Principal("telegram", "100000001")
CHAT = "telegram:4242"


@pytest.fixture(autouse=True)
def _clean_slate():
    """Die installierte Tabelle ist Prozesszustand — kein Test darf einen anderen faerben."""
    modelinfo.install(modelinfo.EMPTY)
    yield
    modelinfo.install(modelinfo.EMPTY)


def _env(tmp_path: Path, monkeypatch, **werte: str) -> None:
    """Eine Installation, die nur aus `talos.env` liest — wie in `test_first_run`."""
    datei = tmp_path / "talos.env"
    datei.write_text("".join(f"{k}={v}\n" for k, v in werte.items()), encoding="utf-8")
    monkeypatch.setattr(config_mod, "LOCAL_ENV", datei)
    monkeypatch.setattr(config_mod, "SECRETS_ENV", tmp_path / "gibt-es-nicht.env")
    for name in ("TELEGRAM_BOT_TOKEN", "TALOS_ALLOWED_PRINCIPALS", "TALOS_ALLOWED_USER_IDS", VAR):
        monkeypatch.delenv(name, raising=False)


class _FakeReasoner:
    def cancel(self) -> bool:
        return False


class _FakeWorker:
    def pending(self) -> int:
        return 0

    def busy(self) -> bool:
        return False

    def drain(self) -> int:
        return 0


def _center(tmp_path: Path, **extras) -> CommandCenter:
    manifest = ToolManifest().with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
    policy = PolicyKernel(manifest, frozenset({OWNER}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"), approvals=ApprovalStore(), policy=policy,
        started_at=0.0, bot_username="Talos_bot", reasoner=_FakeReasoner(),
        worker=_FakeWorker(), repo_dir=tmp_path, mint=CapabilityMint(policy), **extras,
    )


def _priced(model: str) -> ModelInfo:
    return ModelInfo(input_price=3.0, output_price=15.0,
                     overridden=frozenset({"input_price", "output_price"}))


# --- Parsen: was ein Override sein darf ------------------------------------------------


def test_a_valid_override_carries_exactly_the_named_fields() -> None:
    overrides = modelinfo.parse(OPUS)

    info = overrides.entries["claude-opus-5"]
    assert info.context_window == 200_000
    assert info.input_price == 15.0 and info.output_price == 75.0
    assert info.vision is True and info.reasoning is False
    assert info.overridden == {"context_window", "input_price", "output_price", "vision"}
    assert overrides.dropped == ()


def test_broken_json_stops_the_start_and_names_the_variable_not_the_value() -> None:
    """Ein Startfehler darf sagen WO, aber nicht WAS drinstand — der Wert landet sonst
    in Logs und Tickets. Und er muss laut sein: still ignorierte Konfiguration ist
    schlimmer als ein Abbruch, der eine Minute kostet."""
    with pytest.raises(ValueError) as fehler:
        modelinfo.parse('{"claude-opus-5": {oops')
    assert VAR in str(fehler.value)
    assert "oops" not in str(fehler.value)

    with pytest.raises(ValueError, match="ANDERE_VARIABLE"):
        modelinfo.parse("{", variable="ANDERE_VARIABLE")


def test_a_list_or_string_at_the_top_is_refused() -> None:
    for wert in ('[{"context_window": 1}]', '"claude-opus-5"', "42", "null"):
        with pytest.raises(ValueError, match=VAR):
            modelinfo.parse(wert)


def test_an_empty_value_means_no_overrides() -> None:
    assert modelinfo.parse("") == modelinfo.EMPTY
    assert modelinfo.parse("   ").entries == {}


def test_shell_quotes_around_the_object_are_forgiven() -> None:
    """`KEY='{...}'` ist Shell-Gewohnheit; ein Objekt kann nie mit einem Anfuehrungszeichen
    beginnen, also ist das Abstreifen eindeutig und kein Raten."""
    assert modelinfo.parse(f"'{OPUS}'").entries.keys() == {"claude-opus-5"}
    assert modelinfo.parse(f'"{OPUS}"').entries.keys() == {"claude-opus-5"}


def test_an_unknown_field_is_dropped_with_a_warning_and_the_rest_stays() -> None:
    overrides = modelinfo.parse('{"m": {"context_window": 1000, "colour": "blue"}}')

    assert overrides.entries["m"].context_window == 1000
    assert overrides.entries["m"].overridden == {"context_window"}
    assert len(overrides.dropped) == 1
    assert "colour" in overrides.dropped[0] and "m" in overrides.dropped[0]


def test_provider_url_and_key_are_never_overridable() -> None:
    """Der Kern der Sache: ein Override korrigiert Zahlen UEBER ein Modell. Wer damit
    den Anbieter, die Adresse oder den Schluessel biegen koennte, haette einen zweiten
    Weg zu Rechten — am Katalog und an `credentials.py` vorbei."""
    overrides = modelinfo.parse(json.dumps({"claude-opus-5": {
        "provider": "openai-api", "base_url": "http://evil.example", "env_key": "X",
        "api_key": "sk-evil", "wire": "openai", "slug": "evil",
    }}))

    assert "claude-opus-5" not in overrides.entries
    assert overrides.dropped                              # jedes Feld einzeln benannt
    text = " ".join(overrides.dropped)
    for feld in ("provider", "base_url", "env_key", "api_key", "wire", "slug"):
        assert feld in text
    assert "evil" not in text                              # Werte werden nie wiederholt


def test_wrong_types_and_negative_numbers_drop_the_field_not_the_entry() -> None:
    overrides = modelinfo.parse(json.dumps({"m": {
        "context_window": "200k", "input_price": -1, "output_price": 3, "vision": "yes",
    }}))

    info = overrides.entries["m"]
    assert info.overridden == {"output_price"}
    assert info.output_price == 3.0
    assert len(overrides.dropped) == 3
    assert "200k" not in " ".join(overrides.dropped)


def test_a_bool_is_not_a_number_and_a_number_is_not_a_flag() -> None:
    """`True` ist in Python ein `int` — ohne diese Pruefung waere `"context_window": true`
    ein Fenster von einem Token."""
    overrides = modelinfo.parse('{"m": {"context_window": true, "input_price": false, "vision": 1}}')
    assert "m" not in overrides.entries
    assert len(overrides.dropped) == 4                    # drei Felder + der leere Eintrag


def test_zero_and_fractions_are_valid_prices() -> None:
    """Ein lokales Modell kostet nichts, ein kleines 0.15 $ je Million — beides echt."""
    overrides = modelinfo.parse('{"m": {"input_price": 0, "output_price": 0.15}}')
    info = overrides.entries["m"]
    assert info.input_price == 0.0 and info.output_price == 0.15
    assert info.has_prices


def test_an_entry_that_is_not_an_object_is_dropped() -> None:
    overrides = modelinfo.parse('{"m": 5, "n": [1], "o": null}')
    assert overrides.entries == {}
    assert len(overrides.dropped) == 3


def test_a_key_that_is_no_model_id_is_dropped() -> None:
    overrides = modelinfo.parse(json.dumps({
        "": {"context_window": 1}, "has space": {"context_window": 1},
        "x" * 200: {"context_window": 1}, "-leading": {"context_window": 1},
    }))
    assert overrides.entries == {}
    assert len(overrides.dropped) == 4


def test_more_entries_than_the_bound_are_cut_with_a_warning() -> None:
    viele = {f"model-{n}": {"context_window": 1} for n in range(modelinfo.MAX_ENTRIES + 3)}
    overrides = modelinfo.parse(json.dumps(viele))
    assert len(overrides.entries) == modelinfo.MAX_ENTRIES
    assert any(str(modelinfo.MAX_ENTRIES) in grund for grund in overrides.dropped)


# --- Abgleich mit dem Katalog: nie ein neues Modell --------------------------------------


def test_reconcile_drops_names_the_catalog_does_not_know_and_never_adds_one() -> None:
    overrides = modelinfo.parse('{"claude-opus-5": {"context_window": 1}, '
                                '"ghost-9": {"context_window": 1}, "m": {"nope": 1}}')

    bereinigt = modelinfo.reconcile(overrides, {"claude-opus-5", "gpt-5.2"})

    assert bereinigt.entries.keys() == {"claude-opus-5"}
    assert any("ghost-9" in grund for grund in bereinigt.dropped)
    assert any("nope" in grund for grund in bereinigt.dropped)   # Parse-Befund bleibt erhalten
    assert set(bereinigt.entries) <= set(overrides.entries)      # nie mehr als vorher


def test_reconcile_without_overrides_is_a_no_op() -> None:
    assert modelinfo.reconcile(modelinfo.EMPTY, {"a"}) == modelinfo.EMPTY


# --- Nachschlagen: Override ueber dem ausgelieferten Wert --------------------------------


def test_lookup_lays_the_override_over_the_shipped_value_field_by_field(monkeypatch) -> None:
    monkeypatch.setattr(catalog, "MODEL_INFO", {"m": ModelInfo(context_window=100, input_price=1.0)})
    modelinfo.install(modelinfo.parse('{"m": {"input_price": 2}}'))

    info = modelinfo.lookup("m")

    assert info.context_window == 100                     # ausgeliefert, nicht ueberschrieben
    assert info.input_price == 2.0                        # ueberschrieben
    assert info.overridden == {"input_price"}


def test_lookup_for_a_model_nobody_described_is_all_unknown() -> None:
    info = modelinfo.lookup("nothing-known")
    assert info == ModelInfo()
    assert not info.has_prices and not info.known


def test_install_replaces_the_whole_table() -> None:
    modelinfo.install(modelinfo.parse('{"a": {"context_window": 1}}'))
    modelinfo.install(modelinfo.parse('{"b": {"context_window": 2}}'))
    assert modelinfo.lookup("a") == ModelInfo()
    assert modelinfo.lookup("b").context_window == 2
    assert modelinfo.active().entries.keys() == {"b"}


def test_the_shipped_table_is_checked_at_import_like_the_providers() -> None:
    """Dieselbe Disziplin wie `_check_all`: ein negativer Preis oder ein leerer Name im
    ausgelieferten Bestand darf nicht erst beim Nutzer auffallen."""
    with pytest.raises(ValueError):
        catalog._check_model_info({"": ModelInfo()})
    with pytest.raises(ValueError):
        catalog._check_model_info({"m": ModelInfo(input_price=-1)})
    with pytest.raises(ValueError):
        # Der ausgelieferte Bestand darf sich nicht als Betreiber-Wort ausgeben.
        catalog._check_model_info({"m": ModelInfo(overridden=frozenset({"vision"}))})
    catalog._check_model_info({"m": ModelInfo(context_window=1)})


# --- Preise: /usage rechnet mit den Betreiber-Werten -------------------------------------


def test_cost_is_price_per_million_times_tokens() -> None:
    info = ModelInfo(input_price=3.0, output_price=15.0)
    assert modelinfo.cost_usd(info, 1_000_000, 500_000) == pytest.approx(10.5)
    assert modelinfo.cost_usd(info, 0, 0) == 0.0


def test_the_meter_prices_a_run_the_reasoner_left_unpriced() -> None:
    """Der API-Weg meldet Token, aber keinen Preis (der ist Tarifsache). Mit einem
    Betreiber-Preis wird daraus eine Zahl — und sie sagt, woher sie kommt."""
    meter = UsageMeter(infos=lambda m: _priced(m) if m == "m" else ModelInfo())
    meter.record(Run(at=1.0, ok=True, duration_s=1.0, model="m",
                     input_tokens=1_000_000, output_tokens=100_000))

    snap = meter.snapshot()
    assert snap.cost_usd == pytest.approx(3.0 + 1.5)
    assert snap.cost_override_usd == pytest.approx(4.5)
    assert snap.last is not None and snap.last.cost_source == "override"


def test_a_reported_cost_is_never_overwritten() -> None:
    """Was die CLI meldet, ist gemessen; ein Betreiber-Preis ersetzt keine Messung."""
    meter = UsageMeter(infos=_priced)
    meter.record(Run(at=1.0, ok=True, duration_s=1.0, model="m",
                     input_tokens=1_000_000, cost_usd=0.02))

    snap = meter.snapshot()
    assert snap.cost_usd == pytest.approx(0.02)
    assert snap.cost_override_usd == 0.0
    assert snap.last is not None and snap.last.cost_source == ""


def test_a_run_without_tokens_or_prices_gets_no_price() -> None:
    meter = UsageMeter(infos=_priced)
    meter.record(Run(at=1.0, ok=False, duration_s=1.0, model="m", note="timeout"))
    meter.record(Run(at=1.0, ok=True, duration_s=1.0, model="m"))
    assert meter.snapshot().cost_usd == 0.0

    ohne = UsageMeter(infos=lambda m: ModelInfo())
    ohne.record(Run(at=1.0, ok=True, duration_s=1.0, model="m", input_tokens=1_000_000))
    assert ohne.snapshot().cost_usd == 0.0


def test_the_default_meter_reads_the_installed_overrides() -> None:
    """So ist der Zaehler in `__main__` verdrahtet: ohne Parameter, ueber die beim
    Laden installierte Tabelle."""
    modelinfo.install(modelinfo.parse(OPUS))
    meter = UsageMeter()
    meter.record(Run(at=1.0, ok=True, duration_s=1.0, model="claude-opus-5",
                     input_tokens=1_000_000))
    assert meter.snapshot().cost_usd == pytest.approx(15.0)


# --- Konfiguration: dieselbe Ebene wie TALOS_MODEL --------------------------------------


def test_load_config_reads_the_overrides_from_the_env_file_and_installs_them(
    tmp_path, monkeypatch
) -> None:
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="cli:501", **{VAR: OPUS})

    config = config_mod.load_config(require_channel=False)

    assert config.model_overrides.entries.keys() == {"claude-opus-5"}
    assert modelinfo.active().entries.keys() == {"claude-opus-5"}


def test_broken_overrides_stop_the_start_with_the_variable_name(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="cli:501", **{VAR: "{not json"})

    with pytest.raises(ValueError, match=VAR) as fehler:
        config_mod.load_config(require_channel=False)
    assert "not json" not in str(fehler.value)


def test_the_process_env_wins_over_the_file_like_every_other_key(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="cli:501", **{VAR: OPUS})
    monkeypatch.setenv(VAR, '{"gpt-5.2": {"context_window": 400000}}')

    config = config_mod.load_config(require_channel=False)

    assert config.model_overrides.entries.keys() == {"gpt-5.2"}


def test_a_config_without_overrides_resets_the_installed_table(tmp_path, monkeypatch) -> None:
    modelinfo.install(modelinfo.parse(OPUS))
    _env(tmp_path, monkeypatch, TALOS_ALLOWED_PRINCIPALS="cli:501")

    config_mod.load_config(require_channel=False)

    assert modelinfo.active() == modelinfo.EMPTY


def test_load_model_overrides_needs_no_full_config(tmp_path, monkeypatch) -> None:
    """`talos models` zeigt den Katalog auch ohne Token und Allowlist — die Overrides
    gehoeren dazu, also muessen sie ohne die ganze Konfiguration lesbar sein."""
    _env(tmp_path, monkeypatch, **{VAR: OPUS})
    assert config_mod.load_model_overrides().entries.keys() == {"claude-opus-5"}


# --- Schema: eine Einstellung, keine Politik ---------------------------------------------


def test_the_key_is_a_setting_on_the_same_tier_as_talos_model() -> None:
    """Das Kriterium aus `schema.py`: kann die Aenderung Befehlsgeber zulassen, einen
    Filter lockern, Daten umleiten oder Zugangsdaten ersetzen? Eine Zahl ueber ein
    Modell kann nichts davon — die Feldliste ist geschlossen und fuehrt weder
    Anbieter noch Adresse noch Schluessel. Also SETTING, wie TALOS_MODEL."""
    key = schema.get(VAR)
    assert key is not None
    assert key.kind == schema.SETTING == schema.get("TALOS_MODEL").kind
    assert key.writable and key.readable
    assert key.validate is not None


def test_config_set_refuses_broken_json_and_writes_a_valid_object(tmp_path: Path) -> None:
    ziel = tmp_path / "talos.env"
    text = io.StringIO()

    assert configcli.cmd_set(VAR, "{oops", ziel, text.write) == 1
    assert not ziel.exists()
    assert VAR in text.getvalue() and "oops" not in text.getvalue()

    assert configcli.cmd_set(VAR, OPUS, ziel, text.write) == 0
    assert configcli.read_file(ziel)[VAR] == OPUS


def test_config_list_and_validate_know_the_key(tmp_path: Path) -> None:
    liste = io.StringIO()
    configcli.cmd_list({VAR: OPUS}, liste.write)
    zeile = next(z for z in liste.getvalue().splitlines() if VAR in z)
    assert "setting" in zeile and "set" in zeile

    pruefung = io.StringIO()
    assert configcli.cmd_validate({VAR: "{oops"}, tmp_path / "x.env", pruefung.write) == 1
    assert VAR in pruefung.getvalue() and "oops" not in pruefung.getvalue()


# --- Der Start: Abgleich mit dem echten Katalog, mit dem Log zur Hand ---------------------


def _registry() -> ProviderRegistry:
    return ProviderRegistry((
        Provider("anthropic-api", "Anthropic", ("claude-opus-5", "claude-sonnet-5")),
        Provider("openai-api", "OpenAI", ("gpt-5.2",)),
    ))


def test_the_boot_reconciles_overrides_against_the_real_catalog_and_logs_what_it_drops(
    tmp_path: Path,
) -> None:
    modelinfo.install(modelinfo.parse(json.dumps({
        "claude-opus-5": {"context_window": 200_000},
        "ghost-9": {"context_window": 1},
        "gpt-5.2": {"context_window": 400_000, "colour": "blue"},
    })))
    log = EventLog(tmp_path / "events.db")
    registry = _registry()
    vorher = registry.providers

    gewaehlt = resolve_fallback(log, registry, ModelSelection("anthropic-api", "claude-opus-5"))

    assert gewaehlt == ModelSelection("anthropic-api", "claude-opus-5")
    assert modelinfo.active().entries.keys() == {"claude-opus-5", "gpt-5.2"}
    gruende = [e["payload"]["reason"] for e in log.recent(10, ("model.override_dropped",))]
    assert any("ghost-9" in g for g in gruende)
    assert any("colour" in g for g in gruende)
    assert registry.providers == vorher                   # der Katalog selbst bleibt, wie er war
    assert all("ghost-9" not in p.models for p in registry.providers)


def test_the_configured_model_counts_as_known_even_outside_the_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    """`resolve_fallback` laesst ein Modell ausserhalb des Katalogs zu, wenn der Betreiber
    eine eigene Adresse gesetzt hat (lokaler Server). Dann ist es das laufende Modell,
    und sein Override darf nicht als 'unbekannt' verschwinden."""
    monkeypatch.setenv("TALOS_BASE_URL_OPENAI_API", "http://localhost:11434/v1")
    modelinfo.install(modelinfo.parse('{"qwen3.5:27b-int4": {"input_price": 0, "output_price": 0}}'))
    log = EventLog(tmp_path / "events.db")

    resolve_fallback(log, _registry(), ModelSelection("openai-api", "qwen3.5:27b-int4"))

    assert modelinfo.active().entries.keys() == {"qwen3.5:27b-int4"}
    assert log.recent(10, ("model.override_dropped",)) == []


def test_without_overrides_the_boot_logs_nothing(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.db")
    resolve_fallback(log, _registry(), ModelSelection("openai-api", "gpt-5.2"))
    assert log.recent(10, ("model.override_dropped",)) == []


# --- `talos models`: Betreiber-Werte sind als solche gekennzeichnet ------------------------


def _models_cli(tmp_path: Path, monkeypatch, wert: str) -> tuple[int, str]:
    monkeypatch.setattr(models, "_catalog", _registry)
    monkeypatch.setattr(config_mod, "MODEL_CACHE", tmp_path / "models-cache.json")
    monkeypatch.setattr(config_mod, "LOCAL_ENV", tmp_path / "keine.env")
    monkeypatch.setattr(config_mod, "SECRETS_ENV", tmp_path / "keine-secrets.env")
    monkeypatch.setenv(VAR, wert)
    text = io.StringIO()
    code = models.run_models([], out=text)
    return code, text.getvalue()


def test_talos_models_marks_operator_values_as_overrides(tmp_path, monkeypatch) -> None:
    code, text = _models_cli(tmp_path, monkeypatch, OPUS)

    assert code == 0
    zeile = next(z for z in text.splitlines() if "claude-opus-5" in z)
    assert "200k" in zeile and "$15" in zeile and "$75" in zeile and "vision" in zeile
    assert "override" in zeile                            # niemand haelt den Katalog fuer die Quelle
    assert "1 model override" in text
    assert not any("claude-sonnet-5:" in z for z in text.splitlines())   # nur, wo etwas gesetzt ist


def test_talos_models_warns_about_an_override_for_a_model_it_does_not_list(
    tmp_path, monkeypatch
) -> None:
    code, text = _models_cli(tmp_path, monkeypatch, '{"ghost-9": {"context_window": 1}}')

    assert code == 0                                      # eine Warnung, kein Fehlschlag
    assert "ghost-9" in text
    assert "ghost-9" not in " ".join(z for z in text.splitlines() if "models" in z and ":" not in z)
    assert "dropped" in text


def test_talos_models_fails_loudly_on_broken_json(tmp_path, monkeypatch) -> None:
    code, text = _models_cli(tmp_path, monkeypatch, "{oops")

    assert code == 1
    assert VAR in text and "oops" not in text
    assert "anthropic-api" in text                        # der Katalog wird trotzdem gezeigt


# --- `talos doctor` ------------------------------------------------------------------


def test_the_doctor_reports_overrides_and_their_drops() -> None:
    overrides = modelinfo.parse('{"claude-opus-5": {"context_window": 1, "nope": 1}, '
                                '"ghost-9": {"context_window": 1}}')

    befunde = doctor.check_model_overrides(overrides, known=frozenset({"claude-opus-5"}))

    assert all(b.area == "model" for b in befunde)
    ok = [b for b in befunde if b.state is doctor.OK]
    assert len(ok) == 1 and "claude-opus-5" in ok[0].detail and "ghost-9" not in ok[0].detail
    warnungen = [b for b in befunde if b.state is doctor.WARN]
    assert len(warnungen) == 2
    assert not any(b.critical for b in befunde)           # nie ein Startverbot


def test_the_doctor_says_nothing_when_there_is_nothing_to_say() -> None:
    assert doctor.check_model_overrides(modelinfo.EMPTY, known=frozenset()) == ()


def test_the_doctor_reads_the_field_the_real_config_has(monkeypatch) -> None:
    """Falle 7 der CLAUDE.md, ein weiteres Mal: gegen die echte Klasse pruefen."""
    from talos.config import TalosConfig

    monkeypatch.setattr(models, "known_model_ids", lambda: frozenset({"claude-opus-5"}))
    config = TalosConfig(
        bot_token="123:abc", bot_username="bot", allowed_principals=frozenset(),
        eventlog_db=Path("/tmp/talos-test/eventlog.db"),
        snapshot_dir=Path("/tmp/talos-test/snapshots"),
        model_overrides=modelinfo.parse(OPUS),
    )

    befunde = doctor.collect(config)

    assert any(b.label == "overrides" and b.state is doctor.OK for b in befunde)


# --- /usage und /status zeigen die korrigierten Werte --------------------------------------


def test_usage_says_which_part_of_the_cost_came_from_operator_prices(tmp_path: Path) -> None:
    meter = UsageMeter(infos=_priced)
    meter.record(Run(at=time.time(), ok=True, duration_s=2.0, model="m", input_tokens=1_000_000))
    center = _center(tmp_path, usage=meter)

    reply = center.dispatch("usage", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "$3.00" in reply
    assert "Betreiber-Preisen" in reply and VAR in reply


def test_usage_shows_context_utilisation_when_the_window_is_known(tmp_path: Path) -> None:
    modelinfo.install(modelinfo.parse(OPUS))
    meter = UsageMeter()
    meter.record(Run(at=time.time(), ok=True, duration_s=2.0, model="claude-opus-5",
                     input_tokens=20_000, cost_usd=0.5))
    center = _center(tmp_path, usage=meter)

    reply = center.dispatch("usage", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "Kontext" in reply and "20.0k" in reply and "200k" in reply and "10 %" in reply


def test_status_shows_context_utilisation_and_the_override_count(tmp_path: Path) -> None:
    modelinfo.install(modelinfo.parse(OPUS))
    meter = UsageMeter()
    meter.record(Run(at=time.time(), ok=True, duration_s=2.0, model="claude-opus-5",
                     input_tokens=20_000))
    center = _center(tmp_path, usage=meter)

    reply = center.dispatch("status", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "Modell-Overrides: 1" in reply and VAR in reply
    assert "10 %" in reply and "200k" in reply


def test_status_stays_silent_about_overrides_when_there_are_none(tmp_path: Path) -> None:
    center = _center(tmp_path, usage=UsageMeter())
    reply = center.dispatch("status", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Overrides" not in reply
