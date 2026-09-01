"""Delegieren — und warum ein Untergebener weniger darf als der, der ihn schickt.

Die letzte der drei Luecken aus derselben Pruefung: parallele Subagenten, die
Teilaufgaben uebernehmen, ohne dass der Betreiber jeden Zwischenschritt steuert.

Der uebliche Bau gibt dem Untergebenen die Rechte seines Auftraggebers — er laeuft ja
„in dessen Auftrag". Genau da liegt der Fehler, und es ist derselbe wie beim Zeitplan
und beim Plan: **Faehigkeit darf nicht heissen, dass die Leine laenger wird.** Ein
delegierter Lauf ist nicht besser beaufsichtigt als ein zeitgesteuerter — im Gegenteil,
er entsteht aus dem Text eines Modells, nicht aus dem Tippen eines Menschen. Wer ihm
volle Rechte gibt, hat einen zweiten Erlaubnisweg gebaut, der nur anders heisst.

Deshalb: **ein delegierter Lauf darf ausschliesslich LESEN.** Keine Schreibarbeit, keine
Shell, nichts Freigabepflichtiges. Damit ist Delegieren fuer genau das gut, wofuer man es
wirklich braucht — nachsehen, suchen, zusammentragen — und oeffnet **keinen einzigen
neuen Wirkungsweg**. Was der Untergebene zurueckbringt, betritt den Lauf des
Auftraggebers als das, was es ist: Daten, keine Anweisung.

Die Decke ist die vierte im Haus (Autonomie-Regler, Kanal-Stufe, unbeaufsichtigt,
delegiert) und gehorcht derselben Regel wie die drei anderen: sie geht durch `stricter`
und kann per Konstruktion nichts erlauben. Thread-gebunden wie `UnattendedCeiling`, damit
ein gleichzeitig getippter Auftrag im selben Prozess unberuehrt bleibt — und damit
mehrere Untergebene spaeter nebeneinander laufen koennen, jeder unter seiner eigenen.

Was bewusst NICHT hier steht: eine Moeglichkeit, die Decke abzuschalten. Ein Aufruf, der
schreiben soll, ist kein Fall fuer einen Untergebenen — den stellt der Hauptlauf selbst,
wo der Kernel ihn sieht und der Betreiber gefragt wird.
"""
from __future__ import annotations

import threading

from .manifest import Effect, ToolSpec
from .policy import Decision, Verdict, stricter

# Ein Untergebener soll nachsehen, nicht arbeiten. Knapp gehalten, weil ein langer
# Nebenlauf den Hauptlauf blockiert: er laeuft synchron in dessen Werkzeugaufruf.
DELEGATE_MAX_STEPS = 12

# Mehrere Untergebene duerfen NEBENEINANDER laufen — genau dafuer ist die Decke
# thread-gebunden. Die Grenze ist keine Kosmetik: jeder Untergebene ist ein eigener
# Modellaufruf, und eine unbegrenzte Zahl waere ein Weg, aus einer Nachricht beliebig
# viel Verbrauch zu erzeugen. Vier ist genug, um Recherche wirklich zu parallelisieren,
# und klein genug, dass ein Ausrutscher ueberschaubar bleibt.
MAX_PARALLEL = 4
MAX_QUESTION_CHARS = 400
MAX_ANSWER_CHARS = 6_000
ANSWER_CUT = " […delegated answer truncated]"

READ_ONLY_REASON = "delegated run — reading only, by construction"

