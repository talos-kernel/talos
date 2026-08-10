"""Conductor: Ingest/Identität/Idempotenz + die Freigabe-Runde end-to-end."""
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import tools
from talos.approval import ApprovalPicker, ApprovalStore
from talos.capability import CapabilityMint, GrantedRunner
from talos.commands import CommandResult
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor, Outcome, Status
from talos.policy import PolicyKernel, ToolRequest
from talos.snapshot import Snapshotter
from talos.channel import CallbackQuery, Inbound, Principal, StructuredMessage, Trust

OWNER = Principal("telegram", "100000001")
STRANGER = Principal("telegram", "111111")
SECOND_ALLOWED = Principal("telegram", "222222")
CHAT_OWNER = "telegram:100000001"
CHAT_STRANGER = "telegram:111111"


def msg(update_id: int, principal: Principal, text: str, conversation: str | None = None) -> Inbound:
    """Eingang bauen. Ohne `conversation` chattet jeder in seinem eigenen Chat."""
    return Inbound(
        principal=principal,
        conversation=conversation or f"telegram:{principal.user_id}",
        text=text,
        dedup_key=f"telegram:update:{update_id}",
    )


class FakeReasoner:
    """Echo — nie ein Tool-Call, immer finale Antwort."""

    def __init__(self) -> None:
        self.calls = 0

    def reason(self, prompt: str) -> str:
        self.calls += 1
        return f"echo: {prompt}"


class ScriptedReasoner:
    """Erste Anfrage -> `first` (i.d.R. ein Tool-Call), danach -> `rest` (Prosa)."""

    def __init__(self, first: str, rest: str = "Fertig.") -> None:
        self._first, self._rest = first, rest
        self.calls = 0

    def reason(self, prompt: str) -> str:
        self.calls += 1
        return self._first if self.calls == 1 else self._rest


def _tool_call(tool: str, args: dict, targets: list[str]) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args, "targets": targets})


def full_trust(_channel: str) -> Trust:
    return Trust.FULL


def _build(
    tmp_path,
    reasoner,
    *,
    clock=None,
    ttl_s=300,
    trust_of=full_trust,
    begin_activity=None,
    begin_reply=None,
    commands=None,
    approval_picker=None,
    send_structured=None,
    allowed_principals=None,
):
    log = EventLog(tmp_path / "ev.db")
    sent: list[tuple[str, str]] = []
    allowed = frozenset({OWNER}) if allowed_principals is None else frozenset(allowed_principals)
    policy = PolicyKernel(tools.default_manifest(), allowed)
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )
    approvals = ApprovalStore(ttl_s=ttl_s, clock=clock) if clock else ApprovalStore(ttl_s=ttl_s)
    conductor = Conductor(
        log=log,
        reasoner=reasoner,
        executor=executor,
        send=lambda conversation, text: sent.append((conversation, text)),
        allowed_principals=allowed,
        trust_of=trust_of,
        approvals=approvals,
        begin_activity=begin_activity,
        begin_reply=begin_reply,
        commands=commands,
        approval_picker=approval_picker,
        send_structured=send_structured,
    )
    return conductor, sent


class FakeActivity:
    def __init__(self) -> None:
        self.events = []
        self.succeeded = 0
        self.failed: list[str] = []

    def progress(self, event) -> None:
        self.events.append(event)

    def succeed(self, footer: str = "") -> None:
        self.succeeded += 1

    def fail(self, error: str) -> None:
        self.failed.append(error)


class FakeCommands:
    def dispatch(self, name, rest, *, principal, conversation):
        return CommandResult(reply=f"command:{name}")


class ApproveCommands(FakeCommands):
    def dispatch(self, name, rest, *, principal, conversation):
        if name == "approve":
            return CommandResult(forward_as="yes")
        return super().dispatch(name, rest, principal=principal, conversation=conversation)


def make(tmp_path):
    reasoner = FakeReasoner()
    conductor, sent = _build(tmp_path, reasoner)
    return conductor, reasoner, sent


# --- Basis: Identität, Idempotenz, einfache Antwort -----------------------------
def test_allowed_user_gets_reasoned_reply(tmp_path):
    conductor, reasoner, sent = make(tmp_path)
    ok = conductor.handle(msg(1, OWNER, "hallo"))
    assert ok is True
    assert reasoner.calls == 1
    assert sent == [(CHAT_OWNER, "echo: hallo")]


