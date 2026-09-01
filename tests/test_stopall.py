"""/stopall (Not-Halt) und das erweiterte /status — Bilanz statt Behauptung.

Der Not-Halt ist der gefährlichste Knopf im Haus: drückt man ihn, will man SICHER
sein, was gestoppt ist und was nicht. Eine Antwort, die „alles gestoppt" sagt,
während ein Hintergrund-Job noch seinen Bericht in den Chat wirft, hätte genau das
Vertrauen verspielt, für das der Knopf da ist. Deshalb prüfen die Tests hier nicht
nur, dass gestoppt wird, sondern dass die Antwort pro Kategorie ehrlich bilanziert.

Zweite Haelfte: `/status` zeigt nur, was die jeweilige Quelle wirklich hergibt —
eine Grösse, die nicht gemessen wird, wird weggelassen statt erfunden.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from talos import background as bg, tools
from talos.approval import ApprovalStore
from talos.capability import CapabilityMint
from talos.channel import Inbound, Principal, Trust
from talos.commands import CommandCenter
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PolicyKernel, ToolRequest
from talos.schedule import ScheduleStore
from talos.snapshot import Snapshotter
from talos.usage import Run, UsageMeter

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"


class _Reasoner:
    def __init__(self, running: bool = False) -> None:
        self.running = running

    def cancel(self) -> bool:
        was, self.running = self.running, False
        return was


class _Worker:
    def __init__(self, busy: bool = False, waiting: int = 0) -> None:
        self._busy, self._waiting = busy, waiting

    def pending(self) -> int:
        return self._waiting

    def busy(self) -> bool:
        return self._busy

    def drain(self) -> int:
        dropped, self._waiting = self._waiting, 0
        return dropped


def _center(tmp_path: Path, **extras) -> CommandCenter:
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"),
        approvals=extras.pop("approvals", ApprovalStore()),
        policy=policy,
        started_at=time.time(),
        bot_username="Talos_bot",
        reasoner=extras.pop("reasoner", _Reasoner()),
        worker=extras.pop("worker", _Worker()),
        repo_dir=tmp_path,
        mint=CapabilityMint(policy),
        **extras,
    )


def _park(store: ApprovalStore, conversation: str = CHAT) -> None:
    store.park(
        conversation,
        ToolRequest("write_file", OWNER, {"path": str(Path.home() / "notiz.txt")}),
        "Schreiben nach ~/notiz.txt erlauben?",
    )


# --- /stopall: die ehrliche Bilanz -------------------------------------------------


def test_stopall_with_nothing_running_says_so_and_is_idempotent(tmp_path: Path) -> None:
    center = _center(tmp_path, background=bg.BackgroundDesk())
    erstes = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT)
    assert erstes.reply and "Nichts zu stoppen" in erstes.reply
    zweites = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT)
    assert zweites.reply and "Nichts zu stoppen" in zweites.reply


def test_stopall_alias_estop_does_the_same(tmp_path: Path) -> None:
    center = _center(tmp_path, reasoner=_Reasoner(running=True))
    ergebnis = center.dispatch("estop", "", principal=OWNER, conversation=CHAT)
    assert ergebnis.reply and "abgebrochen" in ergebnis.reply


def test_stopall_cancels_thought_and_drains_queue(tmp_path: Path) -> None:
    center = _center(tmp_path, reasoner=_Reasoner(running=True), worker=_Worker(busy=True, waiting=2))
    ergebnis = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT)
    text = ergebnis.reply or ""
    assert "abgebrochen" in text  # Denkzug
    assert "2" in text and "verworfen" in text  # Warteschlange


def test_stopall_discards_pending_approvals_and_never_approves(tmp_path: Path) -> None:
    """⚠️ Die Richtung ist der Punkt: ein Not-Halt, der freigäbe statt zu verwerfen,
    wäre der bequemste Weg, eine unbeantwortete Frage in eine Erlaubnis zu drehen."""
    store = ApprovalStore()
    _park(store)
    center = _center(tmp_path, approvals=store)
    ergebnis = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT)
    assert store.get(CHAT) is None  # verworfen, nicht genehmigt
    assert "1" in (ergebnis.reply or "") and "genehmigt" in (ergebnis.reply or "")


def test_stopall_detaches_background_jobs_and_says_what_that_means(tmp_path: Path) -> None:
    desk = bg.BackgroundDesk()
    desk.accept("durchsuch die protokolle", run_id="aaaaaaaaaaaaaa")
    center = _center(tmp_path, background=desk)
    ergebnis = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT)
    text = ergebnis.reply or ""
    assert "1 abgemeldet" in text
    # Ehrlichkeit heisst: der Text sagt dazu, was „abgemeldet" konkret bedeutet.
    assert "Denkschritt" in text and "Bericht" in text
    assert desk.busy() == 0  # der Platz ist sofort frei


def test_stopall_states_plainly_that_schedules_keep_firing(tmp_path: Path) -> None:
    """Ein Not-Halt, der stille Termine mitlöschte, schüfe den nächsten Vorfall
    („warum kommt mein Morgenbericht nicht mehr?"), während er den ersten beendet."""
    center = _center(tmp_path, reasoner=_Reasoner(running=True))
    text = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Zeitpläne" in text and "unschedule" in text



# --- ApprovalStore: die zwei neuen Auskünfte ----------------------------------------


def test_pending_count_skips_expired_entries() -> None:
    uhr = [1000.0]
    store = ApprovalStore(ttl_s=60, clock=lambda: uhr[0])
    _park(store, "chat-a")
    _park(store, "chat-b")
    assert store.pending_count() == 2
    uhr[0] += 120  # beide abgelaufen
    assert store.pending_count() == 0


def test_discard_all_empties_every_chat_and_reports_the_count() -> None:
    store = ApprovalStore()
    _park(store, "chat-a")
    _park(store, "chat-b")
    assert store.discard_all() == 2
    assert store.discard_all() == 0  # idempotent
    assert store.get("chat-a") is None and store.get("chat-b") is None


# --- BackgroundDesk: die Abmeldung ---------------------------------------------------


def test_cancel_unknown_or_finished_task_is_an_honest_no() -> None:
    desk = bg.BackgroundDesk()
    assert desk.cancel("bg_gibtsnicht") is False
    task = desk.accept("etwas", run_id="r1234567890123")
    desk.finish(task.task_id)
    assert desk.cancel(task.task_id) is False


def test_cancel_frees_the_slot_immediately() -> None:
    """Wer abgemeldet ist, hält keinen der drei Plätze mehr — sonst stünde der
    Betreiber nach einem Not-Halt vor „Too many background tasks", obwohl er
    gerade alles abgemeldet hat."""
    desk = bg.BackgroundDesk()
    # Bewusst unterschiedliche run_id-Praeﬁxe: `task_id` ist nur `bg_<12 Zeichen>`,
    # und zwei Auftraege mit demselben Praeﬁx waeren DERSELBE Auftrag.
    tasks = [desk.accept(f"auftrag {i}", run_id=rid)
             for i, rid in enumerate(("r1111111111111", "r2222222222222", "r3333333333333"))]
    assert desk.full()
    assert desk.cancel(tasks[0].task_id) is True
    assert not desk.full() and desk.busy() == 2
    assert desk.accept("neu", run_id="r9999999999999") is not None


def test_was_cancelled_survives_until_the_thread_finishes() -> None:
    """Der Worker-Thread fragt die Markierung an der Schrittgrenze und beim
    Bericht ab — `finish` (sein eigener Aufräumer) darf sie erst dann tilgen."""
    desk = bg.BackgroundDesk()
    task = desk.accept("etwas", run_id="r1234567890123")
    desk.cancel(task.task_id)
    assert desk.was_cancelled(task.task_id) is True
    desk.finish(task.task_id)
    assert desk.was_cancelled(task.task_id) is False


def test_cancel_all_returns_what_was_actually_running() -> None:
    desk = bg.BackgroundDesk()
    desk.accept("eins", run_id="r1111111111111")
    desk.accept("zwei", run_id="r2222222222222")
    tasks = desk.cancel_all()
    assert len(tasks) == 2 and desk.busy() == 0
    assert desk.cancel_all() == ()  # zweites Mal: ehrlich nichts


def test_stopall_without_wired_desk_reports_that_honestly(tmp_path: Path) -> None:
    center = _center(tmp_path, reasoner=_Reasoner(running=True))  # background=None
    text = center.dispatch("stopall", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Hintergrund" in text and "nicht verdrahtet" in text

# --- Der verworfene Bericht (Conductor, echter _run_task-Pfad) -----------------------


def _events(log: EventLog):
    return log.recent(50, types=("conductor.reply_discarded",))


class _Decke:
    def active(self):
        class _Ctx:
            def __enter__(self):
                return None

            def __exit__(self, *_):
                return False

        return _Ctx()


def test_a_cancelled_background_run_ends_quietly_without_a_report(tmp_path: Path) -> None:
    """⚠️ Der ganze Sinn der Abmeldung: der Bericht eines abgebrochenen Laufs darf
    der Unterhaltung nicht mehr quer kommen — sie ist laengst weiter. Gleichzeitig
    ist ein laufender Modellaufruf ein blockierender Subprozess und bleibt
    unantastbar: der aktuelle Denkschritt endet, der nächste beginnt nicht mehr.
    """
    import json

    betreten = threading.Event()
    freigabe = threading.Event()
    gesendet: list[tuple[str, str]] = []

    class ZweiSchrittReasoner:
        """Erster Zug: ein Werkzeugwunsch (nach dem Warten). Zweiter Zug: kaeme
        nie — die Schleife stoppt vorher an der Schrittgrenze."""

        def reason(self, prompt: str) -> str:
            betreten.set()
            assert freigabe.wait(timeout=5)
            return "TOOL_CALL: " + json.dumps(
                {"tool": "read_file", "args": {"path": __file__}, "targets": [__file__]}
            )

        def cancel(self) -> bool:
            return False

    from talos.capability import GrantedRunner

    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    conductor = Conductor(
        log=log,
        reasoner=ZweiSchrittReasoner(),
        executor=Executor(policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
                          runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
                          mint=mint),
        send=lambda c, t: gesendet.append((c, t)),
        allowed_principals=frozenset({OWNER}),
        trust_of=lambda _c: Trust.FULL,
        unattended=_Decke(),
    )
    auftrag = Inbound(principal=OWNER, conversation=CHAT, text="zaehle", dedup_key="k-1")
    assert conductor._start_background(auftrag, "run-1", "zaehle die dateien") is True
    task = conductor.background.running()[0]
    assert betreten.wait(timeout=3)
    assert conductor.background.cancel(task.task_id) is True
    freigabe.set()
    ende = time.monotonic() + 3
    while time.monotonic() < ende and not _events(log):
        time.sleep(0.02)
    # Kein Bericht nach dem Abmelden — nur die sofortige Quittung vom Start.
    assert all("finished" not in t for _c, t in gesendet)
    assert _events(log)





# --- /status: nur Fakten, die die Quelle hergibt --------------------------------------


def _usage_with_one_run() -> UsageMeter:
    meter = UsageMeter()
    meter.record(Run(at=time.time(), ok=True, duration_s=7.0, model="claude-x",
                     input_tokens=1200, output_tokens=300, cost_usd=0.04))
    return meter


def test_status_shows_usage_latency_approvals_background_and_schedules(tmp_path: Path) -> None:
    store = ApprovalStore()
    _park(store)
    desk = bg.BackgroundDesk()
    desk.accept("durchsuch die protokolle", run_id="aaaaaaaaaaaaaa")
    schedules = ScheduleStore(tmp_path / "plan.db")
    aufgabe = schedules.add(conversation=CHAT, principal=str(OWNER), prompt="morgenbericht",
                            interval_s=3600)
    center = _center(tmp_path, approvals=store, usage=_usage_with_one_run(),
                     background=desk, schedules=schedules)
    text = center.dispatch("status", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Verbrauch:" in text and "1.5k" in text  # 1200+300 Token der laufenden Session
    assert "Letzter Denkzug: 7s" in text  # Latenz des letzten Reasoner-Aufrufs
    assert "Offene Freigaben gesamt: 1" in text
    assert "Hintergrund:" in text and "#1" in text
    assert aufgabe is not None and aufgabe.id in text  # naechste Zeitplaene


def test_status_omits_what_no_source_provides(tmp_path: Path) -> None:
    """Wo keine Grösse gemessen wird, steht keine — ein erfundener Verbrauch waere
    schlimmer als eine fehlende Zeile (dieselbe Disziplin wie bei `/usage`)."""
    center = _center(tmp_path)  # kein usage, kein background, keine schedules
    text = center.dispatch("status", "", principal=OWNER, conversation=CHAT).reply or ""
    assert "Verbrauch:" not in text
    assert "Letzter Denkzug" not in text
    assert "Hintergrund:" not in text
    assert "Offene Freigaben gesamt" not in text  # null ist keine Meldung wert
    # …aber die alten Zeilen bleiben:
    assert "Laufzeit:" in text and "Offene Freigabe:" in text

