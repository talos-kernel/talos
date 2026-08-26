"""Laufzeit-Fallback-Kette: der naechste Anbieter, wenn dieser Lauf klassifiziert scheitert.

Der Anlass: ein Reasoner, der heute mit „HTTP 529 — provider overloaded" endet, laesst
den Betreiber im Regen stehen, obwohl ein zweiter, lange konfigurierter Anbieter (etwa
das lokale Ollama) die Frage beantwortet haette. Der bisherige Rueckfall —
`resolve_fallback` in `provider.py` — wirkt einmal beim START, wenn der Katalog die
gewaehlte Modell-ID nicht kennt. Er hilft nichts gegen einen Anbieter, der mitten im
Betrieb ausfaellt. Diese Kette schliesst genau diese Luecke.

Drei Entscheidungen tragen das:

⚠️ **Ausgeloest wird nur durch eine klassifizierte Ausnahme.** `ApiReasoner` wirft
`ReasonerFailure` mit einer Fehlerart (`kind`); die Kette schaltet nur weiter, wenn die
Art in `FALLBACKABLE_KINDS` liegt. Ein HTTP-4xx-Fachfehler (HTTP_FAILED) tut das nie:
das Modell hat die Anfrage verstanden und abgelehnt — der naechste Anbieter bekaeme
dieselbe Anfrage und loeste denselben Fehler aus, nur teurer. *Grenze, bewusst:*
`ClaudeCliReasoner` meldet Fehler als AntwortTEXT und `HermesCliReasoner` als nackte
`RuntimeError` ohne Klassifikation — beides ist von einer echten Antwort bzw. einer
Betriebsstoerung („Reasoner laeuft bereits") nicht sauber zu unterscheiden, ohne an
Texten zu raten. CLI-Reasoner loesen die Kette deshalb nicht aus; sie kann nur als
Sprungziel dienen. Wer den Fehler eines CLI-Laufs faengt, faengt am Text — und das
waere genau das Raten, fuer das `ReasonerFailure.kind` existiert.

⚠️ **Die Kette gilt pro Lauf und schreibt nichts.** Die persistierte Wahl
(`model.selected` im Event-Log) bleibt unangetastet — der naechste Lauf startet wieder
beim Primaer-Anbieter. Ein Fallback ist eine Antwort auf eine Stoerung, keine
Entscheidung ueber die Zukunft; wer dauerhaft wechseln will, sagt `/model`.

⚠️ **Jeder Hop ist belegt.** Jeder Versuch landet als `model.fallback.runtime`-Event im
Event-Log: wohin, mit welcher Fehlerart, mit welchem Ausgang. Ein stiller Wechsel des
Denkers hinter dem Ruecken des Betreibers ist derselbe Vertrauensbruch wie der stille
Katalog-Rueckfall, den `restore_selection` einmal beheben musste.

Fail-closed beim Bauen: ein Hop ohne hinterlegten Schluessel wird uebersprungen (mit
Event), nie laeuft der Lauf deshalb in einen Traceback. Ein Hop, der gar nicht gebaut
werden kann, kostet die Kette einen Eintrag, nicht den Zug.
"""
from __future__ import annotations

from typing import Callable

from .api_reasoner import FALLBACKABLE_KINDS, ReasonerFailure
from .eventlog import Event, EventLog, new_run_id
from .provider import ModelSelection, _takes_sink
from .stream import OnText

__all__ = ["FALLBACK_EVENT", "FallbackReasoner", "parse_chain"]

FALLBACK_EVENT = "model.fallback.runtime"

# Der Hinweis im Antworttext ist Teil des GESPRAECHS mit dem Betreiber, nicht der
# Maschinen-Konsole — er folgt dessen Sprache. Die Texte nennen die Art, nie Details:
# die stehen gesaeubert in `ReasonerFailure.message`, und die Kette gibt sie nur im
# Totalversagen weiter.
_GRUND = {
    "key_rejected": "Schlüssel abgelehnt",
    "rate_limited": "Kontingent erschöpft",
    "overloaded": "überlastet",
    "network_failed": "Netzfehler",
    "timed_out": "Zeitüberschreitung",
}


def parse_chain(raw: str) -> tuple[ModelSelection, ...]:
    """Kommagetrennte `provider/model`-Specs in Prioritaetsreihenfolge.

    Unlesbare Stuecke fallen weg statt umzufallen: ein Tippfehler in der Umgebung darf
    den Start nicht kosten — der betroffene Hop fehlt dann einfach in der Kette.
    Leer heisst: keine Kette, alles wie bisher. Modell-IDs duerfen selbst `/` tragen
    (etwa `nvidia-nim/nvidia/llama-…`); getrennt wird am ERSTEN Schraegstrich.
    """
    hops: list[ModelSelection] = []
    for part in str(raw).split(","):
        provider, trenner, model = part.partition("/")
        provider, model = provider.strip(), model.strip()
        if trenner and provider and model:
            hops.append(ModelSelection(provider, model))
    return tuple(hops)


