"""Cron-Gedaechtnis und Monitor-Delta — was ein Zeitplan-Lauf von seinem Vorlauf weiss.

Drei Zusicherungen tragen diese Datei, und keine davon ist Komfort:

  1. **Die Sonde bekommt keine neue Erlaubnis.** Sie ist ein gewoehnlicher `run_shell`
     des Auftrag-Principals, geht durch denselben Executor und denselben Kernel wie
     jeder Shell-Lauf — unter der unbeaufsichtigten Decke wird `NEEDS_HUMAN` zu
     `DENY`, und ein DENY bringt die Sonde zum Scheitern, nie zum Durchrutschen.
  2. **Ein kaputter Sensor verschluckt keinen Alarm.** Scheitert die Sonde (DENY,
     Fehler, verweigerte Sandbox, Timeout), feuert der Auftrag normal — nur ein
     UNVERAENDERTER, erfolgreich gelesener Fingerabdruck spart den Modellzug.
  3. **Kaputte Werte schalten AUS, nie an.** Eine aeltere Datenbank oeffnet mit
     allen Schaltern auf aus; Muell in einer Spalte heisst „Funktion aus".
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from talos import tools
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Inbound, Principal, Trust
from talos.conductor import Conductor
from talos.continuity import Continuity, error_key, fingerprint, framed_prompt
from talos.eventlog import EventLog, new_run_id
from talos.executor import Executor, Outcome, Status
from talos.policy import PolicyKernel, ToolRequest
from talos.schedule import MAX_PROBE_CHARS, MAX_RESULT_CHARS, ScheduleStore, UnattendedCeiling
from talos.snapshot import Snapshotter

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:100000001"


def _store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path / "schedules.db")


def _task(store: ScheduleStore, **flags):
    task = store.add(
        conversation=CHAT, principal=str(OWNER), prompt="Pruefe die Platte",
        interval_s=3600, now=1000.0, **flags,
    )
    assert task is not None
    return task


def _reload(store: ScheduleStore, task_id: str):
    for task in store.list_for(CHAT):
        if task.id == task_id:
            return task
    raise AssertionError(f"task {task_id} is gone")


def _events(log: EventLog, run_id: str, typ: str) -> list[dict]:
    return [e for e in log.by_run(run_id) if e["type"] == typ]


# --- Der Speicher: Felder, Migration, kaputte Werte ---------------------------------

def test_flags_round_trip_through_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store, continuity=True, monitor=True, probe="df -h /")
    assert task.continuity and task.monitor and task.probe == "df -h /"
    wieder = _reload(store, task.id)
    assert wieder.continuity and wieder.monitor and wieder.probe == "df -h /"
    assert wieder.last_fingerprint == "" and wieder.last_result == "" and wieder.last_error_key == ""
    assert "[continuity, monitor]" in wieder.describe()


def test_everything_is_off_by_default(tmp_path: Path) -> None:
    task = _task(_store(tmp_path))
    assert not task.continuity and not task.monitor and task.probe == ""
    assert "[" not in task.describe()


def test_monitor_and_probe_belong_together(tmp_path: Path) -> None:
    """Ein Monitor ohne Sonde haette nichts zu vergleichen, eine Sonde ohne Monitor
    liefe nie — beides ist ein Tippfehler, den der Betreiber sofort sehen soll."""
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="belong together"):
        _task(store, monitor=True)
    with pytest.raises(ValueError, match="belong together"):
        _task(store, probe="df -h")


def test_a_probe_is_one_bounded_line(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="at most"):
        _task(store, monitor=True, probe="x" * (MAX_PROBE_CHARS + 1))
    with pytest.raises(ValueError, match="one line"):
        _task(store, monitor=True, probe="df -h\nrm -rf /")


_OLD_SCHEMA = """
CREATE TABLE schedules (
    id TEXT PRIMARY KEY, conversation TEXT NOT NULL, principal TEXT NOT NULL,
    prompt TEXT NOT NULL, interval_s INTEGER NOT NULL, cron TEXT NOT NULL DEFAULT '',
    once INTEGER NOT NULL DEFAULT 0, next_run REAL NOT NULL, created REAL NOT NULL,
    last_run REAL
);
"""


def test_an_older_database_opens_with_every_switch_off(tmp_path: Path) -> None:
    """Ein Update gegen eine laufende Installation darf weder umfallen noch etwas
    einschalten, das der Betreiber nie angelegt hat."""
    pfad = tmp_path / "schedules.db"
    alt = sqlite3.connect(str(pfad))
    alt.executescript(_OLD_SCHEMA)
    alt.execute(
        "INSERT INTO schedules (id, conversation, principal, prompt, interval_s, next_run, created)"
        " VALUES ('alt1', ?, ?, 'alter auftrag', 3600, 5000.0, 1000.0)", (CHAT, str(OWNER)),
    )
    alt.commit()
    alt.close()

    store = _store(tmp_path)
    assert store.available, store.reason
    spalten = {row[1] for row in sqlite3.connect(str(pfad)).execute("PRAGMA table_info(schedules)")}
    assert {"continuity", "monitor", "probe", "last_fingerprint", "last_result", "last_error_key"} <= spalten
    (task,) = store.list_for(CHAT)
    assert task.id == "alt1" and task.prompt == "alter auftrag"
    assert not task.continuity and not task.monitor and task.probe == ""
    # Und die Datei ist danach voll benutzbar — ein zweites Oeffnen migriert nichts doppelt.
    assert _task(store, continuity=True).continuity
    assert _store(tmp_path).available


def test_broken_values_switch_a_feature_off_never_on(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store, continuity=True, monitor=True, probe="df -h")
    conn = sqlite3.connect(str(tmp_path / "schedules.db"))
    conn.execute("UPDATE schedules SET continuity = 'banana', monitor = 2 WHERE id = ?", (task.id,))
    conn.commit()
    kaputt = _reload(store, task.id)
    assert kaputt.continuity is False and kaputt.monitor is False
    # Ein Monitor-Schalter ohne Sonde ist kein Monitor — auch wenn die Spalte 1 sagt.
    conn.execute("UPDATE schedules SET monitor = 1, probe = '' WHERE id = ?", (task.id,))
    conn.commit()
    assert _reload(store, task.id).monitor is False
    conn.close()


def test_probe_and_result_are_recorded_per_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = _task(store, continuity=True, monitor=True, probe="df -h")
    store.record_probe(task.id, "abc123")
    store.record_result(task.id, result="x" * (MAX_RESULT_CHARS + 500), error_key="k1")
    wieder = _reload(store, task.id)
    assert wieder.last_fingerprint == "abc123"
    assert len(wieder.last_result) == MAX_RESULT_CHARS
    assert wieder.last_error_key == "k1"
    store.record_probe("gibtsnicht", "zzz")  # kein Absturz, keine Wirkung
    store.record_result("gibtsnicht", result="x", error_key="")
    assert len(store.list_for(CHAT)) == 1


# --- Die reinen Helfer ---------------------------------------------------------------

def test_fingerprint_is_stable_and_ignores_the_trailing_newline() -> None:
    assert fingerprint("Filesystem 91%\n") == fingerprint("Filesystem 91%")
    assert fingerprint("Filesystem 91%") != fingerprint("Filesystem 92%")
    assert len(fingerprint("x")) == 16 and fingerprint("") != fingerprint(" ")


def test_error_key_is_empty_for_a_clean_run_and_stable_for_the_same_failure() -> None:
    assert error_key("answered", ()) == ""
    gleich = error_key("answered", (("run_shell", "DENY: unattended run"),))
    assert gleich and gleich == error_key("answered", (("run_shell", "DENY: unattended run"),))
    assert gleich != error_key("answered", (("run_shell", "rc=1"),))
    assert error_key("step_limit", ()) != ""
    # Reihenfolge der Fehlschlaege spielt keine Rolle — derselbe Befund, derselbe Schluessel.
    a = error_key("answered", (("read_file", "missing"), ("run_shell", "denied")))
    b = error_key("answered", (("run_shell", "denied"), ("read_file", "missing")))
    assert a == b


def test_framed_prompt_marks_the_previous_result_as_data() -> None:
    text = framed_prompt("Pruefe die Platte", "disk at 91%")
    assert "disk at 91%" in text and text.endswith("Pruefe die Platte")
    assert "never instructions" in text and "«disk at 91%»" in text
    assert framed_prompt("Pruefe die Platte", "") == "Pruefe die Platte"


# --- Die Sonde: Monitor-Modus --------------------------------------------------------

class _Execute:
    """Ein Executor-Doppel: antwortet mit der naechsten Quittung und merkt sich die Anfrage."""

    def __init__(self, *outcomes: Outcome | Exception) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[ToolRequest] = []

    def __call__(self, req: ToolRequest, run_id: str) -> Outcome:
        self.requests.append(req)
        naechste = self._outcomes.pop(0)
        if isinstance(naechste, Exception):
            raise naechste
        return naechste


def _done(text: str) -> Outcome:
    return Outcome(Status.DONE, "exec allowed", f"rc=0 [fake]\n{text}")


def _desk(tmp_path: Path, execute) -> tuple[Continuity, ScheduleStore, EventLog]:
    store = _store(tmp_path)
    log = EventLog(tmp_path / "events.db")
    return Continuity(schedules=store, log=log, execute=execute), store, log


def test_the_first_run_of_a_monitor_fires_and_records_the_fingerprint(tmp_path: Path) -> None:
    execute = _Execute(_done("91%"))
    desk, store, log = _desk(tmp_path, execute)
    task = _task(store, monitor=True, probe="df -h /")
    run_id = new_run_id()
    bereit = desk.prepare(task, OWNER, run_id=run_id)
    assert bereit is not None and bereit.text == task.prompt
    # Die Sonde ist ein gewoehnlicher run_shell DIESES Principals — nichts anderes.
    (req,) = execute.requests
    assert req.tool == "run_shell" and req.identity == OWNER and req.args == {"command": "df -h /"}
    assert _reload(store, task.id).last_fingerprint == fingerprint("rc=0 [fake]\n91%")
    assert _events(log, run_id, "schedule.skipped_unchanged") == []


def test_unchanged_probe_output_skips_the_model_run(tmp_path: Path) -> None:
    execute = _Execute(_done("91%"))
    desk, store, log = _desk(tmp_path, execute)
    task = _task(store, monitor=True, probe="df -h /")
    store.record_probe(task.id, fingerprint("rc=0 [fake]\n91%"))
    run_id = new_run_id()
    assert desk.prepare(_reload(store, task.id), OWNER, run_id=run_id) is None
    (ereignis,) = _events(log, run_id, "schedule.skipped_unchanged")
    assert ereignis["payload"]["id"] == task.id


def test_changed_probe_output_fires_and_moves_the_fingerprint_on(tmp_path: Path) -> None:
    execute = _Execute(_done("92%"))
    desk, store, log = _desk(tmp_path, execute)
    task = _task(store, monitor=True, probe="df -h /")
    store.record_probe(task.id, fingerprint("rc=0 [fake]\n91%"))
    assert desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id()) is not None
    assert _reload(store, task.id).last_fingerprint == fingerprint("rc=0 [fake]\n92%")


@pytest.mark.parametrize("quittung", [
    Outcome(Status.DENIED, "unattended run — anything that needs your approval is reported"),
    Outcome(Status.NEEDS_HUMAN, "shell without sandbox — needs your approval"),
    Outcome(Status.ERROR, "boom"),
    Outcome(Status.DONE, "exec allowed", "rc=refused\nno sandbox available"),
    Outcome(Status.DONE, "exec allowed", "rc=124 [bwrap]\npartial\n[timed out]"),
    RuntimeError("executor exploded"),
])
def test_a_failing_probe_fires_instead_of_swallowing_the_alarm(tmp_path: Path, quittung) -> None:
    """⚠️ Der wichtigste Test des Monitor-Modus: ein Sensor, der nicht lesen kann, darf
    nicht „unveraendert" melden. Sonst waere ein DENY auf die Sonde der bequemste Weg,
    einen Waechter stumm zu schalten."""
    desk, store, log = _desk(tmp_path, _Execute(quittung))
    task = _task(store, monitor=True, probe="df -h /")
    store.record_probe(task.id, "vorher")
    run_id = new_run_id()
    bereit = desk.prepare(_reload(store, task.id), OWNER, run_id=run_id)
    assert bereit is not None and bereit.text == task.prompt
    (ereignis,) = _events(log, run_id, "schedule.probe_failed")
    assert ereignis["payload"]["id"] == task.id and ereignis["payload"]["reason"]
    assert _events(log, run_id, "schedule.skipped_unchanged") == []
    # Der letzte GUTE Abdruck bleibt stehen — ein Fehlversuch ist kein Messwert.
    assert _reload(store, task.id).last_fingerprint == "vorher"


def test_a_task_without_monitor_never_runs_a_probe(tmp_path: Path) -> None:
    execute = _Execute()
    desk, store, _ = _desk(tmp_path, execute)
    task = _task(store)
    bereit = desk.prepare(task, OWNER, run_id=new_run_id())
    assert bereit is not None and bereit.text == task.prompt and bereit.before_reply is None
    assert execute.requests == []


def _real_executor(tmp_path: Path, policy, *, shell, log: EventLog) -> Executor:
    mint = CapabilityMint(policy)
    return Executor(
        policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners={"run_shell": shell}), mint=mint,
    )


def test_the_probe_is_judged_by_the_real_kernel_not_by_this_module(tmp_path: Path) -> None:
    """Kein Umweg um policy.py: mit `shell_needs_human` scheitert die Sonde am Kernel und
    die Shell wird nie gerufen; ohne laeuft sie — und ein Hardline-Kommando bleibt DENY."""
    gerufen: list[str] = []

    def shell(req: ToolRequest) -> str:
        gerufen.append(req.args["command"])
        return "rc=0 [fake]\n91%"

    log = EventLog(tmp_path / "events.db")
    store = _store(tmp_path)
    streng = PolicyKernel(tools.default_manifest(), frozenset({OWNER}), shell_needs_human=True)
    desk = Continuity(
        schedules=store, log=log, execute=_real_executor(tmp_path, streng, shell=shell, log=log).run,
    )
    task = _task(store, monitor=True, probe="df -h /")
    run_id = new_run_id()
    assert desk.prepare(task, OWNER, run_id=run_id) is not None
    assert gerufen == []  # NEEDS_HUMAN ohne Menschen: nicht gelaufen
    assert _events(log, run_id, "schedule.probe_failed")
    (intent,) = _events(log, run_id, "exec.intent")
    assert intent["payload"]["tool"] == "run_shell" and intent["payload"]["verdict"] == "needs_human"

    locker = PolicyKernel(tools.default_manifest(), frozenset({OWNER}), shell_needs_human=False)
    desk = Continuity(
        schedules=store, log=log, execute=_real_executor(tmp_path, locker, shell=shell, log=log).run,
    )
    assert desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id()) is not None
    assert gerufen == ["df -h /"]
    assert _reload(store, task.id).last_fingerprint == fingerprint("rc=0 [fake]\n91%")

    hart = _task(store, monitor=True, probe="rm -rf /")
    run_id = new_run_id()
    assert desk.prepare(hart, OWNER, run_id=run_id) is not None
    assert gerufen == ["df -h /"]  # die Hardline hielt, der Waechter feuert trotzdem
    assert _events(log, run_id, "schedule.probe_failed")


def test_under_the_unattended_ceiling_the_probe_cannot_reach_an_approval(tmp_path: Path) -> None:
    """Dieselbe Decke wie fuer den Lauf: was einen Menschen braeuchte, wird DENY —
    und die Sonde scheitert daran ehrlich, statt sich eine Freigabe zu holen."""
    gerufen: list[str] = []
    log = EventLog(tmp_path / "events.db")
    store = _store(tmp_path)
    ceiling = UnattendedCeiling()
    kernel = GovernedKernel(
        PolicyKernel(tools.default_manifest(), frozenset({OWNER}), shell_needs_human=True),
        AutonomyGovernor(5), lambda _c: Trust.FULL, unattended=ceiling,
    )
    executor = _real_executor(
        tmp_path, kernel, shell=lambda req: gerufen.append(req.args["command"]) or "rc=0 [x]\nok",
        log=log,
    )
    desk = Continuity(schedules=store, log=log, execute=executor.run)
    task = _task(store, monitor=True, probe="df -h /")
    run_id = new_run_id()
    with ceiling.active():
        bereit = desk.prepare(task, OWNER, run_id=run_id)
    assert bereit is not None and gerufen == []
    (ergebnis,) = _events(log, run_id, "exec.result")
    assert ergebnis["payload"]["status"] == "denied" and "unattended" in ergebnis["payload"]["detail"]


# --- Das Gedaechtnis: Continuity ------------------------------------------------------

def test_continuity_prepends_the_previous_result_as_data(tmp_path: Path) -> None:
    desk, store, _ = _desk(tmp_path, _Execute())
    task = _task(store, continuity=True)
    store.record_result(task.id, result="disk at 91%", error_key="")
    bereit = desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id())
    assert bereit is not None
    assert bereit.text == framed_prompt(task.prompt, "disk at 91%")
    assert bereit.before_reply is not None


def test_without_a_previous_result_the_prompt_is_untouched(tmp_path: Path) -> None:
    desk, store, _ = _desk(tmp_path, _Execute())
    task = _task(store, continuity=True)
    bereit = desk.prepare(task, OWNER, run_id=new_run_id())
    assert bereit is not None and bereit.text == task.prompt and bereit.before_reply is not None


def test_the_hook_records_the_result_and_delivers_a_clean_run(tmp_path: Path) -> None:
    desk, store, log = _desk(tmp_path, _Execute())
    task = _task(store, continuity=True)
    bereit = desk.prepare(task, OWNER, run_id=new_run_id())
    lauf = new_run_id()
    assert bereit.before_reply(lauf, "answered", "all good") is True
    wieder = _reload(store, task.id)
    assert wieder.last_result == "all good" and wieder.last_error_key == ""


def _failed_run(log: EventLog, detail: str) -> str:
    """Ein Lauf, in dem der Executor einen Fehlschlag protokolliert hat."""
    from talos.eventlog import Event

    run_id = new_run_id()
    log.append(Event(run_id, "executor", "exec.result",
                     {"tool": "run_shell", "status": "denied", "detail": detail}))
    return run_id


def test_the_same_error_twice_is_logged_not_sent_and_recovery_is_always_sent(tmp_path: Path) -> None:
    desk, store, log = _desk(tmp_path, _Execute())
    task = _task(store, continuity=True)

    erster = desk.prepare(task, OWNER, run_id=new_run_id())
    assert erster.before_reply(_failed_run(log, "unattended run"), "answered", "could not") is True

    zweiter = desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id())
    wiederholt = _failed_run(log, "unattended run")
    assert zweiter.before_reply(wiederholt, "answered", "still could not") is False
    (ereignis,) = _events(log, wiederholt, "schedule.error_repeated")
    assert ereignis["payload"]["id"] == task.id and ereignis["payload"]["key"]
    # Das Gedaechtnis lernt den unterdrueckten Lauf trotzdem: er IST das letzte Ergebnis.
    assert _reload(store, task.id).last_result == "still could not"

    # Ein ANDERER Fehler geht raus …
    dritter = desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id())
    assert dritter.before_reply(_failed_run(log, "rc=1 no such file"), "answered", "hm") is True
    # … und die Genesung erst recht.
    vierter = desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id())
    assert vierter.before_reply(new_run_id(), "answered", "fixed") is True
    assert _reload(store, task.id).last_error_key == ""


def test_the_hook_reads_failures_from_the_log_not_from_the_text(tmp_path: Path) -> None:
    """Zwei identische Antworttexte mit verschiedenen Fehlschlaegen sind zwei Befunde —
    das Protokoll entscheidet, nicht die Prosa des Modells."""
    desk, store, log = _desk(tmp_path, _Execute())
    task = _task(store, continuity=True)
    erster = desk.prepare(task, OWNER, run_id=new_run_id())
    assert erster.before_reply(_failed_run(log, "denied A"), "answered", "same words") is True
    zweiter = desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id())
    assert zweiter.before_reply(_failed_run(log, "denied B"), "answered", "same words") is True


def test_a_task_that_ends_the_same_way_but_clean_is_never_deduplicated(tmp_path: Path) -> None:
    """Dedup ist fuer wiederholte FEHLER. Ein Bericht, der zweimal dasselbe Gute sagt,
    ist ein Bericht — und wer ihn nicht will, nimmt den Monitor-Modus."""
    desk, store, _ = _desk(tmp_path, _Execute())
    task = _task(store, continuity=True)
    for _ in range(3):
        bereit = desk.prepare(_reload(store, task.id), OWNER, run_id=new_run_id())
        assert bereit.before_reply(new_run_id(), "answered", "all fine") is True


# --- Die Naht im Conductor: der Hook sieht die Antwort VOR der Zustellung -----------

class _EchoReasoner:
    def reason(self, prompt: str) -> str:
        return "echo: alles gut"


def _conductor(tmp_path: Path):
    log = EventLog(tmp_path / "ev.db")
    gesendet: list[tuple[str, str]] = []
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy, log=log, snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)), mint=mint,
    )
    conductor = Conductor(
        log=log, reasoner=_EchoReasoner(), executor=executor,
        send=lambda conversation, text: gesendet.append((conversation, text)),
        allowed_principals=frozenset({OWNER}), trust_of=lambda _c: Trust.FULL,
    )
    return conductor, gesendet, log


def _update(text: str = "Pruefe die Platte", n: int = 1) -> Inbound:
    return Inbound(principal=OWNER, conversation=CHAT, text=text, dedup_key=f"schedule:t1:{n}")


def test_handle_offers_the_reply_to_the_hook_before_delivery(tmp_path: Path) -> None:
    conductor, gesendet, log = _conductor(tmp_path)
    gesehen: list[tuple[str, str, str]] = []

    def hook(run_id: str, status: str, reply: str) -> bool:
        gesehen.append((run_id, status, reply))
        assert gesendet == []  # VOR der Zustellung
        return True

    assert conductor.handle(_update(), before_reply=hook) is True
    (run_id, status, reply) = gesehen[0]
    assert status == "answered" and reply == "echo: alles gut"
    assert gesendet == [(CHAT, "echo: alles gut")]
    assert _events(log, run_id, "reply.sent")


def test_a_hook_that_says_no_withholds_the_message_but_leaves_a_trace(tmp_path: Path) -> None:
    conductor, gesendet, log = _conductor(tmp_path)
    gesehen: list[str] = []
    assert conductor.handle(_update(), before_reply=lambda rid, *_: gesehen.append(rid) or False) is False
    assert gesendet == []
    (run_id,) = gesehen
    assert _events(log, run_id, "conductor.reply_suppressed")
    assert _events(log, run_id, "reply.sent") == []
    # Nicht zugestellt heisst nicht gemerkt — wie beim verworfenen Hintergrundbericht.
    assert conductor.memory.recall(CHAT) == ()


def test_a_broken_hook_never_swallows_the_answer(tmp_path: Path) -> None:
    """Fail-open in Richtung Zustellung: der Hook ist Komfort, die Antwort nicht."""
    conductor, gesendet, log = _conductor(tmp_path)

    def kaputt(run_id: str, status: str, reply: str) -> bool:
        raise RuntimeError("hook exploded")

    assert conductor.handle(_update(), before_reply=kaputt) is True
    assert gesendet == [(CHAT, "echo: alles gut")]
    fehler = [e for e in log.recent(50, types=("error",)) if e["payload"].get("stage") == "before_reply"]
    assert fehler and "hook exploded" in fehler[0]["payload"]["error"]


def test_without_a_hook_nothing_changes(tmp_path: Path) -> None:
    conductor, gesendet, _ = _conductor(tmp_path)
    assert conductor.handle(_update()) is True
    assert gesendet == [(CHAT, "echo: alles gut")]
