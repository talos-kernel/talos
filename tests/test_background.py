"""`/background` — eine zweite Aufgabe, ohne dass die Leine laenger wird.

Der Kern dieser Datei ist eine Einordnung: ein Hintergrundlauf ist sicherheitstechnisch
ein ZEITPLAN-Lauf, kein Subagent. Er entsteht aus dem Tippen eines Menschen (also darf er
mehr als ein Subagent, der aus Modelltext entsteht) — aber niemand sitzt davor und wartet
(also gilt dieselbe Decke wie beim zeitgesteuerten Lauf). Beide Haelften werden hier
festgehalten, und die zweite ist die, die schiefgehen kann.
"""
from __future__ import annotations

import time

from talos import background as bg, tools
from talos.approval import ApprovalStore
from talos.capability import CapabilityMint
from talos.channel import Inbound, Principal
from talos.commands import CommandCenter, CommandResult
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.policy import PolicyKernel


def _center(tmp_path) -> CommandCenter:
    policy = PolicyKernel(tools.default_manifest(), frozenset({Principal("telegram", "1")}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"), approvals=ApprovalStore(), policy=policy,
        started_at=0.0, bot_username="Talos_bot", reasoner=None, worker=None,
        repo_dir=tmp_path, mint=CapabilityMint(policy),
    )


def _inbound(text: str = "/background finde die fehler", conversation: str = "chat-1"):
    return Inbound(principal=Principal("telegram", "1"), conversation=conversation,
                   text=text, dedup_key=f"k-{text}")


class _Decke:
    """Eine Decke, die mitzaehlt, wie oft sie wirklich aufgespannt wurde."""

    def __init__(self) -> None:
        self.aktiv = 0

    def active(self):
        decke = self

        class _Ctx:
            def __enter__(self_inner):
                decke.aktiv += 1
                return None

            def __exit__(self_inner, *_):
                return False
        return _Ctx()


def _conductor(tmp_path, *, decke=None):
    gesendet: list[tuple[str, str]] = []
    laeufe: list[dict] = []

    conductor = Conductor(
        log=EventLog(tmp_path / "ev.db"), reasoner=None, executor=None,
        send=lambda c, t: gesendet.append((c, t)),
        allowed_principals=frozenset(), trust_of=lambda _: None,
        unattended=decke,
    )

    def falscher_lauf(update, run_id, text=None, *, past_override=None,
                      leading_note="", **rest):
        laeufe.append({"text": text, "past_override": past_override,
                       "conversation": update.conversation, "leading_note": leading_note})
        return True

    object.__setattr__(conductor, "_run_task", falscher_lauf)
    return conductor, gesendet, laeufe


def _warten(bedingung, grenze: float = 3.0) -> bool:
    ende = time.monotonic() + grenze
    while time.monotonic() < ende:
        if bedingung():
            return True
        time.sleep(0.02)
    return False


# --- Die Haelfte, die schiefgehen kann --------------------------------------------------
def test_without_a_ceiling_the_task_is_refused_not_run_uncapped(tmp_path) -> None:
    """⚠️ Der wichtigste Test dieser Datei.

    Ein vergessener Parameter darf nur weniger erlauben, nie mehr. Liefe der Auftrag ohne
    Decke, waere `/background` der bequemste Weg, jede Rueckfrage zu umgehen — man muesste
    sie nur nicht stellen lassen.
    """
    conductor, gesendet, laeufe = _conductor(tmp_path, decke=None)
    conductor._start_background(_inbound(), "run-1", "finde die fehler")
    assert laeufe == []
    assert gesendet and "not available" in gesendet[0][1]


def test_every_background_turn_runs_under_the_ceiling(tmp_path) -> None:
    """Niemand sitzt davor, also wird `NEEDS_HUMAN` zu `DENY` — dieselbe Regel wie beim
    Zeitplan, und aus demselben Grund."""
    decke = _Decke()
    conductor, _, laeufe = _conductor(tmp_path, decke=decke)
    conductor._start_background(_inbound(), "run-1", "finde die fehler")
    assert _warten(lambda: decke.aktiv >= 1), "die Decke wurde nie aufgespannt"
    assert _warten(lambda: len(laeufe) == 1)


def test_the_receipt_says_that_approvals_will_be_refused(tmp_path) -> None:
    """Sonst wundert sich der Betreiber in zwei Minuten ueber ein DENY, das im Vordergrund
    eine Rueckfrage gewesen waere — und haelt die Regel fuer einen Fehler."""
    conductor, gesendet, _ = _conductor(tmp_path, decke=_Decke())
    conductor._start_background(_inbound(), "run-1", "finde die fehler")
    assert "unattended" in gesendet[0][1] and "approval" in gesendet[0][1]


# --- Isolation ---------------------------------------------------------------------------
def test_the_background_run_starts_with_an_empty_context(tmp_path) -> None:
    """⚠️ Zwei Laeufe, die sich einen Verlauf teilen, schreiben einander hinein — und
    hinterher ist nicht mehr zu sagen, welcher was gesagt hat."""
    conductor, _, laeufe = _conductor(tmp_path, decke=_Decke())
    conductor._start_background(_inbound(), "run-1", "finde die fehler")
    assert _warten(lambda: len(laeufe) == 1)
    assert laeufe[0]["past_override"] == ()


def test_the_prompt_is_what_was_typed_not_the_command(tmp_path) -> None:
    """Der Lauf soll „finde die fehler" bearbeiten, nicht „/background finde die fehler"."""
    conductor, _, laeufe = _conductor(tmp_path, decke=_Decke())
    conductor._start_background(_inbound(), "run-1", "finde die fehler")
    assert _warten(lambda: len(laeufe) == 1)
    assert laeufe[0]["text"] == "finde die fehler"


def test_the_answer_goes_back_to_the_same_conversation(tmp_path) -> None:
    conductor, _, laeufe = _conductor(tmp_path, decke=_Decke())
    conductor._start_background(_inbound(conversation="chat-7"), "run-1", "etwas")
    assert _warten(lambda: len(laeufe) == 1)
    assert laeufe[0]["conversation"] == "chat-7"


# --- Deckel ------------------------------------------------------------------------------
def test_too_many_at_once_is_refused_immediately(tmp_path) -> None:
    """Die Absage soll der Betreiber sofort lesen — nicht erst, wenn irgendwann nichts
    kommt. Jeder Lauf kostet einen Thread, Token und Modellzeit."""
    desk = bg.BackgroundDesk()
    for i in range(bg.MAX_CONCURRENT):
        assert desk.accept(f"auftrag {i}", run_id=f"r{i}") is not None
    assert desk.accept("einer zu viel", run_id="rx") is None
    assert desk.full()


def test_a_finished_task_frees_its_slot() -> None:
    desk = bg.BackgroundDesk()
    task = desk.accept("etwas", run_id="r1")
    desk.finish(task.task_id)
    assert desk.busy() == 0 and not desk.full()


def test_numbers_count_up_for_humans_ids_stay_unique() -> None:
    """Die Nummer ist zum Ansprechen da („#2 ist fertig"), die id fuers Protokoll."""
    desk = bg.BackgroundDesk()
    a = desk.accept("eins", run_id="aaaaaaaaaaaaaa")
    b = desk.accept("zwei", run_id="bbbbbbbbbbbbbb")
    assert (a.number, b.number) == (1, 2)
    assert a.task_id != b.task_id


# --- Das Kommando -------------------------------------------------------------------------
def test_the_command_only_hands_the_task_over(tmp_path) -> None:
    """⚠️ Ein Kommando, das selbst einen Agenten startete, waere ein zweiter
    Ausfuehrungspfad. Der Lauf gehoert dem Conductor, weil nur dort Kernel, Decke und
    Zustellweg zusammenkommen."""
    center = _center(tmp_path)
    ergebnis = center.dispatch("background", "sieh nach", principal=Principal("telegram", "1"),
                               conversation="chat-1")
    assert isinstance(ergebnis, CommandResult)
    assert ergebnis.background == "sieh nach"
    assert ergebnis.forward_as is None and ergebnis.request is None


def test_an_empty_background_command_explains_itself(tmp_path) -> None:
    center = _center(tmp_path)
    ergebnis = center.dispatch("background", "   ", principal=Principal("telegram", "1"),
                               conversation="chat-1")
    assert ergebnis.background is None and "usage" in (ergebnis.reply or "")


# --- Der Bericht ---------------------------------------------------------------------------
def test_the_answer_reaches_the_operator_marked_as_background(tmp_path) -> None:
    """⚠️ Gemessen, nicht gemeint.

    Der erste Lauf im Betrieb lieferte seine Antwort voellig ungekennzeichnet mitten ins
    Gespraech, direkt hinter den Eingabe-Prompt — wer das liest, haelt es fuer die Antwort
    auf seine letzte Frage. Die Funktion `report()` war getestet, der WEG dorthin nicht.
    Deshalb prueft dieser Test den Parameter, mit dem der Lauf tatsaechlich startet.
    """
    conductor, _, laeufe = _conductor(tmp_path, decke=_Decke())
    conductor._start_background(_inbound(), "run-1", "zaehle die dateien")
    assert _warten(lambda: len(laeufe) == 1)
    kopf = laeufe[0]["leading_note"]
    assert kopf and "Background #1" in kopf and "zaehle die dateien" in kopf


def test_the_marker_is_a_head_and_not_a_foot() -> None:
    """Ein Hinweis UNTER dem Text kaeme zu spaet — da ist die Antwort schon falsch
    verstanden."""
    from talos.conductor import _append_note

    kopf = bg.header(bg.Task(1, "bg_x", "etwas"))
    zusammengesetzt = f"{kopf}\n\ndie Antwort"
    assert zusammengesetzt.startswith("— Background #1")
    assert _append_note("die Antwort", kopf).startswith("die Antwort")   # so waere es falsch


def test_the_report_is_marked_as_a_report(tmp_path) -> None:
    """⚠️ Ohne Kennzeichnung liest sich eine Hintergrundantwort wie die Antwort auf die
    zuletzt gestellte Frage — und genau das ist sie nicht."""
    task = bg.Task(2, "bg_abc", "durchsuch die protokolle")
    text = bg.report(task, "drei Fehler gefunden")
    assert "Background #2" in text and "durchsuch die protokolle" in text


def test_an_empty_result_is_still_a_message() -> None:
    """Ein Lauf, der nichts sagt, sieht sonst aus wie ein Lauf, der haengt."""
    assert "(no output)" in bg.report(bg.Task(1, "bg_x", "etwas"), "   ")
