"""Innenansicht: `/usage`, `/model`, `/reasoning`, `/debug`.

Diese vier Kommandos zeigen nur an. Ihr Risiko ist deshalb kein Bypass, sondern eine
**Behauptung**: eine Zahl, die niemand gemessen hat, oder ein Schalter, der nichts
umstellt. Genau darauf zielen die Tests hier — plus die eine Sache, die eine Anzeige
wirklich gefaehrlich macht: dass sie ein Secret mit ausgibt.
"""
from __future__ import annotations

import json
from pathlib import Path

from talos.approval import ApprovalStore
from talos.autonomy import AutonomyGovernor
from talos.capability import CapabilityMint
from talos.channel import ChannelRegistry, Principal, Trust
from talos.commands import CommandCenter
from talos.eventlog import EventLog
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.memory import Memory
from talos.policy import PolicyKernel
from talos.reasoner import ClaudeCliReasoner, _interpret, _run_from
from talos.usage import Run, Snapshot, UsageMeter

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:4242"
TOKEN = "8123456789:AAH-nicht-echt-aber-formatgleich"


class _FakeWorker:
    def pending(self) -> int:
        return 0

    def busy(self) -> bool:
        return False

    def drain(self) -> int:
        return 0


class _FakeReasoner:
    def cancel(self) -> bool:
        return False


class _FakeChannel:
    def __init__(self, name: str, trust: Trust) -> None:
        self.name = name
        self.trust = trust
        self.token = TOKEN  # wie beim echten Telegram-Kanal: das Secret haengt am Objekt

    def poll(self) -> list:
        return []

    def send(self, conversation: str, text: str) -> None:
        return None


def _manifest() -> ToolManifest:
    return (
        ToolManifest()
        .with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
        .with_tool(ToolSpec("write_file", Effect.WRITE, reversible=True))
    )


def _center(tmp_path: Path, **kwargs) -> CommandCenter:
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    defaults = dict(
        log=EventLog(tmp_path / "events.db"),
        approvals=ApprovalStore(),
        policy=policy,
        started_at=0.0,
        bot_username="Talos_bot",
        reasoner=_FakeReasoner(),
        worker=_FakeWorker(),
        repo_dir=tmp_path,
        mint=CapabilityMint(policy),
    )
    defaults.update(kwargs)
    return CommandCenter(**defaults)


def _say(center: CommandCenter, name: str) -> str:
    return center.dispatch(name, "", principal=OWNER, conversation=CHAT).reply or ""


def _run(**kwargs) -> Run:
    base = dict(at=1_700_000_000.0, ok=True, duration_s=12.0)
    base.update(kwargs)
    return Run(**base)  # type: ignore[arg-type]


# --- /usage: gemessen, nicht geschaetzt -------------------------------------------


def test_usage_without_meter_says_so_instead_of_showing_zeros(tmp_path: Path) -> None:
    """Null Laeufe und „kein Zaehler" sind zwei verschiedene Aussagen."""
    assert "Kein Verbrauchszaehler" in _say(_center(tmp_path), "usage")


def test_usage_without_runs_shows_nothing_rather_than_zeros(tmp_path: Path) -> None:
    reply = _say(_center(tmp_path, usage=UsageMeter()), "usage")
    assert "Noch kein Denk-Lauf" in reply
    assert "$" not in reply


def test_usage_shows_measured_numbers(tmp_path: Path) -> None:
    meter = UsageMeter()
    meter.record(_run(input_tokens=1200, output_tokens=340, cost_usd=0.25, model="claude-opus-4-8"))
    meter.record(_run(input_tokens=800, output_tokens=160, cost_usd=0.15, duration_s=8.0))

    reply = _say(_center(tmp_path, usage=meter), "usage")

    assert "Laeufe: 2" in reply
    assert "2.0k rein" in reply  # 1200 + 800
    assert "500 raus" in reply
    assert "$0.40" in reply


