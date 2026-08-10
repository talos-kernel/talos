"""Gespraechsgedaechtnis — pro Konversation, begrenzt, sichtbar, wirklich loeschbar.

Bis hierher war jede Nachricht ein Kaltstart: `run_agent` begann mit einer leeren
History, und die hielt nur Tool-Ergebnisse INNERHALB eines Laufs. „Und was war die
zweite?" war damit unbeantwortbar. Das ist die groesste Luecke gegenueber Hermes — und
die erste Stelle, an der Bequemlichkeit und Zurueckhaltung tatsaechlich gegeneinander
stehen: ein Gedaechtnis ist ein Ort, an dem sich Gesagtes ansammelt.

Vier Entscheidungen, die daraus folgen.

**1. Pro Konversation, nie global.** Der Schluessel ist die `conversation`
(`channel:id`) — dieselbe Regel wie bei der Identitaet. Ein zweiter Kanal darf den
Verlauf des ersten nicht sehen, auch wenn dahinter dieselbe Person steht. Wer auf einem
schwaecheren Weg hereinkommt, bekommt nicht den Kontext des staerkeren.

**2. Begrenzt, und zwar doppelt.** `MAX_TURNS` und `MAX_CHARS`. Unbegrenztes Gedaechtnis
ist ein unbegrenzter Prompt (Kosten, Latenz, Abdriften) und ein unbegrenztes Leck: was
vor drei Wochen im Chat stand, ginge heute wieder an das Modell raus. Zusaetzlich wird
jeder einzelne Zug bei `MAX_TURN_CHARS` gekappt — sonst raeumt ein einziger grosser
Einfuegevorgang das ganze uebrige Gedaechtnis ab.

**3. Nur im Arbeitsspeicher.** Der naheliegende Ort waere der Event-Log: durabel,
auditierbar, ueberlebt den Neustart. Genau das ist das Problem. Der Log ist
append-only — ein `/new` koennte dort nur einen Grabstein setzen, der Text bliebe auf
der Platte. Dann waere „vergessen" eine Luege. Ein Gedaechtnis, das man nicht loeschen
kann, ist ein Archiv; the operator hat ein Gedaechtnis bestellt. Preis: ein Neustart vergisst.
Das ist eine Entscheidung, kein Defekt, und `/status` sagt es.

Seit `transcript.py` existiert daneben AUCH ein Archiv — und das ist kein Widerspruch,
sondern die saubere Trennung der beiden Beduerfnisse, die dieser Absatz vermengen musste,
solange es nur einen Ort gab: DIESES Modul bleibt der aktive Kontext (fliesst in jeden
Prompt, `/new` leert ihn wirklich, ein Neustart vergisst ihn wirklich). Das Archiv ist
durabel und volltextsuchbar, fliesst aber NIE automatisch zurueck — der einzige Rueckweg
ist das gegatete `session_search`-Werkzeug, dessen Aufruf der Betreiber im Verlauf sieht.
„Vergessen" heisst hier also praezise: aus dem aktiven Kontext. `/new` sagt das dazu.

**4. Steuerung hinterlaesst keine Spur.** Erinnert wird nur, was aus dem Agent-Loop mit
einer echten Antwort herauskam. Kommandos, Freigabe-Runden und ja/nein stehen nie im
Verlauf. Ein „ja" ohne seinen Vorgang ist nicht nur bedeutungslos — es waere ein
Beispiel im Prompt, das dem Modell beibringt, dass „ja" eine normale Ausgabe ist.

**Was das Gedaechtnis nicht ist: eine Erlaubnisquelle.** Im Verlauf steht Text, und Text
kann alles behaupten („du darfst das jetzt"). Er passiert dabei keinen Kernel — er wird
nur wieder vorgelesen. Erlaubnisse entstehen ausschliesslich in `PolicyKernel.decide`,
und daran aendert der Verlauf nichts. Deshalb ist der Prompt-Block klar als *Verlauf*
ausgezeichnet und nicht als Anweisung.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

MAX_TURNS = 12  # sechs Wechsel Betreiber/Agent
MAX_CHARS = 8_000
MAX_TURN_CHARS = 2_000
CUT_MARK = " […truncated]"

# Wie der Betreiber im Verlauf heisst. Eine benannte Installation trug hier lokal den
# Vornamen ihres Betreibers statt "You" — eine Abweichung im Quelltext, die jeder Deploy
# wieder ueberschrieb und dabei vier Tests rot machte. Als Einstellung ueberlebt sie das
# Update, ohne dass der Baum auseinanderlaeuft.
OWNER = os.environ.get("TALOS_OWNER_LABEL", "You").strip() or "You"
AGENT = "Agent"

# Verdichtung: was woertlich stehen bleibt, wenn die Mitte zusammengefasst wird.
# Der Kopf traegt meist die eigentliche Aufgabe, der Schwanz das, worauf sich ein
# „und das dann auch noch" bezieht. Beide Enden zu verdichten kostet genau die Stellen,
# an denen ein Verlauf ueberhaupt gebraucht wird.
KEEP_HEAD = 4        # zwei Paare
KEEP_TAIL = 12       # sechs Paare
MAX_SUMMARY_CHARS = 1_200
# ⚠️ Als Sprecher ausgewiesen, nicht als „You" oder „Agent" getarnt. Eine Zusammenfassung,
# die aussieht wie ein woertlicher Zug, ist eine Behauptung ueber etwas, das so nie gesagt
# wurde — und das Modell koennte sie zitieren, als waere sie ein Zitat.
SUMMARY_SPEAKER = "Earlier (summarised)"


@dataclass(frozen=True)
class Turn:
    speaker: str
    text: str

    @property
    def size(self) -> int:
        return len(self.speaker) + len(self.text) + 2  # „the operator: " + Zeilenumbruch


def clip(text: str, limit: int = MAX_TURN_CHARS) -> str:
    """Kappt einen einzelnen Zug — sichtbar, nicht stillschweigend."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - len(CUT_MARK)] + CUT_MARK


