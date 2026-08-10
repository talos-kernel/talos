"""`talos chat` — eine Sitzung im Terminal, mit allem was der Chat auch hat.

`talos ask` beantwortet eine Frage und geht. Fuer alles, was mehr als einen Zug braucht,
musste der Betreiber bisher nach Telegram wechseln — auf derselben Maschine, an der er
gerade sitzt. Das ist der Grund fuer dieses Modul, und es ist der einzige.

Was hier NICHT entsteht, ist ein zweiter Weg in den Agenten. Es ist derselbe Kanalname
(`cli`), dieselbe Kennung (`cli:<uid>`), derselbe Conductor, derselbe Kernel, dasselbe
Protokoll. Die Schleife unten ist Zeile fuer Zeile die des Telegram-Betriebs, nur dass
die Nachrichten aus `stdin` kommen statt aus `getUpdates`.

⚠️ **Die unbeaufsichtigte Decke haengt am TTY, nicht am Befehlsnamen.**

Das war die strittige Stelle. `ask` laeuft immer unter der Decke, weil ein Einzeiler auf
nichts wartet: es gibt keinen Rueckkanal und niemanden, der eine Freigabe erteilt.
Dieselbe Regel auf eine interaktive Sitzung anzuwenden waere falsch — dann koennte `chat`
nie etwas schreiben, und der Betreiber muesste fuer jede Freigabe doch wieder in den
Messenger. Sie wegzulassen waere ebenfalls falsch: `talos chat < auftraege.txt` in einem
Cron sieht von innen genauso aus wie ein Mensch am Terminal.

Deshalb wird nicht behauptet, sondern gemessen: **`stdin` UND `stdout` muessen ein
echtes Terminal sein.** Eine Pipe, eine Umleitung, ein Cron-Lauf haben keins — dort
bleibt die Decke, `NEEDS_HUMAN` wird `DENY`, mit Ansage. Die Decke ist fuer den Fall
gebaut, dass niemand da ist; ist jemand da, ist sie am falschen Platz.

⚠️ **Aus der Sandbox heraus verweigert**, aus demselben Grund wie `ask`: der Agent hat
eine Shell. Koennte er darin `talos chat` starten, gaebe er sich selbst Auftraege — ohne
Kanal, ohne fremde Kennung, ohne dass jemand mitliest.

⚠️ **Kein eigener Vorrat an Befehlen.** `/help`, `/policy`, `/undo`, `/autonomy` und der
Rest sind dieselben wie im Messenger und laufen durch dieselbe `CommandCenter`. Ein
zweiter Satz Befehle, der nur hier gilt, waere genau die Doppelung, die dieses Projekt
an anderer Stelle teuer bezahlt hat.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from .askcli import CHANNEL_NAME, IN_SANDBOX, NOT_ALLOWED, check_identity, refuse_in_sandbox
from .channel import Inbound, Principal, StructuredMessage, Trust

__all__ = ["ChatChannel", "banner", "interactive", "read_line", "should_quit"]

PROMPT = "› "
# Woerter, die die Sitzung beenden. Bewusst OHNE Schraegstrich: mit Schraegstrich waere
# es ein Kommando, und Kommandos gehoeren dem Conductor — `/exit` muesste dort erfunden
# werden und gaebe es dann im Messenger auch, wo es nichts zu beenden gibt.
QUIT_WORDS = frozenset({"exit", "quit", "bye"})


@dataclass
class ChatChannel:
    """Ein Kanal, der aus der Tastatur liest und auf stdout schreibt.

    Gefuettert wird er von der Schleife (`feed`), nicht von einem Netzabruf — sonst
    braeuchte `poll()` einen blockierenden Lesevorgang, und ein blockierender Kanal
    haengt den ganzen Registry-Durchlauf auf.
    """

    uid: int
    out: object = None
    _queue: list[str] = field(default_factory=list, init=False)
    _seq: int = field(default=0, init=False)

    name = CHANNEL_NAME

    @property
    def trust(self) -> Trust:
        """`FULL` — die Stimme des Betreibers, der an dieser Maschine eine Shell hat.

        Dieselbe Stufe wie bei `ask`, und aus demselben Grund: die Stufe sagt, WESSEN
        Stimme das ist. Ob jemand da ist, um eine Freigabe zu erteilen, beantwortet die
        Decke — eine Rechtefrage und eine Anwesenheitsfrage sind zwei Fragen.
        """
        return Trust.FULL

    @property
    def principal(self) -> Principal:
        return Principal(CHANNEL_NAME, str(self.uid))

    @property
    def conversation(self) -> str:
        return f"{CHANNEL_NAME}:{self.uid}"

    def feed(self, text: str) -> None:
        self._queue.append(text)

    def poll(self) -> list[Inbound]:
        wartend, self._queue = self._queue, []
        eingaben = []
        for text in wartend:
            self._seq += 1
            eingaben.append(Inbound(
                principal=self.principal,
                conversation=self.conversation,
                text=text,
                # Fortlaufend je Sitzung: zweimal dieselbe Frage hintereinander ist im
                # Gespraech normal und darf nicht als Doublette verschluckt werden.
                dedup_key=f"{CHANNEL_NAME}:{self.uid}:{os.getpid()}:{self._seq}",
            ))
        return eingaben

    def send(self, conversation: str, text: str) -> None:
        schreiben = (self.out or sys.stdout).write
        schreiben(text.rstrip("\n") + "\n")

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        """Knoepfe gibt es hier nicht — der Text schon.

        Eine Freigabefrage, die stumm bliebe, saehe aus wie ein Haenger; der Betreiber
        tippt seine Antwort stattdessen als naechste Zeile, und der Conductor liest sie
        ueber genau denselben Weg wie ein „ja" im Messenger.
        """
        self.send(conversation, getattr(message, "text", "") or str(message))


def attended(stdin=None, stdout=None) -> bool:
    """Sitzt ein Mensch davor? Gemessen, nicht behauptet.

    ⚠️ BEIDE Seiten muessen ein Terminal sein. Nur `stdin` zu pruefen liesse
    `talos chat > protokoll.txt` als beaufsichtigt durchgehen — dort sieht niemand die
    Freigabefrage, die er beantworten soll.
    """
    ein = sys.stdin if stdin is None else stdin
    aus = sys.stdout if stdout is None else stdout
    try:
        return bool(ein.isatty() and aus.isatty())
    except (AttributeError, ValueError):
        # Ein Objekt ohne `isatty` oder ein geschlossener Datenstrom ist kein Terminal.
        # Fail-closed: im Zweifel bleibt die Decke stehen.
        return False


def should_quit(text: str) -> bool:
    return text.strip().lower() in QUIT_WORDS


def read_line(prompt: str = PROMPT, *, reader=None) -> str | None:
    """Eine Zeile — oder `None` bei EOF/Ctrl-C, also „Sitzung zu Ende"."""
    try:
        return (reader or input)(prompt)
    except (EOFError, KeyboardInterrupt):
        return None