# Werkzeuge, die ein Untergebener NICHT bekommt, obwohl sie `Effect.READ` sind: Steuern
# ist das eine Lesen, das keines ist. `delegate_steer` bewegt einen ANDEREN Lauf — einen
# Hintergrundlauf unter der unbeaufsichtigten Decke, der schreiben darf, wo der Kernel
# ALLOW sagt. Ein Untergebener, der ihn lenken koennte, haette ueber den Umweg genau die
# Leine, die ihm hier genommen wird: er entsteht aus Modelltext und muss WENIGER koennen
# als sein Auftraggeber, nicht ueber Bande gleich viel. READ bleibt die ehrliche
# Einordnung des Werkzeugs (nach aussen wirkt es nicht, die Decke des Ziels bleibt);
# diese Liste sagt nur, WER es rufen darf. Namentlich, weil das Manifest keinen Begriff
# fuer „lenkt einen anderen Lauf" hat — und ein neues Feld fuer ein Werkzeug waere mehr
# Kernel als Nutzen.
NOT_FOR_DELEGATES = frozenset({"delegate_steer"})
STEER_REASON = "delegated run — steering another run is not reading"


class ReadOnlyCeiling:
    """Die vierte Decke: waehrend eines delegierten Laufs ist alles ausser Lesen `DENY`.

    Zwei Dinge werden zugedreht, und beide mit demselben Satz begruendet:

      * **Alles, was nicht `Effect.READ` ist.** Ein Untergebener, der schreiben darf,
        waere ein Weg, eine Wirkung zu erzielen, ohne dass der Hauptlauf sie beantragt
        hat — die Freigabe haenge dann an einer Aufgabe, die der Betreiber nie gesehen hat.
      * **Alles, was einen Menschen braucht.** Nicht weil niemand da waere (der
        Betreiber ist es), sondern weil die Frage ihn ohne den Zusammenhang erreichte,
        aus dem sie stammt: mitten im Lauf eines Untergebenen, dessen Auftrag das
        Modell formuliert hat. Genau so entsteht das Wegklicken.

    Ein fehlendes Tool-Manifest (`spec is None`) wird wie Nicht-Lesen behandelt. Der
    Kernel sagt dazu ohnehin schon `DENY`; die Decke raet hier nicht in die andere
    Richtung, weil ein unbekanntes Werkzeug das einzige ist, ueber das sie nichts weiss.
    """

    def __init__(self) -> None:
        self._threads: set[int] = set()
        self._lock = threading.Lock()

    def active(self) -> "_Delegated":
        return _Delegated(self)

    def is_delegated(self) -> bool:
        with self._lock:
            return threading.get_ident() in self._threads

    def _enter(self) -> None:
        with self._lock:
            self._threads = self._threads | {threading.get_ident()}

    def _leave(self) -> None:
        with self._lock:
            self._threads = self._threads - {threading.get_ident()}

    def apply(self, decision: Decision, spec: ToolSpec | None = None) -> Decision:
        """Verschaerft — und kann per Konstruktion nichts erlauben."""
        if not self.is_delegated():
            return decision
        if spec is not None and spec.name in NOT_FOR_DELEGATES:
            return stricter(decision, Decision(Verdict.DENY, STEER_REASON))
        if spec is not None and spec.effect is Effect.READ and decision.verdict is not Verdict.NEEDS_HUMAN:
            return decision
        return stricter(decision, Decision(Verdict.DENY, READ_ONLY_REASON))


class _Delegated:
    def __init__(self, ceiling: ReadOnlyCeiling) -> None:
        self._ceiling = ceiling

    def __enter__(self) -> None:
        self._ceiling._enter()

    def __exit__(self, *_exc: object) -> None:
        self._ceiling._leave()


def bound_answer(text: str) -> str:
    """Die Antwort eines Untergebenen ist fremder Text und wird begrenzt wie ein Tool-Ergebnis."""
    raw = " ".join(str(text).split())
    if len(raw) <= MAX_ANSWER_CHARS:
        return raw
    return raw[: MAX_ANSWER_CHARS - len(ANSWER_CUT)] + ANSWER_CUT


__all__ = [
    "DELEGATE_MAX_STEPS",
    "MAX_PARALLEL",
    "MAX_QUESTION_CHARS",
    "NOT_FOR_DELEGATES",
    "READ_ONLY_REASON",
    "STEER_REASON",
    "ReadOnlyCeiling",
    "bound_answer",
]
