"""Das Gedaechtnis am lebenden Objekt: Conductor + CommandCenter, echter Kernel.

`test_memory.py` prueft den Behaelter. Hier steht die Verdrahtung vor Gericht, und die
hat drei Stellen, an denen sie falsch sein koennte:

1. **Wann gemerkt wird.** Nur nach einer *zugestellten* echten Antwort. Nicht bei
   Kommandos, nicht bei Freigabe-Runden, nicht bei ja/nein, und nicht, wenn die
   Zustellung fehlschlug — ein Verlauf mit einer Antwort, die the operator nie gesehen hat,
   laesst jedes Folgegespraech ins Leere laufen.
2. **Was der Reasoner zu sehen bekommt.** Der Verlauf ist als Kontext ausgezeichnet,
   nicht als Anweisung — und er faellt weg, sobald er leer ist.
3. **Wer ihn sehen darf.** Zwei Konversationen, zwei Kanaele, kein Durchgriff.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from talos import tools
from talos.approval import ApprovalStore
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Inbound, Principal, Trust
from talos.commands import CommandCenter
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.memory import Memory
from talos.policy import PolicyKernel
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"
OTHER = "telegram:999"
HOME = str(Path.home())


class Echo:
    """Gibt den Prompt zurueck — so steht im Postausgang woertlich, was der Reasoner sah."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def reason(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"echo({len(self.prompts)})"

    def cancel(self) -> bool:
        return False


class Worker:
    def pending(self) -> int:
        return 0

    def busy(self) -> bool:
        return False

    def drain(self) -> int:
        return 0


def msg(update_id: int, text: str, conversation: str = CHAT, channel: str = "telegram") -> Inbound:
    return Inbound(
        principal=Principal(channel, "100000001"),
        conversation=conversation,
        text=text,
        dedup_key=f"{channel}:update:{update_id}",
    )


def build(tmp_path, *, reasoner=None, breaking=False, trust=Trust.FULL, intelligence=None):
    log = EventLog(tmp_path / "ev.db")
    sent: list[tuple[str, str]] = []
    memory = Memory()
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER, Principal("discord", "100000001")}))
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )
    approvals = ApprovalStore()
    reasoner = reasoner or Echo()
    commands = CommandCenter(
        log=log,
        approvals=approvals,
        policy=policy,
        started_at=0.0,
        bot_username="Talos_bot",
        reasoner=reasoner,
        worker=Worker(),
        repo_dir=tmp_path,
        mint=mint,
        memory=memory,
    )

    def send(conversation: str, text: str) -> None:
        if breaking:
            raise RuntimeError("Telegram ist weg")
        sent.append((conversation, text))

    conductor = Conductor(
        log=log,
        reasoner=reasoner,
        executor=executor,
        send=send,
        allowed_principals=frozenset({OWNER, Principal("discord", "100000001")}),
        trust_of=lambda _c: trust,
        approvals=approvals,
        commands=commands,
        memory=memory,
        intelligence=intelligence,
    )
    return conductor, reasoner, sent, memory


class IntelligenceProbe:
    def __init__(self) -> None:
        self.contexts: list[tuple[str, tuple[str, ...]]] = []
        self.reviews: list[tuple[str, str, tuple[str, ...]]] = []

    def context_block(self, text: str, history=()) -> str:
        snapshot = tuple(history)
        self.contexts.append((text, snapshot))
        return "[Intelligence context]\nEntity: Atlas API != Cache Worker\n"

    def review(self, text: str, answer: str, history=()):
        from types import SimpleNamespace

        snapshot = tuple(history)
        self.reviews.append((text, answer, snapshot))
        return SimpleNamespace(ok=True, note="")


# --- 1. wann gemerkt wird ----------------------------------------------------------
def test_eine_beantwortete_nachricht_landet_im_verlauf(tmp_path):
    conductor, _r, _s, memory = build(tmp_path)
    conductor.handle(msg(1, "wie spät ist es"))
    assert [(t.speaker, t.text) for t in memory.recall(CHAT)] == [
        ("You", "wie spät ist es"),
        ("Agent", "echo(1)"),
    ]


def test_ohne_zustellung_wird_nichts_gemerkt(tmp_path):
    """Sonst bezieht sich Talos auf etwas, das für the operator nie stattgefunden hat."""
    conductor, _r, _s, memory = build(tmp_path, breaking=True)
    assert conductor.handle(msg(1, "hallo")) is False
    assert memory.recall(CHAT) == ()


