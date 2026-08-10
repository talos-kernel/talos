"""`talos chat` — eine Sitzung im Terminal, ohne einen zweiten Weg hinein.

Der strittige Punkt, an dem sich zwei beratende Agenten uneinig waren, ist der Kern
dieser Datei: darf man in einer CLI-Sitzung freigeben? Die Antwort hier lautet „nur wenn
ein Mensch nachweislich davorsitzt" — und nachweisbar heisst gemessen, nicht behauptet.
Alles andere (Kanalname, Kennung, Kommandos) ist absichtlich dasselbe wie bei `ask`; die
Tests halten das fest, damit aus „auch erreichbar" nie „auch erlaubt" wird.
"""
from __future__ import annotations

import io

from talos import chatcli
from talos.askcli import CHANNEL_NAME
from talos.channel import Trust


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


# --- Die Decke haengt am Terminal, nicht am Befehlsnamen --------------------------------
def test_a_human_at_a_terminal_counts_as_attended() -> None:
    """Die Decke ist fuer den Fall gebaut, dass NIEMAND da ist. Ist jemand da, ist sie am
    falschen Platz — sonst koennte `chat` nie etwas schreiben, und der Betreiber muesste
    fuer jede Freigabe doch wieder in den Messenger."""
    assert chatcli.attended(stdin=_Tty(), stdout=_Tty()) is True


def test_a_pipe_is_never_a_human() -> None:
    """`talos chat < auftraege.txt` in einem Cron sieht von innen aus wie ein Mensch.
    Genau deshalb wird gemessen statt geglaubt."""
    assert chatcli.attended(stdin=_Pipe(), stdout=_Tty()) is False


def test_a_redirected_output_is_not_attended_either() -> None:
    """⚠️ BEIDE Seiten. Nur `stdin` zu pruefen liesse `talos chat > log.txt` als
    beaufsichtigt durchgehen — dort sieht niemand die Freigabefrage, die er
    beantworten soll."""
    assert chatcli.attended(stdin=_Tty(), stdout=_Pipe()) is False


def test_something_that_is_not_a_stream_is_not_a_terminal() -> None:
    """Fail-closed: im Zweifel bleibt die Decke stehen."""
    assert chatcli.attended(stdin=object(), stdout=object()) is False


# --- Kein zweiter Weg hinein ------------------------------------------------------------
def test_the_session_speaks_under_the_same_identity_as_ask() -> None:
    """⚠️ Derselbe Kanalname und dieselbe Kennung — sonst gaebe es zwei Eintraege in der
    Allowlist fuer dieselbe Person, und einer davon wuerde irgendwann vergessen."""
    kanal = chatcli.ChatChannel(uid=1000)
    assert kanal.name == CHANNEL_NAME
    assert str(kanal.principal) == f"{CHANNEL_NAME}:1000"
    assert kanal.trust is Trust.FULL


def test_two_identical_lines_are_both_delivered() -> None:
    """Zweimal dieselbe Frage ist im Gespraech normal. Mit einem festen Dedup-Schluessel
    verschwaende die zweite lautlos — und der Betreiber saehe einen Haenger."""
    kanal = chatcli.ChatChannel(uid=1000)
    kanal.feed("wie spaet"); kanal.feed("wie spaet")
    eingaben = kanal.poll()
    assert len(eingaben) == 2
    assert eingaben[0].dedup_key != eingaben[1].dedup_key


def test_polling_twice_does_not_repeat_a_line() -> None:
    kanal = chatcli.ChatChannel(uid=1000)
    kanal.feed("hallo")
    assert len(kanal.poll()) == 1 and kanal.poll() == []


def test_an_approval_question_is_never_swallowed() -> None:
    """Knoepfe gibt es im Terminal nicht. Bliebe die Frage deshalb stumm, saehe sie aus
    wie ein Haenger — und der Betreiber tippt seine Antwort nie."""
    aus = io.StringIO()
    kanal = chatcli.ChatChannel(uid=1000, out=aus)

    class Strukturiert:
        text = "May I write /tmp/a? yes/no"

    kanal.send_structured("cli:1000", Strukturiert())
    assert "May I write" in aus.getvalue()


