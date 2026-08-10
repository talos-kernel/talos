"""Der Selbstreview meldet, was sich aendern sollte — und aendert selbst nichts.

Zwei Sorten Test. Die erste prueft, dass die Befunde stimmen und dass die eine Verbindung
haelt, die den Review ueberhaupt lohnt: eine Luecke wird erst durch das Protokoll zur
Baustelle. Die zweite ist die Sicherheitssorte — ein Agent, der aus seiner Geschichte
Rechte ableitet, haette einen zweiten Erlaubnisweg neben dem Kernel, und den gibt es
hier aus gutem Grund nicht.
"""
from __future__ import annotations

import ast
from pathlib import Path

from talos import review


def _fail(tool: str, detail: str = "boom") -> dict:
    return {"type": "exec.result", "payload": {"tool": tool, "status": "FAILED", "detail": detail}}


def _done(tool: str) -> dict:
    return {"type": "exec.result", "payload": {"tool": tool, "status": "DONE"}}


def _intent(tool: str, verdict: str, reason: str = "") -> dict:
    return {"type": "exec.intent", "payload": {"tool": tool, "verdict": verdict, "reason": reason}}


def _grant(fp: str, tool: str = "run_shell") -> dict:
    return {"type": "grant.issued", "payload": {"action_fp": fp, "tool": tool}}


# --- Dieselbe Wand mehrfach ------------------------------------------------------------
def test_the_same_wall_twice_becomes_a_finding() -> None:
    """Einmal ist ein Vorfall. Zweimal kostet jedes Mal einen Lauf und gehoert gemeldet."""
    (befund,) = review.survey([_fail("run_shell", "No module named pytest")] * 2)
    assert befund.kind == "repeat-failure" and befund.count == 2


def test_once_is_not_a_pattern() -> None:
    assert review.survey([_fail("run_shell")]) == ()


def test_a_wall_that_was_torn_down_is_not_a_building_site() -> None:
    """⚠️ Dieselbe Verjaehrung wie bei den Lehren, aus demselben Anlass: `web_search`
    scheiterte zweimal und lief danach. Den Betreiber auf diese Reparatur zu schicken
    hiesse, ihn zu einer Baustelle zu fahren, die es nicht mehr gibt."""
    assert review.survey([_fail("web_search"), _fail("web_search"), _done("web_search")]) == ()


# --- Die Verbindung, die den Review lohnt ----------------------------------------------
def test_a_gap_becomes_a_finding_only_once_it_has_cost_something() -> None:
    """⚠️ Der Kern dieses Moduls.

    Dass `ddgs` fehlt, sagt der Doktor seit jeher. Erst zusammen mit dem Protokoll wird
    daraus eine Aussage, auf die hin jemand handelt: es hat drei Laeufe gekostet.
    """
    befunde = review.survey(
        [_fail("web_search", "no ddgs")] * 3,
        gaps=[("web_search (ddgs)", "missing — `pip install ddgs`")],
    )
    luecke = [f for f in befunde if f.kind == "gap-cost"]
    assert luecke and luecke[0].count == 3 and "pip install ddgs" in luecke[0].note


def test_a_gap_that_never_stopped_anyone_stays_a_footnote() -> None:
    """Sonst steht der Bericht voll mit Faehigkeiten, die niemand vermisst — und was
    unten steht, liest keiner mehr."""
    befunde = review.survey([], gaps=[("speak (piper)", "no voice installed")])
    assert not [f for f in befunde if f.kind == "gap-cost"]


# --- Abgenutzte Rueckfragen ------------------------------------------------------------
def test_three_approvals_of_the_same_action_are_a_worn_prompt() -> None:
    """Die vierte Rueckfrage schuetzt niemanden mehr. Sie erzieht zum Wegklicken."""
    (befund,) = review.survey([_grant("abc123def456")] * 3)
    assert befund.kind == "worn-approval" and befund.count == 3


def test_approvals_are_counted_per_action_and_never_per_tool_name() -> None:
    """„Du erlaubst `run_shell` staendig" waere eine Aussage ueber ein Wort. Genau diese
    Verwechslung ist der Grund, warum Dauerrechte an Werkzeugnamen aus diesem Projekt
    entfernt wurden — sie darf hier nicht durch die Hintertuer zurueckkommen."""
    assert review.survey([_grant("aaa"), _grant("bbb"), _grant("ccc")]) == ()


# --- Wiederholt abgelehnte Vorschlaege --------------------------------------------------
def test_a_proposal_the_kernel_keeps_refusing_says_something_about_the_task() -> None:
    (befund,) = review.survey([_intent("run_shell", "deny", "hardline floor")] * 3)
    assert befund.kind == "repeat-refusal" and "hardline floor" in befund.note


def test_what_the_kernel_allowed_is_not_a_finding() -> None:
    assert review.survey([_intent("read_file", "allow")] * 9) == ()