def test_stranger_is_rejected_without_reasoning(tmp_path):
    conductor, reasoner, sent = make(tmp_path)
    ok = conductor.handle(msg(2, STRANGER, "hi"))
    assert ok is False
    assert reasoner.calls == 0
    assert sent == []


def test_normal_turn_gets_one_activity_which_succeeds_after_clean_reply(tmp_path):
    created: list[FakeActivity] = []

    def begin(_conversation: str) -> FakeActivity:
        activity = FakeActivity()
        created.append(activity)
        return activity

    conductor, sent = _build(tmp_path, FakeReasoner(), begin_activity=begin)
    assert conductor.handle(msg(20, OWNER, "hallo")) is True
    assert len(created) == 1
    assert created[0].events
    assert created[0].succeeded == 1
    assert created[0].failed == []
    assert sent == [(CHAT_OWNER, "echo: hallo")]


def test_reasoner_failure_finalizes_activity_without_dirty_result(tmp_path):
    class BrokenReasoner:
        def reason(self, _prompt: str) -> str:
            raise RuntimeError("Modell nicht erreichbar")

    activity = FakeActivity()
    conductor, sent = _build(tmp_path, BrokenReasoner(), begin_activity=lambda _c: activity)
    assert conductor.handle(msg(21, OWNER, "hallo")) is False
    assert activity.succeeded == 0
    assert activity.failed == ["Modell nicht erreichbar"]
    assert sent == []


def test_unauthorized_and_commands_never_create_activity(tmp_path):
    began: list[str] = []

    def begin(conversation: str):
        began.append(conversation)
        return FakeActivity()

    conductor, _sent = _build(
        tmp_path, FakeReasoner(), begin_activity=begin, commands=FakeCommands()
    )
    assert conductor.handle(msg(22, STRANGER, "spioniere")) is False
    assert conductor.handle(msg(23, OWNER, "/status")) is True
    assert began == []


def test_duplicate_update_is_skipped(tmp_path):
    conductor, reasoner, sent = make(tmp_path)
    upd = msg(7, OWNER, "eins")
    assert conductor.handle(upd) is True
    assert conductor.handle(upd) is False  # idempotent
    assert reasoner.calls == 1
    assert len(sent) == 1


def test_lone_yes_without_pending_executes_nothing(tmp_path):
    conductor, reasoner, sent = make(tmp_path)
    ok = conductor.handle(msg(3, OWNER, "yes"))
    assert ok is True
    assert reasoner.calls == 0  # „ja" darf nie an den Reasoner gehen
    assert "no approval is pending" in sent[-1][1].lower()


# --- Freigabe-Runde -------------------------------------------------------------
def test_needs_human_parks_and_shows_kernel_facts(tmp_path):
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, sent = _build(tmp_path, reasoner)
    ok = conductor.handle(msg(1, OWNER, "sag hi"))
    assert ok is True
    parked = conductor.approvals.get(CHAT_OWNER)
    assert parked is not None and parked.req.tool == "run_shell"
    assert parked.request_text == "sag hi"
    prompt = sent[-1][1]
    assert "Tool: run_shell" in prompt          # Kernel-Wahrheit, nicht LLM-Text
    assert "Command: echo hi" in prompt          # voller Command-String
    assert "Reason:" in prompt


def test_pending_decisions_are_worker_routed_while_stop_stays_inline(tmp_path):
    conductor, _reasoner, _sent = make(tmp_path)
    assert conductor.is_inline(msg(30, OWNER, "yes")) is True
    conductor.approvals.park(
        CHAT_OWNER,
        ToolRequest("run_shell", OWNER, {"command": "true"}),
        "approve",
    )
    assert conductor.is_inline(msg(31, OWNER, "yes")) is False
    assert conductor.is_inline(msg(32, OWNER, "no")) is False
    assert conductor.is_inline(msg(33, OWNER, "/stop")) is True