class FallbackReasoner:
    """Ein Reasoner um den aktiven Router herum: scheitert der Lauf klassifiziert,
    denkt der naechste Anbieter der Kette.

    Alles ausser `reason` gehoert weiterhin dem primaeren Objekt (`__getattr__` reicht
    durch): `current`, `select`, `cancel` und `can_select` sind Router-Geschaeft, die
    Kette entscheidet nur ueber diesen einen Lauf.
    """

    def __init__(
        self,
        primary: object,
        chain: tuple[ModelSelection, ...],
        build: Callable[[ModelSelection], object],
        log: EventLog,
    ) -> None:
        self._primary = primary
        self._chain = tuple(chain)
        self._build = build
        self._log = log

    def __getattr__(self, name: str):
        return getattr(self._primary, name)

    def reason(self, prompt: str, on_text: OnText | None = None) -> str:
        try:
            return str(self._primary.reason(prompt, on_text=on_text))  # type: ignore[attr-defined]
        except ReasonerFailure as err:
            if not self._chain or err.kind not in FALLBACKABLE_KINDS:
                # Genau der bisherige Text — dieselbe Zeile, die der Reasoner ohne Kette
                # selbst ausgeliefert haette. e2e/redteam haengen an diesem Wortlaut.
                return err.message
            # `err` wird am Ende des except-Blocks geloescht (Python-Semantik) — die
            # Referenz braucht einen eigenen Namen, sonst ist die Kette unten leer.
            failure = err
        run_id = new_run_id()
        ausloeser = self._spec(self._primary)
        quelle = "Primär-Provider"
        fehler: BaseException = failure
        for hop in self._chain:
            ziel = f"{hop.provider}/{hop.model}"
            try:
                hop_reasoner = self._build(hop)
            except Exception as error:
                # Fail-closed pro Hop: fehlender Schluessel, fehlende CLI — der Hop wird
                # uebersprungen und belegt, der Zug stuerzt deshalb nie ab.
                self._record(run_id, ausloeser, ziel, _kind_of(fehler), "skipped",
                             str(error)[:200])
                continue
            try:
                antwort = _call(hop_reasoner, prompt, on_text)
            except ReasonerFailure as hop_fehler:
                self._record(run_id, ausloeser, ziel, hop_fehler.kind, "failed",
                             hop_fehler.note)
                if hop_fehler.kind not in FALLBACKABLE_KINDS:
                    # Ein Fachfehler mitten in der Kette beendet sie: weiterschalten
                    # hiesse, dieselbe abgelehnte Anfrage an den naechsten zu stellen.
                    return hop_fehler.message
                ausloeser, quelle, fehler = ziel, ziel, hop_fehler
                continue
            except Exception as error:
                # Ein Hop, der wirft statt klassifiziert (etwa eine CLI): zaehlt als
                # gescheiterter Hop. Bleibt er der letzte Fehler, fliegt er wie bisher —
                # ein erfundener Meldungstext waere schlimmer als die Ausnahme.
                self._record(run_id, ausloeser, ziel, _kind_of(fehler), "failed",
                             str(error)[:200])
                ausloeser, quelle, fehler = ziel, ziel, error
                continue
            self._record(run_id, ausloeser, ziel, _kind_of(fehler), "ok", "")
            grund = _GRUND.get(_kind_of(fehler), _kind_of(fehler))
            return f"(Fallback: {ziel} — Grund: {quelle} {grund})\n{antwort}"
        # Totales Kettenversagen: das bisherige Textverhalten des LETZTEN Fehlers.
        if isinstance(fehler, ReasonerFailure):
            return fehler.message
        raise fehler

    @staticmethod
    def _spec(reasoner: object) -> str:
        """`provider/model` des aktiven Denkers — leer, wenn das Objekt es nicht weiss."""
        current = getattr(reasoner, "current", None)
        if isinstance(current, ModelSelection):
            return f"{current.provider}/{current.model}"
        return ""

    def _record(
        self,
        run_id: str,
        von: str,
        nach: str,
        kind: str,
        ausgang: str,
        detail: str,
    ) -> None:
        """Der Beleg darf selbst nie der Grund sein, warum der Zug scheitert."""
        try:
            self._log.append(Event(run_id, "provider", FALLBACK_EVENT, {
                "from": von,
                "to": nach,
                "kind": kind,
                "outcome": ausgang,
                "detail": detail,
            }))
        except Exception:
            pass


def _call(reasoner: object, prompt: str, on_text: OnText | None) -> str:
    """Ein Hop-Lauf. `reason_strict` wo vorhanden — nur so traegt der Fehler seine Art."""
    method = getattr(reasoner, "reason_strict", None) or getattr(reasoner, "reason")  # type: ignore[attr-defined]
    if on_text is not None and _takes_sink(method):
        return str(method(prompt, on_text=on_text))
    return str(method(prompt))


def _kind_of(fehler: BaseException) -> str:
    return fehler.kind if isinstance(fehler, ReasonerFailure) else "error"
