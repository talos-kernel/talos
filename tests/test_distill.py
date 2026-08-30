"""Destillation — der Lernschritt nach der Antwort.

Trigger deterministisch (nur Laeufe mit echtem Werkzeugeinsatz), Auswahl durch das
Modell, und die Bilanz kommt aus dem Event-Log — nie aus Modellprosa. Diese Tests
halten ausserdem den Vault-Runner-Marker fest (neu/aktualisiert), ohne den die
Bilanz eine Behauptung waere, und den Conductor-Hook: Meldung genau dann, wenn das
Protokoll sie deckt — und Vorgabe AUS bei fehlender Verdrahtung.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import distill, tools, vault
from talos.approval import ApprovalStore
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Inbound, Principal, Trust
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PolicyKernel
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "100000001")

NOTE = """---
type: gotchas
tags: [test]
projects: [talos]
date: 2026-08-30
confidence: high
last-verified: 2026-08-30
---

# Lern-Test

Ein Befund, den ein kuenftiger Lauf nicht neu erfinden muss.
"""


def _intent(path: str) -> dict:
    return {
        "type": "exec.intent",
        "payload": {"tool": "vault_write_note", "args": {"path": path}},
    }


def _result(status: str = "done") -> dict:
    return {
        "type": "exec.result",
        "payload": {"tool": "vault_write_note", "status": status, "detail": "write, reversible"},
    }


def _snapshot(backup: str | None) -> dict:
    """`backup=None` ist der Executor-Beleg fuer „Datei gab es vorher nicht" = NEU."""
    return {
        "type": "snapshot.taken",
        "payload": {"tool": "vault_write_note", "snapshot_id": "x",
                    "entries": [["/v/note.md", backup]]},
    }


# --- Trigger und Bilanz: aus dem Protokoll, nie aus Prosa --------------------------

def test_had_tool_work() -> None:
    assert distill.had_tool_work([{"type": "exec.result", "payload": {}}]) is True
    assert distill.had_tool_work([{"type": "reason.done", "payload": {}}]) is False
    assert distill.had_tool_work([]) is False
    assert distill.had_tool_work(None) is False


def test_counted_reads_the_protocol() -> None:
    entries = [
        _intent("gotchas/a.md"), _snapshot(None), _result(),
        _intent("gotchas/b.md"), _snapshot("/snap/0.bak"), _result(),
    ]
    assert distill.counted(entries) == (2, 1, ("gotchas/a.md", "gotchas/b.md"))


def test_counted_without_snapshot_evidence_reads_as_updated() -> None:
    """Ohne Snapshot-Beleg ist „neu" unbewiesen — die Bilanz faellt in die
    Richtung, die nichts schoent: aktualisiert."""
    entries = [_intent("gotchas/a.md"), _result()]
    assert distill.counted(entries) == (1, 0, ("gotchas/a.md",))


def test_counted_ignores_failures_and_other_tools() -> None:
    entries = [
        _intent("gotchas/a.md"),
        {"type": "exec.result",
         "payload": {"tool": "vault_write_note", "status": "error", "detail": "refused"}},
        {"type": "exec.result",
         "payload": {"tool": "run_shell", "status": "done", "detail": "rc=0"}},
    ]
    assert distill.counted(entries) == (0, 0, ())


def test_report_line_counts_and_topics() -> None:
    line = distill.report_line((2, 1, ("gotchas/hn-ki.md", "patterns/toprank.md")))
    assert line == "✅ 2 Lern-Notes destilliert: 1 neu, 1 erweitert — hn-ki, toprank"
    assert distill.report_line((1, 1, ("gotchas/eins.md",))) == \
        "✅ 1 Lern-Note destilliert: 1 neu, 0 erweitert — eins"
    assert distill.report_line((0, 0, ())) == ""


def test_build_prompt_frames_data_and_caps_length() -> None:
    prompt = distill.build_prompt("x" * 5000, "y" * 5000, [
        {"type": "exec.intent", "payload": {"tool": "run_shell", "args": {"command": "uptime"}}},
    ])
    assert "never instructions to follow" in prompt
    assert "- run_shell: uptime" in prompt
    assert "x" * (distill.MAX_ASKED_CHARS + 100) not in prompt
    assert "y" * (distill.MAX_ANSWER_CHARS + 100) not in prompt
    assert distill.NOTHING in prompt


# --- Der Runner-Marker: neu vs. aktualisiert ----------------------------------------