def test_yes_executes_pending_once_then_second_yes_is_noop(tmp_path):
    reasoner = ScriptedReasoner(
        _tool_call("run_shell", {"command": "echo hi"}, []),
        "**Status**\n\n- Ausgabe: hi",
    )
    conductor, sent = _build(tmp_path, reasoner)
    conductor.handle(msg(1, OWNER, "sag hi"))
    assert reasoner.calls == 1

    assert conductor.handle(msg(2, OWNER, "yes")) is True
    assert reasoner.calls == 2                     # zweiter Lauf präsentiert nur das Ergebnis
    assert conductor.approvals.get(CHAT_OWNER) is None    # one-shot: geleert
    assert "hi" in sent[-1][1]                      # echo-Ausgabe geliefert
    assert "rc=0" not in sent[-1][1]
    assert "TOOL_CALL" not in sent[-1][1]

    # zweites „ja" läuft ins Leere — nichts mehr geparkt
    assert conductor.handle(msg(3, OWNER, "yes")) is True
    assert "no approval is pending" in sent[-1][1].lower()


def test_approved_effect_serializes_next_reasoning_turn(tmp_path):
    effect_started = threading.Event()
    release_effect = threading.Event()
    reason_started = threading.Event()

    class ObservedReasoner:
        def reason(self, prompt: str) -> str:
            reason_started.set()
            return "done"

    class BlockingExecutor:
        def run(self, req, run_id, *, expected=None, human_approved=False):
            assert human_approved is True
            effect_started.set()
            assert release_effect.wait(2)
            return Outcome(Status.DONE, "approved effect complete")

    log = EventLog(tmp_path / "serialized.db")
    approvals = ApprovalStore()
    conductor = Conductor(
        log=log,
        reasoner=ObservedReasoner(),
        executor=BlockingExecutor(),  # type: ignore[arg-type]
        send=lambda _conversation, _text: None,
        allowed_principals=frozenset({OWNER}),
        trust_of=full_trust,
        approvals=approvals,
    )
    approvals.park(
        CHAT_OWNER,
        ToolRequest("run_shell", OWNER, {"command": "true"}),
        "approve",
    )

    approval = threading.Thread(target=lambda: conductor.handle(msg(40, OWNER, "yes")))
    approval.start()
    assert effect_started.wait(1)
    assert approvals.get(CHAT_OWNER) is None

    next_turn = threading.Thread(target=lambda: conductor.handle(msg(41, OWNER, "next")))
    next_turn.start()
    assert not reason_started.wait(0.2), "new reasoning overlapped the approved effect"

    release_effect.set()
    approval.join(2)
    next_turn.join(2)
    assert not approval.is_alive()
    assert not next_turn.is_alive()
    assert reason_started.is_set()


def test_approval_resumes_full_agent_loop_until_verified_final_answer(tmp_path):
    target = tmp_path / "identitaet.md"
    target.write_text("name: Talos\n", encoding="utf-8")
    steps = iter(
        (
            _tool_call("run_shell", {"command": f"printf '{target}'"}, []),
            _tool_call("read_file", {"path": str(target)}, [str(target)]),
            _tool_call(
                "run_shell",
                {"command": f"printf 'name: Talos\\n' > '{target}'"},
                [],
            ),
            _tool_call("read_file", {"path": str(target)}, [str(target)]),
            "**Erledigt**\n\n`identitaet.md` enthält jetzt `name: Talos` und wurde verifiziert.",
        )
    )

    class SequenceReasoner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return next(steps)

    reasoner = SequenceReasoner()
    conductor, sent = _build(tmp_path, reasoner)

    assert conductor.handle(msg(501, OWNER, "Ändere deinen Namen in identitaet.md zu Talos"))
    pending_find = conductor.approvals.get(CHAT_OWNER)
    assert pending_find is not None and pending_find.req.tool == "run_shell"

    # Freigabe des Suchschritts setzt denselben Task fort; der Shell-Write fragt separat.
    assert conductor.handle(msg(502, OWNER, "yes"))
    pending_write = conductor.approvals.get(CHAT_OWNER)
    assert pending_write is not None and pending_write.req.tool == "run_shell"
    assert target.read_text(encoding="utf-8") == "name: Talos\n"
    assert "Final Answer" not in sent[-1][1]

    assert conductor.handle(msg(503, OWNER, "yes"))
    assert conductor.approvals.get(CHAT_OWNER) is None
    assert target.read_text(encoding="utf-8") == "name: Talos\n"
    assert "wurde verifiziert" in sent[-1][1]
    assert "rc=0" not in sent[-1][1]
    assert "run_shell" not in sent[-1][1]
    remembered = conductor.memory.recall(CHAT_OWNER)
    assert remembered[-2].text == "Ändere deinen Namen in identitaet.md zu Talos"
    assert "wurde verifiziert" in remembered[-1].text


