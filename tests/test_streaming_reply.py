"""Die mitwachsende Antwort: was gezeigt wird, was stumm bleibt, und dass es EINE bleibt.

Zwei Ebenen: `TelegramReply` allein (gefaelschte Uhr, gefaelschter Client) und der Weg
end-to-end durch den Conductor mit einem Reasoner, der wie die echte CLI in Bruchstuecken
liefert. Kein Netz, kein Modell.
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import tools
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Inbound, Principal, Trust
from talos.conductor import Conductor, reply_starter
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PolicyKernel
from talos.snapshot import Snapshotter
from talos.telegram import TelegramChannel, TelegramReply

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"
CHAT_ID = 100000001


@dataclass
class FakeChatClient:
    """Bot-API-Attrappe mit steigenden Nachrichten-IDs und schaltbaren Ausfaellen."""

    now: list[float] = field(default_factory=lambda: [0.0])
    sent: list[tuple[int, str, dict]] = field(default_factory=list)
    edited: list[tuple[int, int, str, dict]] = field(default_factory=list)
    actions: list[tuple[int, str]] = field(default_factory=list)
    next_id: int = 500
    fail_edits: bool = False
    fail_markdown: bool = False

    def send_message(self, chat_id: int, text: str, **kwargs) -> int:
        self.sent.append((chat_id, text, kwargs))
        self.next_id += 1
        return self.next_id

    def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        if self.fail_edits:
            raise RuntimeError("edit refused")
        if self.fail_markdown and kwargs.get("parse_mode"):
            raise RuntimeError("Bad Request: can't parse entities")
        self.edited.append((chat_id, message_id, text, kwargs))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        raise AssertionError("die Antwort wird nie geloescht")

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))

    # --- Bequemlichkeit fuer die Zusicherungen ---------------------------------
    @property
    def texts(self) -> list[str]:
        return [text for _chat, text, _kw in self.sent] + [
            text for _chat, _mid, text, _kw in self.edited
        ]

    @property
    def messages(self) -> int:
        return len(self.sent)


def _reply(client: FakeChatClient, **kwargs) -> TelegramReply:
    return TelegramReply(client, CHAT_ID, clock=lambda: client.now[0], **kwargs)


def _tool_call(tool: str, args: dict) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args, "targets": []})


# --- TelegramReply allein: die TOOL_CALL-Falle ---------------------------------------
def test_a_tool_call_never_reaches_the_chat_even_split_across_deltas() -> None:
    """Die Zeile ist Maschinerie. Sie darf auch nicht bruchstueckhaft auftauchen."""
    client = FakeChatClient()
    reply = _reply(client)

    for delta in ("TO", "OL", "_CA", "LL: {\"tool\": ", "\"read_file\"", ", \"args\": {}}"):
        reply.push(delta)

    assert client.sent == []
    assert client.edited == []
    assert reply.adopt("Fertig.") is False   # nichts gewachsen -> normal senden


def test_leading_whitespace_does_not_force_the_decision() -> None:
    """`parse_tool_call` erlaubt Leerraum vor der Zeile — hier darf er nichts entscheiden."""
    client = FakeChatClient()
    reply = _reply(client)

    for delta in ("\n", "  ", "TOOL_CALL: {\"tool\": \"run_shell\", \"args\": {}}"):
        reply.push(delta)

    assert client.texts == []


def test_prose_that_merely_starts_like_the_marker_still_appears() -> None:
    """Nur der echte Marker schweigt — sonst verschluckte ein Wort die Antwort."""
    client = FakeChatClient()
    reply = _reply(client)

    reply.push("TOOL")     # noch unentscheidbar: echtes Praefix
    assert client.texts == []
    reply.push("s sind bereit.")

    assert client.sent[0][1] == "TOOLs sind bereit."


# --- Die PLAN-Falle: rohes Plan-JSON gehoert nie in den Chat -------------------
def test_a_plan_line_never_reaches_the_chat_even_split_across_deltas() -> None:
    """Befund 27.08.: der Betreiber sah `PLAN: {"goal": …}` als rohes JSON im Chat.
    Die Zeile ist Maschinerie wie TOOL_CALL — ihre menschliche Form ist die
    Aktivitaetszeile (ProgressStage.PLAN), nicht die Nachricht."""
    client = FakeChatClient()
    reply = _reply(client)

    for delta in ("PL", "AN: {\"goal\": ", "\"SSZ-Ads\"", ", \"steps\": [\"suchen\"]}"):
        reply.push(delta)

    assert client.sent == []
    assert client.edited == []
    assert reply.adopt("Fertig.") is False   # nichts gewachsen -> normal senden


def test_plan_and_tool_call_in_one_turn_stay_silent() -> None:
    """Der Normalfall: Ankuendigung und erster Schritt im selben Zug."""
    client = FakeChatClient()
    reply = _reply(client)

    reply.push('PLAN: {"goal": "x", "steps": ["a", "b"]}\n')
    reply.push('TOOL_CALL: {"tool": "vault_search", "args": {"query": "x"}}')

    assert client.texts == []


def test_prose_that_merely_starts_like_plan_still_appears() -> None:
    """`PLANET der Affen` ist Prosa — nur der echte Marker mit Doppelpunkt schweigt."""
    client = FakeChatClient()
    reply = _reply(client)

    reply.push("PLAN")     # noch unentscheidbar: echtes Praefix
    assert client.texts == []
    reply.push("ET der Affen.")

    assert client.sent[0][1] == "PLANET der Affen."


def test_prose_grows_visibly_and_the_last_version_is_exactly_the_answer() -> None:
    client = FakeChatClient()
    reply = _reply(client, min_edit_interval=1.2)

    reply.push("Ja, ")                       # erste Fassung sofort
    client.now[0] = 1.3
    reply.push("das ")
    client.now[0] = 2.6
    reply.push("passt.")
    assert client.messages == 1              # eine Nachricht, die waechst
    assert len(client.edited) == 2           # zweimal sichtbar gewachsen

    assert reply.adopt("Ja, das passt.") is True
    assert client.messages == 1              # und keine zweite daneben
    assert client.edited[-1][2] == "Ja, das passt."
    assert client.edited[-1][3]["parse_mode"] == "HTML"


def test_growth_is_raw_and_only_the_final_version_is_formatted() -> None:
    """Waehrend des Wachsens kann ein Codeblock offen sein — formatiert waere das ein 400."""
    client = FakeChatClient()
    reply = _reply(client, min_edit_interval=0.0)

    reply.push("Hier: ```py\nx = 1")
    assert "parse_mode" not in client.sent[0][2]
    reply.push("\n```")
    assert "parse_mode" not in client.edited[0][3]

    reply.adopt("Hier:\n```py\nx = 1\n```")
    assert client.edited[-1][3]["parse_mode"] == "HTML"


def test_edits_keep_the_minimum_interval_instead_of_flooding() -> None:
    client = FakeChatClient()
    reply = _reply(client, min_edit_interval=1.2)

    for index in range(12):
        reply.push(f"Wort{index} ")
    assert client.messages == 1 and client.edited == []   # alles gebuendelt

    client.now[0] = 1.19
    reply.push("noch nicht ")
    assert client.edited == []

    client.now[0] = 1.2
    reply.push("jetzt")
    assert len(client.edited) == 1
    assert client.edited[-1][2].endswith("noch nicht jetzt")


def test_settle_flushes_the_throttled_tail() -> None:
    client = FakeChatClient()
    reply = _reply(client, min_edit_interval=1.2)
    reply.push("Anfang ")
    reply.push("und Rest")          # gedrosselt, noch unsichtbar

    reply.settle()
    assert client.edited[-1][2] == "Anfang und Rest"


def test_a_second_turn_reuses_the_same_message() -> None:
    """Pro Lauf hoechstens eine wachsende Nachricht — sonst waere sie selbst die Dublette."""
    client = FakeChatClient()
    reply = _reply(client, min_edit_interval=0.0)
    reply.push("Zwischenstand")

    reply.begin_turn()
    reply.push("Endgueltig")

    assert client.messages == 1
    assert client.edited[-1][2] == "Endgueltig"


def test_adopt_reports_delivered_when_the_answer_already_stands() -> None:
    """Telegram lehnt eine unveraenderte Bearbeitung ab — das ist kein Zustellfehler."""
    client = FakeChatClient(fail_edits=True)
    reply = _reply(client, min_edit_interval=0.0)
    reply.push("Alles klar.")

    assert client.sent[0][1] == "Alles klar."
    assert reply.adopt("Alles klar.") is True


def test_broken_markdown_costs_the_formatting_not_the_answer() -> None:
    client = FakeChatClient(fail_markdown=True)
    reply = _reply(client, min_edit_interval=0.0)
    reply.push("Teil ")

    assert reply.adopt("Teil *zwei") is True
    assert client.edited[-1][2] == "Teil *zwei"
    assert "parse_mode" not in client.edited[-1][3]


def test_channel_hands_out_a_reply_stream_for_its_chat() -> None:
    client = FakeChatClient()
    stream = TelegramChannel(client).begin_reply("telegram:4242")

    assert isinstance(stream, TelegramReply)
    stream.push("Hallo")
    assert client.sent[0][0] == 4242


# --- End-to-end durch den Conductor ---------------------------------------------------
class StreamingReasoner:
    """Antwortet in Bruchstuecken wie die echte CLI — ein Zug pro Aufruf."""

    def __init__(self, *turns: tuple[str, ...]) -> None:
        self._turns = turns
        self.calls = 0
        self.kwargs: list[tuple] = []

    def reason(self, prompt: str, on_text=None) -> str:
        chunks = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        self.kwargs.append((prompt, on_text))
        if on_text is not None:
            for chunk in chunks:
                on_text(chunk)
        return "".join(chunks)


class PlainReasoner:
    """Kennt keine Senke — wie Hermes und der Modell-Router heute."""

    def __init__(self, answer: str = "Antwort ohne Stream") -> None:
        self._answer = answer
        self.calls = 0

    def reason(self, prompt: str) -> str:
        self.calls += 1
        return self._answer


def _conductor(tmp_path, reasoner, *, client: FakeChatClient | None = None):
    log = EventLog(tmp_path / "stream.db")
    sent: list[tuple[str, str]] = []
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )
    begin_reply = None
    if client is not None:
        stream = _reply(client, min_edit_interval=0.0)
        begin_reply = lambda _conversation: stream   # noqa: E731 — eine Zeile, ein Zweck
    conductor = Conductor(
        log=log,
        reasoner=reasoner,
        executor=executor,
        send=lambda conversation, text: sent.append((conversation, text)),
        allowed_principals=frozenset({OWNER}),
        trust_of=lambda _channel: Trust.FULL,
        begin_reply=begin_reply,
    )
    return conductor, sent


def _msg(update_id: int, text: str) -> Inbound:
    return Inbound(
        principal=OWNER,
        conversation=CHAT,
        text=text,
        dedup_key=f"telegram:update:{update_id}",
    )


def test_answer_grows_in_place_and_is_never_sent_a_second_time(tmp_path) -> None:
    client = FakeChatClient()
    reasoner = StreamingReasoner(("Der ", "Kessel ", "ist kalt."))
    conductor, sent = _conductor(tmp_path, reasoner, client=client)

    assert conductor.handle(_msg(1, "wie warm ist es?")) is True
    assert sent == []                                   # keine zweite Nachricht
    assert client.messages == 1
    assert client.edited[-1][2] == "Der Kessel ist kalt."
    assert client.edited[-1][3]["parse_mode"] == "HTML"


def test_tool_turn_stays_silent_and_only_the_prose_turn_shows(tmp_path) -> None:
    """Der Lauf hat zwei Zuege: Werkzeug (stumm) und Antwort (sichtbar)."""
    target = tmp_path / "kessel.md"
    target.write_text("kalt\n", encoding="utf-8")
    client = FakeChatClient()
    reasoner = StreamingReasoner(
        ("TOOL_", "CALL: ", json.dumps({"tool": "read_file", "args": {"path": str(target)}})),
        ("Steht ", "drin: kalt."),
    )
    conductor, sent = _conductor(tmp_path, reasoner, client=client)

    assert conductor.handle(_msg(2, "was steht in der Datei?")) is True
    assert reasoner.calls == 2
    assert sent == []
    assert client.messages == 1
    assert all("TOOL" not in text for text in client.texts)
    assert client.edited[-1][2] == "Steht drin: kalt.\n\n1 tool call, 0 failed"


def test_an_approval_run_leaves_no_stray_answer_message(tmp_path) -> None:
    """Der Freigabe-Dialog ist die einzige Nachricht; der stumme Zug hinterlaesst nichts."""
    client = FakeChatClient()
    reasoner = StreamingReasoner(
        ("TOOL_CALL: ", json.dumps({"tool": "run_shell", "args": {"command": "echo hi"}})),
    )
    conductor, sent = _conductor(tmp_path, reasoner, client=client)

    assert conductor.handle(_msg(3, "raeum auf")) is True
    assert client.texts == []
    assert "Approval required" in sent[-1][1]


def test_the_answer_arrives_even_when_every_edit_fails(tmp_path) -> None:
    """Die Anzeige ist Komfort, die Antwort nicht."""
    client = FakeChatClient(fail_edits=True)
    reasoner = StreamingReasoner(("Trotzdem ", "zugestellt."))
    conductor, sent = _conductor(tmp_path, reasoner, client=client)

    assert conductor.handle(_msg(4, "und?")) is True
    assert sent == [(CHAT, "Trotzdem zugestellt.")]


def test_a_reasoner_without_a_sink_behaves_exactly_as_before(tmp_path) -> None:
    """Regression: Hermes und der Modell-Router nehmen nur den Prompt."""
    client = FakeChatClient()
    reasoner = PlainReasoner()
    conductor, sent = _conductor(tmp_path, reasoner, client=client)

    assert conductor.handle(_msg(5, "hallo")) is True
    assert reasoner.calls == 1
    assert client.texts == []                     # nichts gewachsen
    assert sent == [(CHAT, "Antwort ohne Stream")]


def test_without_a_reply_sink_the_stream_is_never_offered(tmp_path) -> None:
    reasoner = StreamingReasoner(("Antwort",))
    conductor, sent = _conductor(tmp_path, reasoner)   # kein Client -> kein begin_reply

    assert conductor.handle(_msg(6, "hallo")) is True
    assert reasoner.kwargs[0][1] is None
    assert sent == [(CHAT, "Antwort")]


def test_reply_starter_bridges_a_registry_and_stays_quiet_for_other_channels() -> None:
    client = FakeChatClient()

    class Registry:
        def get(self, name: str):
            return TelegramChannel(client) if name == "telegram" else object()

    start = reply_starter(Registry())
    assert isinstance(start("telegram:4242"), TelegramReply)
    assert start("mail:robin@example.test") is None
