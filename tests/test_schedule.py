"""Zeitgesteuerte Auftraege — und die Decke, die sie schwaecher macht als getippte.

Der Kern ist nicht der Zeitplan, sondern was ein Lauf OHNE Menschen darf. Verbreitete
Agenten-Frameworks lassen einen Cron-Job mit voller Macht laufen; hier muss er weniger duerfen,
sonst waere die zeitgesteuerte Ausfuehrung ein zweiter Erlaubnisweg neben dem Kernel.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.channel import Principal, Trust
from talos.manifest import Effect
from talos.policy import Decision, PolicyKernel, ToolRequest, Verdict
from talos.schedule import (
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    ScheduleStore,
    UnattendedCeiling,
)
from talos.tools import default_manifest

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"
CHAT_B = "telegram:4242"
HOME = str(Path.home())


def store(tmp_path: Path, **kw) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.db", **kw)


def kernel_with(ceiling: UnattendedCeiling) -> GovernedKernel:
    return GovernedKernel(
        PolicyKernel(default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5),
        lambda _c: Trust.FULL,
        unattended=ceiling,
    )


# --- Die Decke: der eigentliche Punkt ----------------------------------------------
def test_an_unattended_run_cannot_reach_anything_that_needs_a_human() -> None:
    """`NEEDS_HUMAN` wird `DENY` — nicht weil die Handlung schlimmer waere, sondern weil
    der einzige, der sie erlauben koennte, gerade nicht da ist."""
    ceiling = UnattendedCeiling()
    kernel = kernel_with(ceiling)
    riskant = ToolRequest("write_file", OWNER, {"path": f"{HOME}/.bashrc", "content": "x"})

    assert kernel.decide(riskant).verdict is Verdict.NEEDS_HUMAN  # beaufsichtigt: fragt
    with ceiling.active():
        entschieden = kernel.decide(riskant)
    assert entschieden.verdict is Verdict.DENY
    assert "unattended" in entschieden.reason


def test_the_ceiling_never_widens_anything() -> None:
    """Sie kann ausschliesslich verschaerfen — ein DENY bleibt DENY, ein ALLOW bleibt
    ALLOW. Eine Decke, die etwas erlaubt, waere ein zweiter Erlaubnisweg."""
    ceiling = UnattendedCeiling()
    with ceiling.active():
        assert ceiling.apply(Decision(Verdict.ALLOW, "read")).verdict is Verdict.ALLOW
        assert ceiling.apply(Decision(Verdict.DENY, "hardline")).verdict is Verdict.DENY


def test_ordinary_work_still_runs_unattended() -> None:
    """Gegenprobe: eine Decke, die alles sperrt, waere kein Fortschritt, sondern ein
    ausgeschalteter Zeitplan."""
    ceiling = UnattendedCeiling()
    kernel = kernel_with(ceiling)
    harmlos = ToolRequest("read_file", OWNER, {"path": f"{HOME}/talos/README.md"})
    with ceiling.active():
        assert kernel.decide(harmlos).verdict is Verdict.ALLOW


def test_the_ceiling_binds_to_its_thread_only() -> None:
    """Ein gleichzeitig GETIPPTER Auftrag darf nicht mitgesperrt werden — sonst koennte
    der Betreiber waehrend eines Zeitplan-Laufs nichts mehr freigeben."""
    ceiling = UnattendedCeiling()
    gesehen: list[bool] = []

    def anderer_thread() -> None:
        gesehen.append(ceiling.is_unattended())

    with ceiling.active():
        assert ceiling.is_unattended()
        t = threading.Thread(target=anderer_thread)
        t.start()
        t.join()
    assert gesehen == [False]
    assert not ceiling.is_unattended()


# --- Der Speicher -------------------------------------------------------------------
def test_add_and_list_round_trip(tmp_path: Path) -> None:
    s = store(tmp_path)
    task = s.add(conversation=CHAT, principal=str(OWNER), prompt="pruefe die Platte", interval_s=3600)
    assert task is not None and task.interval_s == 3600
    assert [t.id for t in s.list_for(CHAT)] == [task.id]


def test_schedules_are_scoped_to_their_conversation(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.add(conversation=CHAT, principal=str(OWNER), prompt="a", interval_s=3600)
    assert s.list_for(CHAT_B) == ()


def test_a_second_chat_cannot_remove_someone_elses_schedule(tmp_path: Path) -> None:
    """Sonst waere „mein Waechter meldet sich nicht mehr" von einem Defekt nicht zu
    unterscheiden."""
    s = store(tmp_path)
    task = s.add(conversation=CHAT, principal=str(OWNER), prompt="a", interval_s=3600)
    assert s.remove(task.id, conversation=CHAT_B) is False
    assert len(s.list_for(CHAT)) == 1
    assert s.remove(task.id, conversation=CHAT) is True


def test_interval_bounds_are_enforced(tmp_path: Path) -> None:
    s = store(tmp_path)
    for bad in (MIN_INTERVAL_S - 1, MAX_INTERVAL_S + 1, 0, -5):
        with pytest.raises(ValueError):
            s.add(conversation=CHAT, principal=str(OWNER), prompt="a", interval_s=bad)


def test_the_number_of_schedules_is_capped(tmp_path: Path) -> None:
    s = store(tmp_path, max_tasks=2)
    for i in range(2):
        s.add(conversation=CHAT, principal=str(OWNER), prompt=f"a{i}", interval_s=3600)
    with pytest.raises(ValueError, match="at most"):
        s.add(conversation=CHAT, principal=str(OWNER), prompt="zuviel", interval_s=3600)


def test_due_respects_the_clock_and_mark_run_moves_it_on(tmp_path: Path) -> None:
    s = store(tmp_path)
    task = s.add(conversation=CHAT, principal=str(OWNER), prompt="a", interval_s=60, now=1000.0)
    assert s.due(now=1030.0) == ()
    assert [t.id for t in s.due(now=1061.0)] == [task.id]
    s.mark_run(task.id, now=1061.0)
    assert s.due(now=1061.0) == ()
    assert [t.id for t in s.due(now=1122.0)] == [task.id]


def test_a_broken_store_is_a_limitation_not_a_crash(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    s = ScheduleStore(blocker / "schedules.db")
    assert not s.available and s.reason
    assert s.add(conversation=CHAT, principal=str(OWNER), prompt="a", interval_s=60) is None
    assert s.due() == () and s.list_for(CHAT) == () and s.count() == 0
