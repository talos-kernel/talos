"""Live-Fortschritt im Terminal — dieselben Stationen wie die Telegram-Anzeige.

Drei der vier Entscheidungen aus `TelegramActivity` gelten hier unveraendert:

1. **Keine Anzeige vor dem ersten Werkzeug.** Eine reine Textantwort bekommt
   keinen Kopf und keine Zeile — wer „Hallo" fragt, soll „Hallo" sehen, nicht
   einen Statusblock (Trap 6 in CLAUDE.md).
2. **Die Denkphase wird sichtbar, sobald die Anzeige existiert.** Bis zum
   ersten Werkzeug ist sie nur gemessene Wartezeit; danach steht sie als erste
   Zeile mit ihrer Dauer da — sie war die laengste Phase des Laufs.
3. **Der Verlauf bleibt stehen** und traegt Dauer pro Zeile; Gesamtzeit und
   Werkzeugzahl stehen im Fuss.

Der einzige Unterschied ist Bauart: kein Heartbeat-Thread. Das Terminal ist
synchron — eine fehlende naechste Zeile IST die Anzeige, dass noch gedacht
wird. Ein Takt, der sie flimmert, waere Laerm statt Beleg.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable

from .agent_loop import AgentProgress, ProgressStage
from .ux import GEOMETRIC, Style

__all__ = ["CliActivity"]


class CliActivity:
    """Schreibt die Stationen eines Laufs als je eine Zeile ins Terminal.

    Fail-open wie die Telegram-Anzeige: ein Schreibfehler kostet die Zeile,
    nie den Lauf — die Anzeige ist Beleg, nicht Teil der Wirkung.
    """

    def __init__(
        self,
        out: object = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        style: Style = GEOMETRIC,
    ) -> None:
        self._schreiben = (out or sys.stdout).write
        self._clock = clock
        self._style = style
        self._start = clock()
        self._denkt_seit: float | None = None
        self._denk_schritt = (0, 0)
        self._sichtbar = False          # Trap 6: erst ein Werkzeug macht sichtbar
        self._werkzeug_seit: float | None = None
        self._werkzeuge = 0
        self._fertig = False

    def _zeile(self, text: str) -> None:
        try:
            self._schreiben(f"  {text}\n")
        except Exception:
            pass

    def _dauer(self, seit: float) -> str:
        return f"{self._clock() - seit:.1f}s"

    def _sichtbar_machen(self) -> None:
        """Der erste sichtbare Anlass. Eine etwaige Denkphase davor ist damit
        gemessen und bekommt ihre Zeile — nicht als geschaetzte, als vergangene."""
        if self._sichtbar:
            return
        self._sichtbar = True
        if self._denkt_seit is not None:
            schritt, von = self._denk_schritt
            zaehler = f" — step {schritt}/{von}" if von else ""
            self._zeile(f"{self._style.thinking} thinking ({self._dauer(self._denkt_seit)}){zaehler}")
            self._denkt_seit = None

    def progress(self, event: Any) -> None:
        if self._fertig or not isinstance(event, AgentProgress):
            return
        try:
            self._fortschritt(event)
        except Exception:
            pass  # eine Anzeige darf den Lauf nie stossen

    def _fortschritt(self, event: AgentProgress) -> None:
        zaehler = f" — step {event.step}/{event.max_steps}" if event.max_steps else ""
        if event.stage is ProgressStage.THINKING:
            if self._sichtbar:
                self._zeile(f"{self._style.thinking} thinking{zaehler}")
            else:
                self._denkt_seit = self._clock()
                self._denk_schritt = (event.step, event.max_steps)
        elif event.stage is ProgressStage.PLAN:
            self._sichtbar_machen()
            self._zeile(f"{self._style.plan} plan — {event.summary}")
        elif event.stage is ProgressStage.TOOL:
            self._sichtbar_machen()
            self._werkzeug_seit = self._clock()
            ziel = f" {event.summary}" if event.summary else ""
            self._zeile(f"{self._style.tool_symbol(event.tool)} {event.tool}{ziel}")
        elif event.stage is ProgressStage.RESULT:
            self._sichtbar_machen()
            self._werkzeuge += 1
            dauer = f" ({self._dauer(self._werkzeug_seit)})" if self._werkzeug_seit else ""
            self._werkzeug_seit = None
            grund = f" — {event.summary}" if event.summary else f" — {event.status}"
            urteil = str(event.status).lower()
            if urteil == "done":
                self._zeile(f"{self._style.ok} {event.tool}{dauer}")
            elif urteil == "denied":
                # Der Kernel hat entschieden, nicht das Werkzeug versagt — eigenes Zeichen.
                self._zeile(f"{self._style.blocked} {event.tool}{dauer}{grund}")
            elif urteil == "needs_human":
                self._zeile(f"{self._style.gate} {event.tool}{dauer}{grund}")
            else:
                self._zeile(f"{self._style.fail} {event.tool}{dauer}{grund}")
        elif event.stage is ProgressStage.REDIRECTED:
            if self._sichtbar:
                # Kein Glyphen-Plagiat: die Richtungskorrektur hat kein eigenes Zeichen,
                # sie steht als Wort da (ein Zeichen, eine Bedeutung — ux.py).
                self._zeile("· direction changed by the operator")

    def succeed(self, footer: str = "") -> None:
        if not self._sichtbar or self._fertig:
            return
        self._fertig = True
        fuss = f"  {self._style.ok} done in {self._dauer(self._start)} · {self._werkzeuge} tool(s)"
        if footer:
            fuss += f"\n{footer.rstrip()}"
        self._zeile(fuss)

    def fail(self, error: str) -> None:
        if not self._sichtbar or self._fertig:
            return
        self._fertig = True
        grund = " ".join(str(error or "").split())[:200]
        self._zeile(f"{self._style.fail} failed — {grund}")
