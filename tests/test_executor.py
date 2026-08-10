"""Executor: die gegatete Pipeline rollt bei jedem Fehlpfad zurück und loggt Intent + Receipt."""
from __future__ import annotations

from pathlib import Path

from talos.capability import CapabilityMint, GrantedRunner
from talos.eventlog import EventLog
from talos.executor import APPROVED_DETAIL, Executor, Status
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest
from talos.snapshot import Snapshotter
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")


def _manifest() -> ToolManifest:
    return (
        ToolManifest()
        .with_tool(ToolSpec("write_file", Effect.WRITE, reversible=True))
        .with_tool(ToolSpec("send_mail", Effect.EXEC, reversible=False))
    )


def _executor(tmp_path: Path, runner) -> Executor:
    """Die rohen Runner liegen jetzt im GrantedRunner, nicht mehr im Executor.

    Die Test-Runner bleiben bewusst einarmig (`req`) — das Token nimmt ihnen der
    GrantedRunner ab, bevor sie ueberhaupt aufgerufen werden.
    """
    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    snap = Snapshotter(tmp_path / ".snap")
    return Executor(
        policy=policy,
        log=log,
        snapshotter=snap,
        runner=GrantedRunner(mint=mint, runners={"write_file": runner, "send_mail": runner}),
        mint=mint,
    )


def test_denied_tool_records_intent_and_result(tmp_path: Path) -> None:
    ex = _executor(tmp_path, lambda req: "should not run")
    out = ex.run(ToolRequest("write_file", OWNER, {"path": "/etc/passwd"}, ()), "run1")
    assert out.status is Status.DENIED
    assert ex.log.count() == 2  # exec.intent + exec.result — kein grant.issued dazwischen


def test_irreversible_needs_human_and_does_not_run(tmp_path: Path) -> None:
    calls = []
    ex = _executor(tmp_path, lambda req: calls.append(1))
    out = ex.run(ToolRequest("send_mail", OWNER, {}), "run2")
    assert out.status is Status.NEEDS_HUMAN
    assert calls == []  # nichts ausgeführt


def test_allowed_write_runs_and_returns_result(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"

    def runner(req):
        target.write_text("geschrieben", encoding="utf-8")
        return "ok"

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run3")
    assert out.status is Status.DONE
    assert out.result == "ok"
    assert target.read_text(encoding="utf-8") == "geschrieben"


def test_runner_exception_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "cfg.txt"
    target.write_text("original", encoding="utf-8")

    def runner(req):
        target.write_text("halb geschrieben", encoding="utf-8")
        raise RuntimeError("boom")

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run4")
    assert out.status is Status.ERROR
    assert target.read_text(encoding="utf-8") == "original"  # zurückgerollt


def test_verify_failed_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "cfg.txt"
    target.write_text("original", encoding="utf-8")

    def runner(req):
        target.write_text("neu", encoding="utf-8")
        return "actual"

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run5", expected="other")
    assert out.status is Status.VERIFY_FAILED
    assert target.read_text(encoding="utf-8") == "original"


def test_approved_run_receipt_does_not_still_ask_for_approval(tmp_path: Path) -> None:
    """Ein gelaufenes Werkzeug quittiert den Lauf, nicht die Bedingung davor.

    Der Beleg geht ueber `tool_history_entry` in den naechsten Prompt. Stand dort
    weiterhin „needs your approval", erklaerte das Modell dem Betreiber danach, es warte
    noch — Sekunden nachdem er freigegeben hatte und die Datei laengst geschrieben war.
    Das Modell liest die Prosa, nicht den Status; deshalb muss die Prosa stimmen.
    """
    calls: list[int] = []
    ex = _executor(tmp_path, lambda req: calls.append(1) or "sent")
    out = ex.run(ToolRequest("send_mail", OWNER, {}), "run7", human_approved=True)
    assert out.status is Status.DONE
    assert calls == [1]
    assert out.detail == APPROVED_DETAIL
    assert "needs your approval" not in out.detail
    assert "will be executed later" not in out.detail

    receipt = [e for e in ex.log.recent(20) if e["type"] == "exec.result"][0]
    assert "needs your approval" not in receipt["payload"]["detail"]


def test_allowed_run_keeps_the_kernel_reason_as_receipt(tmp_path: Path) -> None:
    """Ohne Freigabe-Umweg bleibt der Kernel-Grund die Quittung — nur NEEDS_HUMAN wird ersetzt."""
    target = tmp_path / "out.txt"
    ex = _executor(tmp_path, lambda req: target.write_text("x", encoding="utf-8") or "ok")
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run8")
    assert out.status is Status.DONE
    assert out.detail == ex.policy.decide(
        ToolRequest("write_file", OWNER, {"path": str(target)})
    ).reason


def test_receipt_names_the_grant_that_authorised_the_run(tmp_path: Path) -> None:
    """Jeder Lauf hinterlaesst sein Recht im Log — und der Beleg nennt dessen ID.

    Ohne das steht im Log zwar „ausgefuehrt", aber nicht „aufgrund welcher Erlaubnis".
    """
    target = tmp_path / "out.txt"

    def runner(req):
        target.write_text("x", encoding="utf-8")
        return "ok"

    ex = _executor(tmp_path, runner)
    out = ex.run(ToolRequest("write_file", OWNER, {"path": str(target)}), "run6")
    assert out.status is Status.DONE

    events = ex.log.recent(20)
    issued = [e for e in events if e["type"] == "grant.issued"]
    result = [e for e in events if e["type"] == "exec.result"]
    assert len(issued) == 1
    assert issued[0]["payload"]["ttl_s"] > 0
    assert issued[0]["payload"]["human_approved"] is False
    assert result[0]["payload"]["grant_id"] == issued[0]["payload"]["grant_id"]