def banner(*, agent: str, version: str, model: str, autonomy: str, uid: int,
           attended_now: bool) -> str:
    """Was diese Sitzung ist, in vier Zeilen.

    Ein Agent fuehlt sich nicht nackt an, weil ihm Verben fehlen, sondern weil man ihm
    nicht ansieht, woran man ist. Deshalb steht hier, womit er denkt, unter welcher
    Stufe er laeuft, als wer man spricht — und ob eine Freigabe in dieser Sitzung
    ueberhaupt moeglich waere. Das letzte ist das wichtigste: es erklaert vorab, warum
    ein `NEEDS_HUMAN` gleich als `DENY` zurueckkommt, statt es raten zu lassen.
    """
    decke = ("approvals possible — you are at a terminal" if attended_now
             else "unattended: no terminal, so NEEDS_HUMAN becomes DENY")
    return (
        f"{agent} {version}  ·  {model}  ·  autonomy {autonomy}\n"
        f"speaking as {CHANNEL_NAME}:{uid}  ·  {decke}\n"
        f"/help for commands, `exit` to leave\n"
    )


def interactive(channel: ChatChannel, registry, conductor, *, unattended=None,
                reader=None, out=None) -> int:
    """Die Schleife. Zeile lesen, einspeisen, abarbeiten — bis EOF.

    Sie ist absichtlich dieselbe wie im Telegram-Betrieb: einspeisen, `poll_all()`,
    `handle()`. Die Freigabe-Runde braucht deshalb hier keinen einzigen Sonderfall — die
    geparkte Anfrage liegt an der Konversation, und die naechste getippte Zeile ist die
    Antwort darauf, genau wie ein „ja" im Messenger.

    `unattended` ist gesetzt, wenn KEIN Terminal da ist. Dann laeuft jeder Zug unter der
    Decke — dieselbe wie im Zeitplan.
    """
    schreiben = (out or sys.stdout).write
    while True:
        zeile = read_line(reader=reader)
        if zeile is None:
            schreiben("\n")
            return 0
        if not zeile.strip():
            continue
        if should_quit(zeile):
            return 0
        channel.feed(zeile)
        for update in registry.poll_all():
            if unattended is None:
                conductor.handle(update)
            else:
                with unattended.active():
                    conductor.handle(update)