def test_approved_vpn_probe_is_interpreted_instead_of_dumped(tmp_path):
    outputs = iter(
        (
            _tool_call(
                "run_shell",
                {
                    "command": (
                    )
                },
                [],
            ),
            (
                "**VPN-Status – VPS**\n\n"
                "- Tunnel/Proxy: kein zusätzlicher VPN-Tunnel\n\n"
            ),
        )
    )

    class VpnReasoner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return next(outputs)

    reasoner = VpnReasoner()
    conductor, sent = _build(tmp_path, reasoner)
    conductor.handle(msg(504, OWNER, "VPN-Status auf VPS prüfen"))
    assert conductor.handle(msg(505, OWNER, "yes"))

    assert "[Tool results so far]" in reasoner.prompts[-1]
    assert "VPN-Status auf VPS prüfen" in reasoner.prompts[-1]
    assert "rc=0" not in sent[-1][1]


def test_resumed_loop_bounds_tool_output_before_sending_it_to_reasoner(tmp_path):
    class BoundedReasoner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return _tool_call("run_shell", {"command": "python3 -c 'print(\"x\" * 50000)'"}, [])
            return "Ausgabe war sehr lang, Statusprüfung abgeschlossen."

    reasoner = BoundedReasoner()
    conductor, sent = _build(tmp_path, reasoner)
    conductor.handle(msg(506, OWNER, "Lange Diagnose"))
    assert conductor.handle(msg(507, OWNER, "yes"))
    assert len(reasoner.prompts[-1]) < 20000
    assert "truncated" in reasoner.prompts[-1]
    assert "Statusprüfung abgeschlossen" in sent[-1][1]


def test_no_rejects_and_runs_nothing(tmp_path):
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, sent = _build(tmp_path, reasoner)
    conductor.handle(msg(1, OWNER, "sag hi"))
    assert conductor.handle(msg(2, OWNER, "no")) is True
    assert conductor.approvals.get(CHAT_OWNER) is None
    assert "discarded" in sent[-1][1].lower()
    assert not any("rc=0" in text for _, text in sent)  # kein Lauf


def test_yes_aborts_when_target_changed_since_asking(tmp_path):
    conductor, sent = _build(tmp_path, FakeReasoner())
    target = tmp_path / "cfg"
    target.write_text("orig", encoding="utf-8")
    req = ToolRequest("write_file", OWNER, {"path": str(target), "content": "neu"}, ())
    conductor.approvals.park(CHAT_OWNER, req, "prompt")       # simuliere geparkte Freigabe
    target.write_text("getauscht", encoding="utf-8")   # Datei nach dem Fragen tauschen

    assert conductor.handle(msg(9, OWNER, "yes")) is True
    assert target.read_text(encoding="utf-8") == "getauscht"  # NICHT überschrieben
    assert conductor.approvals.get(CHAT_OWNER) is None
    assert "changed" in sent[-1][1].lower()


def test_yes_after_ttl_expiry_is_noop(tmp_path):
    now = [1000.0]
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, sent = _build(tmp_path, reasoner, clock=lambda: now[0], ttl_s=300)
    conductor.handle(msg(1, OWNER, "sag hi"))
    assert conductor.approvals.get(CHAT_OWNER) is not None

    now[0] = 1400.0  # über die TTL hinaus
    assert conductor.handle(msg(2, OWNER, "yes")) is True
    assert "no approval is pending" in sent[-1][1].lower()
    assert not any("rc=0" in text for _, text in sent)  # nichts ausgeführt


def test_stranger_yes_cannot_resolve_alis_pending(tmp_path):
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, sent = _build(tmp_path, reasoner)
    conductor.handle(msg(1, OWNER, "sag hi"))
    assert conductor.approvals.get(CHAT_OWNER) is not None

    ok = conductor.handle(msg(2, STRANGER, "yes"))
    assert ok is False                                  # fremde Identität abgewiesen
    assert conductor.approvals.get(CHAT_OWNER) is not None     # des Betreibers Freigabe unangetastet


