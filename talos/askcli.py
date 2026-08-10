"""`talos ask` — ein Zug von der Kommandozeile, ohne Chat.

Der Anlass: eine laufende Installation war von aussen nur ueber Telegram erreichbar.
Vergleichbare Agenten haben laengst einen Abfragebefehl; Talos hatte nichts — kein Skript,
kein Cron, kein `ssh host 'talos ask …'` konnte ihn etwas fragen.

Was hier NICHT gebaut wird, ist ein zweiter Weg in den Agenten hinein. Es ist derselbe:
dieselbe Frage, derselbe Conductor, derselbe Kernel, dasselbe Ereignisprotokoll. Die
Kommandozeile ist ein **Kanal wie jeder andere** — sie erfuellt dasselbe Protokoll
(`name`, `trust`, `poll`, `send`, `send_structured`) und hat kein einziges Sonderrecht.

Drei Entscheidungen, die das tragen:

⚠️ **Wer hier tippt, muss in der Allowlist stehen.** Die Kennung ist `cli:<uid>`, und sie
gilt so wenig von selbst wie eine Telegram-Nummer. Wer sie automatisch zuliesse, haette
einen Eingang gebaut, den niemand freigegeben hat — und zwar den mit Shell-Rechten.
Eintragen: `TALOS_ALLOWED_PRINCIPALS=telegram:123,cli:1000`.

⚠️ **Immer unter der unbeaufsichtigten Decke.** Ein Einzeiler wartet auf nichts: es gibt
keine Knoepfe, keinen Rueckkanal, niemanden, der eine Freigabe erteilt. `NEEDS_HUMAN`
wird deshalb zu `DENY`, mit klarer Ansage — dieselbe Decke wie im Zeitplan-Lauf
(`schedule.UnattendedCeiling`). Wer freigeben will, tut es im Chat. Alles andere waere
eine Freigabe, die sich der Aufrufer selbst erteilt.

⚠️ **Aus der Sandbox heraus verweigert.** Der Agent hat eine Shell. Koennte er darin
`talos ask` starten, haette er einen Weg, sich selbst Auftraege zu geben — ohne Kanal,
ohne fremde Kennung, ohne dass jemand es liest. `sandbox.MARKER` steht in der reduzierten
Umgebung jedes Sandbox-Laufs; ist er gesetzt, bricht dieser Befehl ab. Fail-closed:
lieber ein abgelehnter Aufruf zu viel als ein Selbstauftrag zu wenig.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

from .channel import Inbound, Principal, StructuredMessage, Trust

CHANNEL_NAME = "cli"
# Wie lange auf die Antwort gewartet wird, bevor abgebrochen wird. Ein Denkzug samt
# Werkzeugen darf dauern; ein Skript, das ewig haengt, darf es nicht.
DEFAULT_TIMEOUT_S = 600
POLL_INTERVAL_S = 0.2

NOT_ALLOWED = (
    "cli:{uid} is not in TALOS_ALLOWED_PRINCIPALS.\n"
    "  The command line is a channel like any other — it grants nothing by itself.\n"
    "  Add it (comma separated) and restart nothing; the next run reads it."
)
IN_SANDBOX = (
    "refused: this is running inside the agent's own sandbox.\n"
    "  `ask` would let the agent give itself orders, with no channel and no reader."
)


@dataclass
class CliChannel:
    """Ein Kanal, der genau eine Frage liefert und die Antwort auf stdout schreibt.

    `poll()` gibt die Frage **einmal** zurueck und danach nie wieder — der Zug soll
    einmal laufen, nicht in einer Schleife dieselbe Frage beantworten.
    """

    question: str
    uid: int
    out: object = None
    _delivered: bool = field(default=False, init=False)
    answered: bool = field(default=False, init=False)

    name = CHANNEL_NAME

    @property
    def trust(self) -> Trust:
        """`FULL` — und trotzdem kann hier niemand freigeben.

        Die Stufe sagt, wessen Stimme das ist: die des Betreibers, der an dieser Maschine
        eine Shell hat. Ob jemand DA ist, um eine Freigabe zu erteilen, ist eine andere
        Frage — die beantwortet die unbeaufsichtigte Decke, nicht die Vertrauensstufe.
        Beides zu vermischen hiesse, eine Rechtefrage mit einer Anwesenheitsfrage zu
        beantworten.
        """
        return Trust.FULL

    @property
    def principal(self) -> Principal:
        return Principal(CHANNEL_NAME, str(self.uid))

    @property
    def conversation(self) -> str:
        return f"{CHANNEL_NAME}:{self.uid}"

    def poll(self) -> list[Inbound]:
        if self._delivered:
            return []
        self._delivered = True
        return [Inbound(
            principal=self.principal,
            conversation=self.conversation,
            text=self.question,
            # Ein Lauf, eine Frage: der Schluessel muss eindeutig sein, damit zwei
            # Aufrufe nacheinander nicht als Doublette durchfallen.
            dedup_key=f"{CHANNEL_NAME}:{self.uid}:{os.getpid()}",
        )]

    def send(self, conversation: str, text: str) -> None:
        schreiben = (self.out or sys.stdout).write
        schreiben(text.rstrip("\n") + "\n")
        self.answered = True

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        """Knoepfe gibt es hier nicht. Der Text kommt trotzdem an — eine Rueckfrage, die
        stumm bliebe, saehe aus wie ein Haenger."""
        self.send(conversation, getattr(message, "text", "") or str(message))


def refuse_in_sandbox(env: dict | None = None) -> str:
    """Leer, wenn der Aufruf von aussen kommt; sonst der Grund für die Ablehnung."""
    from .sandbox import MARKER

    umgebung = os.environ if env is None else env
    return IN_SANDBOX if umgebung.get(MARKER) else ""


def check_identity(allowed: frozenset, uid: int) -> str:
    """Leer, wenn diese Kennung befehlen darf; sonst der Grund.

    ⚠️ Diese Funktion kennt KEINE Ausnahme fuer die leere Liste — und das ist Absicht.
    Der Erstlauf-Fall wird an genau einer Stelle entschieden, in `load_config`: ohne
    Messenger und mit leerer Liste traegt sie den lokalen CLI-Aufrufer ein, sodass hier
    (und im Kernel) eine ganz normale, gesetzte Liste ankommt. Zwei Stellen mit derselben
    Lockerung waeren zwei Wahrheiten darueber, wer befehlen darf.
    """
    wer = Principal(CHANNEL_NAME, str(uid))
    return "" if wer in allowed else NOT_ALLOWED.format(uid=uid)


def wait_for_answer(channel: CliChannel, worker, *, timeout_s: int = DEFAULT_TIMEOUT_S,
                    now=time.monotonic, sleep=time.sleep) -> bool:
    """Wartet, bis der Zug beantwortet ist. True = fertig, False = Zeit abgelaufen.

    Gewartet wird auf die ANTWORT, nicht auf einen leeren Worker: ein Lauf, der noch
    aufraeumt, hat seine Antwort laengst geschickt, und ein Skript soll dann weiterlaufen.
    """
    ende = now() + timeout_s
    while now() < ende:
        if channel.answered:
            return True
        sleep(POLL_INTERVAL_S)
    return False


__all__ = [
    "CHANNEL_NAME",
    "DEFAULT_TIMEOUT_S",
    "IN_SANDBOX",
    "NOT_ALLOWED",
    "CliChannel",
    "check_identity",
    "refuse_in_sandbox",
    "wait_for_answer",
]