def test_usage_calls_cost_notional_not_billed(tmp_path: Path) -> None:
    """Der wichtigste Satz der Anzeige: das ist keine Rechnung.

    Talos laeuft ueber ein Abo. Ein Dollarbetrag ohne diesen Hinweis liest sich wie
    eine Abbuchung — und waere damit die Behauptung, die dieses Kommando vermeiden soll.
    """
    meter = UsageMeter()
    meter.record(_run(cost_usd=0.33))

    reply = _say(_center(tmp_path, usage=meter), "usage")

    assert "Rechnerisch" in reply
    assert "Abo" in reply
    assert "abgerechnet wird davon nichts" in reply


def test_usage_counts_failed_runs_too(tmp_path: Path) -> None:
    """Ein Zaehler, der nur Erfolge zeigt, versteckt genau das, was man sucht."""
    meter = UsageMeter()
    meter.record(_run(ok=True))
    meter.record(_run(ok=False, note="Zeitueberschreitung"))

    reply = _say(_center(tmp_path, usage=meter), "usage")

    assert "Laeufe: 2 (1 ohne Ergebnis)" in reply
    assert "Zeitueberschreitung" in reply  # steht als Auffaelligkeit am letzten Lauf


def test_usage_flags_inherited_agent_config_when_cache_is_huge(tmp_path: Path) -> None:
    """Der bekannte Befund soll sichtbar werden, nicht nur im Vault stehen."""
    meter = UsageMeter()
    meter.record(_run(cache_write=51_379, cache_read=14_566))

    reply = _say(_center(tmp_path, usage=meter), "usage")

    assert "Auffaellig" in reply
    assert "Home-Verzeichnis" in reply


def test_usage_stays_quiet_when_cache_is_normal(tmp_path: Path) -> None:
    meter = UsageMeter()
    meter.record(_run(cache_read=500))

    assert "Auffaellig" not in _say(_center(tmp_path, usage=meter), "usage")


# --- /model: Bericht, kein Schalter ------------------------------------------------


def test_model_names_no_model_when_nothing_ran(tmp_path: Path) -> None:
    reply = _say(_center(tmp_path, usage=UsageMeter(), claude_bin=""), "model")
    assert "noch nichts gemeldet" in reply


def test_model_reports_what_actually_thought(tmp_path: Path) -> None:
    meter = UsageMeter()
    meter.record(
        _run(model="claude-opus-4-8", models=("claude-haiku-4-5", "claude-opus-4-8"))
    )

    reply = _say(_center(tmp_path, usage=meter, claude_bin=""), "model")

    assert "opus-4-8" in reply
    assert "haiku-4-5" in reply
    assert "Hauptlast: opus-4-8" in reply


def test_model_says_there_is_no_switch(tmp_path: Path) -> None:
    """Andere Agenten koennen umstellen, Talos nicht — und sagt es, statt einen Knopf zu malen."""
    reply = _say(_center(tmp_path, usage=UsageMeter(), claude_bin=""), "model")
    assert "kein Schalter" in reply or "keinen Schalter" in reply
    assert "Behauptung" in reply


# --- /reasoning: die echten Einstellungen ------------------------------------------


def test_reasoning_shows_real_limits_and_points_at_the_real_dial(tmp_path: Path) -> None:
    center = _center(tmp_path, reasoner_timeout_s=180, memory=Memory())

    reply = _say(center, "reasoning")

    assert "180s" in reply
    assert "abgeschaltet" in reply  # die gesperrten CLI-Werkzeuge
    assert "/autonomy" in reply
    assert "keine Erlaubnisquelle" in reply  # der Verlauf im Prompt


def test_reasoning_reports_the_real_adaptive_effort_router(tmp_path: Path) -> None:
    reply = _say(_center(tmp_path, reasoner_timeout_s=180), "reasoning")
    assert "low" in reply and "medium" in reply and "high" in reply
    assert "automatisch" in reply and "Hermes --reasoning" in reply


# --- /debug: zeigen, ohne zu verraten ----------------------------------------------


