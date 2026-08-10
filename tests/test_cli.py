"""Die Befehle neben dem Agenten — Diagnose und Konfiguration.

Der Punkt dieser Datei ist nicht, dass die Befehle etwas ausgeben. Er ist, dass sie
die Grenzen halten, die den Agenten selbst tragen: der Doktor **aendert nichts** und
geht **nicht von selbst ins Netz**, und `config` schreibt **nur**, was das Schema als
Einstellung fuehrt — nie die Rechteliste, nie ein Geheimnis.
"""
from __future__ import annotations

import io
import os
import re
import stat
from pathlib import Path

import pytest

from talos import configcli, doctor, schema
from talos.credentials import CredentialStore


def _out() -> io.StringIO:
    return io.StringIO()


# --- Schema: die Trennlinie -----------------------------------------------------------
def test_every_key_that_can_grant_power_is_policy_or_secret() -> None:
    """Das Sicherheitskriterium: kann eine Aenderung Befehlsgeber zulassen, einen
    Kernel-Filter lockern, geschuetzte Daten umleiten oder Zugangsdaten ersetzen?

    Der Test steht hier, damit ein spaeter hinzugefuegter Schluessel nicht stillschweigend
    als Einstellung durchgeht. Wer einen der vier hier herausnimmt, muss es begruenden.
    """
    for name in ("TALOS_ALLOWED_PRINCIPALS", "TALOS_SECRETS_ENV",
                 "TALOS_WEB_ALLOWED_ADDRESSES", "TALOS_SHELL_NEEDS_HUMAN"):
        eintrag = schema.get(name)
        assert eintrag is not None and eintrag.kind is schema.POLICY
        assert not eintrag.writable

    for name in ("TELEGRAM_BOT_TOKEN", "TALOS_MAIL_PASSWORD",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TALOS_BRAVE_API_KEY"):
        eintrag = schema.get(name)
        assert eintrag is not None and eintrag.kind is schema.SECRET
        assert not eintrag.writable and not eintrag.readable


def test_a_value_may_not_end_a_line() -> None:
    """Ein `\\n` im Wert haengt eine ZWEITE Zeile an — und die kann jeden anderen
    Schluessel setzen, auch die Rechteliste. Ein schreibbarer Schluessel waere damit
    ein Schreibrecht auf alle."""
    eintrag = schema.get("TALOS_MODEL")
    assert eintrag is not None and eintrag.validate is not None
    with pytest.raises(ValueError):
        eintrag.validate("x\nTALOS_ALLOWED_PRINCIPALS=telegram:666")
    with pytest.raises(ValueError):
        eintrag.validate("x\x00y")


# --- config: lesen ---------------------------------------------------------------------
def test_a_secret_answers_the_same_whether_it_is_set_or_not(tmp_path: Path) -> None:
    """Sonst ist `config get` ein Orakel dafuer, welche Zugaenge eine Maschine hat — die
    Auskunft, die ein Angreifer zuerst braucht. Deshalb keine Sternchen nach Laenge,
    kein Praefix, kein `last4`, kein Hash."""
    leer, voll = _out(), _out()
    assert configcli.cmd_get("TELEGRAM_BOT_TOKEN", {}, leer.write) == 0
    assert configcli.cmd_get("TELEGRAM_BOT_TOKEN", {"TELEGRAM_BOT_TOKEN": "123:geheim"},
                             voll.write) == 0
    assert leer.getvalue() == voll.getvalue() == schema.REDACTED + "\n"
    assert "geheim" not in voll.getvalue()


def test_the_listing_says_nothing_about_secrets(tmp_path: Path) -> None:
    text = _out()
    configcli.cmd_list({"TELEGRAM_BOT_TOKEN": "123:geheim", "TALOS_MODEL": "x"}, text.write)
    ausgabe = text.getvalue()
    assert "geheim" not in ausgabe
    zeile = next(z for z in ausgabe.splitlines() if "TELEGRAM_BOT_TOKEN" in z)
    assert "set" not in zeile                       # auch „gesetzt" ist eine Auskunft


# --- config: schreiben -----------------------------------------------------------------
def test_the_permission_list_is_not_settable_from_a_command_line(tmp_path: Path) -> None:
    """Nicht einmal mit Bestaetigung — eine Bestaetigung ist genau das, was weggeklickt
    wird. Wer diesen Schluessel schreiben kann, muss den Kernel nicht ueberreden."""
    ziel = tmp_path / "talos.env"
    text = _out()
    assert configcli.cmd_set("TALOS_ALLOWED_PRINCIPALS", "telegram:666", ziel, text.write) == 1
    assert not ziel.exists()


def test_a_secret_is_not_settable_from_a_command_line(tmp_path: Path) -> None:
    """Ein Wert auf der Kommandozeile landet in der Shell-Historie und in `ps` —
    fuer jeden Nutzer der Maschine."""
    ziel = tmp_path / "talos.env"
    text = _out()
    assert configcli.cmd_set("TELEGRAM_BOT_TOKEN", "123:abc", ziel, text.write) == 1
    assert not ziel.exists()
    assert "shell history" in text.getvalue()


def test_a_setting_is_written_and_the_operators_own_lines_survive(tmp_path: Path) -> None:
    """Wer eine Konfiguration umschreibt und dabei die Notizen des Betreibers verliert,
    wird beim naechsten Mal von Hand editiert."""
    ziel = tmp_path / "talos.env"
    ziel.write_text("# meine Notiz\nTALOS_MODEL=alt\nFREMDER_WERT=x\n", encoding="utf-8")
    assert configcli.cmd_set("TALOS_MODEL", "neu", ziel, _out().write) == 0
    inhalt = ziel.read_text(encoding="utf-8")
    assert "# meine Notiz" in inhalt and "FREMDER_WERT=x" in inhalt
    assert configcli.parse_env(inhalt)["TALOS_MODEL"] == "neu"


def test_the_new_file_carries_its_mode_from_the_first_byte(tmp_path: Path) -> None:
    """Der Fehler, den Hermes gemacht hat: erst schreiben, dann `chmod 600` — die Datei
    war dazwischen world-readable, und ein Zeitfenster reicht. Deshalb `os.open` mit
    Modus statt `chmod` hinterher."""
    ziel = tmp_path / "talos.env"
    configcli.write_key(ziel, "TALOS_MODEL", "x")
    assert stat.S_IMODE(ziel.stat().st_mode) == 0o600


def test_a_symlink_in_the_way_does_not_redirect_the_write(tmp_path: Path) -> None:
    """Laege an der Stelle der temporaeren Datei ein Symlink, schriebe ein gewoehnliches
    `open()` an dessen Ziel — und wer den Symlink legen durfte, bestimmte damit, wohin
    die Konfiguration geht.

    Zwei Dinge halten das: der Link wird entfernt, und die Datei danach mit
    `O_EXCL | O_NOFOLLOW` angelegt. `O_EXCL` schliesst das Fenster dazwischen — wer in
    diesem Moment einen neuen Link legt, erreicht ein Fehlschlagen, nie eine Umleitung.
    """
    ziel = tmp_path / "talos.env"
    ziel.write_text("TALOS_MODEL=alt\n", encoding="utf-8")
    beute = tmp_path / "beute.txt"
    beute.write_text("unberuehrt", encoding="utf-8")
    (tmp_path / "talos.env.neu").symlink_to(beute)

    configcli.write_key(ziel, "TALOS_MODEL", "neu")
    assert beute.read_text(encoding="utf-8") == "unberuehrt"
    assert configcli.parse_env(ziel.read_text(encoding="utf-8"))["TALOS_MODEL"] == "neu"
    assert not (tmp_path / "talos.env.neu").exists()


def test_a_symlink_appearing_in_the_race_window_fails_instead_of_redirecting(
    tmp_path: Path, monkeypatch
) -> None:
    """Das Fenster selbst, nachgestellt: zwischen Entfernen und Anlegen schiebt jemand
    einen Link unter. `O_EXCL` laesst den Schreibvorgang scheitern — und die Beute
    bleibt unberuehrt. Ein Fehlschlag ist hier das richtige Ergebnis."""
    ziel = tmp_path / "talos.env"
    ziel.write_text("TALOS_MODEL=alt\n", encoding="utf-8")
    beute = tmp_path / "beute.txt"
    beute.write_text("unberuehrt", encoding="utf-8")

    echtes_unlink = Path.unlink

    def unterschieben(self, *args, **kwargs):
        echtes_unlink(self, *args, **kwargs)
        if self.name.endswith(".neu"):
            self.symlink_to(beute)          # der Angreifer, genau im Fenster
    monkeypatch.setattr(Path, "unlink", unterschieben)

    with pytest.raises(OSError):
        configcli.write_key(ziel, "TALOS_MODEL", "neu")
    assert beute.read_text(encoding="utf-8") == "unberuehrt"


def test_a_duplicate_key_collapses_into_one(tmp_path: Path) -> None:
    """Zwei Zeilen desselben Schluessels sind eine Falle: welche gilt, entscheidet die
    Lesereihenfolge, und die sieht man der Datei nicht an."""
    ziel = tmp_path / "talos.env"
    ziel.write_text("TALOS_MODEL=a\nTALOS_MODEL=b\n", encoding="utf-8")
    configcli.write_key(ziel, "TALOS_MODEL", "c")
    assert ziel.read_text(encoding="utf-8").count("TALOS_MODEL=") == 1


def test_validate_reports_foreign_keys_without_calling_them_errors(tmp_path: Path) -> None:
    """Eine Konfiguration darf Zeilen tragen, die Talos nicht liest. Wer sie verboten
    kaeme, zwaenge dazu, sie woanders zu verstecken."""
    ziel = tmp_path / "talos.env"
    ziel.write_text("TALOS_MODEL=x\nMEIN_SKRIPT_PFAD=/tmp/a\n", encoding="utf-8")
    text = _out()
    assert configcli.cmd_validate(configcli.read_file(ziel), ziel, text.write) == 0
    assert "MEIN_SKRIPT_PFAD" in text.getvalue()


def test_validate_fails_on_a_value_the_schema_rejects(tmp_path: Path) -> None:
    ziel = tmp_path / "talos.env"
    ziel.write_text("TALOS_POLL_TIMEOUT_S=-5\n", encoding="utf-8")
    text = _out()
    assert configcli.cmd_validate(configcli.read_file(ziel), ziel, text.write) == 1


# --- doctor ----------------------------------------------------------------------------
def test_the_doctor_changes_nothing(tmp_path: Path) -> None:
    """Ein Doktor, der nebenbei repariert, ist keiner: dann weiss niemand mehr, in
    welchem Zustand die Maschine vorher war. Insbesondere legt er kein Verzeichnis an."""
    fehlend = tmp_path / "gibt-es-nicht"
    vorher = sorted(p.name for p in tmp_path.iterdir())
    befunde = doctor.check_state(data_dir=fehlend, workspace=fehlend / "workspace")
    assert befunde
    assert not fehlend.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == vorher


def _echte_config(**over):
    """Eine ECHTE `TalosConfig`, kein Double.

    ⚠️ Der Grund steht in Falle 7 der `CLAUDE.md`, und sie hat hier sofort zugeschlagen:
    ein handgebautes Double hatte ein Feld `model`, die echte Klasse heisst `model_name`.
    Alle Tests blieben gruen, und `talos doctor` fiel auf dem Pi mit `AttributeError` um —
    ausgerechnet der Befehl, der Stoerungen finden soll. Ein Double bildet die
    Wirklichkeit nur so lange ab, wie jemand es nachzieht.
    """
    from talos.config import TalosConfig

    werte = dict(
        bot_token="123:abc", bot_username="bot", allowed_principals=frozenset(),
        eventlog_db=Path("/tmp/talos-test/eventlog.db"),
        snapshot_dir=Path("/tmp/talos-test/snapshots"),
    )
    werte.update(over)
    return TalosConfig(**werte)


def test_the_doctor_reads_only_fields_the_real_config_has() -> None:
    """Der Waechter ueber dem Fall oben: `collect` gegen die echte Klasse, nicht gegen
    ein Double. Ein falscher Feldname fliegt hier statt im Betrieb."""
    befunde = doctor.collect(_echte_config())
    assert {"model", "channels", "identity"} <= {b.area for b in befunde}


def test_the_doctor_does_not_go_online_unless_asked() -> None:
    """Ein Diagnosebefehl, der ungefragt den Bot-Token an Telegram schickt, ist eine
    Ueberraschung — und ausgerechnet dort unbrauchbar, wo man ihn braucht: ohne Netz."""
    gerufen: list[str] = []

    doctor.collect(_echte_config(), online=False,
                   get=lambda *a, **k: gerufen.append(a) or {})
    assert gerufen == []


def test_the_doctor_still_reports_when_the_config_will_not_load(tmp_path: Path) -> None:
    """Der haeufigste Grund, den Befehl aufzurufen — und ausgerechnet dann duerfen nicht
    alle Befunde ausfallen."""
    befunde = doctor.collect(None)
    bereiche = {b.area for b in befunde}
    assert {"runtime", "capabilities", "sandbox", "state"} <= bereiche
    assert "model" not in bereiche          # ohne Konfiguration ehrlich nichts dazu


def test_only_something_truly_missing_makes_the_doctor_fail() -> None:
    """Sonst faellt ein Cron-Waechter wegen einer Faehigkeit um, die niemand benutzt."""
    nur_notizen = (
        doctor.Check("capabilities", "ffmpeg", doctor.WARN, "missing"),
        doctor.Check("capabilities", "piper", doctor.FAIL, "missing"),   # nicht kritisch
    )
    _, code = doctor.summary(nur_notizen)
    assert code == 0

    mit_hindernis = nur_notizen + (
        doctor.Check("identity", "allowlist", doctor.FAIL, "empty", critical=True),
    )
    text, code = doctor.summary(mit_hindernis)
    assert code == 1 and "allowlist" in text


def test_the_doctor_names_no_secret() -> None:
    """Eine Diagnose, die man nicht in ein Ticket kopieren darf, wird nicht benutzt."""
    config = _echte_config(
        bot_token="123:ABCgeheim", model_provider="anthropic-api",
        mail_host="imap.example", mail_user="wer@example", mail_password="s3hrGeheim",
    )
    text = doctor.render(doctor.collect(config))
    assert "ABCgeheim" not in text and "s3hrGeheim" not in text


def test_a_config_file_readable_by_everyone_is_reported(tmp_path: Path) -> None:
    """`chmod 644` an dieser Datei ist kein Schoenheitsfehler: jeder Nutzer der Maschine
    liest dann den Bot-Token und die Rechteliste."""
    datei = tmp_path / "talos.env"
    datei.write_text("TELEGRAM_BOT_TOKEN=1\n", encoding="utf-8")
    os.chmod(datei, 0o644)
    befund = doctor.check_config((datei,))[0]
    assert befund.state is doctor.WARN and "world-readable" in befund.detail

    os.chmod(datei, 0o600)
    assert doctor.check_config((datei,))[0].state is doctor.OK


def test_a_provider_without_its_cli_is_a_blocking_finding() -> None:
    """Der haeufigste stille Ausfall: der Agent startet, nimmt Nachrichten an und
    scheitert erst am ersten Gedanken."""
    befunde = doctor.check_model(
        "claude-cli", "claude-fable-5", claude_bin="claude", hermes_bin="hermes",
        credentials=CredentialStore(), which=lambda _name: None,
    )
    assert any(b.blocking for b in befunde)

    befunde = doctor.check_model(
        "claude-cli", "claude-fable-5", claude_bin="claude", hermes_bin="hermes",
        credentials=CredentialStore(), which=lambda name: f"/usr/local/bin/{name}",
    )
    assert not any(b.blocking for b in befunde)


# --- Live-Modellliste ------------------------------------------------------------------
def test_a_provider_that_is_down_does_not_empty_the_catalogue() -> None:
    """Der umgekehrte Weg — live gewinnt — machte einen erreichbaren Fremdserver zu dem,
    der bestimmt, womit dieser Agent denkt."""
    from talos import models
    from talos.provider import Provider, ProviderRegistry

    katalog = ProviderRegistry((Provider("openai-api", "OpenAI", ("gpt-5.2", "o4-mini")),))
    voll = models.merged(katalog, {})               # leerer Zwischenspeicher = Ausfall
    assert voll.providers[0].models == ("gpt-5.2", "o4-mini")


def test_live_names_are_added_after_the_curated_ones() -> None:
    from talos import models
    from talos.provider import Provider, ProviderRegistry

    katalog = ProviderRegistry((Provider("openai-api", "OpenAI", ("gpt-5.2",)),))
    cache = {"openai-api": {"models": ["gpt-5.2", "gpt-6-neu"], "fetched_at": 1000.0}}
    voll = models.merged(katalog, cache, now=lambda: 1010.0)
    assert voll.providers[0].models == ("gpt-5.2", "gpt-6-neu")


def test_a_live_list_cannot_smuggle_a_claude_model_under_a_foreign_provider() -> None:
    """Sonst laeuft ein Aufruf ueber ein Konto, ueber das niemand entschieden hat."""
    from talos import models
    from talos.provider import Provider, ProviderRegistry

    katalog = ProviderRegistry((Provider("openai-api", "OpenAI", ("gpt-5.2",)),))
    cache = {"openai-api": {"models": ["claude-opus-5", "gpt-6"], "fetched_at": 1000.0}}
    voll = models.merged(katalog, cache, now=lambda: 1010.0)
    assert voll.providers[0].models == ("gpt-5.2", "gpt-6")


def test_a_stale_cache_is_ignored_and_so_is_a_clock_that_jumped() -> None:
    from talos import models

    cache = {"x": {"models": ["a"], "fetched_at": 1000.0}}
    assert models.fresh_models(cache, "x", now=lambda: 1010.0) == ("a",)
    assert models.fresh_models(cache, "x", now=lambda: 1000.0 + models.CACHE_TTL_S + 1) == ()
    assert models.fresh_models(cache, "x", now=lambda: 500.0) == ()      # Uhr sprang zurueck


def test_a_hostile_answer_cannot_break_the_picker() -> None:
    """Die Antwort ist fremder Text und landet in Telegram-Callback-Daten (64 Byte).
    Form, Laenge und Anzahl werden deshalb begrenzt — und Doubletten fallen weg."""
    from talos import models

    roh = ["gut-1", "x" * 300, "", "  ", "boes;rm -rf /", "gut-1", "../../etc/passwd",
           "<script>", *[f"m{i}" for i in range(200)]]
    sauber = models.clean_names(roh)
    assert "gut-1" in sauber
    assert len(sauber) <= models.MAX_MODELS
    assert all(len(n) <= models.MAX_NAME_CHARS for n in sauber)
    assert not any(z in "".join(sauber) for z in " ;<>")
    assert len(set(sauber)) == len(sauber)


def test_no_key_means_no_request_at_all() -> None:
    """Sonst wandert eine Anfrage an einen fremden Server, ohne dass jemand dafuer
    etwas hinterlegt haette."""
    from talos import models

    gerufen: list[str] = []
    ergebnis = models.fetch("openai-api", api_key="  ",
                            get=lambda *a, **k: gerufen.append(a) or None, now=lambda: 1.0)
    assert gerufen == [] and ergebnis.models == () and "no key" in ergebnis.error


def test_a_failed_fetch_keeps_yesterdays_list(tmp_path: Path) -> None:
    """Eine Stoerung beim Anbieter darf keine Liste vernichten, die gestern noch stimmte."""
    from talos import models

    pfad = tmp_path / "cache.json"
    models.save_cache(pfad, {"openai-api": {"models": ["gpt-5.2"], "fetched_at": 900.0}})

    def kaputt(*_a, **_k):
        raise OSError("network down")

    models.refresh(("openai-api",), keys={"openai-api": "k"}, path=pfad, get=kaputt,
                   now=lambda: 1000.0)
    assert models.load_cache(pfad)["openai-api"]["models"] == ["gpt-5.2"]


def test_the_key_goes_in_the_header_the_provider_expects() -> None:
    """Anthropic will `x-api-key` plus Version, OpenAI ein Bearer-Token. Ein Schluessel
    im falschen Kopf ist ein 401, das wie ein gesperrtes Konto aussieht."""
    from talos import models

    gesehen: dict[str, dict] = {}

    def merken(url, headers=None, timeout=None):
        gesehen[url] = dict(headers or {})
        return type("A", (), {"json": lambda self: {"data": [{"id": "m1"}]}})()

    models.fetch("anthropic-api", api_key="k1", get=merken, now=lambda: 1.0)
    models.fetch("openai-api", api_key="k2", get=merken, now=lambda: 1.0)
    anthropic = gesehen["https://api.anthropic.com/v1/models"]
    openai = gesehen["https://api.openai.com/v1/models"]
    assert anthropic["x-api-key"] == "k1" and anthropic["anthropic-version"]
    assert openai["Authorization"] == "Bearer k2"


# --- Die Befehlszeile selbst ------------------------------------------------------------
def test_the_help_names_exactly_the_commands_that_exist() -> None:
    """Eine Hilfe, die einen Befehl nennt, den es nicht gibt, ist schlimmer als keine —
    und ein Befehl, den die Hilfe verschweigt, wird nie benutzt."""
    from talos import cli

    for name in cli.COMMANDS:
        assert name in cli.HELP, f"{name} fehlt in der Hilfe"
    # Eine Befehlszeile in der Hilfe ist: zwei Leerzeichen, der Name, dann ein Abstand
    # von mindestens vier Leerzeichen zur Beschreibung. Fliesstext faellt damit heraus.
    eintrag = re.compile(r"^  ([a-z][\w-]*)[^\n]*?\s{4,}\S")
    genannt = {
        treffer.group(1) for treffer in
        (eintrag.match(zeile) for zeile in cli.HELP.splitlines()) if treffer
    }
    unbekannt = genannt - set(cli.COMMANDS) - {"python"}
    assert not unbekannt, f"die Hilfe nennt, was es nicht gibt: {unbekannt}"
    assert set(cli.COMMANDS) <= genannt


def test_no_argument_means_run_and_an_unknown_one_means_two() -> None:
    """`None` heisst starten. Ein Tippfehler darf NICHT starten — sonst laeuft der Agent,
    weil jemand `talos doctro` geschrieben hat."""
    from talos import cli

    assert cli.dispatch([]) is None
    assert cli.dispatch(["--once"]) is None
    assert cli.dispatch(["doctro"]) == 2


def test_every_subcommand_is_reachable_without_a_configuration(monkeypatch) -> None:
    """⚠️ Die Unterbefehle laufen VOR `load_config()`. Sonst stirbt ausgerechnet `setup`
    an der fehlenden Konfiguration, die es anlegen soll — und `doctor` an genau dem
    Zustand, den zu diagnostizieren seine Aufgabe ist."""
    import talos.config as config
    from talos import cli

    def kaputt():
        raise ValueError("TELEGRAM_BOT_TOKEN fehlt")

    monkeypatch.setattr(config, "load_config", kaputt)
    for name in ("version", "help"):
        assert cli.TABLE[name]([]) == 0


def test_status_reads_the_log_and_invents_nothing() -> None:
    """Bewusst nicht „laeuft der Dienst": das weiss der Dienstverwalter besser, und eine
    zweite Antwort darauf waere eine, die manchmal luegt."""
    from talos import cli

    class _Log:
        def recent(self, limit=10, types=()):
            if types == ("model.selected",):
                return [{"payload": {"provider": "claude-cli", "model": "claude-fable-5"}}]
            return [{"ts": 1785941996.0, "type": "tool.done", "actor": "telegram:1"}]

    text = _out()
    assert cli.cmd_status(text, log=_Log()) == 0
    ausgabe = text.getvalue()
    assert "claude-cli / claude-fable-5" in ausgabe
    assert "2026-" in ausgabe                     # ein Datum, keine abgeschnittene Zahl


def test_verify_reports_an_intact_log_with_exit_zero() -> None:
    from talos import cli

    class _Log:
        def verify(self):
            return None

        def count(self):
            return 5

        def protected_count(self):
            return 5

    text = _out()
    assert cli.cmd_verify(text, log=_Log()) == 0
    assert "intact" in text.getvalue()


def test_verify_names_the_break_and_exits_nonzero() -> None:
    """Ein Skript (Cron, Installer) soll an einem manipulierten Log SCHEITERN, nicht still
    weiterlaufen — deshalb Exit 1 und die erste gebrochene id im Klartext."""
    from talos import cli

    class _Log:
        def verify(self):
            return 3

        def count(self):
            return 5

        def protected_count(self):
            return 5

    text = _out()
    assert cli.cmd_verify(text, log=_Log()) == 1
    assert "id 3" in text.getvalue()


def test_verify_is_honest_about_an_unproven_legacy_prefix() -> None:
    """Frisch aktualisiert: die Kette deckt nur die neuen Zeilen. „intakt" darf das nicht
    verschweigen, sonst ist es die Halbwahrheit, die diese Software zu vermeiden verspricht."""
    from talos import cli

    class _Log:
        def verify(self):
            return None

        def count(self):
            return 10

        def protected_count(self):
            return 4

    text = _out()
    assert cli.cmd_verify(text, log=_Log()) == 0
    ausgabe = text.getvalue()
    assert "intact" in ausgabe
    assert "6" in ausgabe                          # 10 - 4 ungeschuetzte Alt-Zeilen benannt


def test_a_config_file_owned_by_someone_else_is_the_stricter_setup(tmp_path: Path) -> None:
    """⚠️ Der Eigentuemer zaehlt mehr als der Modus.

    `640` unter einem FREMDEN Benutzer ist strenger als `600` unter dem eigenen: dann kann
    der Agent seine Rechteliste ueberhaupt nicht mehr schreiben, auch nicht an einem
    Codefehler vorbei. Ein Doktor, der das als Mangel meldet, erzieht zum Rueckbau —
    genau die Einrichtung, die die Pruefung als einzige echte Grenze nennt.
    """
    datei = tmp_path / "agent.env"
    datei.write_text("TELEGRAM_BOT_TOKEN=1\n", encoding="utf-8")
    os.chmod(datei, 0o640)

    # Aus Sicht eines anderen Benutzers: die Datei gehoert nicht uns und ist nicht
    # schreibbar. `uid` wird injiziert, weil ein Test nicht root sein darf.
    os.chmod(datei, 0o440)
    befund = doctor.check_config((datei,), uid=os.stat(datei).st_uid + 1)[0]
    assert befund.state is doctor.OK and "cannot rewrite its own allowlist" in befund.detail


# --- talos ask: ein Zug von der Kommandozeile -------------------------------------------
def test_the_command_line_is_a_channel_and_grants_nothing_by_itself() -> None:
    """⚠️ Wer hier tippt, muss in der Allowlist stehen — genau wie eine Telegram-Nummer.

    Die Kennung automatisch zuzulassen hiesse, einen Eingang zu bauen, den niemand
    freigegeben hat: ausgerechnet den mit Shell-Rechten daneben.
    """
    from talos.askcli import CHANNEL_NAME, check_identity
    from talos.channel import Principal

    assert check_identity(frozenset(), 1000)
    assert "cli:1000" in check_identity(frozenset(), 1000)
    assert check_identity(frozenset({Principal("telegram", "1000")}), 1000)   # anderer Kanal!
    assert not check_identity(frozenset({Principal(CHANNEL_NAME, "1000")}), 1000)


def test_the_agent_cannot_give_itself_orders_through_its_own_shell() -> None:
    """Der Agent hat eine Shell. Koennte er darin `talos ask` starten, haette er einen
    Weg, sich selbst Auftraege zu geben — ohne Kanal, ohne fremde Kennung, ohne Leser."""
    from talos import sandbox
    from talos.askcli import refuse_in_sandbox

    assert not refuse_in_sandbox({})
    assert refuse_in_sandbox({sandbox.MARKER: "1"})
    # Der Marker steht wirklich in der Umgebung, die die Sandbox setzt — sonst prueft
    # der Riegel etwas, das es im Betrieb nicht gibt.
    assert sandbox.sandbox_env(Path("/tmp/talos-test")).get(sandbox.MARKER) == "1"


def test_the_question_is_delivered_exactly_once() -> None:
    """Der Zug soll einmal laufen, nicht in einer Schleife dieselbe Frage beantworten."""
    from talos.askcli import CliChannel

    kanal = CliChannel("wie spaet ist es?", 1000, out=_out())
    erste = kanal.poll()
    assert len(erste) == 1 and erste[0].text == "wie spaet ist es?"
    assert erste[0].principal.channel == "cli"
    assert kanal.poll() == []


def test_the_answer_goes_to_stdout_and_marks_the_turn_done() -> None:
    from talos.askcli import CliChannel

    text = _out()
    kanal = CliChannel("frage", 1000, out=text)
    assert not kanal.answered
    kanal.send(kanal.conversation, "die Antwort\n")
    assert kanal.answered and text.getvalue() == "die Antwort\n"


def test_a_question_back_is_printed_rather_than_swallowed() -> None:
    """Knoepfe gibt es hier nicht — eine Rueckfrage, die stumm bliebe, saehe aus wie
    ein Haenger."""
    from talos.askcli import CliChannel

    text = _out()
    kanal = CliChannel("frage", 1000, out=text)
    kanal.send_structured(kanal.conversation,
                          type("M", (), {"text": "welche Datei meinst du?"})())
    assert "welche Datei" in text.getvalue()


# --- Die Beispielkonfiguration ist Teil des Vertrags ------------------------------------
def test_the_example_config_only_names_keys_the_schema_knows() -> None:
    """`.env.example` ist die erste Datei, die ein Fremder kopiert.

    Nennt sie einen Schluessel, den das Schema nicht kennt, taucht er in `config list`
    nicht auf — und nennt sie einen, den es nicht mehr gibt, richtet sich jemand nach
    einer Anleitung, die der Agent beim Start ablehnt. Genau das ist mit
    `TALOS_API_BASE_URL` passiert.
    """
    import re

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    genannt = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    unbekannt = {name for name in genannt if schema.get(name) is None}
    assert not unbekannt, f".env.example nennt Schluessel ohne Schema-Eintrag: {sorted(unbekannt)}"


def test_the_example_config_does_not_name_the_retired_base_url() -> None:
    """Sie stoppt den Start — eine Vorlage, die sie nennt, ist eine Falle."""
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "TALOS_API_BASE_URL=" not in text
