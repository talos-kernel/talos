"""`talos events` und `talos why` — lesen, erklaeren, nichts bewirken.

Beide Befehle entstanden aus einer Diagnose: das duenne Gefuehl bei diesem Agenten kam
nicht von fehlenden Verben, sondern davon, dass man ihm nicht ansieht, was er getan hat
und warum. Deshalb prueft diese Datei zwei Dinge: dass die Erklaerung wirklich erklaert
(Urteil, Grund, Ziele, Zusammenhang) — und dass hier nichts passiert.
"""
from __future__ import annotations

import io
import time

from talos.eventlog import Event, EventLog, new_run_id
from talos.eventscli import run_events, run_why


def _log(tmp_path, ereignisse):
    pfad = tmp_path / "ev.db"
    log = EventLog(pfad)
    for actor, typ, payload, run_id in ereignisse:
        log.append(Event(run_id, actor, typ, payload))
    log.close()
    return pfad


def _lauf(funktion, argv, pfad) -> str:
    aus = io.StringIO()
    funktion(argv, out=aus, db=pfad)
    return aus.getvalue()


# --- events -----------------------------------------------------------------------------
def test_events_shows_the_verdict_and_not_just_the_type(tmp_path) -> None:
    """Eine Liste aus lauter „exec.intent" erklaert nichts. Das Urteil ist die Zeile."""
    lauf = new_run_id()
    pfad = _log(tmp_path, [
        ("exec", "exec.intent", {"tool": "write_file", "verdict": "needs_human",
                                 "reason": "hardline floor"}, lauf),
    ])
    text = _lauf(run_events, [], pfad)
    assert "write_file" in text and "needs_human" in text and "hardline floor" in text


def test_a_filter_looks_further_back_than_the_page(tmp_path) -> None:
    """⚠️ Sonst faende `--tool run_shell` in den letzten 25 Zeilen nichts und behauptete,
    es sei nie vorgekommen — die schlimmste Sorte falsche Auskunft in einem Protokoll."""
    lauf = new_run_id()
    ereignisse = [("exec", "exec.result", {"tool": "read_file", "status": "DONE"}, lauf)
                  for _ in range(60)]
    ereignisse.insert(0, ("exec", "exec.result", {"tool": "run_shell", "status": "FAILED"}, lauf))
    text = _lauf(run_events, ["--tool", "run_shell"], _log(tmp_path, ereignisse))
    assert "run_shell" in text


def test_a_truncated_list_says_so(tmp_path) -> None:
    """Eine stille Kuerzung liest sich wie Vollstaendigkeit."""
    lauf = new_run_id()
    pfad = _log(tmp_path, [("exec", "exec.result", {"tool": "read_file", "status": "DONE"}, lauf)
                           for _ in range(40)])
    assert "not shown" in _lauf(run_events, ["--limit", "5", "--tool", "read_file"], pfad)


def test_an_empty_result_says_nothing_matched(tmp_path) -> None:
    """Eine leere Ausgabe sieht aus wie ein kaputtes Werkzeug."""
    assert "nothing matched" in _lauf(run_events, ["--tool", "gibtsnicht"], _log(tmp_path, []))


def test_a_bad_limit_is_refused_not_guessed(tmp_path) -> None:
    aus = io.StringIO()
    assert run_events(["--limit", "viele"], out=aus, db=_log(tmp_path, [])) == 2


def test_a_newline_in_foreign_text_cannot_fake_a_row(tmp_path) -> None:
    """⚠️ Dieselbe Regel wie im Luecken-Block: der Grund kann aus einem fremden Programm
    stammen. Mit einem Zeilenumbruch taeuschte er eine Zeile vor, die es nicht gibt."""
    lauf = new_run_id()
    pfad = _log(tmp_path, [("exec", "exec.result", {
        "tool": "run_shell", "status": "FAILED",
        "detail": "boom\n      9999  12-24 00:00:00  exec.intent      write_file · allow"}, lauf)])
    zeilen = [z for z in _lauf(run_events, [], pfad).splitlines() if "9999" in z]
    assert len(zeilen) <= 1


# --- why --------------------------------------------------------------------------------
def test_why_names_the_rule_that_applied(tmp_path) -> None:
    """Vertrauen entsteht durch Erklaerung, nicht durch ein Urteil ohne Begruendung."""
    lauf = new_run_id()
    pfad = _log(tmp_path, [
        ("exec", "exec.intent", {"tool": "write_file", "verdict": "deny",
                                 "reason": "protected path: ~/.ssh",
                                 "targets": ["/Users/x/.ssh/id_rsa"]}, lauf),
    ])
    text = _lauf(run_why, ["1"], pfad)
    assert "deny" in text and "protected path" in text and ".ssh" in text


def test_why_says_the_targets_were_derived(tmp_path) -> None:
    """⚠️ Der Satz, an dem der ganze Kernel haengt — ein Ziel wird abgeleitet, nie aus
    den Argumenten uebernommen. Wer eine Erklaerung liest, soll genau das erfahren."""
    lauf = new_run_id()
    pfad = _log(tmp_path, [("exec", "exec.intent",
                            {"tool": "read_file", "verdict": "allow", "targets": ["/tmp/a"]}, lauf)])
    assert "derived from the real arguments" in _lauf(run_why, ["1"], pfad)