# --- Alter -------------------------------------------------------------------------------
def test_a_pattern_from_two_weeks_ago_is_history_not_a_building_site() -> None:
    """⚠️ Gefunden am eigenen Bericht, nicht am Reissbrett.

    Der erste Lauf gegen ein echtes Protokoll meldete 17 abgelehnte `run_shell` mit der
    Begruendung „Shell ohne Sandbox" — alle vom 1. August, aus der Zeit vor der
    Einrichtung von bubblewrap. Der Betreiber haette eine Baustelle besucht, die seit
    fuenf Tagen keine mehr war.
    """
    jetzt = 1_000_000.0
    alt = [{"ts": jetzt - review.MAX_AGE_S - 1, **_intent("run_shell", "needs_human", "x")}] * 5
    assert review.survey(alt, now=jetzt) == ()
    assert review.survey(alt) != ()          # ohne Uhr zaehlt weiterhin alles


def test_a_count_without_a_date_reads_as_a_state_of_today() -> None:
    """⚠️ Gemessen am eigenen ersten Bericht: „run_shell — 17×" war richtig gezaehlt und
    trotzdem irrefuehrend, weil alle 17 aus der Zeit vor der Sandbox-Einrichtung stammten.
    Ob eine Ablehnung auf einer Regel oder einem behobenen Zustand beruht, kann die
    Maschine nicht entscheiden — wann sie zuletzt vorkam, schon. Also steht es dabei.
    """
    jetzt = 1_000_000.0
    fuenf_tage_alt = [{"ts": jetzt - 5 * 86_400, **_intent("run_shell", "needs_human", "x")}] * 4
    text = review.render(review.survey(fuenf_tage_alt, now=jetzt), now=jetzt)
    assert "4× · last 5d ago" in text


def test_an_entry_without_a_timestamp_still_counts() -> None:
    """Fehlende Zeit heisst „unbekannt". Einen Befund deswegen verschwinden zu lassen
    machte den Bericht stiller, ohne dass jemand etwas repariert haette."""
    assert review.survey([_fail("run_shell", "x")] * 2, now=1_000_000.0) != ()


# --- Der Review misst sich selbst -------------------------------------------------------
def test_a_finding_reported_before_says_so() -> None:
    """Ein Review, der woechentlich dieselbe Liste schickt und es nicht bemerkt, ist ein
    Ritual — und Rituale liest niemand mehr, auch der Betreiber nicht."""
    protokoll = [{"type": "review.reported", "payload": {"keys": ["repeat-failure:run_shell"]}}]
    protokoll += [_fail("run_shell", "No module named pytest")] * 2
    (befund,) = review.survey(protokoll)
    assert befund.seen_before == 1
    assert "reported 1×" in review.render([befund])


# --- Bericht ----------------------------------------------------------------------------
def test_nothing_to_improve_says_nothing() -> None:
    """Ein Selbstreview, der „alles in Ordnung" meldet, trainiert genau das Wegklicken
    an, gegen das sein zweiter Befund anschreibt."""
    assert review.render(review.survey([])) == ""


def test_the_costliest_finding_stands_first() -> None:
    """Ein Bericht wird von oben gelesen. Was unten steht, wird nicht gelesen."""
    text = review.render(review.survey([_fail("selten", "x")] * 2 + [_fail("teuer", "y")] * 7))
    assert text.index("• teuer") < text.index("• selten")


def test_the_report_says_out_loud_that_it_changed_nothing() -> None:
    """Der Unterschied zwischen einem Vorschlag und einer Freigabe ist der ganze Kernel.
    Er muss im Text stehen, nicht nur in der Absicht des Autors."""
    text = review.render(review.survey([_fail("run_shell", "x")] * 2))
    assert "Proposals only" in text and "by hand" in text


def test_the_first_review_happens_instead_of_waiting_out_an_interval() -> None:
    """Eine frische Installation ist der Moment, in dem eine fehlende Bibliothek noch
    billig zu beheben ist."""
    assert review.due(None, now=0.0, interval_s=86_400.0) is True
    assert review.due(100.0, now=200.0, interval_s=86_400.0) is False
    assert review.due(100.0, now=100_000.0, interval_s=86_400.0) is True


# --- „Pflicht" heisst: ein Lauf loest ihn aus, nicht ein Befehl -------------------------
def _conductor(tmp_path, gaps=()):
    from talos.conductor import Conductor
    from talos.eventlog import EventLog

    gesendet: list[tuple[str, str]] = []
    conductor = Conductor(
        log=EventLog(tmp_path / "ev.db"), reasoner=None, executor=None,
        send=lambda conversation, text: gesendet.append((conversation, text)),
        allowed_principals=frozenset(), trust_of=lambda _: None,
        capability_gaps=lambda: gaps,
    )
    return conductor, gesendet


def _inbound(conversation: str = "chat-1"):
    from talos.channel import Inbound, Principal

    return Inbound(principal=Principal("telegram", "1"), conversation=conversation,
                   text="egal", dedup_key="k1")


