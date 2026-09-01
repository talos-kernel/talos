"""`delegate_steer` — eine Kurskorrektur erreicht einen LAUFENDEN Hintergrundauftrag.

Vorbild ist die getippte Korrektur (`redirect.py`): sie wird an der Schrittgrenze
eingelegt, ist als Zug ohne zusaetzliches Recht gerahmt, und der Kernel urteilt
danach ueber jeden Werkzeugwunsch wie vorher. Hier kommt die Korrektur nicht aus dem
Chat, sondern ausdruecklich adressiert ueber ein Werkzeug — und genau das entscheidet,
WAS steuerbar ist: nur Auftraege mit einer Schrittgrenze in diesem Prozess, also die
Laeufe aus `/background`. Alles andere (synchrone `delegate`-Untergebene, Worker-Jobs,
Zeitplaene) hat keinen Einspeisepunkt und bekommt eine ehrliche Absage statt einer
Schein-Steuerung.

Die zwei Haelften, die schiefgehen koennen, stehen zuerst: die Decke des Ziel-Laufs
darf durch eine Anweisung nie weiter werden, und ein Untergebener darf keinen anderen
Lauf lenken.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from talos import background as bg, tools
from talos.agent_loop import AgentStatus, ProgressStage, run_agent
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Inbound, Principal, Trust
from talos.conductor import AskContext, Conductor
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.manifest import Effect
from talos.policy import TARGET_EXTRACTORS, PolicyKernel, ToolRequest, Verdict
from talos.schedule import UnattendedCeiling
from talos.snapshot import Snapshotter
from talos.subagent import MAX_QUESTION_CHARS, ReadOnlyCeiling

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"
HOME = str(Path.home())


def _tool_call(tool: str, args: dict) -> str:
    return "TOOL_CALL: " + json.dumps({"tool": tool, "args": args})


def _desk_with_task(prompt: str = "durchsuch die protokolle") -> tuple[bg.BackgroundDesk, bg.Task]:
    desk = bg.BackgroundDesk()
    task = desk.accept(prompt, run_id="r1234567890123", principal=str(OWNER), conversation=CHAT)
    assert task is not None
    return desk, task


def _steer(desk: bg.BackgroundDesk, task_id: str, text: str = "schau zuerst in /var/log") -> bg.Steer:
    return desk.steer(task_id, text, principal=str(OWNER), conversation=CHAT)


def _executor(tmp_path: Path, policy: object, runners: dict | None = None) -> Executor:
    mint = CapabilityMint(policy)
    return Executor(
        policy=policy,
        log=EventLog(tmp_path / "ev.db"),
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=runners if runners is not None else dict(tools.RUNNERS)),
        mint=mint,
    )


def _warten(bedingung, grenze: float = 5.0) -> bool:
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.02)
    return False


# --- Die Haelfte, die schiefgehen kann: die Decke des Ziel-Laufs ---------------------------
def test_a_steer_cannot_widen_the_ceiling_of_the_run_it_reaches(tmp_path: Path) -> None:
    """⚠️ Der wichtigste Test dieser Datei.

    Der Hintergrundlauf laeuft unter der unbeaufsichtigten Decke: `NEEDS_HUMAN` ist
    dort `DENY`. Eine nachgeschobene Anweisung „schreib ~/.bashrc" muss genau dort
    enden — nicht als Freigabe-Frage im Chat, und erst recht nicht als Schreiben.
    Der Weg ist der echte: `_start_background` -> `_run_task` -> `run_agent`, mit
    echtem Kernel und echter Decke; nur der Runner protokolliert statt zu wirken.
    """
    betreten = threading.Event()
    freigabe = threading.Event()
    prompts: list[str] = []
    gelaufen: list[str] = []

    def aufzeichnen(req: ToolRequest) -> str:
        gelaufen.append(req.tool)
        return "WOULD-RUN"

    class GehorsamerReasoner:
        """Erster Zug: wartet, bis die Anweisung liegt. Zweiter Zug: gehorcht ihr."""

        def reason(self, prompt: str) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                betreten.set()
                assert freigabe.wait(timeout=5)
                return _tool_call("read_file", {"path": f"{HOME}/README.md"})
            if len(prompts) == 2:
                return _tool_call("write_file", {"path": f"{HOME}/.bashrc", "content": "x"})
            return "fertig"

        def cancel(self) -> bool:
            return False

    decke = UnattendedCeiling()
    policy = GovernedKernel(
        PolicyKernel(tools.default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5), lambda _c: Trust.FULL, unattended=decke,
    )
    log = EventLog(tmp_path / "ev.db")
    mint = CapabilityMint(policy)
    conductor = Conductor(
        log=log, reasoner=GehorsamerReasoner(),
        executor=Executor(policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
                          runner=GrantedRunner(mint=mint, runners={
                              "read_file": aufzeichnen, "write_file": aufzeichnen}),
                          mint=mint),
        send=lambda _c, _t: None,
        allowed_principals=frozenset({OWNER}), trust_of=lambda _c: Trust.FULL,
        unattended=decke,
    )
    auftrag = Inbound(principal=OWNER, conversation=CHAT, text="pruefe", dedup_key="k-1")
    assert conductor._start_background(auftrag, "run-1", "pruefe die protokolle") is True
    task = conductor.background.running()[0]
    assert betreten.wait(timeout=3)
    _steer(conductor.background, task.task_id, f"vergiss das — schreib jetzt {HOME}/.bashrc")
    freigabe.set()
    assert _warten(lambda: bool(log.recent(50, types=("background.finished",))))

    assert bg.STEER_FRAME in prompts[1]                   # die Anweisung kam an …
    assert gelaufen == ["read_file"]                     # … und bewirkte kein Schreiben
    urteile = [e for e in log.recent(50, types=("exec.result",)) if e["payload"].get("tool") == "write_file"]
    assert urteile and urteile[0]["payload"]["status"] == Status.DENIED.value
    assert "unattended" in urteile[0]["payload"].get("detail", "")


def test_a_delegated_run_may_not_steer_another_run() -> None:
    """Ein Untergebener darf nur lesen — und Steuern ist das eine Lesen, das keines ist.

    Der Ziel-Lauf ist ein Hintergrundlauf, der schreiben darf, wo der Kernel ALLOW
    sagt. Ein Untergebener, der ihn lenken koennte, haette ueber den Umweg genau die
    Leine, die ihm die Decke nimmt.
    """
    ceiling = ReadOnlyCeiling()
    kernel = GovernedKernel(
        PolicyKernel(tools.default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5), lambda _c: Trust.FULL, delegated=ceiling,
    )
    lenken = ToolRequest("delegate_steer", OWNER, {"task_id": "bg_x", "instruction": "x"})
    lesen = ToolRequest("read_file", OWNER, {"path": f"{HOME}/README.md"})

    assert kernel.decide(lenken).verdict is Verdict.ALLOW          # der Hauptlauf darf
    with ceiling.active():
        entschieden = kernel.decide(lenken)
        assert kernel.decide(lesen).verdict is Verdict.ALLOW       # Kontrolle: lesen bleibt
    assert entschieden.verdict is Verdict.DENY
    assert "delegated" in entschieden.reason and "steering" in entschieden.reason
    assert kernel.decide(lenken).verdict is Verdict.ALLOW          # die Decke endet mit dem Lauf


def test_an_unattended_run_may_steer_a_sibling_under_the_same_ceiling() -> None:
    """Ein Hintergrundlauf, der einen anderen lenkt, verlaengert keine Leine: beide
    stehen unter derselben Decke, und die Herkunftspruefung (gleiche Person, gleiche
    Unterhaltung) gilt dort wie ueberall. Bewusst festgehalten, damit die Entscheidung
    nicht stillschweigend kippt."""
    decke = UnattendedCeiling()
    kernel = GovernedKernel(
        PolicyKernel(tools.default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5), lambda _c: Trust.FULL, unattended=decke,
    )
    lenken = ToolRequest("delegate_steer", OWNER, {"task_id": "bg_x", "instruction": "x"})
    with decke.active():
        assert kernel.decide(lenken).verdict is Verdict.ALLOW


# --- Manifest, Extractor, Angebot -------------------------------------------------------
def test_steering_is_a_read_without_a_filesystem_target() -> None:
    """READ wie `delegate_status`: der Aufruf bewegt nach aussen nichts — die Decke des
    Ziel-Laufs bleibt, jeder Werkzeugwunsch danach passiert denselben Kernel. Die
    `task_id` ist KEIN Pfad: ein Scheinziel im Dateisystem-Floor waere schlechter als
    keins."""
    spec = tools.default_manifest().get("delegate_steer")
    assert spec is not None and spec.effect is Effect.READ and spec.reversible
    assert "delegate_steer" in TARGET_EXTRACTORS
    assert TARGET_EXTRACTORS["delegate_steer"]({"task_id": "bg_abc", "instruction": "x"}) == ()
    assert TARGET_EXTRACTORS["delegate_steer"]({"task_id": "/etc/passwd", "instruction": "x"}) == ()

    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    assert kernel.decide(ToolRequest("delegate_steer", OWNER, {"task_id": "bg_abc", "instruction": "x"})).verdict is Verdict.ALLOW


def test_the_tool_is_offered_to_the_model_and_drawn_in_the_display() -> None:
    """Die Verdrahtung Manifest -> Runner in `run()` prueft `test_media` (Falle 7)."""
    from talos import reasoner
    from talos.ux import EXPRESSIVE

    zeilen = [z for z in reasoner.TOOL_PROTOCOL.splitlines() if z.startswith("- delegate_steer ")]
    assert zeilen and "task_id" in zeilen[0] and "instruction" in zeilen[0]
    assert "delegate_steer" in EXPRESSIVE.tool_glyphs and "delegate_steer" in EXPRESSIVE.tool_verbs


def test_the_cap_matches_the_delegate_question_cap() -> None:
    """Eine Anweisung ist so lang wie eine delegierte Frage — zwei Zahlen, die
    auseinanderdriften, waeren zwei Wahrheiten."""
    assert bg.MAX_STEER_CHARS == MAX_QUESTION_CHARS


# --- Das Postfach ------------------------------------------------------------------------
def test_an_instruction_is_queued_and_taken_exactly_once() -> None:
    desk, task = _desk_with_task()
    steer = _steer(desk, task.task_id, "schau zuerst in /var/log")
    assert steer.text == "schau zuerst in /var/log" and steer.origin == str(OWNER)
    (genommen,) = desk.take_steering(task.task_id)
    assert genommen == steer
    assert desk.take_steering(task.task_id) == ()


def test_the_turn_is_framed_as_a_relayed_correction_without_rights() -> None:
    """Der Wortlaut in der Historie sagt, woher er kommt und was er nicht ist — dass er
    nichts erlaubt, entscheidet ohnehin der Kernel, aber der Rahmen soll es nicht
    einmal behaupten koennen."""
    zug = bg.Steer("mach X", origin=str(OWNER)).as_turn()
    assert zug.startswith(bg.STEER_FRAME)
    assert "no additional rights" in zug and "delegate_steer" in zug
    assert zug.endswith("\nmach X")


def test_an_unknown_or_finished_task_is_an_honest_refusal() -> None:
    desk, task = _desk_with_task()
    with pytest.raises(bg.SteerRefused, match="bg_gibtsnicht"):
        _steer(desk, "bg_gibtsnicht")
    desk.finish(task.task_id)
    with pytest.raises(bg.SteerRefused, match=task.task_id):
        _steer(desk, task.task_id)


def test_a_stopped_task_takes_no_more_instructions_and_drops_pending_ones() -> None:
    """Nach `/stopall` beginnt der Lauf keinen Schritt mehr — eine Anweisung, die dort
    noch laege, waere ein Versprechen ohne Empfaenger."""
    desk, task = _desk_with_task()
    _steer(desk, task.task_id, "noch etwas")
    assert desk.cancel(task.task_id) is True
    assert desk.take_steering(task.task_id) == ()
    with pytest.raises(bg.SteerRefused, match="stopped"):
        _steer(desk, task.task_id)

    desk2, task2 = _desk_with_task()
    _steer(desk2, task2.task_id, "noch etwas")
    desk2.cancel_all()
    assert desk2.take_steering(task2.task_id) == ()


def test_the_mailbox_is_capped_and_frees_up_after_a_step() -> None:
    """Wer viermal nachschiebt, ohne dass ein Schritt vergeht, meint einen neuen
    Auftrag. Der Rest wird abgelehnt und der Sprecher erfaehrt es."""
    desk, task = _desk_with_task()
    for i in range(bg.MAX_PENDING_STEERS):
        _steer(desk, task.task_id, f"anweisung {i}")
    with pytest.raises(bg.SteerRefused, match="pending"):
        _steer(desk, task.task_id, "eine zu viel")
    assert len(desk.take_steering(task.task_id)) == bg.MAX_PENDING_STEERS
    _steer(desk, task.task_id, "geht wieder")


def test_only_the_same_person_in_the_same_conversation_may_steer() -> None:
    """Beides, nicht eines — dieselbe Schranke wie im Vordergrund-Postfach: ein zweiter
    erlaubter Mensch lenkt den Lauf eines anderen nicht, und dieselbe Person in einem
    anderen Chat redet ueber etwas anderes."""
    desk, task = _desk_with_task()
    with pytest.raises(bg.SteerRefused, match="conversation"):
        desk.steer(task.task_id, "x", principal="telegram:100000002", conversation=CHAT)
    with pytest.raises(bg.SteerRefused, match="conversation"):
        desk.steer(task.task_id, "x", principal=str(OWNER), conversation="chat-anders")
    with pytest.raises(bg.SteerRefused):
        desk.steer(task.task_id, "x", principal="", conversation="")
    assert desk.take_steering(task.task_id) == ()

    # Fail-closed: ein Auftrag OHNE aufgezeichnete Herkunft ist von niemandem lenkbar.
    ohne = bg.BackgroundDesk()
    fremd = ohne.accept("etwas", run_id="r9999999999999")
    with pytest.raises(bg.SteerRefused):
        ohne.steer(fremd.task_id, "x", principal=str(OWNER), conversation=CHAT)


def test_empty_or_oversized_instructions_are_refused_not_trimmed() -> None:
    """Eine gekuerzte Anweisung kann das Gegenteil meinen — lieber absagen und den
    Sprecher kuerzen lassen."""
    desk, task = _desk_with_task()
    with pytest.raises(bg.SteerRefused):
        _steer(desk, task.task_id, "   ")
    with pytest.raises(bg.SteerRefused, match=str(bg.MAX_STEER_CHARS)):
        _steer(desk, task.task_id, "x" * (bg.MAX_STEER_CHARS + 1))
    assert desk.take_steering(task.task_id) == ()


# --- Der Runner ----------------------------------------------------------------------------
def test_the_runner_takes_the_origin_from_the_thread_context_never_from_the_arguments() -> None:
    """Das Modell entscheidet nicht, ALS WER es lenkt: Person und Unterhaltung kommen
    aus dem Kontext des ausfuehrenden Threads (`ask_operator`-Bauart)."""
    desk, task = _desk_with_task()
    runner = tools.make_delegate_steer_runner(desk, context=lambda: AskContext(OWNER, CHAT, Trust.FULL))
    quittung = runner(ToolRequest("delegate_steer", OWNER, {
        "task_id": task.task_id, "instruction": "nimm die zweite Datei",
        "principal": "telegram:666", "conversation": "chat-fremd",
    }))
    assert task.task_id in quittung and "next step" in quittung
    (steer,) = desk.take_steering(task.task_id)
    assert steer.origin == str(OWNER) and steer.text == "nimm die zweite Datei"

    ohne_kontext = tools.make_delegate_steer_runner(desk, context=lambda: None)
    with pytest.raises(ValueError, match="context"):
        ohne_kontext(ToolRequest("delegate_steer", OWNER, {"task_id": task.task_id, "instruction": "x"}))
    assert desk.take_steering(task.task_id) == ()


def test_a_refusal_is_a_failed_tool_call_not_a_quiet_success(tmp_path: Path) -> None:
    """Durch den echten Executor: die Absage kommt als ERROR mit dem Grund zurueck und
    steht so auch im Protokoll — kein `done` fuer etwas, das nicht geschah."""
    desk, task = _desk_with_task()
    runner = tools.make_delegate_steer_runner(desk, context=lambda: AskContext(OWNER, CHAT, Trust.FULL))
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    executor = _executor(tmp_path, policy, runners={"delegate_steer": runner})

    daneben = executor.run(ToolRequest("delegate_steer", OWNER, {"task_id": "bg_nope", "instruction": "x"}), "run-a")
    assert daneben.status is Status.ERROR and "bg_nope" in daneben.detail

    fehlt = executor.run(ToolRequest("delegate_steer", OWNER, {"task_id": task.task_id}), "run-b")
    assert fehlt.status is Status.ERROR and "instruction" in fehlt.detail

    gut = executor.run(ToolRequest("delegate_steer", OWNER, {"task_id": task.task_id, "instruction": "weiter"}), "run-c")
    assert gut.status is Status.DONE and task.task_id in str(gut.result)


# --- Die Naht: run_agent liest das Postfach an der Schrittgrenze -------------------------
def test_run_agent_injects_a_steer_between_two_steps_on_the_redirect_seam(tmp_path: Path) -> None:
    """Dieselbe Naht wie die getippte Korrektur — es gibt bewusst keine zweite."""
    desk, task = _desk_with_task()
    gesehen: list[list[str]] = []
    stationen: list[ProgressStage] = []

    def propose(history: list[str]) -> str:
        gesehen.append(list(history))
        if len(gesehen) == 1:
            _steer(desk, task.task_id, "nimm die zweite Datei")
            return _tool_call("read_file", {"path": str(tmp_path / "a")})
        return "fertig"

    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    ergebnis = run_agent(
        propose, _executor(tmp_path, policy), OWNER, "run-naht",
        redirect=desk.inbox(task.task_id), max_steps=4,
        progress=lambda p: stationen.append(p.stage),
    )
    assert ergebnis.status is AgentStatus.ANSWERED
    assert not any(bg.STEER_FRAME in z for z in gesehen[0])
    zweiter = "\n".join(gesehen[1])
    assert bg.STEER_FRAME in zweiter and "nimm die zweite Datei" in zweiter
    assert ProgressStage.REDIRECTED in stationen


def test_a_steer_reaches_the_real_background_run_and_leaves_a_trace(tmp_path: Path) -> None:
    """Vom Werkzeug bis in die Historie des Hintergrundlaufs — der ganze Weg, mit dem
    Beleg im Event-Log (`background.steered`) unter dem run_id des Ziel-Laufs."""
    betreten = threading.Event()
    freigabe = threading.Event()
    prompts: list[str] = []
    gesendet: list[tuple[str, str]] = []

    class ZweiSchrittReasoner:
        def reason(self, prompt: str) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                betreten.set()
                assert freigabe.wait(timeout=5)
                return _tool_call("read_file", {"path": __file__})
            return "drei Fehler gefunden"

        def cancel(self) -> bool:
            return False

    class _Decke:
        def active(self):
            class _Ctx:
                def __enter__(self):
                    return None

                def __exit__(self, *_):
                    return False
            return _Ctx()

    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    conductor = Conductor(
        log=log, reasoner=ZweiSchrittReasoner(),
        executor=Executor(policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
                          runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)), mint=mint),
        send=lambda c, t: gesendet.append((c, t)),
        allowed_principals=frozenset({OWNER}), trust_of=lambda _c: Trust.FULL,
        unattended=_Decke(),
    )
    auftrag = Inbound(principal=OWNER, conversation=CHAT, text="zaehle", dedup_key="k-1")
    assert conductor._start_background(auftrag, "run-1", "zaehle die dateien") is True
    task = conductor.background.running()[0]
    assert task.principal == str(OWNER) and task.conversation == CHAT
    assert betreten.wait(timeout=3)

    # Das Werkzeug, wie es im Vordergrund gerufen wird: der Kontext ist der des Chats.
    runner = tools.make_delegate_steer_runner(
        conductor.background, context=lambda: AskContext(OWNER, CHAT, Trust.FULL))
    runner(ToolRequest("delegate_steer", OWNER, {"task_id": task.task_id, "instruction": "nur die Fehler zaehlen"}))
    freigabe.set()
    assert _warten(lambda: bool(log.recent(50, types=("background.finished",))))

    assert bg.STEER_FRAME not in prompts[0]
    assert bg.STEER_FRAME in prompts[1] and "nur die Fehler zaehlen" in prompts[1]
    (spur,) = log.recent(50, types=("background.steered",))
    assert spur["payload"]["task_id"] == task.task_id
    assert spur["payload"]["origin"] == str(OWNER)
    assert spur["payload"]["text"] == "nur die Fehler zaehlen"
    assert spur["run_id"] != "run-1"                      # der run_id des ZIEL-Laufs
    assert spur["run_id"] == log.recent(50, types=("background.finished",))[0]["run_id"]
    assert any("drei Fehler gefunden" in t for _c, t in gesendet)
    assert conductor.background.busy() == 0
    with pytest.raises(bg.SteerRefused):                  # danach: ehrlich vorbei
        _steer(conductor.background, task.task_id)