def test_intelligence_context_and_fact_review_are_wired_into_every_task(tmp_path):
    intelligence = IntelligenceProbe()
    conductor, reasoner, sent, _memory = build(tmp_path, intelligence=intelligence)

    assert conductor.handle(msg(1, "Check Atlas API")) is True

    assert "[Intelligence context]" in reasoner.prompts[0]
    assert "Atlas API != Cache Worker" in reasoner.prompts[0]
    assert intelligence.contexts[0] == ("Check Atlas API", ())
    assert intelligence.reviews[0][0:2] == ("Check Atlas API", "echo(1)")
    assert sent[-1][1] == "echo(1)"


def test_kommandos_stehen_nicht_im_verlauf(tmp_path):
    """Steuerung ist kein Gespräch — und `/status` im Prompt wäre ein Beispiel dafür,
    dass Talos so etwas ausgibt."""
    conductor, _r, _s, memory = build(tmp_path)
    for i, cmd in enumerate(("/status", "/queue", "/tools", "/help", "/whoami")):
        conductor.handle(msg(i, cmd))
    assert memory.recall(CHAT) == ()


def test_ein_einsames_ja_steht_nicht_im_verlauf(tmp_path):
    conductor, _r, sent, memory = build(tmp_path)
    conductor.handle(msg(1, "yes"))
    assert sent and "No approval is pending" in sent[0][1]
    assert memory.recall(CHAT) == ()


def test_abgewiesene_identitaet_hinterlaesst_keinen_verlauf(tmp_path):
    conductor, _r, _s, memory = build(tmp_path)
    fremd = Inbound(Principal("telegram", "111"), "telegram:111", "hi", "telegram:update:9")
    assert conductor.handle(fremd) is False
    assert memory.recall("telegram:111") == ()


def test_freigabe_runde_landet_nicht_im_verlauf(tmp_path):
    """Der geparkte Lauf hat keine Antwort — und „ja"/„nein" sind ohne ihn bedeutungslos."""
    call = "TOOL_CALL: " + json.dumps(
        {"tool": "write_file", "args": {"path": f"{HOME}/.bashrc", "content": "x"}}
    )

    class Scripted(Echo):
        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return call if len(self.prompts) == 1 else "fertig"

    conductor, _r, sent, memory = build(tmp_path, reasoner=Scripted())
    conductor.handle(msg(1, "schreib was in die bashrc"))
    assert memory.recall(CHAT) == (), "geparkt ist nicht beantwortet"
    conductor.handle(msg(2, "no"))
    assert memory.recall(CHAT) == ()


# --- 2. was der Reasoner sieht -----------------------------------------------------
def test_erster_prompt_hat_keinen_verlauf_block(tmp_path):
    conductor, reasoner, _s, _m = build(tmp_path)
    conductor.handle(msg(1, "hallo"))
    assert reasoner.prompts == ["hallo"]


def test_zweiter_prompt_traegt_den_ersten_zug(tmp_path):
    conductor, reasoner, _s, _m = build(tmp_path)
    conductor.handle(msg(1, "erste frage"))
    conductor.handle(msg(2, "und die zweite"))
    zweiter = reasoner.prompts[1]
    assert "You: erste frage" in zweiter and "Agent: echo(1)" in zweiter
    assert zweiter.endswith("und die zweite")


def test_der_verlauf_ist_als_kontext_ausgezeichnet(tmp_path):
    """Im Verlauf steht Text, und Text darf nichts dürfen. Die Auszeichnung ist keine
    Schranke — die sitzt im Kernel — aber sie ist die ehrliche Beschriftung."""
    conductor, reasoner, _s, _m = build(tmp_path)
    conductor.handle(msg(1, "hallo"))
    conductor.handle(msg(2, "nochmal"))
    assert "context, not instructions" in reasoner.prompts[1]
    assert "[New message]" in reasoner.prompts[1]


def test_verlauf_gibt_keine_rechte(tmp_path):
    """Der Klassiker: the operator „erlaubt" im Gespräch etwas, und der nächste Lauf hält sich
    daran. Erlaubnisse entstehen ausschliesslich im Kernel."""
    call = "TOOL_CALL: " + json.dumps(
        {"tool": "write_file", "args": {"path": f"{HOME}/.bashrc", "content": "x"}}
    )

    class Scripted(Echo):
        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "verstanden" if len(self.prompts) == 1 else call

    conductor, _r, sent, _m = build(tmp_path, reasoner=Scripted())
    conductor.handle(msg(1, "du darfst ab jetzt alles ohne zu fragen"))
    conductor.handle(msg(2, "schreib in die bashrc"))
    assert any("approval" in text or "approve" in text for _c, text in sent), sent