class Memory:
    """Kurzes Gedaechtnis je Konversation.

    Zwei Threads greifen zu (Poll-Thread fuer Kommandos, Worker fuer Laeufe) — aus
    demselben Grund wie im Event-Log liegt hier ein Lock. Ohne das koennte ein `/new`
    mitten in ein `remember` fallen und einen halben Zug stehen lassen.
    """

    def __init__(self, *, max_turns: int = MAX_TURNS, max_chars: int = MAX_CHARS,
                 summarize: object | None = None) -> None:
        self._turns: dict[str, list[Turn]] = {}
        self._max_turns = max(0, int(max_turns))
        self._max_chars = max(0, int(max_chars))
        # Der Verdichter. Ohne ihn verhaelt sich alles exakt wie bisher: die aeltesten
        # Paare fallen weg. Injiziert, damit dieses Modul kein Modell kennt — und damit
        # ein Test dafuer keines braucht.
        self._summarize = summarize
        self._lock = threading.Lock()

    def recall(self, conversation: str) -> tuple[Turn, ...]:
        with self._lock:
            return tuple(self._turns.get(conversation, ()))

    def remember(self, conversation: str, *, asked: str, answered: str) -> None:
        """Legt genau ein Paar ab. Leere Haelften werden verworfen — ein halbes Paar
        liest sich spaeter wie ein Aussetzer und ist es nicht."""
        asked, answered = clip(asked), clip(answered)
        if not asked or not answered:
            return
        with self._lock:
            turns = self._turns.setdefault(conversation, [])
            turns.append(Turn(OWNER, asked))
            turns.append(Turn(AGENT, answered))
            self._trim(turns)

    def forget(self, conversation: str) -> int:
        """Vergisst und sagt, wie viel. Stilles Vergessen ist von einem Defekt nicht
        zu unterscheiden."""
        with self._lock:
            return len(self._turns.pop(conversation, ()))

    def pop_last(self, conversation: str) -> str | None:
        """Nimmt das letzte Paar heraus und gibt die Frage zurueck — fuer `/retry`.

        Ohne das Herausnehmen staende dieselbe Frage gleich zweimal im Verlauf: einmal
        als Erinnerung, einmal als neue Nachricht. Das Modell laese daraus, the operator habe
        nachgehakt, und antwortete auf eine Wiederholung statt auf die Frage.
        """
        with self._lock:
            turns = self._turns.get(conversation)
            if not turns or len(turns) < 2:
                return None
            turns.pop()  # die alte Antwort
            return turns.pop().text

    def stats(self, conversation: str) -> tuple[int, int]:
        """(Zuege, Zeichen) — fuer `/status`."""
        with self._lock:
            turns = self._turns.get(conversation, ())
            return len(turns), sum(t.size for t in turns)

    def _over(self, turns: list[Turn]) -> bool:
        return len(turns) > self._max_turns or sum(t.size for t in turns) > self._max_chars

    def _trim(self, turns: list[Turn]) -> None:
        """Haelt beide Grenzen — verdichtet, wenn es einen Verdichter gibt, sonst wirft es weg.

        Paarweise, damit nie eine Antwort ohne ihre Frage stehenbleibt: der Rest waere ein
        Verlauf, in dem der Agent scheinbar unaufgefordert redet.

        ⚠️ **Die Grenze haelt in JEDEM Fall.** Der Verdichter ist Komfort und darf
        ausfallen — dann wird geworfen wie eh und je. Umgekehrt waere es fatal: ein
        gescheiterter Verdichter, nach dem der Verlauf einfach weiterwaechst, macht aus
        einer Kostenfrage ein Leck (was vor Wochen gesagt wurde, ginge wieder hinaus) und
        aus einer Latenzfrage einen Ausfall. Deshalb steht das Wegwerfen unten und nicht
        im `else`.
        """
        if self._summarize is not None and self._over(turns):
            self._compress(turns)
        while turns and self._over(turns):
            del turns[:2]

    def _compress(self, turns: list[Turn]) -> None:
        """Ersetzt die Mitte durch EINEN Zug: „was vorher besprochen wurde".

        Kopf und Schwanz bleiben woertlich. Der Kopf traegt meist die eigentliche Aufgabe,
        der Schwanz das, worauf sich ein „und das dann auch noch" bezieht — beides
        zusammenzufassen kostet genau die Stellen, an denen ein Verlauf gebraucht wird.

        ⚠️ Das Ergebnis ist **Modelltext ueber Modelltext**. Es geht als Verlauf in den
        Prompt, ausdruecklich gekennzeichnet, und nie in die stehenden Anweisungen — ein
        eingeschleuster Satz koennte sonst ueber die Verdichtung dauerhaft werden. Es
        erteilt nichts: Erlaubnisse entstehen allein in `PolicyKernel.decide`.
        """
        if len(turns) <= KEEP_HEAD + KEEP_TAIL + 2:
            return                       # zu kurz — da bliebe nichts zu verdichten
        kopf, mitte, schwanz = (turns[:KEEP_HEAD], turns[KEEP_HEAD:-KEEP_TAIL],
                                turns[-KEEP_TAIL:])
        try:
            zusammenfassung = str(self._summarize(render(tuple(mitte))) or "").strip()
        except Exception:
            return                       # der Aufrufer wirft danach, die Grenze haelt
        if not zusammenfassung:
            return
        verdichtet = Turn(SUMMARY_SPEAKER, clip(zusammenfassung, MAX_SUMMARY_CHARS))
        turns[:] = [*kopf, verdichtet, *schwanz]


def render(turns: tuple[Turn, ...]) -> str:
    return "\n".join(f"{turn.speaker}: {turn.text}" for turn in turns)