def _full_center(tmp_path: Path) -> CommandCenter:
    meter = UsageMeter()
    meter.record(_run(model="claude-opus-4-8"))
    memory = Memory()
    memory.remember(CHAT, asked="frage", answered="antwort")
    return _center(
        tmp_path,
        usage=meter,
        memory=memory,
        governor=AutonomyGovernor(3),
        channels=ChannelRegistry((_FakeChannel("telegram", Trust.FULL),)),
        claude_bin="",
        reasoner_timeout_s=180,
        eventlog_db=tmp_path / "events.db",
        snapshot_dir=tmp_path,
    )


def test_debug_shows_state(tmp_path: Path) -> None:
    reply = _say(_full_center(tmp_path), "debug")

    assert "Event-Log" in reply
    assert "telegram (FULL)" in reply
    assert "Dial: 3" in reply
    assert "History: 2 turns" in reply  # ein Austausch = Frage + Antwort


def test_debug_leaks_no_secret_and_no_identity(tmp_path: Path) -> None:
    """Die einzige echte Gefahr einer Diagnose-Anzeige.

    `/debug` fasst Objekte an, an denen Secrets haengen (der Kanal traegt sein Token).
    Es zaehlt Identitaeten, statt sie zu nennen — wer seine eigene sehen will, nimmt
    `/whoami`, und das antwortet nur der fragenden Person ueber sich selbst.
    """
    reply = _say(_full_center(tmp_path), "debug")

    assert TOKEN not in reply
    assert "AAH" not in reply
    assert OWNER.user_id not in reply
    assert "Zugelassen: 1 Identitaet(en)" in reply


def test_debug_survives_missing_pieces(tmp_path: Path) -> None:
    """Eine Diagnose, die beim ersten fehlenden Teil abstuerzt, ist keine."""
    reply = _say(_center(tmp_path), "debug")

    assert "Diagnose" in reply
    assert "Ereignisse" in reply


# --- Der Zaehler selbst -------------------------------------------------------------


def test_meter_sums_and_keeps_the_last_run() -> None:
    meter = UsageMeter()
    meter.record(_run(input_tokens=10, cost_usd=0.1, duration_s=1.0))
    meter.record(_run(input_tokens=5, cost_usd=0.2, duration_s=2.0, note="letzter"))

    snap = meter.snapshot()

    assert (snap.runs, snap.input_tokens, snap.seconds) == (2, 15, 3.0)
    assert round(snap.cost_usd, 2) == 0.30
    assert snap.last is not None and snap.last.note == "letzter"


def test_snapshot_is_a_copy_not_a_live_view() -> None:
    """Sonst aendert sich die Anzeige zwischen zwei Zeilen derselben Ausgabe."""
    meter = UsageMeter()
    meter.record(_run())
    before = meter.snapshot()
    meter.record(_run())

    assert before.runs == 1
    assert meter.snapshot().runs == 2


def test_negative_duration_does_not_shrink_the_total() -> None:
    meter = UsageMeter()
    meter.record(_run(duration_s=-5.0))
    assert meter.snapshot().seconds == 0.0


def test_empty_snapshot_has_no_last_run() -> None:
    assert Snapshot().last is None


# --- Die Quelle: was die CLI meldet -------------------------------------------------


def test_interpret_reads_result_from_json() -> None:
    payload = json.dumps({"subtype": "success", "result": "  42  ", "usage": {}})
    text, data, note = _interpret(payload)

    assert (text, note) == ("42", "")
    assert data is not None


def test_interpret_keeps_raw_text_when_json_is_gone() -> None:
    """Formatwechsel darf die Antwort nicht kosten — nur die Zahlen."""
    text, data, note = _interpret("Hallo.")

    assert text == "Hallo."
    assert data is None
    assert "kein JSON" in note


def test_interpret_surfaces_cli_errors() -> None:
    payload = json.dumps({"subtype": "error_during_execution", "result": "Limit erreicht"})
    text, _data, note = _interpret(payload)

    assert "Limit erreicht" in text
    assert note == "Fehler laut CLI"


