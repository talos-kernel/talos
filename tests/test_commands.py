"""Kommandos: deterministisch, ohne Reasoner, und ohne Hintertür am Kernel vorbei.

Die interessanten Fälle sind nicht `/help`, sondern die drei, an denen ein
Steuerkommando gefährlich würde:
  - `/stop` muss ehrlich sein (nichts behaupten, was es nicht getan hat),
  - `/undo` darf kein Universal-Kopierer sein (Daten kommen aus dem Log, nicht aus
    dem, was jemand hineinschreibt) und muss selbst gegatet bleiben,
  - `/policy` darf nichts ausführen und nichts parken.
"""
from __future__ import annotations

import time
from pathlib import Path

from talos.approval import ApprovalStore
from talos.capability import CapabilityMint
from talos.commands import CommandCenter, CommandResult, is_command, parse
from talos.eventlog import Event, EventLog
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.usage import Run, UsageMeter
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")
STRANGER = Principal("telegram", "111111")
CHAT = "telegram:4242"
HOME = str(Path.home())


class _FakeReasoner:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.cancels = 0

    def cancel(self) -> bool:
        self.cancels += 1
        was = self.running
        self.running = False
        return was


class _FakeWorker:
    def __init__(self, busy: bool = False, waiting: int = 0) -> None:
        self._busy = busy
        self._waiting = waiting
        self.drains = 0

    def pending(self) -> int:
        return self._waiting

    def busy(self) -> bool:
        return self._busy

    def drain(self) -> int:
        self.drains += 1
        dropped, self._waiting = self._waiting, 0
        return dropped


def _manifest() -> ToolManifest:
    return (
        ToolManifest()
        .with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
        .with_tool(ToolSpec("write_file", Effect.WRITE, reversible=True))
        .with_tool(ToolSpec("run_shell", Effect.EXEC, reversible=False))
        .with_tool(ToolSpec("undo_last", Effect.WRITE, reversible=False))
    )


def _center(tmp_path: Path, *, reasoner=None, worker=None, approvals=None, **extras) -> CommandCenter:
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"),
        approvals=approvals or ApprovalStore(),
        policy=policy,
        started_at=0.0,
        bot_username="Talos_bot",
        reasoner=reasoner or _FakeReasoner(),
        worker=worker or _FakeWorker(),
        repo_dir=tmp_path,
        mint=CapabilityMint(policy),
        **extras,
    )


def _snapshot_event(log: EventLog, original: str, backup: str) -> None:
    log.append(
        Event(
            "run-1",
            "executor",
            "snapshot.taken",
            {"tool": "write_file", "snapshot_id": "snap-1", "entries": [[original, backup]]},
        )
    )


# --- Parsen ----------------------------------------------------------------------


def test_is_command_only_for_slash() -> None:
    assert is_command("/log")
    assert is_command("  /log  ")
    assert not is_command("log")
    assert not is_command("bitte /log")


def test_parse_strips_botname_and_lowercases() -> None:
    assert parse("/Log@Talos_bot 5") == ("log", "5")
    assert parse("/stop") == ("stop", "")
    assert parse("kein kommando") == ("", "")


def test_unknown_command_is_named_not_guessed(tmp_path: Path) -> None:
    result = _center(tmp_path).dispatch("laufen", "", principal=OWNER, conversation=CHAT)
    assert "Unknown command /laufen" in (result.reply or "")


# --- /stop: ehrlich statt beruhigend ----------------------------------------------


def test_stop_reports_nothing_when_nothing_ran(tmp_path: Path) -> None:
    result = _center(tmp_path).dispatch("stop", "", principal=OWNER, conversation=CHAT)
    assert "Nichts abzubrechen" in (result.reply or "")