# --- Kanal-Decke: parken nur, wo es auch loesbar ist -----------------------------
def ask_trust(_channel: str) -> Trust:
    return Trust.ASK


def test_kein_parken_auf_einem_kanal_der_nicht_freigeben_kann(tmp_path):
    """Sonst blockiert Talos sich selbst: geparkt, aber niemand hier kann „ja" sagen.

    Der Nutzer saehe „Freigabe noetig" und danach bis zum TTL-Ablauf nichts — das
    liest sich wie „gleich passiert etwas", und genau das passiert nie.
    """
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, sent = _build(tmp_path, reasoner, trust_of=ask_trust)

    assert conductor.handle(msg(1, OWNER, "sag hi")) is True
    assert conductor.approvals.get(CHAT_OWNER) is None      # nichts geparkt
    reply = sent[-1][1]
    assert "Not executed" in reply
    assert "Tool: run_shell" in reply                     # die Kernel-Fakten bleiben drin
    assert "Command: echo hi" in reply


def test_abgelehnte_freigabe_steht_im_log(tmp_path):
    """Eine Absage ohne Beleg ist von einem stillen Ausfall nicht zu unterscheiden."""
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, _sent = _build(tmp_path, reasoner, trust_of=ask_trust)
    conductor.handle(msg(1, OWNER, "sag hi"))
    types = [e["type"] for e in conductor.log.recent(50)]
    assert "approval.refused" in types
    assert "approval.parked" not in types


def test_approval_request_has_hermes_style_emoji_buttons_and_callback_executes(tmp_path):
    tokens = iter(("tok1", "tok2", "tok3"))
    picker = ApprovalPicker(token_factory=lambda: next(tokens))
    structured: list[StructuredMessage] = []
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "printf approved"}, []))
    conductor, _sent = _build(
        tmp_path,
        reasoner,
        approval_picker=picker,
        send_structured=lambda _conversation, message: structured.append(message),
    )

    assert conductor.handle(msg(200, OWNER, "mach den test")) is True
    prompt = structured[-1]
    assert [[button.label for button in row] for row in prompt.keyboard] == [
        ["✓ Allow once", "∞ Always allow"],
        ["✕ Deny"],
    ]

    callback = Inbound(
        principal=OWNER,
        conversation=CHAT_OWNER,
        text="",
        dedup_key="telegram:update:201",
        callback=CallbackQuery("query-1", prompt.keyboard[0][0].data, 77),
    )
    assert conductor.handle(callback) is True
    ack = structured[-2]
    result = structured[-1]
    assert ack.edit_message_id == 77
    assert ack.callback_query_id == "query-1"
    assert ack.keyboard == ()
    assert "checking your decision" in ack.text
    assert result.edit_message_id == 77
    assert result.callback_query_id is None
    assert result.keyboard == ()
    assert "Fertig." in result.text
    assert "rc=0" not in result.text
    assert "printf approved" not in result.text
    assert all(
        button.data.startswith("ap:")
        and not any(word in button.data for word in ("yes", "always", "no", "allow", "deny"))
        for row in prompt.keyboard
        for button in row
    )


def test_approval_deny_button_edits_prompt_and_executes_nothing(tmp_path):
    marker = tmp_path / "must-not-exist"
    tokens = iter(("tok1", "tok2", "tok3"))
    picker = ApprovalPicker(token_factory=lambda: next(tokens))
    structured: list[StructuredMessage] = []
    reasoner = ScriptedReasoner(
        _tool_call("run_shell", {"command": f"printf forbidden > {marker}"}, [])
    )
    conductor, _sent = _build(
        tmp_path,
        reasoner,
        approval_picker=picker,
        send_structured=lambda _conversation, message: structured.append(message),
    )
    assert conductor.handle(msg(210, OWNER, "mach das nicht")) is True
    deny = structured[-1].keyboard[1][0]
    callback = Inbound(
        OWNER,
        CHAT_OWNER,
        "",
        "telegram:update:211",
        CallbackQuery("query-deny", deny.data, 88),
    )
    assert conductor.handle(callback) is True
    assert marker.exists() is False
    assert structured[-1].text == "Discarded — nothing ran."
    assert structured[-1].edit_message_id == 88
    assert structured[-1].keyboard == ()