# --- Die Schleife -----------------------------------------------------------------------
class _Registry:
    def __init__(self, kanal) -> None:
        self.kanal = kanal

    def poll_all(self):
        return self.kanal.poll()


class _Conductor:
    def __init__(self) -> None:
        self.gesehen: list[str] = []

    def handle(self, update) -> bool:
        self.gesehen.append(update.text)
        return True


def _lauf(zeilen, *, unattended=None):
    kanal = chatcli.ChatChannel(uid=1000, out=io.StringIO())
    conductor = _Conductor()
    eingabe = iter(zeilen)
    chatcli.interactive(kanal, _Registry(kanal), conductor,
                        unattended=unattended,
                        reader=lambda _p: next(eingabe),
                        out=io.StringIO())
    return conductor.gesehen


def test_each_line_becomes_one_turn() -> None:
    assert _lauf(["erste", "zweite", "exit"]) == ["erste", "zweite"]


def test_end_of_input_ends_the_session() -> None:
    """Ctrl-D beendet, ohne dass jemand ein Wort dafuer kennen muss."""
    def eof(_prompt):
        raise EOFError

    kanal = chatcli.ChatChannel(uid=1000, out=io.StringIO())
    conductor = _Conductor()
    assert chatcli.interactive(kanal, _Registry(kanal), conductor,
                               reader=eof, out=io.StringIO()) == 0
    assert conductor.gesehen == []


def test_an_empty_line_is_not_a_turn() -> None:
    """Sonst kostet ein versehentliches Enter einen Denkzug und echte Token."""
    assert _lauf(["", "   ", "etwas", "exit"]) == ["etwas"]


def test_the_ceiling_wraps_every_turn_when_nobody_is_there() -> None:
    """⚠️ Der Test, der die Entscheidung traegt: ohne Terminal laeuft JEDER Zug unter
    der Decke — nicht nur der erste, und nicht nur der schreibende."""
    class Decke:
        def __init__(self) -> None:
            self.zuege = 0

        def active(self):
            decke = self

            class _Ctx:
                def __enter__(self_inner):
                    decke.zuege += 1
                    return None

                def __exit__(self_inner, *_):
                    return False
            return _Ctx()

    decke = Decke()
    assert _lauf(["eins", "zwei", "exit"], unattended=decke) == ["eins", "zwei"]
    assert decke.zuege == 2


# --- Das Banner sagt vorab, woran man ist -----------------------------------------------
def test_the_banner_says_whether_an_approval_is_even_possible() -> None:
    """Sonst raet der Betreiber, warum ein NEEDS_HUMAN gleich als DENY zurueckkommt.
    Ein Agent wirkt nicht nackt, weil ihm Verben fehlen, sondern weil man ihm nicht
    ansieht, woran man ist."""
    text = chatcli.banner(agent="Talos", version="0.7.0", model="anthropic/x",
                          autonomy="3", uid=1000, attended_now=False)
    assert "NEEDS_HUMAN becomes DENY" in text
    an = chatcli.banner(agent="Talos", version="0.7.0", model="anthropic/x",
                        autonomy="3", uid=1000, attended_now=True)
    assert "approvals possible" in an


def test_the_banner_names_model_and_autonomy() -> None:
    text = chatcli.banner(agent="Talos", version="0.7.0", model="claude-cli/fable-5",
                          autonomy="2", uid=1000, attended_now=True)
    assert "Talos" in text and "fable-5" in text and "autonomy 2" in text


def test_leaving_words_are_plain_and_not_commands() -> None:
    """⚠️ Ohne Schraegstrich, mit Absicht: mit Schraegstrich waere es ein Kommando, und
    Kommandos gehoeren dem Conductor — es gaebe `/exit` dann auch im Messenger, wo es
    nichts zu beenden gibt."""
    assert chatcli.should_quit("exit") and chatcli.should_quit(" QUIT ")
    assert not chatcli.should_quit("/exit")
    assert not chatcli.should_quit("beende den prozess")