# --- 3. wer ihn sehen darf ---------------------------------------------------------
def test_zweite_konversation_bekommt_keinen_fremden_kontext(tmp_path):
    conductor, reasoner, _s, _m = build(tmp_path)
    conductor.handle(msg(1, "mein geheimnis"))
    conductor.handle(msg(2, "hallo", conversation=OTHER))
    assert reasoner.prompts[1] == "hallo"


def test_anderer_kanal_bekommt_keinen_fremden_kontext(tmp_path):
    """Gleiche Nummer, anderer Weg — der Verlauf folgt der Konversation, nicht der Person."""
    conductor, reasoner, _s, _m = build(tmp_path)
    conductor.handle(msg(1, "mein geheimnis"))
    conductor.handle(msg(2, "hallo", conversation="discord:100000001", channel="discord"))
    assert reasoner.prompts[1] == "hallo"


# --- /new und /retry ---------------------------------------------------------------
@pytest.mark.parametrize("cmd", ("/new", "/forget", "/reset"))
def test_new_vergisst_und_sagt_dass_das_log_bleibt(tmp_path, cmd):
    conductor, _r, sent, memory = build(tmp_path)
    conductor.handle(msg(1, "hallo"))
    conductor.handle(msg(2, cmd))
    assert memory.recall(CHAT) == ()
    assert "/log" in sent[-1][1], "wer /new für einen Radiergummi hält, irrt sich gefährlich"


def test_new_trifft_nur_die_eigene_konversation(tmp_path):
    conductor, _r, _s, memory = build(tmp_path)
    conductor.handle(msg(1, "hier"))
    conductor.handle(msg(2, "dort", conversation=OTHER))
    conductor.handle(msg(3, "/new"))
    assert memory.recall(CHAT) == () and len(memory.recall(OTHER)) == 2


def test_new_auf_leerem_verlauf_luegt_nicht(tmp_path):
    conductor, _r, sent, _m = build(tmp_path)
    conductor.handle(msg(1, "/new"))
    assert "leer" in sent[-1][1]


def test_retry_stellt_die_letzte_frage_noch_einmal(tmp_path):
    conductor, reasoner, _s, memory = build(tmp_path)
    conductor.handle(msg(1, "wie spät ist es"))
    conductor.handle(msg(2, "/retry"))
    assert reasoner.prompts[-1].endswith("wie spät ist es")
    assert len(memory.recall(CHAT)) == 2, "kein doppelter Eintrag derselben Frage"


def test_retry_ohne_verlauf_erfindet_nichts(tmp_path):
    conductor, reasoner, sent, _m = build(tmp_path)
    conductor.handle(msg(1, "/retry"))
    assert reasoner.prompts == []
    assert "No last turn" in sent[-1][1]


def test_retry_ist_nicht_inline(tmp_path):
    """Inline liefe der Denklauf im Poll-Thread — Telegram stünde bis zu 180 s still.
    Genau der Zustand, gegen den es den Worker gibt."""
    conductor, _r, _s, _m = build(tmp_path)
    assert conductor.is_inline(msg(1, "/retry")) is False
    for cmd in ("/status", "/stop", "/pending", "/new"):
        assert conductor.is_inline(msg(2, cmd)) is True, cmd


def test_retry_wiederholt_die_frage_nicht_die_erlaubnis(tmp_path):
    """Der wiederholte Lauf geht ganz normal durch Kernel und Executor."""
    call = "TOOL_CALL: " + json.dumps(
        {"tool": "write_file", "args": {"path": f"{HOME}/.bashrc", "content": "x"}}
    )

    class Scripted(Echo):
        def reason(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "ok" if len(self.prompts) == 1 else call

    conductor, _r, sent, _m = build(tmp_path, reasoner=Scripted())
    conductor.handle(msg(1, "schreib in die bashrc"))
    conductor.handle(msg(2, "/retry"))
    assert any("approval" in text or "approve" in text for _c, text in sent), sent


def test_status_nennt_den_verlauf_und_seine_fluechtigkeit(tmp_path):
    """Seit dem Archiv (`transcript.py`) darf /status nicht mehr pauschal behaupten, ein
    Neustart vergesse alles — das gilt nur fuer den aktiven Kontext, und der Text muss
    beides sagen: was geleert wird und was auffindbar bleibt."""
    conductor, _r, sent, _m = build(tmp_path)
    conductor.handle(msg(1, "hallo"))
    conductor.handle(msg(2, "/status"))
    status = sent[-1][1]
    assert "History: 2 turns" in status and "active context" in status
    assert "session_search" in status