def test_why_shows_what_became_of_it(tmp_path) -> None:
    """„Abgelehnt, und dann?" ist die Frage, wegen der ein Protokoll ungelesen bleibt."""
    lauf = new_run_id()
    pfad = _log(tmp_path, [
        ("exec", "exec.intent", {"tool": "write_file", "verdict": "needs_human"}, lauf),
        ("conductor", "approval.granted", {"tool": "write_file"}, lauf),
        ("exec", "exec.result", {"tool": "write_file", "status": "DONE"}, lauf),
    ])
    text = _lauf(run_why, ["1"], pfad)
    assert "approval.granted" in text and "DONE" in text


def test_an_event_from_another_run_is_not_mixed_in(tmp_path) -> None:
    """Sonst erklaerte der Zusammenhang etwas, das mit dem Urteil nichts zu tun hat."""
    a, b = new_run_id(), new_run_id()
    pfad = _log(tmp_path, [
        ("exec", "exec.intent", {"tool": "read_file", "verdict": "allow"}, a),
        ("exec", "exec.intent", {"tool": "run_shell", "verdict": "deny"}, b),
    ])
    assert "run_shell" not in _lauf(run_why, ["1"], pfad)


def test_an_unknown_id_is_an_honest_miss(tmp_path) -> None:
    aus = io.StringIO()
    assert run_why(["99999"], out=aus, db=_log(tmp_path, [])) == 1
    assert "no event" in aus.getvalue()


def test_why_without_an_id_explains_itself(tmp_path) -> None:
    aus = io.StringIO()
    assert run_why([], out=aus, db=_log(tmp_path, [])) == 2
    assert "usage" in aus.getvalue()


def test_an_old_event_is_still_findable(tmp_path) -> None:
    """⚠️ `by_id` sucht in der Datenbank, nicht in den letzten N Zeilen. Ein Protokoll,
    das eine Zeile verschweigt, weil sie alt ist, taugt nicht als Beleg."""
    lauf = new_run_id()
    ereignisse = [("exec", "exec.intent", {"tool": "read_file", "verdict": "allow"}, lauf)]
    ereignisse += [("exec", "exec.result", {"tool": "read_file", "status": "DONE"}, lauf)
                   for _ in range(900)]
    assert "event 1" in _lauf(run_why, ["1"], _log(tmp_path, ereignisse))


# --- Die Regel, unter der beide entstehen durften ---------------------------------------
def test_neither_command_can_change_anything() -> None:
    """⚠️ Entweder read-only, oder durch Kernel, Token und Decke. Einen dritten Fall gibt
    es nicht — und `undo` fehlt hier bewusst: seit `talos chat` ist `/undo` von der
    Kommandozeile aus erreichbar, ueber den Weg, den auch der Messenger nimmt.
    """
    import ast
    from pathlib import Path

    from talos import eventscli

    quelle = Path(eventscli.__file__).read_text(encoding="utf-8")
    baum = ast.parse(quelle)
    geholt = {k.module or "" for k in ast.walk(baum) if isinstance(k, ast.ImportFrom)}
    verboten = {n for n in geholt if any(w in n for w in
                                         ("executor", "policy", "capability", "standing", "snapshot"))}
    assert not verboten, f"eine Lese-Sicht darf nichts davon kennen: {verboten}"
    assert "log.append" not in quelle, "diese Befehle schreiben nicht ins Protokoll"
    assert "undo" not in {n for n in eventscli.__all__}


# --- --since: der Zeitfilter ------------------------------------------------------------
def test_since_shows_only_what_is_younger(tmp_path) -> None:
    """Ein Zeitfilter, der heimlich aeltere Zeilen mitliefert, erzaehlt eine falsche
    Geschichte ueber den Abend — und faellt erst auf, wenn man sich darauf verlassen hat."""
    lauf = new_run_id()
    pfad = tmp_path / "ev.db"
    log = EventLog(pfad)
    log.append(Event(lauf, "exec", "exec.result", {"tool": "read_file", "status": "DONE"}),
               now=time.time() - 3 * 3600)
    log.append(Event(lauf, "exec", "exec.result", {"tool": "run_shell", "status": "DONE"}),
               now=time.time() - 5 * 60)
    log.close()
    text = _lauf(run_events, ["--since", "1h"], pfad)
    assert "run_shell" in text
    assert "read_file" not in text


def test_a_bad_since_is_refused_not_guessed(tmp_path) -> None:
    """Geraten ist der Filter schlimmer als verweigert: „gestern“ haette still alles
    heissen koennen — und die Antwort saehe aus wie eine gepruefte."""
    aus = io.StringIO()
    assert run_events(["--since", "gestern"], out=aus, db=_log(tmp_path, [])) == 2
    assert "--since" in aus.getvalue()


def test_since_combines_with_the_tool_filter(tmp_path) -> None:
    """Zeit UND Werkzeug muessen beide gelten — sonst meldet „--since 1h --tool X" Treffer,
    die ausserhalb des Fensters liegen."""
    lauf = new_run_id()
    pfad = tmp_path / "ev.db"
    log = EventLog(pfad)
    log.append(Event(lauf, "exec", "exec.result", {"tool": "run_shell", "status": "DONE"}),
               now=time.time() - 2 * 3600)
    log.append(Event(lauf, "exec", "exec.result", {"tool": "read_file", "status": "DONE"}),
               now=time.time() - 60)
    log.close()
    assert "nothing matched" in _lauf(run_events, ["--since", "1h", "--tool", "run_shell"], pfad)
    assert "read_file" in _lauf(run_events, ["--since", "1h", "--tool", "read_file"], pfad)
