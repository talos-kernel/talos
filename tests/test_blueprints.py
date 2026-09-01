"""Blueprints — die No-Cron-Schicht ueber dem Zeitplan.

Der Kern ist nicht die JSON-Datei, sondern zwei Zusicherungen: die Zeitangabe ist
menschenlesbar („every morning 08:30" statt `30 8 * * *`), und ein installierter
Blueprint ist ein ganz normaler Zeitplan-Eintrag — gleiche DB, gleicher Ticker,
gleiche UnattendedCeiling. Wer hier eine Abkuerzung baute, haette einen zweiten
Erlaubnisweg neben dem Kernel.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from talos.approval import ApprovalStore
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.blueprints import (
    BlueprintBook,
    BlueprintError,
    describe_next,
    load,
    parse_when,
)
from talos.channel import Principal, Trust
from talos.commands import CommandCenter
from talos.cron import parse as cron_parse
from talos.eventlog import EventLog
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import Decision, PolicyKernel, ToolRequest, Verdict
from talos.schedule import ScheduleStore, UnattendedCeiling
from talos.capability import CapabilityMint

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"
HOME = str(Path.home())


def _schreibe(directory: Path, name: str, **felder) -> Path:
    inhalt = {"name": name, "description": "", "when": "every morning 07:00",
              "prompt": "tu etwas"}
    inhalt.update({k: v for k, v in felder.items() if v is not None})
    datei = directory / f"{name}.json"
    datei.write_text(json.dumps(inhalt), encoding="utf-8")
    return datei


def _buch(tmp_path: Path, *blueprints: str) -> BlueprintBook:
    vorlagen = tmp_path / "vorlagen"
    vorlagen.mkdir(exist_ok=True)
    _schreibe(vorlagen, "morgen", when="every morning 08:30", prompt="Berichte den Stand")
    _schreibe(vorlagen, "puls", when="every 2 hours", prompt="Pruefe den Puls")
    for name in blueprints:
        _schreibe(vorlagen, name)
    return BlueprintBook(
        vorlagen, tmp_path / "stand" / "blueprints.json",
        ScheduleStore(tmp_path / "schedules.db"),
    )


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


def _center(tmp_path: Path, buch: BlueprintBook | None) -> CommandCenter:
    manifest = ToolManifest().with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
    policy = PolicyKernel(manifest, frozenset({OWNER}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"),
        approvals=ApprovalStore(),
        policy=policy,
        started_at=0.0,
        bot_username="Talos_bot",
        reasoner=_FakeReasoner(),
        worker=_FakeWorker(),
        repo_dir=tmp_path,
        mint=CapabilityMint(policy),
        blueprints=buch,
    )


# --- Der Parser: Sprache wird Zeitplan ----------------------------------------------

@pytest.mark.parametrize("text,erwartet", [
    ("every morning 08:30", {"cron": "30 8 * * *"}),
    ("every morning", {"cron": "0 7 * * *"}),
    ("every evening", {"cron": "0 18 * * *"}),
    ("every evening 21:15", {"cron": "15 21 * * *"}),
    ("daily 06:45", {"cron": "45 6 * * *"}),
    ("every day at 06:45", {"cron": "45 6 * * *"}),
    ("weekdays 18:00", {"cron": "0 18 * * MON-FRI"}),
    ("every weekday 18:00", {"cron": "0 18 * * MON-FRI"}),
    ("weekends 10:00", {"cron": "0 10 * * SAT,SUN"}),
    ("monday 09:00", {"cron": "0 9 * * MON"}),
    ("every sunday 23:59", {"cron": "59 23 * * SUN"}),
    ("every 2 hours", {"interval_s": 7200}),
    ("every 45 minutes", {"interval_s": 2700}),
    ("every hour", {"interval_s": 3600}),
    ("every minute", {"interval_s": 60}),
])
def test_plain_language_becomes_a_schedule(text: str, erwartet: dict) -> None:
    assert parse_when(text) == erwartet


def test_the_parser_is_case_insensitive_and_tolerant_of_spacing() -> None:
    assert parse_when("  Every   Morning   08:30 ") == {"cron": "30 8 * * *"}


def test_every_generated_cron_is_one_the_engine_accepts() -> None:
    """Der Parser darf keinen Ausdruck erzeugen, den cron.parse ablehnt — sonst stuende
    im Zeitplan ein Eintrag, der beim ersten Faelligwerden stirbt."""
    for text in ("every morning 08:30", "weekdays 18:00", "weekends 10:00",
                 "monday 09:00", "daily 00:00"):
        ausdruck = parse_when(text)["cron"]
        assert cron_parse(ausdruck).text == ausdruck


@pytest.mark.parametrize("text", [
    "",
    "bald mal irgendwann",
    "every 0 hours",
    "every 200 hours",   # ueber dem Maximum von 7 Tagen
    "every 30 seconds",  # kein Sekundentakt — ein Zeitplan ist kein Timer
    "every morning 25:00",
    "every morning 8:99",
    "monday",            # Kalenderform ohne Uhrzeit
    "weekdays",
    "monday at nine",    # nur HH:MM, keine Woerter
    "every morning 08:30 extra",
    "0 9 * * *",         # Cron bleibt /every vorbehalten — keine Hintertuer hier
])
def test_unreadable_schedules_fail_with_a_helpful_message(text: str) -> None:
    with pytest.raises(BlueprintError) as fehler:
        parse_when(text)
    assert str(fehler.value)


def test_interval_bounds_match_the_schedule_store() -> None:
    """„every 200 hours" muss HIER scheitern, nicht erst im Store — der Betreiber soll
    den Grund beim Schreiben des Blueprints sehen, nicht beim Installieren."""
    assert parse_when("every 168 hours") == {"interval_s": 7 * 24 * 3600}
    with pytest.raises(BlueprintError, match="between"):
        parse_when("every 169 hours")


# --- Das Verzeichnis: Katalog und Verworfene ----------------------------------------

def test_a_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    katalog = load(tmp_path / "gibts-nicht")
    assert katalog.blueprints == () and katalog.rejected == ()


def test_broken_files_are_named_not_swallowed(tmp_path: Path) -> None:
    (tmp_path / "kaputt.json").write_text("{kein json", encoding="utf-8")
    (tmp_path / "leer.json").write_text('{"name": "leer"}', encoding="utf-8")
    (tmp_path / "unlesbar.json").write_text(
        '{"name": "unlesbar", "when": "bald", "prompt": "x"}', encoding="utf-8")
    _schreibe(tmp_path, "gut")
    katalog = load(tmp_path)
    assert [b.name for b in katalog.blueprints] == ["gut"]
    assert len(katalog.rejected) == 3
    assert any("kaputt.json" in grund for grund in katalog.rejected)
    assert any("prompt" in grund for grund in katalog.rejected)
    assert any("unlesbar" in grund for grund in katalog.rejected)


# --- Der Lebenszyklus: installieren, pausieren, entfernen ----------------------------

def test_install_lands_in_the_same_store_as_every(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    task = buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    assert task.cron == "30 8 * * *"
    # Der Eintrag ist vom /schedules des Betreibers nicht zu unterscheiden — gleiche
    # DB, gleiche Konversation, gleiche Identitaet. Genau das ist die Zusicherung.
    assert [t.id for t in buch._schedules.list_for(CHAT)] == [task.id]
    assert task.next_run == cron_parse("30 8 * * *").next_after(task.created)


def test_install_of_an_interval_blueprint(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    task = buch.install("puls", conversation=CHAT, principal=str(OWNER))
    assert task.interval_s == 7200 and not task.cron


def test_installing_twice_is_an_error_not_a_second_entry(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    with pytest.raises(BlueprintError, match="already installed"):
        buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    assert len(buch._schedules.list_for(CHAT)) == 1


def test_disable_keeps_the_record_and_drops_the_schedule(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    buch.disable("morgen")
    assert buch._schedules.list_for(CHAT) == ()
    stand = buch.installed()["morgen"]
    assert stand["enabled"] is False
    assert buch.next_run("morgen") is None
    with pytest.raises(BlueprintError, match="already inactive"):
        buch.disable("morgen")


def test_enable_recreates_the_schedule_entry(tmp_path: Path) -> None:
    """Reaktivieren ist ein Schalter, kein Neuaufbau: Konversation und Identitaet
    kommen aus dem Stand, nicht aus einem neuen Aufruf."""
    buch = _buch(tmp_path)
    erster = buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    buch.disable("morgen")
    zweiter = buch.enable("morgen")
    assert zweiter.id != erster.id
    assert [t.id for t in buch._schedules.list_for(CHAT)] == [zweiter.id]
    assert buch.installed()["morgen"]["enabled"] is True
    with pytest.raises(BlueprintError, match="already active"):
        buch.enable("morgen")


def test_remove_takes_out_record_and_schedule(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    buch.install("puls", conversation=CHAT, principal=str(OWNER))
    buch.remove("morgen")
    assert set(buch.installed()) == {"puls"}
    rest = buch._schedules.list_for(CHAT)
    assert len(rest) == 1 and rest[0].interval_s == 7200
    with pytest.raises(BlueprintError, match="not installed"):
        buch.remove("morgen")


def test_remove_of_a_paused_blueprint_also_works(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    buch.disable("morgen")
    buch.remove("morgen")
    assert buch.installed() == {}


def test_unknown_blueprints_are_named(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    with pytest.raises(BlueprintError, match="no blueprint named"):
        buch.install("gibts-nicht", conversation=CHAT, principal=str(OWNER))


def test_the_state_survives_a_restart_and_stays_private(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    task = buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    # Dasselbe Verzeichnis, derselbe Stand, neue Instanz — wie nach einem Neustart.
    neu = BlueprintBook(buch._directory, buch._state_path, buch._schedules)
    assert neu.installed()["morgen"]["task_id"] == task.id
    assert neu.next_run("morgen") == task.next_run
    assert os.stat(buch._state_path).st_mode & 0o777 == 0o600


def test_a_broken_state_file_means_nothing_installed_not_a_crash(tmp_path: Path) -> None:
    buch = _buch(tmp_path)
    buch._state_path.parent.mkdir(parents=True, exist_ok=True)
    buch._state_path.write_text("{kaputt", encoding="utf-8")
    assert buch.installed() == {}
    with pytest.raises(BlueprintError, match="not installed"):
        buch.remove("morgen")


# --- Die Decke: ein Blueprint-Lauf darf nicht mehr als ein /every-Lauf ----------------

def test_a_blueprint_run_lives_under_the_unattended_ceiling(tmp_path: Path) -> None:
    """Installieren erteilt kein Recht: was der Auftrag spaeter will, geht durch
    denselben GovernedKernel wie jeder Zeitplan-Lauf — NEEDS_HUMAN wird DENY."""
    from talos.tools import default_manifest

    buch = _buch(tmp_path)
    task = buch.install("morgen", conversation=CHAT, principal=str(OWNER))
    assert task is not None
    ceiling = UnattendedCeiling()
    kernel = GovernedKernel(
        PolicyKernel(default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5),
        lambda _c: Trust.FULL,
        unattended=ceiling,
    )
    riskant = ToolRequest("write_file", OWNER, {"path": f"{HOME}/.bashrc", "content": "x"})
    assert kernel.decide(riskant).verdict is Verdict.NEEDS_HUMAN
    with ceiling.active():
        assert kernel.decide(riskant).verdict is Verdict.DENY
        # ... und die Decke laesst durch, was nichts braucht — ein Bericht-BLUEPRINT,
        # der nur liest, laeuft unbeaufsichtigt genauso wie ein getippter.
        harmlos = ToolRequest("read_file", OWNER, {"path": f"{HOME}/talos/README.md"})
        assert kernel.decide(harmlos).verdict is Verdict.ALLOW


# --- Die Kommandos --------------------------------------------------------------------

def test_blueprints_lists_available_and_state(tmp_path: Path) -> None:
    center = _center(tmp_path, _buch(tmp_path))
    antwort = center.dispatch("blueprints", "", principal=OWNER, conversation=CHAT).reply
    assert "morgen" in antwort and "puls" in antwort
    assert "not installed" in antwort


def test_blueprint_install_confirm_and_lifecycle(tmp_path: Path) -> None:
    center = _center(tmp_path, _buch(tmp_path))
    antwort = center.dispatch("blueprint", "install morgen", principal=OWNER, conversation=CHAT).reply
    assert "Installed 'morgen'" in antwort
    # Die Zusage an den Betreiber steht in der Bestaetigung, nicht im Kleingedruckten.
    assert "unattended" in antwort and "Nothing was granted" in antwort

    status = center.dispatch("blueprint", "status morgen", principal=OWNER, conversation=CHAT).reply
    assert "when: every morning 08:30" in status and "state: active" in status

    antwort = center.dispatch("blueprint", "disable morgen", principal=OWNER, conversation=CHAT).reply
    assert "Paused" in antwort
    antwort = center.dispatch("blueprint", "enable morgen", principal=OWNER, conversation=CHAT).reply
    assert "Active again" in antwort
    antwort = center.dispatch("blueprint", "remove morgen", principal=OWNER, conversation=CHAT).reply
    assert "Removed" in antwort
    assert center.blueprints.installed() == {}


def test_blueprint_errors_reach_the_operator_verbatim(tmp_path: Path) -> None:
    center = _center(tmp_path, _buch(tmp_path))
    antwort = center.dispatch("blueprint", "install gibts-nicht", principal=OWNER, conversation=CHAT).reply
    assert "no blueprint named 'gibts-nicht'" in antwort
    antwort = center.dispatch("blueprint", "frobnicate morgen", principal=OWNER, conversation=CHAT).reply
    assert "Unknown verb" in antwort
    antwort = center.dispatch("blueprint", "install", principal=OWNER, conversation=CHAT).reply
    assert "Which one?" in antwort


def test_no_registry_wired_is_a_sentence_not_a_crash(tmp_path: Path) -> None:
    center = _center(tmp_path, None)
    antwort = center.dispatch("blueprints", "", principal=OWNER, conversation=CHAT).reply
    assert antwort == "No blueprint registry wired."


def test_describe_next_matches_the_every_format() -> None:
    import time

    assert describe_next(None) == "not scheduled"
    assert describe_next(1000.0) == time.strftime("%a %d.%m %H:%M", time.localtime(1000.0))


# --- Gedaechtnis und Monitor: optionale Felder, die nur durchgereicht werden ----------

def test_blueprint_flags_reach_the_schedule_entry_unchanged(tmp_path: Path) -> None:
    """`continuity`, `monitor`, `probe` landen im selben Store-Eintrag wie ein /every —
    der Blueprint erfindet keinen zweiten Weg, er fuellt drei Felder."""
    vorlagen = tmp_path / "vorlagen"
    vorlagen.mkdir()
    _schreibe(vorlagen, "platte", when="every 2 hours", prompt="Pruefe die Platte",
              continuity=True, monitor=True, probe="df -h /")
    buch = BlueprintBook(vorlagen, tmp_path / "stand.json", ScheduleStore(tmp_path / "s.db"))
    task = buch.install("platte", conversation=CHAT, principal=str(OWNER))
    assert task.continuity and task.monitor and task.probe == "df -h /"
    # Reaktivieren baut denselben Eintrag wieder auf — Schalter inklusive.
    buch.disable("platte")
    wieder = buch.enable("platte")
    assert wieder.continuity and wieder.monitor and wieder.probe == "df -h /"


def test_blueprint_flags_are_strict_and_name_their_reason(tmp_path: Path) -> None:
    """Ein „yes" ist kein true, ein Monitor ohne Sonde hat nichts zu vergleichen —
    beides wird beim Laden abgewiesen und mit Dateinamen genannt, nie still ignoriert."""
    _schreibe(tmp_path, "text-flag", continuity="yes")
    _schreibe(tmp_path, "ohne-sonde", monitor=True)
    _schreibe(tmp_path, "sonde-ohne-monitor", probe="df -h")
    _schreibe(tmp_path, "sonde-kein-text", monitor=True, probe=["df", "-h"])
    _schreibe(tmp_path, "gut", continuity=True)
    katalog = load(tmp_path)
    assert [b.name for b in katalog.blueprints] == ["gut"]
    assert katalog.blueprints[0].continuity and not katalog.blueprints[0].monitor
    assert len(katalog.rejected) == 4
    assert any("text-flag.json" in g and "true or false" in g for g in katalog.rejected)
    assert any("ohne-sonde.json" in g and "belong together" in g for g in katalog.rejected)
    assert any("sonde-ohne-monitor.json" in g and "belong together" in g for g in katalog.rejected)
    assert any("sonde-kein-text.json" in g and "probe" in g for g in katalog.rejected)


def test_blueprint_status_names_memory_and_monitor(tmp_path: Path) -> None:
    vorlagen = tmp_path / "vorlagen"
    vorlagen.mkdir()
    _schreibe(vorlagen, "platte", when="every 2 hours", prompt="Pruefe die Platte",
              continuity=True, monitor=True, probe="df -h /")
    buch = BlueprintBook(vorlagen, tmp_path / "stand.json", ScheduleStore(tmp_path / "s.db"))
    center = _center(tmp_path, buch)
    status = center.dispatch("blueprint", "status platte", principal=OWNER, conversation=CHAT).reply
    assert "continuity" in status and "monitor" in status and "df -h /" in status
    # Ohne Schalter keine Zeile — was nicht gesetzt ist, wird nicht erwaehnt.
    _schreibe(vorlagen, "schlicht", when="every 2 hours", prompt="x")
    status = center.dispatch("blueprint", "status schlicht", principal=OWNER, conversation=CHAT).reply
    assert "continuity" not in status and "monitor" not in status