def test_typed_decisions_are_bound_to_requesting_principal(tmp_path):
    for index, decision in enumerate(("yes", "always", "no", "/approve"), start=1):
        root = tmp_path / f"case-{index}"
        root.mkdir()
        target = root / "marker"
        reasoner = ScriptedReasoner(
            _tool_call("run_shell", {"command": f"printf owned > {target}"}, [])
        )
        conductor, sent = _build(
            root,
            reasoner,
            commands=ApproveCommands() if decision == "/approve" else None,
            allowed_principals={OWNER, SECOND_ALLOWED},
        )
        assert conductor.handle(msg(index * 10, OWNER, "mach das", CHAT_OWNER)) is True
        pending = conductor.approvals.get(CHAT_OWNER)
        assert pending is not None

        assert conductor.handle(
            msg(index * 10 + 1, SECOND_ALLOWED, decision, CHAT_OWNER)
        ) is True

        assert target.exists() is False, decision
        assert conductor.approvals.get(CHAT_OWNER) is pending, decision
        assert conductor.standing.list(CHAT_OWNER, principal=SECOND_ALLOWED) == (), decision
        assert "another identity" in sent[-1][1].lower()


def test_resume_uses_memory_snapshot_from_original_request(tmp_path):
    class CapturingReasoner:
        def __init__(self):
            self.prompts = []

        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return _tool_call("run_shell", {"command": "printf checked"}, [])
            return "**Ergebnis**\n\n- Prüfung abgeschlossen."

    reasoner = CapturingReasoner()
    conductor, _sent = _build(tmp_path, reasoner)
    conductor.memory.remember(
        CHAT_OWNER, asked="vorherige Frage", answered="gebundener Kontext"
    )
    assert conductor.handle(msg(701, OWNER, "prüfe das", CHAT_OWNER)) is True
    pending = conductor.approvals.get(CHAT_OWNER)
    assert pending is not None
    assert any("gebundener Kontext" in turn.text for turn in pending.memory_context)

    conductor.memory.forget(CHAT_OWNER)
    assert conductor.handle(msg(702, OWNER, "yes", CHAT_OWNER)) is True

    assert len(reasoner.prompts) == 2
    assert "gebundener Kontext" in reasoner.prompts[1]
    assert "checked" in reasoner.prompts[1]


def test_approval_button_is_bound_to_exact_pending_request(tmp_path):
    tokens = iter(("one-a", "always-a", "deny-a"))
    picker = ApprovalPicker(token_factory=lambda: next(tokens), clock=lambda: 100.0)
    store = ApprovalStore(clock=lambda: 100.0)
    first = store.park(CHAT_OWNER, ToolRequest("run_shell", OWNER, {"command": "date"}), "first")
    message = picker.open(first.prompt, first, principal=OWNER, conversation=CHAT_OWNER)
    stale_token = message.keyboard[0][0].data
    second = store.park(CHAT_OWNER, ToolRequest("run_shell", OWNER, {"command": "whoami"}), "second")

    assert picker.consume(
        stale_token,
        principal=OWNER,
        conversation=CHAT_OWNER,
        pending=second,
    ) is None


def test_atomic_claim_rejects_old_pending_and_preserves_newer_request(tmp_path):
    nonces = iter(("approval-a", "approval-b"))
    store = ApprovalStore(clock=lambda: 100.0, nonce_factory=lambda: next(nonces))
    req_a = ToolRequest("run_shell", OWNER, {"command": "printf a"})
    req_b = ToolRequest("run_shell", OWNER, {"command": "printf b"})
    first = store.park(CHAT_OWNER, req_a, "A")
    second = store.park(CHAT_OWNER, req_b, "B")

    assert store.claim_if_current(CHAT_OWNER, first) is None
    assert store.get(CHAT_OWNER) is second
    assert store.claim_if_current(CHAT_OWNER, second) is second
    assert store.get(CHAT_OWNER) is None


