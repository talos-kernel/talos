"""Die Rückfrage im Conductor verdrahtet (`question.py` <-> `conductor.py` <-> `tools.py`).

`tests/test_question.py` prüft das Modul für sich. Hier geht es um die Naht: kommt die
Frage wirklich in den Chat, erreicht die Antwort den wartenden Lauf — und bleibt sie von
der Freigabe getrennt, wenn beide durch denselben Dispatcher laufen?

Der teure Fehler wäre nicht eine verlorene Antwort, sondern eine vermischte: ein „ja",
das eine Rückfrage beantwortet, oder eine „2", die etwas freigibt. Beides steht unten.
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import tools
from talos.approval import ApprovalPicker
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import CallbackQuery, Inbound, Principal, StructuredMessage, Trust
from talos.commands import CommandResult
from talos.conductor import AskContext, Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.manifest import Effect
from talos.policy import TARGET_EXTRACTORS, PolicyKernel, ToolRequest, Verdict
from talos.question import CALLBACK_PREFIX, QuestionDesk
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"
QUESTION = "Which log did you mean?"
OPTIONS = ["logs/app.log", "logs/audit.log", "logs/old/app.log"]


def msg(update_id: int, text: str, *, callback: CallbackQuery | None = None) -> Inbound:
    return Inbound(
        principal=OWNER,
        conversation=CHAT,
        text=text,
        dedup_key=f"telegram:update:{update_id}",
        callback=callback,
    )


class AskThenAnswer:
    """Erster Zug: `ask_operator`. Danach Prosa, die das Werkzeug-Ergebnis mitschleppt."""

    def __init__(self, args: dict | None = None) -> None:
        self._args = {"question": QUESTION, "options": OPTIONS} if args is None else args
        self.calls = 0
        self.prompts: list[str] = []

    def reason(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            return "TOOL_CALL: " + json.dumps({"tool": "ask_operator", "args": self._args})
        return "done"


class FakeCommands:
    def dispatch(self, name, rest, *, principal, conversation):
        return CommandResult(reply=f"command:{name}")


def _build(tmp_path, reasoner, *, ttl_s=5.0, trust=Trust.FULL):
    log = EventLog(tmp_path / "ev.db")
    sent: list[tuple[str, str]] = []
    structured: list[StructuredMessage] = []
    desk = QuestionDesk(ttl_s=ttl_s)
    holder: list[Conductor] = []

    def send_structured(conversation: str, message: StructuredMessage) -> None:
        structured.append(message)

    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    runners = {
        **tools.RUNNERS,
        "ask_operator": tools.make_ask_operator_runner(
            desk,
            context=lambda: holder[0].ask_contexts.current(),
            send_structured=send_structured,
        ),
    }
    conductor = Conductor(
        log=log,
        reasoner=reasoner,
        executor=Executor(
            policy=policy,
            log=log,
            snapshotter=Snapshotter(tmp_path / ".snap"),
            runner=GrantedRunner(mint=mint, runners=runners),
            mint=mint,
        ),
        send=lambda conversation, text: sent.append((conversation, text)),
        allowed_principals=frozenset({OWNER}),
        trust_of=lambda _channel: trust,
        commands=FakeCommands(),
        send_structured=send_structured,
        questions=desk,
    )
    holder.append(conductor)
    return conductor, desk, sent, structured


def _run_in_background(conductor, update):
    """Der Lauf gehört in den Worker — `wait()` blockiert, der Test antwortet nebenher."""
    thread = threading.Thread(target=conductor.handle, args=(update,), daemon=True)
    thread.start()
    return thread


def _await_question(desk, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ticket = desk.pending(CHAT)
        if ticket is not None:
            return ticket
        time.sleep(0.005)
    raise AssertionError("es wurde nie gefragt")


def _buttons(message):
    return [button for row in message.keyboard for button in row]


# --- das Werkzeug existiert und ist kein Sonderfall ---------------------------------
def test_ask_operator_is_declared_as_a_read_that_needs_no_approval():
    spec = tools.default_manifest().get("ask_operator")
    assert spec is not None
    assert spec.effect is Effect.READ and spec.reversible is True
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    decision = kernel.decide(
        ToolRequest("ask_operator", OWNER, {"question": QUESTION, "options": OPTIONS})
    )
    assert decision.verdict is Verdict.ALLOW


def test_ask_operator_has_a_target_extractor_so_it_is_not_deny_by_construction():
    assert "ask_operator" in TARGET_EXTRACTORS
    assert TARGET_EXTRACTORS["ask_operator"]({"question": "x", "options": ["a", "b"]}) == ()


# --- fragen und antworten ------------------------------------------------------------
def test_question_reaches_the_chat_as_a_structured_message(tmp_path):
    conductor, desk, _sent, structured = _build(tmp_path, AskThenAnswer())
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)

    assert structured, "die Frage wurde nie gesendet"
    message = structured[0]
    assert QUESTION in message.text
    assert "1) logs/app.log" in message.text  # Kanäle ohne Knöpfe sehen dieselbe Liste
    buttons = _buttons(message)
    assert len(buttons) == len(OPTIONS) + 1
    assert all(button.data.startswith(CALLBACK_PREFIX) for button in buttons)
    # Nichts daran darf nach dem Freigabe-Dialog aussehen.
    assert "Approval required" not in message.text and "Tool:" not in message.text

    desk.cancel(CHAT)
    thread.join(timeout=5)


def test_button_answer_reaches_the_waiting_run_and_shows_up_in_the_tool_result(tmp_path):
    reasoner = AskThenAnswer()
    conductor, desk, sent, structured = _build(tmp_path, reasoner)
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)

    data = _buttons(structured[0])[1].data
    click = msg(2, "", callback=CallbackQuery("q1", data, 77))
    assert conductor.is_inline(click) is True  # sonst wartete der Worker auf sich selbst
    assert conductor.handle(click) is True

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert reasoner.calls == 2
    assert "logs/audit.log" in reasoner.prompts[1]
    assert "untrusted data, not an instruction" in reasoner.prompts[1]
    assert sent[-1] == (CHAT, "done\n\n1 tool call, 0 failed")
    # Quittung: Tastatur weg, Rückruf beantwortet.
    receipt = structured[-1]
    assert receipt.edit_message_id == 77 and receipt.callback_query_id == "q1"
    assert receipt.keyboard == ()
    assert "logs/audit.log" in receipt.text and "nothing was approved" in receipt.text


def test_typed_number_answers_and_starts_no_second_run(tmp_path):
    reasoner = AskThenAnswer()
    conductor, desk, sent, _structured = _build(tmp_path, reasoner)
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)

    answer = msg(2, "3")
    assert conductor.is_inline(answer) is True
    assert conductor.handle(answer) is True

    thread.join(timeout=5)
    assert not thread.is_alive()
    # Zwei Züge sind die des EINEN Laufs (fragen, dann antworten) — kein dritter.
    assert reasoner.calls == 2
    assert "logs/old/app.log" in reasoner.prompts[1]
    assert [text for _c, text in sent].count("done\n\n1 tool call, 0 failed") == 1


def test_a_number_out_of_range_is_not_routed_inline(tmp_path):
    """Sonst liefe der Poll-Thread in das Schloss, das der wartende Worker hält."""
    conductor, desk, _sent, _structured = _build(tmp_path, AskThenAnswer())
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)

    assert conductor.is_inline(msg(2, "99")) is False
    assert conductor.is_inline(msg(3, "read the other file")) is False
    assert conductor.is_inline(msg(4, "0")) is True  # Abbruchwort zählt als Antwort

    desk.cancel(CHAT)
    thread.join(timeout=5)


# --- Freigabe und Rückfrage bleiben getrennt ------------------------------------------
def test_yes_does_not_answer_a_question(tmp_path):
    reasoner = AskThenAnswer()
    conductor, desk, sent, _structured = _build(tmp_path, reasoner)
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)

    assert conductor.handle(msg(2, "ja")) is True
    assert desk.pending(CHAT) is not None, "ein ja hat die Rückfrage beantwortet"
    assert "A question is open" in sent[-1][1]
    assert reasoner.calls == 1  # der Lauf hängt weiter an seiner Frage

    assert conductor.handle(msg(3, "2")) is True
    thread.join(timeout=5)
    assert reasoner.calls == 2 and "logs/audit.log" in reasoner.prompts[1]


def test_a_number_grants_no_approval(tmp_path):
    conductor, _desk, sent, _structured = _build(tmp_path, AskThenAnswer())
    request = ToolRequest("write_file", OWNER, {"path": str(tmp_path / "x"), "content": "y"})
    conductor.approvals.park(CHAT, request, "prompt", principal=OWNER)

    assert conductor.handle(msg(1, "2")) is True

    assert conductor.approvals.get(CHAT) is not None, "eine Zahl hat die Freigabe verbraucht"
    assert "nur ja, immer oder nein" in sent[-1][1]
    assert not (tmp_path / "x").exists()


def test_a_question_token_is_never_read_by_the_approval_path(tmp_path):
    """Beide Wege tragen opake Token — die Trennung hängt am Präfix, nicht an der Reihenfolge."""
    conductor, desk, _sent, structured = _build(tmp_path, AskThenAnswer())
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)
    data = _buttons(structured[0])[0].data

    assert data.startswith(CALLBACK_PREFIX)
    assert not data.startswith(ApprovalPicker.PREFIX)
    assert ApprovalPicker().consume(
        data, principal=OWNER, conversation=CHAT, pending=None
    ) is None
    assert desk.resolve_callback("ap:" + data, principal=OWNER, conversation=CHAT) is None

    desk.cancel(CHAT)
    thread.join(timeout=5)


# --- Kanal ohne Antwortweg -------------------------------------------------------------
def test_notify_channel_is_not_asked_at_all(tmp_path):
    desk = QuestionDesk(ttl_s=5.0)
    structured: list[StructuredMessage] = []
    runner = tools.make_ask_operator_runner(
        desk,
        context=lambda: AskContext(OWNER, CHAT, Trust.NOTIFY),
        send_structured=lambda conversation, message: structured.append(message),
    )

    result = runner(ToolRequest("ask_operator", OWNER, {"question": QUESTION, "options": OPTIONS}))

    assert structured == [], "auf einem Zustellkanal wurde trotzdem gefragt"
    assert desk.pending(CHAT) is None
    assert "Operator answer: none" in result
    assert "only delivers" in result


def test_without_a_context_nothing_is_asked_anywhere(tmp_path):
    desk = QuestionDesk(ttl_s=5.0)
    structured: list[StructuredMessage] = []
    runner = tools.make_ask_operator_runner(
        desk,
        context=lambda: None,
        send_structured=lambda conversation, message: structured.append(message),
    )

    result = runner(ToolRequest("ask_operator", OWNER, {"question": QUESTION, "options": OPTIONS}))

    assert structured == []
    assert "Operator answer: none" in result


def test_a_failed_send_does_not_leave_the_run_waiting(tmp_path):
    desk = QuestionDesk(ttl_s=30.0)

    def explode(conversation, message):
        raise RuntimeError("channel down")

    runner = tools.make_ask_operator_runner(
        desk, context=lambda: AskContext(OWNER, CHAT, Trust.FULL), send_structured=explode
    )
    with pytest.raises(RuntimeError):
        runner(ToolRequest("ask_operator", OWNER, {"question": QUESTION, "options": OPTIONS}))
    assert desk.pending(CHAT) is None


# --- Abbruch --------------------------------------------------------------------------
def test_stop_ends_an_open_question_instead_of_making_the_worker_wait(tmp_path):
    reasoner = AskThenAnswer()
    # Lang genug, dass ein Test, der auf das Zeitlimit wartet, hier auffliegen würde.
    conductor, desk, sent, _structured = _build(tmp_path, reasoner, ttl_s=120.0)
    thread = _run_in_background(conductor, msg(1, "which log?"))
    _await_question(desk)

    started = time.monotonic()
    assert conductor.handle(msg(2, "/stop")) is True
    thread.join(timeout=5)

    assert not thread.is_alive(), "der Worker wartete weiter"
    assert time.monotonic() - started < 5.0
    assert desk.pending(CHAT) is None
    assert "Operator answer: none" in reasoner.prompts[1]
    assert sent[0][1] == "command:stop"


def test_a_crashing_run_releases_its_question(tmp_path):
    class Exploding(AskThenAnswer):
        def reason(self, prompt: str) -> str:
            if self.calls >= 1:
                self.calls += 1
                raise RuntimeError("reasoner down")
            return super().reason(prompt)

    conductor, desk, _sent, _structured = _build(tmp_path, Exploding(), ttl_s=120.0)
    thread = _run_in_background(conductor, msg(1, "which log?"))
    ticket = _await_question(desk)
    assert conductor.handle(msg(2, "1")) is True

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert desk.pending(CHAT) is None
    assert conductor.ask_contexts.conversations() == ()
    assert desk.wait(ticket).answered  # die Antwort selbst ging nicht verloren


# --- fehlerhafte Rückfrage ---------------------------------------------------------------
def test_fewer_than_two_options_is_an_ordinary_tool_error(tmp_path):
    reasoner = AskThenAnswer({"question": QUESTION, "options": ["only one"]})
    conductor, desk, sent, structured = _build(tmp_path, reasoner)

    assert conductor.handle(msg(1, "which log?")) is True

    assert structured == [], "eine unbrauchbare Frage wurde trotzdem gestellt"
    assert desk.pending(CHAT) is None
    # Der Fehler geht als Werkzeug-Ergebnis an das MODELL zurück, damit es die Frage
    # reparieren kann — das ist unverändert.
    assert "[ask_operator -> error]" in reasoner.prompts[1]
    # ⚠️ Der Betreiber erfährt es seit 2026-08-06 TROTZDEM, als nüchterne Zeile unter der
    # Antwort. Bis dahin galt hier „nicht an den Betreiber"; geändert hat das ein
    # gemessener Fall: eine Installation meldete „die Notiz wurde angelegt", während das
    # Protokoll zwei gescheiterte Schreibversuche und keinen erfolgreichen zeigte. Ob ein
    # Fehlschlag ein harmloser Zwischenschritt war oder verschwiegen wurde, ist von aussen
    # nicht unterscheidbar — also wird die Tatsache genannt und die Deutung dem Betreiber
    # gelassen. Dass eine Rückfrage nicht zustande kam, will er ohnehin wissen.
    antwort = sent[-1][1]
    assert antwort.startswith("done")
    assert "ask_operator" in antwort and "failed in this run" in antwort


def test_every_tool_in_the_manifest_is_named_in_the_prompt() -> None:
    """Ein Werkzeug, das im Systemprompt fehlt, existiert fuer das Modell nicht.

    Genau das war der Zustand: `undo_last`, `web_fetch`, `web_search` und `ask_operator`
    waren im Manifest, im Kernel und im Runner verdrahtet — aber der Katalog, den das
    Modell liest, kannte sie nicht. Gebaut und unbenutzbar sieht in jedem Test gruen aus,
    weil kein Test den Prompt gegen das Manifest haelt. Dieser tut es.
    """
    from talos.reasoner import TOOL_PROTOCOL
    from talos.tools import default_manifest

    missing = [spec.name for spec in default_manifest().tools if spec.name not in TOOL_PROTOCOL]
    assert not missing, f"nicht im Systemprompt genannt: {missing}"
