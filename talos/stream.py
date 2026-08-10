"""Liest den Zeilenstrom der CLI und gibt heraus, was der Nutzer sehen darf.

Warum ein eigenes Modul: der Reasoner startet Subprozesse, dieser Parser nicht. Getrennt
laesst sich das Format gegen echte, aufgezeichnete Zeilen testen, ohne ein Modell zu rufen —
und genau dort sitzen die Fallen.

**Die wichtigste Falle:** Ein Turn hat mehrere `content_block`s, und der erste ist oft ein
*thinking*-Block. Auch der sendet Deltas. Wer blind jedes `text_delta` durchreicht, streamt
die Gedankenkette des Modells in den Chat des Betreibers. Deshalb merkt sich der Reader pro Index, ob der
Block Text ist, und laesst nur diese Indizes durch.

Zweite Falle: die Metadaten (Modell, Token, Dauer) stehen in der **letzten** Zeile, nicht in
den Stream-Ereignissen. Ohne sie faellt `/usage` auf Behauptungen zurueck.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

OnText = Callable[[str], None]


@dataclass
class StreamResult:
    """Was am Ende eines Laufs feststeht."""

    text: str = ""
    payload: dict = field(default_factory=dict)
    note: str = ""


class StreamReader:
    """Zeilen rein, Text-Deltas raus. Kennt weder Subprozesse noch Kanaele.

    `on_text` wird pro Delta gerufen — der Aufrufer entscheidet, ob er drosselt.
    Ein Fehler im Sink darf den Lauf nie stoppen: die Anzeige ist Komfort, die Antwort nicht.
    """

    def __init__(self, on_text: OnText | None = None) -> None:
        self._on_text = on_text
        self._text_blocks: set[int] = set()
        self._parts: list[str] = []
        self._payload: dict = {}
        self._broken = 0

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Eine unlesbare Zeile ist kein Grund, den Lauf zu verlieren — die CLI mischt
            # gelegentlich Nicht-JSON dazwischen. Gezaehlt, damit es nicht still bleibt.
            self._broken += 1
            return
        if not isinstance(obj, dict):
            self._broken += 1
            return

        kind = obj.get("type")
        if kind == "stream_event":
            self._event(obj.get("event") or {})
            return
        # Die Abschlusszeile traegt keine `type`-Angabe, aber Dauer und Verbrauch.
        if "duration_api_ms" in obj or "total_cost_usd" in obj or "usage" in obj:
            self._payload = obj

    def _event(self, event: dict) -> None:
        kind = event.get("type")
        index = event.get("index")

        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "text" and isinstance(index, int):
                self._text_blocks.add(index)
            return

        if kind == "content_block_delta":
            if not isinstance(index, int) or index not in self._text_blocks:
                return  # thinking/signature — gehoert nicht in den Chat
            delta = event.get("delta") or {}
            if delta.get("type") != "text_delta":
                return
            piece = delta.get("text")
            if not isinstance(piece, str) or not piece:
                return
            self._parts.append(piece)
            if self._on_text is not None:
                try:
                    self._on_text(piece)
                except Exception:
                    pass  # ein kaputter Sink darf den Lauf nicht mitnehmen
            return

    @property
    def text(self) -> str:
        return "".join(self._parts)

    def result(self, fallback: str = "") -> StreamResult:
        """Der Endstand. `fallback` greift, wenn der Stream keinen Text lieferte.

        Ein leerer Stream ist kein Erfolg: lieber die Rohausgabe zeigen als Schweigen,
        das wie eine Antwort aussieht.
        """
        text = self.text.strip()
        note = ""
        if self._broken:
            note = f"{self._broken} unlesbare Stream-Zeilen"
        if not text:
            text = fallback.strip()
            note = note or "Stream ohne Text"
        return StreamResult(text=text, payload=self._payload, note=note)