# --- Mitwachsende Antwort: der Conductor reicht die Senke durch ---------------------
class FakeStream:
    """Kanal-neutrale Attrappe der wachsenden Antwort — protokolliert nur mit."""

    def __init__(self, *, adopts: bool = True) -> None:
        self.turns = 0
        self.deltas: list[str] = []
        self.adopted: list[str] = []
        self.settled = 0
        self._adopts = adopts

    def begin_turn(self) -> None:
        self.turns += 1

    def push(self, delta: str) -> None:
        self.deltas.append(delta)

    def adopt(self, text: str) -> bool:
        self.adopted.append(text)
        # Vertrag wie beim echten Kanal: ohne gewachsene Nachricht gibt es nichts zu
        # uebernehmen — dann sendet der Conductor normal.
        return self._adopts and bool(self.deltas)

    def settle(self) -> None:
        self.settled += 1


class SinkReasoner:
    """Reasoner mit Senke: liefert je Zug eine Liste von Bruchstuecken."""

    def __init__(self, *turns: tuple[str, ...]) -> None:
        self._turns = turns
        self.calls = 0

    def reason(self, prompt: str, on_text=None) -> str:
        chunks = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        if on_text is not None:
            for chunk in chunks:
                on_text(chunk)
        return "".join(chunks)


def test_streamed_answer_is_adopted_instead_of_sent_a_second_time(tmp_path):
    stream = FakeStream()
    conductor, sent = _build(
        tmp_path, SinkReasoner(("Fertig", " und rund.")), begin_reply=lambda _c: stream
    )

    assert conductor.handle(msg(60, OWNER, "und?")) is True
    assert stream.turns == 1
    assert stream.deltas == ["Fertig", " und rund."]
    assert stream.adopted == ["Fertig und rund."]   # exakt die endgueltige Antwort
    assert sent == []                               # keine zweite Nachricht


def test_stream_that_cannot_adopt_falls_back_to_a_normal_reply(tmp_path):
    stream = FakeStream(adopts=False)
    conductor, sent = _build(
        tmp_path, SinkReasoner(("Antwort",)), begin_reply=lambda _c: stream
    )

    assert conductor.handle(msg(61, OWNER, "und?")) is True
    assert stream.settled == 1
    assert sent == [(CHAT_OWNER, "Antwort")]


def test_parked_approval_settles_the_stream_and_never_adopts(tmp_path):
    stream = FakeStream()
    reasoner = ScriptedReasoner(_tool_call("run_shell", {"command": "echo hi"}, []))
    conductor, sent = _build(tmp_path, reasoner, begin_reply=lambda _c: stream)

    assert conductor.handle(msg(62, OWNER, "raeum auf")) is True
    assert stream.adopted == []
    assert stream.settled == 1
    assert "Approval required" in sent[-1][1]


def test_reasoner_without_a_sink_is_called_exactly_as_before(tmp_path):
    """Additiv: wer `on_text` nicht kennt, merkt vom Streaming nichts."""
    stream = FakeStream()
    reasoner = FakeReasoner()
    conductor, sent = _build(tmp_path, reasoner, begin_reply=lambda _c: stream)

    assert conductor.handle(msg(63, OWNER, "hallo")) is True
    assert reasoner.calls == 1
    assert stream.turns == 0 and stream.deltas == []
    assert sent == [(CHAT_OWNER, "echo: hallo")]


def test_a_reply_stream_that_cannot_start_never_costs_the_answer(tmp_path):
    def begin(_conversation: str):
        raise RuntimeError("kein Stream moeglich")

    conductor, sent = _build(tmp_path, SinkReasoner(("Antwort",)), begin_reply=begin)
    assert conductor.handle(msg(64, OWNER, "hallo")) is True
    assert sent == [(CHAT_OWNER, "Antwort")]


def test_a_reply_stream_that_breaks_mid_run_never_costs_the_answer(tmp_path):
    class BrokenStream(FakeStream):
        def begin_turn(self) -> None:
            raise RuntimeError("stream kaputt")

        def settle(self) -> None:
            raise RuntimeError("auch das noch")

    stream = BrokenStream()
    conductor, sent = _build(
        tmp_path, SinkReasoner(("Antwort",)), begin_reply=lambda _c: stream
    )
    assert conductor.handle(msg(65, OWNER, "hallo")) is True
    assert stream.deltas == []                       # nie gestreamt
    assert sent == [(CHAT_OWNER, "Antwort")]         # trotzdem zugestellt
