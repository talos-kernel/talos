"""Der WEG, nicht die Teile.

Das Postfach hat eigene Tests (`test_redirect.py`) und die Agentenschleife auch. Beide
waren gruen, waehrend der Weg dazwischen noch gar nicht existierte — genau die Luecke,
die an diesem Tag schon dreimal zugeschlagen hat. Hier wird geprueft, was der Poll-Loop
mit einer Nachricht TUT, waehrend ein Auftrag laeuft.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from talos.__main__ import _queued_on_purpose, _steers_the_running_task
from talos.channel import Inbound, Principal
from talos.redirect import Redirect


@dataclass
class FakeConductor:
    redirect: Redirect


class FakeDesk:
    """Nur die eine Frage, die die Sperre stellt: haengt hier eine Rueckfrage?"""

    def __init__(self, offen: str | None = None) -> None:
        self._offen = offen

    def pending(self, conversation: str):
        return object() if conversation == self._offen else None


def _update(text: str = "nein, das andere Verzeichnis", *, uid: str = "1000",
            conversation: str = "chat-7") -> Inbound:
    return Inbound(
        principal=Principal("telegram", uid),
        conversation=conversation,
        text=text,
        dedup_key=f"telegram:{uid}:{len(text)}",
    )


def _laufend(uid: str = "1000", conversation: str = "chat-7") -> FakeConductor:
    postfach = Redirect()
    postfach.open(f"telegram:{uid}", conversation)
    return FakeConductor(postfach)


# --- Der gewollte Fall --------------------------------------------------------------


def test_a_normal_message_steers_the_running_task() -> None:
    conductor = _laufend()

    assert _steers_the_running_task(conductor, FakeDesk(), _update()) is True

    (korrektur,) = conductor.redirect.take()
    assert "das andere Verzeichnis" in korrektur.text


def test_with_nothing_running_it_is_queued_as_before() -> None:
    """`False` heisst „reih ein", nicht „wirf weg"."""
    conductor = FakeConductor(Redirect())

    assert _steers_the_running_task(conductor, FakeDesk(), _update()) is False


# --- Die Sperren --------------------------------------------------------------------


def test_an_open_approval_makes_the_message_an_answer_not_a_correction() -> None:
    """⚠️ Die teuerste Verwechslung, die dieser Weg machen koennte.

    Steht eine Freigabe an, ist die naechste Nachricht DEREN Antwort. Sie als
    Kurskorrektur in einen Lauf zu schieben, waehrend der Betreiber glaubt, „ja" zu
    einer Handlung gesagt zu haben, waere genau der Fehler, gegen den `review` schon
    gebaut wurde.
    """
    conductor = _laufend()

    entschieden = _steers_the_running_task(conductor, FakeDesk(offen="chat-7"), _update("ja"))

    assert entschieden is False
    assert conductor.redirect.take() == (), "die Antwort landete im laufenden Lauf"


def test_a_second_allowed_person_does_not_steer_the_run() -> None:
    conductor = _laufend(uid="1000")

    assert _steers_the_running_task(conductor, FakeDesk(), _update(uid="2000")) is False
    assert conductor.redirect.take() == ()


def test_the_same_person_in_another_conversation_does_not_steer_the_run() -> None:
    conductor = _laufend(conversation="chat-7")

    assert _steers_the_running_task(
        conductor, FakeDesk(), _update(conversation="chat-9")
    ) is False
    assert conductor.redirect.take() == ()


def test_an_empty_message_steers_nothing() -> None:
    conductor = _laufend()

    assert _steers_the_running_task(conductor, FakeDesk(), _update("   ")) is False


# --- Die Gegenrichtung: /queue ------------------------------------------------------


def test_queue_with_text_is_a_second_task_and_bypasses_the_redirect() -> None:
    """Ohne diesen Weg gaebe es waehrend eines Laufs keinen zweiten Auftrag mehr."""
    angehaengt = _queued_on_purpose(_update("/queue lies noch die zweite Datei"))

    assert angehaengt is not None
    assert angehaengt.text == "lies noch die zweite Datei"


def test_bare_queue_stays_the_status_command() -> None:
    assert _queued_on_purpose(_update("/queue")) is None
    assert _queued_on_purpose(_update("/queue    ")) is None


def test_a_normal_message_is_not_a_queue_command() -> None:
    assert _queued_on_purpose(_update("lies die Datei")) is None


# --- Was der Lauf daraus macht ------------------------------------------------------


def test_the_correction_reaches_the_next_step_of_the_real_loop(tmp_path) -> None:
    """Vom Poll-Loop bis in die Historie — der ganze Weg in einem Test.

    Er haette das Loch gefunden, das die Einzeltests offen liessen: Postfach gruen,
    Agentenschleife gruen, und dazwischen nichts verdrahtet.
    """
    # OWNER und der Executor entsprechen denen aus test_agent_loop, stehen aber
    # hier eigenstaendig: ein Testmodul importiert nicht aus einem anderen (der
    # oeffentliche Baum laeuft mit einem Python, in dem `tests` ein fremdes
    # site-packages-Paket sein kann).
    from talos import tools
    from talos.agent_loop import AgentStatus, run_agent
    from talos.capability import CapabilityMint, GrantedRunner
    from talos.eventlog import EventLog
    from talos.executor import Executor
    from talos.policy import PolicyKernel
    from talos.snapshot import Snapshotter

    besitzer = Principal("telegram", "100000001")

    def executor(wurzel) -> Executor:
        log = EventLog(wurzel / "ev.db")
        kernel = PolicyKernel(tools.default_manifest(), frozenset({besitzer}))
        mint = CapabilityMint(kernel)
        return Executor(
            policy=kernel,
            log=log,
            snapshotter=Snapshotter(wurzel / ".snap"),
            runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
            mint=mint,
        )

    conductor = _laufend(uid=besitzer.user_id, conversation="chat-7")
    gesehen: list[list[str]] = []

    def propose(history: list[str]) -> str:
        gesehen.append(list(history))
        if len(gesehen) == 1:
            # Die Nachricht kommt herein, waehrend der Lauf denkt — auf demselben Weg
            # wie im Betrieb, ueber die Entscheidungsfunktion des Poll-Loops.
            eingegangen = replace(
                _update(uid=besitzer.user_id, conversation="chat-7"),
                principal=besitzer,
            )
            assert _steers_the_running_task(conductor, FakeDesk(), eingegangen) is True
            return 'TOOL_CALL: {"tool": "read_file", "args": {"path": "/tmp/a"}}'
        return "fertig"

    ergebnis = run_agent(
        propose, executor(tmp_path), besitzer, "run-weg",
        redirect=conductor.redirect, max_steps=4,
    )

    assert ergebnis.status is AgentStatus.ANSWERED
    zweiter = "\n".join(gesehen[1])
    assert "das andere Verzeichnis" in zweiter
    assert "[correction from the operator" in zweiter