def test_a_finished_run_delivers_the_due_review(tmp_path) -> None:
    """⚠️ Der Test, der „Pflicht" ueberhaupt bedeutet.

    Ein Modul, das einen Bericht erzeugen KANN, ist ein Befehl, den man vergisst.
    Geprueft wird deshalb der Weg bis zur Zustellung — und die Konversation ist die, in
    der der Betreiber gerade selbst geschrieben hat.
    """
    from talos.eventlog import Event, new_run_id

    conductor, gesendet = _conductor(tmp_path)
    for _ in range(3):
        conductor.log.append(Event(new_run_id(), "exec", "exec.result",
                                   {"tool": "run_shell", "status": "FAILED",
                                    "detail": "No module named pytest"}))
    conductor._maybe_review(_inbound())
    assert gesendet and "run_shell" in gesendet[0][1]
    assert gesendet[0][0] == "chat-1"


def test_the_review_does_not_repeat_itself_within_the_interval(tmp_path) -> None:
    """Sonst haengt unter jedem Lauf derselbe Bericht — und wer ihn zweimal wegwischt,
    wischt ihn beim dritten Mal ungelesen weg."""
    from talos.eventlog import Event, new_run_id

    conductor, gesendet = _conductor(tmp_path)
    for _ in range(3):
        conductor.log.append(Event(new_run_id(), "exec", "exec.result",
                                   {"tool": "run_shell", "status": "FAILED", "detail": "x"}))
    conductor._maybe_review(_inbound())
    conductor._maybe_review(_inbound())
    assert len(gesendet) == 1


def test_the_review_stays_out_of_an_open_approval(tmp_path) -> None:
    """⚠️ Endete der Lauf mit einer Freigabefrage, ist die naechste Nachricht des
    Betreibers ein „ja". Ein Bericht dazwischen macht aus einer klaren Frage eine
    unklare — und ein „ja", das sich versehentlich auf den falschen Vorgang bezieht,
    ist genau die Sorte Unfall, gegen die der ganze Freigabeweg gebaut ist.
    """
    from talos.eventlog import Event, new_run_id
    from talos.policy import ToolRequest

    conductor, gesendet = _conductor(tmp_path)
    for _ in range(3):
        conductor.log.append(Event(new_run_id(), "exec", "exec.result",
                                   {"tool": "run_shell", "status": "FAILED", "detail": "x"}))
    conductor.approvals.park(
        "chat-1", ToolRequest("write_file", {"path": "/tmp/a"}, ("/tmp/a",)), "darf ich?",
    )
    conductor._maybe_review(_inbound())
    assert gesendet == []


def test_a_quiet_machine_is_never_told_it_is_quiet(tmp_path) -> None:
    """Ein taeglicher Bericht „alles in Ordnung" ist genau die Gewoehnung, gegen die der
    zweite Befund des Reviews anschreibt."""
    conductor, gesendet = _conductor(tmp_path)
    conductor._maybe_review(_inbound())
    assert gesendet == []


def test_a_broken_gap_source_costs_one_finding_and_not_the_report(tmp_path) -> None:
    """Fail-open, aber nur so weit wie noetig: faellt der Doktor aus, fehlt der
    Luecken-Befund — die Fehlschlaege aus dem Protokoll stehen trotzdem im Bericht.

    Ein „alles oder nichts" waere hier die schlechtere Wahl: der Betreiber verloere
    wegen einer Nebensache die Befunde, die ihn wirklich etwas kosten.
    """
    from talos.eventlog import Event, new_run_id

    def kaputt():
        raise RuntimeError("doctor am Boden")

    conductor, gesendet = _conductor(tmp_path)
    object.__setattr__(conductor, "capability_gaps", kaputt)   # das Feld ist frozen
    for _ in range(3):
        conductor.log.append(Event(new_run_id(), "exec", "exec.result",
                                   {"tool": "run_shell", "status": "FAILED", "detail": "x"}))
    conductor._maybe_review(_inbound())
    assert gesendet and "run_shell" in gesendet[0][1]


# --- Die Haelfte, die nicht verhandelbar ist -------------------------------------------
def test_the_review_cannot_reach_a_verdict_or_write_a_rule() -> None:
    """⚠️ Der eigentliche Test dieser Datei.

    Selbstverbesserung, die sich selbst anwenden darf, ist keine Verbesserung mehr,
    sondern ein zweiter Erlaubnisweg. Das prueft man am Quelltext, nicht an der Absicht:
    ein Modul ohne Zugriff auf Urteil und Rechteliste kann sie auch nicht aendern.
    """
    baum = ast.parse(Path(review.__file__).read_text(encoding="utf-8"))
    geholt = {k.module or "" for k in ast.walk(baum) if isinstance(k, ast.ImportFrom)} | {
        a.name for k in ast.walk(baum) if isinstance(k, ast.Import) for a in k.names
    }
    verboten = {n for n in geholt if any(w in n for w in ("policy", "standing", "capability"))}
    assert not verboten, f"review darf nichts erteilen, importiert aber {verboten}"


def test_no_public_name_here_promises_a_permission() -> None:
    verdaechtig = {"allow", "permit", "grant", "approve", "apply", "enable"}
    for name in review.__all__:
        assert not any(wort in name.lower() for wort in verdaechtig), name