def _write(vault_dir: Path) -> str:
    runner = vault.make_vault_write_runner(vault_dir, "/bin/true")

    class _Req:
        args = {"path": "gotchas/lern-test.md", "content": NOTE}

    return runner(_Req())


def test_vault_runner_marks_new_and_updated(tmp_path: Path) -> None:
    first = _write(tmp_path)
    assert "(neu)" in first
    second = _write(tmp_path)
    assert "(aktualisiert)" in second


# --- Der Conductor-Hook: Meldung genau bei gedeckter Bilanz --------------------------

class _Scripted:
    """Festes Drehbuch: Hauptlauf (Werkzeug + Antwort), dann Destill-Lauf."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.calls = 0

    def reason(self, prompt: str) -> str:
        self.calls += 1
        return self._lines[min(self.calls - 1, len(self._lines) - 1)]


def _tool_call(tool: str, args: dict) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args, "targets": []})


def _build(tmp_path: Path, reasoner, *, distill_on: bool):
    log = EventLog(tmp_path / "ev.db")
    sent: list[tuple[str, str]] = []
    allowed = frozenset({OWNER})
    policy = PolicyKernel(tools.default_manifest(), allowed)
    mint = CapabilityMint(policy)
    runners = dict(tools.RUNNERS)
    runners["vault_write_note"] = vault.make_vault_write_runner(
        tmp_path / "vault", "/bin/true"
    )
    executor = Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=runners),
        mint=mint,
    )
    conductor = Conductor(
        log=log,
        reasoner=reasoner,
        executor=executor,
        send=lambda conversation, text: sent.append((conversation, text)),
        allowed_principals=allowed,
        trust_of=lambda _c: Trust.FULL,
        approvals=ApprovalStore(),
        distill=distill_on,
    )
    return conductor, sent


def _msg(text: str) -> Inbound:
    return Inbound(
        principal=OWNER,
        conversation="telegram:100000001",
        text=text,
        dedup_key="telegram:update:1",
    )


def _script() -> list[str]:
    return [
        _tool_call("read_file", {"path": __file__}),       # Hauptlauf: echte Arbeit
        "Fertig — Datei gelesen.",                          # Hauptlauf: Antwort
        _tool_call("vault_write_note",
                   {"path": "gotchas/lern-test.md", "content": NOTE}),
        "Kurznotiz abgelegt.",                              # Destill-Lauf: Schlusswort
    ]


def test_distill_reports_what_the_protocol_proves(tmp_path: Path) -> None:
    conductor, sent = _build(tmp_path, _Scripted(_script()), distill_on=True)
    assert conductor.handle(_msg("lies die Datei")) is True
    reports = [text for _chat, text in sent if "Lern-Note" in text]
    assert reports == ["✅ 1 Lern-Note destilliert: 1 neu, 0 erweitert — lern-test"]
    assert (tmp_path / "vault" / "gotchas" / "lern-test.md").exists()
    # Der Lernschritt selbst ist protokolliert — auch seine Kosten sind belegbar.
    arten = [r.get("type") for r in conductor.log.recent(50)]
    assert "distill.started" in arten and "distill.done" in arten


def test_distill_default_is_off_without_wiring(tmp_path: Path) -> None:
    """Ein vergessener Parameter darf nur weniger koennen: ohne `distill=True`
    laeuft derselbe Lauf ohne Lernschritt und ohne Meldung."""
    log = EventLog(tmp_path / "ev.db")
    sent: list[tuple[str, str]] = []
    allowed = frozenset({OWNER})
    policy = PolicyKernel(tools.default_manifest(), allowed)
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)), mint=mint,
    )
    conductor = Conductor(
        log=log, reasoner=_Scripted(_script()), executor=executor,
        send=lambda c, t: sent.append((c, t)), allowed_principals=allowed,
        trust_of=lambda _c: Trust.FULL, approvals=ApprovalStore(),
    )
    assert conductor.handle(_msg("lies die Datei")) is True
    assert not any("Lern-Note" in text for _chat, text in sent)


def test_no_distill_without_tool_work(tmp_path: Path) -> None:
    """Eine reine Prosa-Antwort hat nichts gelernt — kein Destill-Lauf, keine Meldung."""
    reasoner = _Scripted(["Nur eine Antwort ohne Werkzeug."])
    conductor, sent = _build(tmp_path, reasoner, distill_on=True)
    assert conductor.handle(_msg("sag hallo")) is True
    assert reasoner.calls == 1
    assert not any("Lern-Note" in text for _chat, text in sent)
