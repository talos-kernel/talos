from __future__ import annotations

from pathlib import Path

from talos import tools
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import CallbackQuery, Inbound, Principal, StructuredMessage, Trust
from talos.commands import CommandCenter
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PolicyKernel
from talos.provider import ModelPicker, ModelRouter, ModelSelection, Provider, ProviderRegistry
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "7")
STRANGER = Principal("telegram", "8")
CHAT = "telegram:42"


class Reasoner:
    def __init__(self, selection=None): self.selection = selection
    def reason(self, prompt): return "answer"
    def cancel(self): return False


class Worker:
    def pending(self): return 0
    def busy(self): return False
    def drain(self): return 0


def build(tmp_path: Path):
    log = EventLog(tmp_path / "e.db")
    registry = ProviderRegistry((Provider("alpha", "Alpha", ("one", "two")), Provider("beta", "Beta", ("x",)),))
    router = ModelRouter(registry, ModelSelection("alpha", "one"), Reasoner, log)
    picker = ModelPicker(registry, router, token_factory=lambda: "tok")
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    executor = Executor(policy, log, Snapshotter(tmp_path / "snap"), GrantedRunner(mint, dict(tools.RUNNERS)), mint)
    commands = CommandCenter(log, __import__('talos.approval', fromlist=['ApprovalStore']).ApprovalStore(), policy, 0, "bot", router, Worker(), tmp_path, model_picker=picker)
    sent: list[StructuredMessage] = []
    conductor = Conductor(log, router, executor, lambda c, t: None, frozenset({OWNER}), lambda c: Trust.FULL, commands=commands, send_structured=lambda c, ui: sent.append(ui))
    return conductor, router, sent


def inbound(uid: int, principal: Principal, text="", callback=None):
    return Inbound(principal, CHAT, text, f"telegram:update:{uid}", callback=callback)


def test_blocking_model_selection_is_worker_routed_but_navigation_stays_inline(tmp_path: Path) -> None:
    conductor, _router, _sent = build(tmp_path)
    assert conductor.is_inline(inbound(1, OWNER, "/model")) is True
    assert conductor.is_inline(inbound(2, OWNER, "/model alpha two")) is False
    assert conductor.is_inline(
        inbound(3, OWNER, callback=CallbackQuery("p", "tm:tok:p:0", 5))
    ) is True
    assert conductor.is_inline(
        inbound(4, OWNER, callback=CallbackQuery("m", "tm:tok:m:1", 5))
    ) is False
    assert conductor.is_inline(
        inbound(5, OWNER, callback=CallbackQuery("a", "ap:opaque", 5))
    ) is False
    assert conductor.is_inline(inbound(6, OWNER, "/approve")) is False
    assert conductor.is_inline(inbound(7, OWNER, "/deny")) is False
    assert conductor.is_inline(inbound(8, OWNER, "/stop")) is True


def test_model_command_returns_structured_provider_picker(tmp_path: Path) -> None:
    conductor, _router, sent = build(tmp_path)
    assert conductor.handle(inbound(1, OWNER, "/model"))
    assert sent and sent[-1].keyboard
    assert "Select a provider" in sent[-1].text


def test_authorized_callback_switches_but_unauthorized_callback_cannot(tmp_path: Path) -> None:
    conductor, router, sent = build(tmp_path)
    conductor.handle(inbound(1, OWNER, "/model"))
    provider_data = sent[-1].keyboard[0][0].data

    assert conductor.handle(inbound(2, STRANGER, callback=CallbackQuery("bad", provider_data, 5))) is False
    assert router.current == ModelSelection("alpha", "one")

    assert conductor.handle(inbound(3, OWNER, callback=CallbackQuery("ok1", provider_data, 5)))
    model_data = sent[-1].keyboard[0][1].data
    assert conductor.handle(inbound(4, OWNER, callback=CallbackQuery("ok2", model_data, 5)))
    assert router.current == ModelSelection("alpha", "two")
    assert sent[-1].edit_message_id == 5
    assert sent[-1].callback_query_id == "ok2"


def test_typed_model_switch_is_deterministic_and_full_trust_control(tmp_path: Path) -> None:
    conductor, router, _sent = build(tmp_path)
    assert conductor.handle(inbound(1, OWNER, "/model alpha two"))
    assert router.current == ModelSelection("alpha", "two")


# --- Der Router darf die Text-Senke nicht verschlucken ---------------------------


class StreamingReasoner:
    """Ein Reasoner, der `on_text` ausdruecklich kennt — wie die claude-CLI."""

    def __init__(self, selection=None):
        self.selection = selection
        self.saw_sink = False

    def reason(self, prompt, on_text=None):
        self.saw_sink = on_text is not None
        if on_text is not None:
            on_text("hal")
            on_text("lo")
        return "hallo"

    def cancel(self):
        return False


def test_router_hands_the_text_sink_to_a_reasoner_that_knows_it(tmp_path: Path) -> None:
    """Ohne diese Zeile ist das gesamte Streaming still wirkungslos.

    Der Router steht zwischen Conductor und Reasoner. Nahm seine `reason` das Argument
    nicht an, bot der Conductor eine Senke an, die nie ankam — und niemand merkte es,
    weil die Antwort korrekt blieb. Genau dieser Zustand war live.
    """
    log = EventLog(tmp_path / "e.db")
    registry = ProviderRegistry((Provider("alpha", "Alpha", ("one",)),))
    built: list[StreamingReasoner] = []

    def build_one(selection):
        made = StreamingReasoner(selection)
        built.append(made)
        return made

    router = ModelRouter(registry, ModelSelection("alpha", "one"), build_one, log)
    seen: list[str] = []
    assert router.reason("frage", on_text=seen.append) == "hallo"
    assert seen == ["hal", "lo"]
    assert built[0].saw_sink is True


def test_router_leaves_out_the_sink_where_the_reasoner_has_none(tmp_path: Path) -> None:
    """Hermes kennt `on_text` nicht. Weiterreichen waere ein TypeError statt einer Antwort."""
    log = EventLog(tmp_path / "e.db")
    registry = ProviderRegistry((Provider("alpha", "Alpha", ("one",)),))
    router = ModelRouter(registry, ModelSelection("alpha", "one"), Reasoner, log)
    assert router.reason("frage", on_text=lambda _: None) == "answer"
