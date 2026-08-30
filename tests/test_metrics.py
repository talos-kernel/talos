"""Metriken — Latenz und Erfolgsquoten aus dem Protokoll.

Gerechnet wird aus Log-Eintraegen, nie aus Modellprosa; fehlende Messungen
(ungestreamte Antworten, abgestuerzte Zuege) werden ehrlich gezaehlt statt
erfunden. Diese Tests halten die Paarung, die Perzentile, die Werkzeug-Quoten,
die Konsolenform und den CLI-Einstieg fest — inklusive des TTFT-Belegs im
Conductor-Stream.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import metrics
from talos.channel import Principal
from talos.conductor import Conductor
from talos.eventlog import Event, EventLog

OWNER = Principal("telegram", "42")


def _e(run_id: str, typ: str, ts: float, payload: dict | None = None) -> dict:
    return {"run_id": run_id, "type": typ, "ts": ts, "payload": payload or {}}


def _protokoll() -> list[dict]:
    return [
        _e("r1", "reason.started", 100.0),
        _e("r1", "reason.first_token", 102.0),
        _e("r1", "reason.done", 110.0),
        _e("r2", "reason.started", 200.0),
        _e("r2", "reason.done", 220.0),          # kein Stream -> keine TTFT-Messung
        _e("r1", "exec.result", 105.0, {"tool": "run_shell", "status": "done"}),
        _e("r1", "exec.result", 106.0, {"tool": "run_shell", "status": "error"}),
        _e("r2", "exec.result", 210.0, {"tool": "vault_search", "status": "done"}),
        _e("r2", "exec.result", 211.0, {"tool": "vault_search", "status": "done"}),
    ]


def test_collect_pairs_durations_and_counts() -> None:
    stats = metrics.collect(_protokoll())
    assert stats.reasoner.n == 2
    assert stats.reasoner.avg == 15.0          # 10s und 20s
    assert stats.ttft.n == 1
    assert stats.ttft.avg == 2.0
    assert stats.ttft_ohne_messung == 1        # r2 hatte keinen Stream
    werkzeuge = dict((name, (n, ok)) for name, n, ok in stats.werkzeuge)
    assert werkzeuge["run_shell"] == (2, 1)
    assert werkzeuge["vault_search"] == (2, 2)


def test_render_is_honest_about_missing_measurements() -> None:
    text = metrics.render(metrics.collect(_protokoll()))
    assert "reasoner: 2 turns" in text
    assert "ttft:     1 streams" in text
    assert "1 turns without a stream" in text
    assert "run_shell" in text and "50% ok" in text
    assert "tools:    4 calls · 75% ok" in text


def test_render_empty_window_is_honest() -> None:
    assert metrics.render(metrics.collect([])) == "no events in this window — nothing to measure"


def test_cli_reads_a_log_and_filters_the_window(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "ev.db")
    log.append(Event("r1", "conductor", "reason.started", {}))
    log.append(Event("r1", "reasoner", "reason.first_token", {}))
    log.append(Event("r1", "reasoner", "reason.done", {}))
    log.close()
    out = io.StringIO()
    assert metrics.run_metrics(["--since", "1h"], out=out, db=tmp_path / "ev.db") == 0
    assert "reasoner: 1 turns" in out.getvalue()
    out = io.StringIO()
    assert metrics.run_metrics(["--since", "unsinn"], out=out, db=tmp_path / "ev.db") == 2


# --- Der TTFT-Beleg im Conductor-Stream ------------------------------------------------

class _Stream:
    """Minimale ReplyStream: zaehlt Deltas und meldet sich beginnbar."""

    def __init__(self) -> None:
        self.deltas: list[str] = []

    def begin_turn(self) -> None:
        pass

    def push(self, delta: str) -> None:
        self.deltas.append(delta)

    def adopt(self, text: str) -> bool:
        return True

    def settle(self) -> None:
        pass


class _StreamingReasoner:
    """Antwortet in Deltas ueber die Senke — wie der Produktiv-Reasoner."""

    def reason(self, prompt: str, on_text=None) -> str:
        if on_text is not None:
            on_text("Hal")
            on_text("lo")
        return "Hallo"


def test_first_token_enters_the_log_exactly_once(tmp_path: Path) -> None:
    from talos import tools
    from talos.approval import ApprovalStore
    from talos.capability import CapabilityMint, GrantedRunner
    from talos.channel import Inbound, Trust
    from talos.executor import Executor
    from talos.policy import PolicyKernel
    from talos.snapshot import Snapshotter

    log = EventLog(tmp_path / "ev.db")
    allowed = frozenset({OWNER})
    policy = PolicyKernel(tools.default_manifest(), allowed)
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)), mint=mint,
    )
    conductor = Conductor(
        log=log, reasoner=_StreamingReasoner(), executor=executor,
        send=lambda _c, _t: True, allowed_principals=allowed,
        trust_of=lambda _c: Trust.FULL, approvals=ApprovalStore(),
        begin_reply=lambda _conversation: _Stream(),
    )
    ok = conductor.handle(Inbound(
        principal=OWNER, conversation="telegram:42",
        text="sag hallo", dedup_key="telegram:update:1",
    ))
    assert ok is True
    tokens = [r for r in log.recent(50) if r.get("type") == "reason.first_token"]
    assert len(tokens) == 1