def test_stop_cancels_run_and_drains_queue(tmp_path: Path) -> None:
    reasoner, worker = _FakeReasoner(running=True), _FakeWorker(busy=True, waiting=3)
    center = _center(tmp_path, reasoner=reasoner, worker=worker)

    reply = center.dispatch("stop", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "laufendes Denken abgebrochen" in reply
    assert "3 wartende" in reply
    assert reasoner.cancels == 1 and worker.drains == 1


# --- /approve, /deny: kein zweiter Freigabe-Pfad ----------------------------------


def test_approve_and_deny_only_forward_text(tmp_path: Path) -> None:
    """Sie führen NICHTS aus — sie sagen dem Conductor „das war ein ja/nein".
    Damit läuft die Freigabe durch dieselbe Runde wie ein getipptes „ja"."""
    center = _center(tmp_path)
    approve = center.dispatch("approve", "", principal=OWNER, conversation=CHAT)
    deny = center.dispatch("deny", "", principal=OWNER, conversation=CHAT)
    assert approve == CommandResult(forward_as="yes")
    assert deny == CommandResult(forward_as="no")


def test_pending_shows_prompt_verbatim(tmp_path: Path) -> None:
    approvals = ApprovalStore()
    center = _center(tmp_path, approvals=approvals)
    assert "Keine offene Freigabe" in (center.dispatch("pending", "", principal=OWNER, conversation=CHAT).reply or "")

    req = ToolRequest("run_shell", OWNER, {"command": "date"})
    approvals.park(CHAT, req, "⚠️ Freigabe nötig — Kernel-Fakten:\nTool: run_shell")
    reply = center.dispatch("pending", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Tool: run_shell" in reply and "valid for another" in reply


# --- /log -------------------------------------------------------------------------


def test_log_shows_only_events_with_effect(tmp_path: Path) -> None:
    center = _center(tmp_path)
    center.log.append(Event("r", "ingress", "task.received", {"principal": str(OWNER)}))
    center.log.append(Event("r", "executor", "exec.result", {"tool": "run_shell", "status": "done"}))

    reply = center.dispatch("log", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "exec.result" in reply
    assert "task.received" not in reply  # Poll-Rauschen bleibt draußen


def test_log_rejects_non_numeric_argument(tmp_path: Path) -> None:
    reply = _center(tmp_path).dispatch("log", "viele", principal=OWNER, conversation=CHAT).reply or ""
    assert "braucht eine Zahl" in reply


def test_log_without_events_says_so(tmp_path: Path) -> None:
    reply = _center(tmp_path).dispatch("log", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Noch keine Ereignisse" in reply


# --- /undo: Daten aus dem Log, Gating wie jeder Schreibzugriff ---------------------


def test_undo_without_snapshot_refuses(tmp_path: Path) -> None:
    result = _center(tmp_path).dispatch("undo", "", principal=OWNER, conversation=CHAT)
    assert result.request is None
    assert "Nothing to roll back" in (result.reply or "")


def test_undo_builds_request_from_log_not_from_input(tmp_path: Path) -> None:
    center = _center(tmp_path)
    _snapshot_event(center.log, str(tmp_path / "ziel.txt"), str(tmp_path / "backup"))

    # Der „Parameter" wird ignoriert: die Quelle ist ausschließlich der Event-Log.
    result = center.dispatch("undo", "/etc/passwd", principal=OWNER, conversation=CHAT)

    assert result.request is not None
    assert result.request.tool == "undo_last"
    assert result.request.args["entries"] == [[str(tmp_path / "ziel.txt"), str(tmp_path / "backup")]]
    assert result.request.args["snapshot_id"] == "snap-1"


def test_undo_refuses_while_approval_is_open(tmp_path: Path) -> None:
    approvals = ApprovalStore()
    center = _center(tmp_path, approvals=approvals)
    _snapshot_event(center.log, str(tmp_path / "ziel.txt"), str(tmp_path / "backup"))
    approvals.park(CHAT, ToolRequest("run_shell", OWNER, {"command": "date"}), "prompt")

    result = center.dispatch("undo", "", principal=OWNER, conversation=CHAT)

    assert result.request is None  # sonst wäre unklar, worauf sich ein „ja" bezieht
    assert "offene Freigabe" in (result.reply or "")


def test_undo_twice_is_refused(tmp_path: Path) -> None:
    center = _center(tmp_path)
    _snapshot_event(center.log, str(tmp_path / "ziel.txt"), str(tmp_path / "backup"))
    center.log.append(
        Event("run-2", "executor", "exec.result", {"tool": "undo_last", "status": "done"})
    )

    result = center.dispatch("undo", "", principal=OWNER, conversation=CHAT)

    assert result.request is None
    assert "already rolled back" in (result.reply or "")


def test_undo_request_is_gated_like_a_write(tmp_path: Path) -> None:
    """Der Rückwärtsgang ist keine Hintertür: dieselbe Anfrage, derselbe Kernel."""
    center = _center(tmp_path)
    _snapshot_event(center.log, f"{HOME}/.bashrc", str(tmp_path / "backup"))

    result = center.dispatch("undo", "", principal=OWNER, conversation=CHAT)

    assert result.request is not None
    assert center.policy.decide(result.request).verdict is Verdict.NEEDS_HUMAN


# --- /policy: Trockenlauf, garantiert wirkungsfrei --------------------------------


def test_policy_dry_run_on_secret_path(tmp_path: Path) -> None:
    reply = _center(tmp_path).dispatch(
        "policy", f"{HOME}/.secrets/talos-telegram.env", principal=OWNER, conversation=CHAT
    ).reply or ""
    assert "read_file: abgelehnt" in reply
    assert "write_file: fragt dich" in reply


def test_policy_dry_run_expands_tilde(tmp_path: Path) -> None:
    """Die Schreibweise darf das Urteil nicht ändern — sonst log ich the operator an."""
    reply = _center(tmp_path).dispatch(
        "policy", "~/.secrets/talos-telegram.env", principal=OWNER, conversation=CHAT
    ).reply or ""
    assert "read_file: abgelehnt" in reply


def test_policy_dry_run_changes_nothing(tmp_path: Path) -> None:
    approvals = ApprovalStore()
    center = _center(tmp_path, approvals=approvals)
    before = center.log.count()

    center.dispatch("policy", "rm -rf ~/talos", principal=OWNER, conversation=CHAT)

    assert approvals.get(CHAT) is None  # nichts geparkt
    assert center.log.count() == before  # nichts protokolliert, weil nichts geschah


def test_policy_shows_path_classification_for_shell(tmp_path: Path) -> None:
    reply = _center(tmp_path).dispatch(
        "policy", "echo evil >> ~/.bashrc", principal=OWNER, conversation=CHAT
    ).reply or ""
    assert "Persistenz" in reply


# --- /tools, /whoami, /status ------------------------------------------------------


def test_tools_lists_gating_per_tool(tmp_path: Path) -> None:
    reply = _center(tmp_path).dispatch("tools", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "read_file" in reply and "undo_last" in reply
    assert "needs approval" in reply  # run_shell, solange keine Sandbox existiert


def test_whoami_reports_admission(tmp_path: Path) -> None:
    center = _center(tmp_path)
    assert "Zugelassen: ja" in (center.dispatch("whoami", "", principal=OWNER, conversation=CHAT).reply or "")
    assert "Zugelassen: nein" in (center.dispatch("whoami", "", principal=STRANGER, conversation=CHAT).reply or "")


def test_status_reports_open_approval(tmp_path: Path) -> None:
    approvals = ApprovalStore()
    center = _center(tmp_path, approvals=approvals)
    approvals.park(CHAT, ToolRequest("write_file", OWNER, {"path": "/tmp/x", "content": "y"}), "p")

    reply = center.dispatch("status", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "Offene Freigabe: ja — write_file" in reply


# --- /forget: die Verzweigung, die jahrelang toter Code war -------------------------


def _center_with_memories(tmp_path: Path) -> CommandCenter:
    from talos.memory import Memory
    from talos.recall import Recall

    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    memory = Memory()
    memory.remember(CHAT, asked="hallo", answered="gruss zurueck")
    recall = Recall(tmp_path / "recall.db")
    recall.remember("Der Betreiber mag kurze Antworten", kind="preference", conversation=CHAT, principal=str(OWNER))
    center = CommandCenter(
        log=EventLog(tmp_path / "events.db"),
        approvals=ApprovalStore(),
        policy=policy,
        started_at=0.0,
        bot_username="Talos_bot",
        reasoner=_FakeReasoner(),
        worker=_FakeWorker(),
        repo_dir=tmp_path,
        mint=CapabilityMint(policy),
        memory=memory,
        recall=recall,
    )
    return center


def test_forget_without_argument_stays_the_history_reset(tmp_path: Path) -> None:
    center = _center_with_memories(tmp_path)
    reply = center.dispatch("forget", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "History cleared" in reply
    assert center.recall.count() == 1  # die Recall-Notiz blieb unangetastet


def test_forget_with_id_finally_reaches_the_recall_branch(tmp_path: Path) -> None:
    """Vorher fing `("new", "forget", "reset")` JEDES /forget ab — der Zweig fuer
    `/forget <id|all>` (den der HELP-Text seit jeher verspricht) war unerreichbar,
    loeschte in Wahrheit den Verlauf und ignorierte das Argument still."""
    center = _center_with_memories(tmp_path)
    note_id = center.recall.recent(limit=1)[0].id

    reply = center.dispatch("forget", str(note_id), principal=OWNER, conversation=CHAT).reply or ""

    assert "Dropped it." in reply
    assert center.recall.count() == 0
    # und der Gespraechsverlauf blieb stehen — genau andersherum als vor dem Fix
    assert center.memory.stats(CHAT)[0] == 2


def test_forget_all_drops_every_note(tmp_path: Path) -> None:
    center = _center_with_memories(tmp_path)
    reply = center.dispatch("forget", "all", principal=OWNER, conversation=CHAT).reply or ""
    assert "Dropped 1 note(s)." in reply
    assert center.recall.count() == 0


# --- /health: die Ampel liest nur ------------------------------------------------------
def test_health_reports_a_clean_log_honestly(tmp_path: Path) -> None:
    """Ohne Fehler und ohne Zaehler sagt die Ampel genau das — statt Schweigen, das
    nach „kaputt“ aussieht, oder eine Erfindung, die nach „gemessen“ aussieht."""
    reply = _center(tmp_path).dispatch("health", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Zustand" in reply
    assert "kein Fehler unter den letzten 100" in reply
    assert "kein Zaehler verdrahtet" in reply


def test_health_names_the_newest_error_from_the_log(tmp_path: Path) -> None:
    """Die Ampel liest das Protokoll, nicht Behauptungen: ein eingetragener Fehler muss
    mit seinem Text auftauchen — sonst graebt man wieder, statt hinzusehen."""
    center = _center(tmp_path)
    center.log.append(
        Event("run-1", "conductor", "error", {"stage": "reason", "error": "boom nach 12s"})
    )
    reply = center.dispatch("health", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "1 Fehler" in reply
    assert "boom nach 12s" in reply


def test_health_counts_failed_runs_from_the_meter(tmp_path: Path) -> None:
    """Fehlversuche zaehlen mit — eine Ampel, die nur die geglueckten Laeufe kennt,
    zeigt gruen im Sturm."""
    meter = UsageMeter()
    meter.record(Run(at=time.time(), ok=True, duration_s=3.0))
    meter.record(Run(at=time.time(), ok=False, duration_s=1.0, note="timeout"))
    center = _center(tmp_path, usage=meter)
    reply = center.dispatch("health", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "2 Laeufe, 1 ohne Ergebnis" in reply
    assert "FEHLGESCHLAGEN" in reply
    assert "timeout" in reply
