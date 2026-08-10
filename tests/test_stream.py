"""Der Stream-Parser entscheidet, was Robin zu sehen bekommt.

Die Zeilen hier sind echte Ausgaben der CLI (gekuerzt), keine erfundenen. Die teuerste
Verwechslung waere, den *thinking*-Block als Antwort zu streamen — dann steht die
Gedankenkette des Modells im Chat. Genau das prueft der erste Fall.
"""
from __future__ import annotations

import json

from talos.stream import StreamReader

# Echte Reihenfolge eines Turns: erst ein thinking-Block (Index 0), dann Text (Index 1).
THINKING_START = json.dumps({"type": "stream_event", "event": {
    "type": "content_block_start", "index": 0,
    "content_block": {"type": "thinking", "thinking": "", "signature": ""}}})
THINKING_DELTA = json.dumps({"type": "stream_event", "event": {
    "type": "content_block_delta", "index": 0,
    "delta": {"type": "thinking_delta", "thinking": "Robin fragt nach dem Speicher …"}}})
SIG_DELTA = json.dumps({"type": "stream_event", "event": {
    "type": "content_block_delta", "index": 0,
    "delta": {"type": "signature_delta", "signature": "CAISnAIKiAEIEBgC"}}})
TEXT_START = json.dumps({"type": "stream_event", "event": {
    "type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}})


def _text_delta(piece: str, index: int = 1) -> str:
    return json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": index,
        "delta": {"type": "text_delta", "text": piece}}})


RESULT_LINE = json.dumps({
    "is_error": False, "duration_api_ms": 4007, "num_turns": 1,
    "usage": {"input_tokens": 2, "output_tokens": 11}, "total_cost_usd": 1.03})


def test_thinking_never_reaches_the_chat() -> None:
    """Der teuerste Fehler: die Gedankenkette als Antwort ausgeben."""
    seen: list[str] = []
    r = StreamReader(seen.append)
    for line in (THINKING_START, THINKING_DELTA, SIG_DELTA, TEXT_START,
                 _text_delta("hello "), _text_delta("world")):
        r.feed(line)
    assert r.text == "hello world"
    assert seen == ["hello ", "world"]
    assert "Robin fragt" not in r.text


def test_deltas_arrive_one_by_one_so_the_display_can_grow() -> None:
    seen: list[str] = []
    r = StreamReader(seen.append)
    r.feed(TEXT_START)
    for piece in ("Der ", "VPS ", "hat ", "43%."):
        r.feed(_text_delta(piece))
    assert seen == ["Der ", "VPS ", "hat ", "43%."]
    assert r.result().text == "Der VPS hat 43%."


def test_metadata_comes_from_the_final_line() -> None:
    """Ohne diese Zeile fiele /usage auf Behauptungen zurueck."""
    r = StreamReader()
    r.feed(TEXT_START)
    r.feed(_text_delta("ok"))
    r.feed(RESULT_LINE)
    res = r.result()
    assert res.payload["duration_api_ms"] == 4007
    assert res.payload["usage"]["output_tokens"] == 11
    assert res.note == ""


def test_unreadable_lines_are_survived_and_counted() -> None:
    """Die CLI mischt gelegentlich Nicht-JSON dazwischen. Kein Grund, die Antwort zu verlieren."""
    r = StreamReader()
    r.feed(TEXT_START)
    r.feed("das ist kein json")
    r.feed(_text_delta("trotzdem da"))
    r.feed("[]")            # gueltiges JSON, aber kein Objekt
    res = r.result()
    assert res.text == "trotzdem da"
    assert "unlesbar" in res.note


def test_an_empty_stream_falls_back_instead_of_answering_with_silence() -> None:
    r = StreamReader()
    r.feed(THINKING_START)
    r.feed(THINKING_DELTA)
    res = r.result(fallback="Rohausgabe der CLI")
    assert res.text == "Rohausgabe der CLI"
    assert res.note == "Stream ohne Text"


def test_a_broken_sink_never_takes_the_run_with_it() -> None:
    """Die Anzeige ist Komfort. Die Antwort ist es nicht."""
    def explode(_: str) -> None:
        raise RuntimeError("Telegram weg")

    r = StreamReader(explode)
    r.feed(TEXT_START)
    r.feed(_text_delta("kommt trotzdem an"))
    assert r.result().text == "kommt trotzdem an"


def test_deltas_before_a_start_are_ignored() -> None:
    """Ohne content_block_start ist unbekannt, ob der Block Text oder Denken ist."""
    r = StreamReader()
    r.feed(_text_delta("waise", index=7))
    assert r.text == ""
