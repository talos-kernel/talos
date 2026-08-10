"""Eine Korrektur erreicht den LAUFENDEN Auftrag, statt hinter ihm zu warten.

Bis hierher galt: was waehrend eines Laufs hereinkommt, wird eingereiht. Das ist bei
einem zweiten, unabhaengigen Auftrag richtig — und falsch bei dem Satz, der am
haeufigsten kommt: „nein, das andere Verzeichnis". Die Korrektur wurde geschrieben,
*weil* der Lauf gerade laeuft, und sie hinter ihm anzustellen heisst, ihn erst falsch
zuende laufen zu lassen und dann noch einmal von vorn.

Was das NICHT ist
-----------------
Kein Eingriff in einen laufenden Modellaufruf — der ist ein blockierender Subprozess und
bleibt unantastbar. Die Korrektur wird zwischen zwei Schritten der Agentenschleife
eingelegt, also an genau der Stelle, an der der naechste Zug ohnehin aus der Historie
gebildet wird.

Kein zusaetzliches Recht. Die Korrektur ist ein Zug DESSELBEN Sprechers, der den Lauf
gestartet hat. Jeder Werkzeugaufruf danach geht durch denselben Kernel wie vorher; eine
abgelehnte Handlung wird durch eine nachgeschobene Nachricht nicht erlaubt. Wer das
umgehen wollte, muesste den Kernel umgehen, nicht dieses Postfach.

Kein Weg fuer Dritte. `offer()` nimmt nur an, was von **derselben Kennung** in
**derselben Unterhaltung** kommt wie der laufende Auftrag. Beides, nicht eines: ein
zweiter erlaubter Mensch darf den Lauf eines anderen nicht lenken, und dieselbe Person
in einem anderen Chat redet ueber etwas anderes.

Und ausdruecklich kein Weg an einer offenen Rueckfrage vorbei. Steht eine Freigabe an,
ist die naechste Nachricht deren Antwort — sie darf nicht als Kurskorrektur in einen
Lauf rutschen, waehrend der Betreiber glaubt, „ja" zu einer Handlung gesagt zu haben.
Diese Sperre setzt der Conductor, der als Einziger weiss, ob eine Rueckfrage offen ist;
hier steht die zweite, unabhaengige Schranke (Kennung und Unterhaltung), damit ein
Fehler dort nicht schon alles ist.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

# Mehr als das staut sich nicht an: wer viermal nachschiebt, ohne dass ein Schritt
# vergeht, meint einen neuen Auftrag und keine Korrektur. Der Rest wird abgelehnt und
# der Sprecher erfaehrt es — stillschweigendes Verwerfen waere hier das Schlimmste.
MAX_PENDING = 4
MAX_LENGTH = 4000


@dataclass(frozen=True)
class Correction:
    """Ein Zug, der einen bereits laufenden Auftrag erreicht hat."""

    text: str
    principal: str
    conversation: str

    def as_turn(self) -> str:
        """Der Wortlaut, wie er in die Historie geht.

        ⚠️ Ausdruecklich als *Nachricht des Betreibers* markiert und nicht als Anweisung
        von aussen: die Historie ist der Ort, an dem fremder Text am teuersten waere.
        Der Rahmen sagt, woher sie kommt und was sie nicht ist — dass sie nichts
        erlaubt, entscheidet ohnehin der Kernel und nicht dieser Satz.
        """
        return (
            "[correction from the operator, while this run was in progress — same person, "
            f"same conversation, no additional rights]\n{self.text}"
        )


class Redirect:
    """Ein Postfach fuer den einen laufenden Auftrag. Von zwei Threads benutzt.

    Der Poll-Thread legt ab (`offer`), die Agentenschleife nimmt (`take`). Beides unter
    demselben Schloss — der Worker hat genau einen Platz, aber `offer` kommt aus einem
    anderen Thread als `take`, und eine Liste ohne Schloss verliert dabei Eintraege.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run: tuple[str, str] | None = None      # (principal, conversation)
        self._pending: list[Correction] = []

    def open(self, principal: str, conversation: str) -> None:
        """Ein Lauf erklaert sich fuer lenkbar. Verwirft Reste eines frueheren Laufs."""
        with self._lock:
            self._run = (principal, conversation)
            self._pending = []

    def close(self) -> None:
        """Kein Lauf mehr. Danach wird nichts mehr angenommen."""
        with self._lock:
            self._run = None
            self._pending = []

    def is_open(self) -> bool:
        with self._lock:
            return self._run is not None

    def offer(self, principal: str, conversation: str, text: str) -> bool:
        """True = die Nachricht lenkt den laufenden Auftrag; False = sie tut es nicht.

        False heisst NICHT „verworfen": der Aufrufer reiht dann ein wie bisher. Diese
        Funktion entscheidet nur, ob umgelenkt wird — sie wirft nie etwas weg.
        """
        text = text.strip()
        if not text or len(text) > MAX_LENGTH:
            return False
        with self._lock:
            if self._run is None:
                return False
            # ⚠️ BEIDES muss stimmen. Nur die Kennung zu pruefen liesse dieselbe Person
            # aus einem anderen Chat in einen Lauf hineinreden, ueber den dort nie die
            # Rede war; nur die Unterhaltung zu pruefen liesse einen zweiten erlaubten
            # Menschen den Lauf eines anderen lenken.
            if self._run != (principal, conversation):
                return False
            if len(self._pending) >= MAX_PENDING:
                return False
            self._pending.append(Correction(text, principal, conversation))
            return True

    def take(self) -> tuple[Correction, ...]:
        """Holt alles Wartende und leert das Postfach. Leer, wenn nichts da ist."""
        with self._lock:
            wartend = tuple(self._pending)
            self._pending = []
            return wartend