def test_interpret_empty_output_is_not_an_empty_answer() -> None:
    _text, _data, note = _interpret("   ")
    assert note == "leere Ausgabe"


def test_run_from_reads_the_fields_the_cli_reports() -> None:
    payload = {
        "duration_ms": 9000,
        "total_cost_usd": 0.33,
        "num_turns": 2,
        "session_id": "abc",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 34,
            "cache_read_input_tokens": 14_566,
            "cache_creation_input_tokens": 51_379,
        },
        "modelUsage": {
            "claude-haiku-4-5": {"outputTokens": 5},
            "claude-opus-4-8": {"outputTokens": 29},
        },
    }
    run = _run_from(payload, ok=True, note="", measured=99.0)

    assert run.duration_s == 9.0  # die gemeldete Dauer schlaegt die gemessene
    assert run.model == "claude-opus-4-8"  # Hauptlast = meiste Ausgabe-Token
    assert run.cache_write == 51_379
    assert run.cost_usd == 0.33


def test_run_from_falls_to_zero_never_to_a_guess() -> None:
    run = _run_from({"usage": "kaputt", "duration_ms": "viel"}, ok=False, note="x", measured=7.0)

    assert (run.input_tokens, run.cost_usd, run.model) == (0, 0.0, "")
    assert run.duration_s == 7.0  # ohne gemeldete Dauer die gemessene
    assert run.ok is False


def test_run_from_without_payload_still_counts() -> None:
    run = _run_from(None, ok=False, note="Zeitueberschreitung", measured=180.0)

    assert run.duration_s == 180.0
    assert run.note == "Zeitueberschreitung"


def test_failed_start_is_measured_not_swallowed(tmp_path: Path) -> None:
    """Auch ein Lauf, der gar nicht erst startet, ist ein Lauf."""
    meter = UsageMeter()
    reasoner = ClaudeCliReasoner(str(tmp_path / "gibt-es-nicht"), 5, meter)

    reply = reasoner.reason("hallo")

    assert "nicht startbar" in reply
    snap = meter.snapshot()
    assert (snap.runs, snap.failed) == (1, 1)


def test_reasoner_works_without_a_meter(tmp_path: Path) -> None:
    """Der Zaehler ist Zubehoer — ohne ihn denkt Talos weiter."""
    reasoner = ClaudeCliReasoner(str(tmp_path / "gibt-es-nicht"), 5)
    assert "nicht startbar" in reasoner.reason("hallo")


def test_claude_cli_model_is_explicit_and_validated_before_switch(tmp_path: Path, monkeypatch) -> None:
    capture = tmp_path / "argv.json"
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(capture)!r}, 'w').write(json.dumps({{'argv':sys.argv[1:],'env':{{k:os.environ.get(k) for k in ('ANTHROPIC_API_KEY','CLAUDE_CODE_USE_BEDROCK','GOOGLE_APPLICATION_CREDENTIALS','CLAUDE_CODE_SAFE_MODE')}}}}))\n"
        "print(json.dumps({'subtype':'success','result':'TALOS_READY','usage':{}}))\n"
    )
    cli.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secret.json")
    reasoner = ClaudeCliReasoner(str(cli), 5, model="claude-fable-5")
    assert reasoner.reason("hallo") == "TALOS_READY"
    captured = json.loads(capture.read_text())
    argv = captured["argv"]
    assert argv[:2] == ["--model", "claude-fable-5"]
    assert "--safe-mode" in argv
    assert "--disable-slash-commands" in argv
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--tools") + 1] == ""
    assert "--disallowed-tools" in argv
    assert captured["env"] == {
        "ANTHROPIC_API_KEY": None,
        "CLAUDE_CODE_USE_BEDROCK": None,
        "GOOGLE_APPLICATION_CREDENTIALS": None,
        "CLAUDE_CODE_SAFE_MODE": "1",
    }
    reasoner.validate()
