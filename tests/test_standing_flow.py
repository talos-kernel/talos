"""Stehende Freigaben im Zusammenspiel: Conductor-Runde, /allowed, /revoke, Regler.

Der Kern liegt in `test_standing.py` (Fingerabdruck, Store, Restore). Hier steht die
Frage, auf die es sicherheitsseitig ankommt: wirkt „immer" wirklich nur da, wo the operator sonst
„ja" tippen würde — und nirgends sonst?
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import standing, tools
from talos.approval import ApprovalStore, is_always
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Inbound, Principal, Trust
from talos.commands import CommandCenter
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PolicyKernel
from talos.snapshot import Snapshotter
from talos.standing import StandingStore

_NEXT_UPDATE = iter(range(1, 10_000))

OWNER = Principal("telegram", "100000001")
ZWEITER = Principal("telegram", "222222")
CHAT = "telegram:100000001"


def msg(update_id: int, text: str, *, principal: Principal = OWNER, conversation: str = CHAT) -> Inbound:
    return Inbound(
        principal=principal,
        conversation=conversation,
        text=text,
        dedup_key=f"telegram:update:{update_id}",
    )


class Scripted:
    """Antwortet der Reihe nach; wiederholt die letzte Zeile, wenn das Skript ausgeht."""

    def __init__(self, *lines: str) -> None:
        self._lines = list(lines)
        self.calls = 0

    def reason(self, prompt: str) -> str:
        self.calls += 1
        index = min(self.calls - 1, len(self._lines) - 1)
        return self._lines[index]

    def cancel(self) -> bool:
        return False


def call(tool: str, args: dict, targets: list[str] | None = None) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args, "targets": targets or []})


def full_trust(_channel: str) -> Trust:
    return Trust.FULL


class Rig:
    """Conductor + CommandCenter auf einem Log — so wie `__main__` sie verdrahtet."""

    # Stufe 3 als Vorgabe: dort fragt jede Wirkung. Auf 5 wuerde `write_file` nach
    # tmp_path glatt durchlaufen — dann gaebe es gar keine Freigabe-Runde zu testen.
    def __init__(self, tmp_path: Path, reasoner, *, level: int = 3, store: StandingStore | None = None,
                 log: EventLog | None = None, trust_of=full_trust) -> None:
        self.log = log if log is not None else EventLog(tmp_path / "ev.db")
        self.sent: list[tuple[str, str]] = []
        self.governor = AutonomyGovernor(level)
        kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER, ZWEITER}))
        self.policy = GovernedKernel(kernel, self.governor, full_trust)
        mint = CapabilityMint(self.policy, governor=self.governor)
        self.executor = Executor(
            policy=self.policy,
            log=self.log,
            snapshotter=Snapshotter(tmp_path / ".snap"),
            runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
            mint=mint,
        )
        self.approvals = ApprovalStore()
        self.standing = store if store is not None else StandingStore(self.log)
        self.commands = CommandCenter(
            log=self.log,
            approvals=self.approvals,
            policy=kernel,
            started_at=time.time(),
            bot_username="talos_bot",
            reasoner=reasoner,
            worker=_NullWorker(),
            repo_dir=tmp_path,
            governor=self.governor,
            standing=self.standing,
        )
        self.conductor = Conductor(
            log=self.log,
            reasoner=reasoner,
            executor=self.executor,
            send=lambda conversation, text: self.sent.append((conversation, text)),
            allowed_principals=frozenset({OWNER, ZWEITER}),
            trust_of=trust_of,
            approvals=self.approvals,
            standing=self.standing,
            commands=self.commands,
        )
    def say(self, text: str, *, principal: Principal = OWNER, conversation: str = CHAT) -> str:
        """Die Antwort auf DIESEN Auftrag — nicht einfach die zuletzt gesendete Nachricht.

        Der Unterschied entstand mit dem Selbstreview: der wird nach einem Lauf faellig
        und geht als EIGENE Nachricht hinterher. `sent[-1]` lieferte dann den Bericht
        statt der Antwort, und der Test behauptete einen Fehler, den es nicht gab.
        """
        vorher = len(self.sent)
        self.conductor.handle(msg(next(_NEXT_UPDATE), text, principal=principal, conversation=conversation))
        return self.sent[vorher][1] if len(self.sent) > vorher else ""

    def types(self) -> list[str]:
        return [row["type"] for row in self.log.recent(200, ())]


class _NullWorker:
    def pending(self) -> int:
        return 0

    def busy(self) -> bool:
        return False

    def drain(self) -> int:
        return 0


def write_to(path: Path, text: str = "hallo") -> str:
    return call("write_file", {"path": str(path), "content": text})


# --- Der Freigabe-Text nennt jetzt drei Wege ------------------------------------
def test_prompt_names_all_three_answers(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    prompt = rig.say("schreib was")
    assert "yes" in prompt and "always" in prompt and "no (discard)" in prompt
    assert "Tool: write_file" in prompt  # weiter kernel-abgeleitet, kein LLM-Text


def test_reprompt_names_all_three_answers(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    assert "Bitte nur ja, immer oder nein." in rig.say("vielleicht")


def test_immer_is_inline_and_never_reaches_the_reasoner(tmp_path):
    reasoner = Scripted("Fertig.")
    rig = Rig(tmp_path, reasoner)
    assert is_always("always")
    assert rig.conductor.is_inline(msg(99, "always"))
    reply = rig.say("always")
    assert reasoner.calls == 0
    assert "no approval is pending" in reply.lower()


# --- „immer": einmal ausführen UND merken ---------------------------------------
def test_immer_executes_once_and_stores_the_rule(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    reply = rig.say("always")
    assert target.read_text() == "hallo"
    assert "Standing approval created" in reply
    assert "rc=0" not in reply
    assert "run_shell" not in reply
    assert rig.approvals.get(CHAT) is None
    assert "approval.standing" in rig.types()


def test_second_identical_request_runs_without_asking(tmp_path):
    target = tmp_path / "a.txt"
    # Derselbe Tool-Call in beiden Runden — „immer" selbst fragt den Reasoner nie.
    rig = Rig(tmp_path, Scripted(write_to(target)))
    rig.say("schreib was")
    rig.say("always")
    target.unlink()

    reply = rig.say("nochmal dasselbe")
    assert target.exists()                      # gelaufen
    assert rig.approvals.get(CHAT) is None      # nichts geparkt
    assert "Standing approval used" in reply
    assert "rc=0" not in reply
    assert "run_shell" not in reply
    assert "approval.standing_used" in rig.types()


def test_standing_rule_does_not_unlock_a_different_target(tmp_path):
    erlaubt, fremd = tmp_path / "a.txt", tmp_path / "b.txt"
    rig = Rig(tmp_path, Scripted(write_to(erlaubt), "Fertig."))
    rig.say("schreib a")
    rig.say("always")

    rig2 = Rig(tmp_path, Scripted(write_to(fremd), "Fertig."), store=rig.standing, log=rig.log)
    rig2.say("schreib b")
    assert not fremd.exists()
    assert rig2.approvals.get(CHAT) is not None  # es wird gefragt


def test_standing_shell_rule_is_the_exact_command(tmp_path):
    rig = Rig(tmp_path, Scripted(call("run_shell", {"command": "date"}), "Fertig."))
    rig.say("wie spaet")
    rig.say("always")

    rig2 = Rig(tmp_path, Scripted(call("run_shell", {"command": "date; rm -rf /tmp/x"}), "Fertig."),
               store=rig.standing, log=rig.log)
    rig2.say("nochmal")
    parked = rig2.approvals.get(CHAT)
    assert parked is not None and parked.req.args["command"] == "date; rm -rf /tmp/x"


def test_rule_belongs_to_the_person_who_said_immer(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")
    target.unlink()

    rig2 = Rig(tmp_path, Scripted(write_to(target), "Fertig."), store=rig.standing, log=rig.log)
    rig2.say("schreib was", principal=ZWEITER)
    assert not target.exists()
    assert rig2.approvals.get(CHAT) is not None


def test_rule_belongs_to_the_chat_it_was_given_in(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")
    target.unlink()

    rig2 = Rig(tmp_path, Scripted(write_to(target), "Fertig."), store=rig.standing, log=rig.log)
    rig2.say("schreib was", conversation="telegram:999")
    assert not target.exists()
    assert rig2.approvals.get("telegram:999") is not None


# --- Der Regler steht davor ------------------------------------------------------
def test_standing_rule_has_no_effect_below_level_three(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")
    target.unlink()

    zu = Rig(tmp_path, Scripted(write_to(target), "Fertig."), store=rig.standing, log=rig.log, level=1)
    zu.say("schreib was")
    assert not target.exists()
    assert zu.approvals.get(CHAT) is None  # nicht mal geparkt — der Regler sagte DENY


def test_immer_after_the_dial_was_closed_stores_nothing(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.governor.set_level("1", principal=OWNER, allowed_identities=frozenset({OWNER}))
    reply = rig.say("always")
    assert not target.exists()
    assert "Keine stehende Freigabe angelegt" in reply
    assert rig.standing.list(CHAT, principal=OWNER) == ()


# --- /allowed und /revoke --------------------------------------------------------
def test_allowed_lists_the_rule_and_warns_about_content(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")

    reply = rig.say("/allowed")
    assert "1. write_file" in reply and str(target) in reply
    assert "content" in reply  # sagt, dass der Inhalt nicht gebunden ist


def test_allowed_is_empty_for_a_foreign_chat(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")
    assert "No standing approvals" in rig.say("/allowed", conversation="telegram:999")


def test_revoke_makes_talos_ask_again(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")
    target.unlink()

    assert "Revoked" in rig.say("/revoke 1")
    assert "approval.standing_revoked" in rig.types()

    rig2 = Rig(tmp_path, Scripted(write_to(target), "Fertig."), store=rig.standing, log=rig.log)
    rig2.say("schreib was")
    assert not target.exists()
    assert rig2.approvals.get(CHAT) is not None


def test_revoke_without_a_number_is_refused(tmp_path):
    rig = Rig(tmp_path, Scripted("Fertig."))
    assert "braucht eine Nummer" in rig.say("/revoke")
    assert "braucht eine Nummer" in rig.say("/revoke abc")


def test_revoke_out_of_range_says_so(tmp_path):
    rig = Rig(tmp_path, Scripted("Fertig."))
    assert "No standing approval number 3" in rig.say("/revoke 3")


def test_help_mentions_the_new_commands(tmp_path):
    rig = Rig(tmp_path, Scripted("Fertig."))
    reply = rig.say("/help")
    assert "/allowed" in reply and "/revoke" in reply


# --- Über den Neustart hinweg ----------------------------------------------------
def test_rule_survives_a_restart_through_the_log(tmp_path):
    target = tmp_path / "a.txt"
    rig = Rig(tmp_path, Scripted(write_to(target), "Fertig."))
    rig.say("schreib was")
    rig.say("always")
    target.unlink()

    neu = Rig(tmp_path, Scripted(write_to(target), "Fertig."),
              store=standing.restore(rig.log), log=rig.log)
    neu.say("schreib was")
    assert target.exists()
    assert neu.approvals.get(CHAT) is None
