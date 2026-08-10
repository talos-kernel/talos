"""Das Gespraechsarchiv und sein Werkzeug `session_search`.

Die eine Eigenschaft, die hier alles traegt: eine Konversation sieht NUR sich selbst.
Der Runner nimmt die Konversation aus dem Thread-Kontext (`ask_operator`-Bauart), nie
aus den Argumenten — die Faelle hier beweisen beides einzeln: dass die Grenze im Store
per SQL haelt, und dass der Runner ein untergeschobenes `conversation`-Feld ignoriert.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from talos import tools, transcript
from talos.channel import Principal
from talos.manifest import Effect
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.reasoner import TOOL_PROTOCOL
from talos.transcript import TranscriptStore, render_results

OWNER = Principal("telegram", "100000001")
CHAT_A = "telegram:100000001"
CHAT_B = "telegram:200000002"
SECRET_WORD = "morgenstern-widerhaken"


@dataclass(frozen=True)
class FakeContext:
    conversation: str


def store_with(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "transcript.db")


def runner_for(store: TranscriptStore, conversation: str | None):
    ctx = None if conversation is None else FakeContext(conversation)
    return tools.make_session_search_runner(store, context=lambda: ctx)


def req(**args: object) -> ToolRequest:
    return ToolRequest("session_search", OWNER, dict(args))


# --- Store ------------------------------------------------------------------------
def test_record_and_search_round_trip_with_fts(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    assert store.available and store.full_text
    store.record(CHAT_A, asked="Wie hiess der Server?", answered=f"Er hiess {SECRET_WORD}.")
    found = store.search(CHAT_A, "server hiess")
    assert len(found) == 1 and SECRET_WORD in found[0].answered


def test_search_never_crosses_conversations(tmp_path: Path) -> None:
    """Die Kern-Eigenschaft: unter A geschrieben ist aus B nicht auffindbar —
    gleicher Principal, gleiche Woerter, andere Konversation."""
    store = store_with(tmp_path)
    store.record(CHAT_A, asked="frage", answered=f"antwort {SECRET_WORD}")
    assert store.search(CHAT_B, SECRET_WORD) == ()
    assert store.recent(CHAT_B) == ()
    assert len(store.search(CHAT_A, SECRET_WORD)) == 1


def test_like_fallback_holds_the_same_conversation_boundary(tmp_path: Path) -> None:
    """Der LIKE-Weg (SQLite ohne FTS5) darf nicht die schwaechere Haelfte sein."""
    store = store_with(tmp_path)
    store.record(CHAT_A, asked="frage", answered=f"antwort {SECRET_WORD}")
    like_hits = store._search_like(CHAT_A, (SECRET_WORD,), 5)
    assert len(like_hits) == 1
    assert store._search_like(CHAT_B, (SECRET_WORD,), 5) == ()


def test_empty_query_returns_recent_of_this_conversation_only(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.record(CHAT_A, asked="a", answered="eins")
    store.record(CHAT_B, asked="b", answered="zwei")
    recent = store.search(CHAT_A, "")
    assert len(recent) == 1 and recent[0].answered == "eins"


def test_full_text_is_stored_only_the_output_is_bounded(tmp_path: Path) -> None:
    """Die Quelle bleibt vollstaendig — gedeckelt wird nur, was zurueckfliesst."""
    store = store_with(tmp_path)
    long_answer = "wort " * 10_000 + SECRET_WORD
    store.record(CHAT_A, asked="lange frage", answered=long_answer)
    kept = store.recent(CHAT_A)[0]
    assert SECRET_WORD in kept.answered  # das Ende ueberlebte — nichts gekappt
    out = render_results(store.recent(CHAT_A))
    assert len(out) <= transcript.MAX_SEARCH_OUTPUT_CHARS


def test_render_results_redacts_secrets(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.record(CHAT_A, asked="zeig den header", answered="Bearer abc123def456ghi789jkl")
    out = render_results(store.recent(CHAT_A))
    assert "abc123def456ghi789jkl" not in out and "[REDACTED]" in out


def test_broken_store_is_a_limitation_not_a_crash(tmp_path: Path) -> None:
    """Fail-open: `record`/`search` auf einem toten Store sind No-ops, keine Fehler."""
    target = tmp_path / "not-a-dir"
    target.write_text("file, not directory", encoding="utf-8")
    store = TranscriptStore(target / "transcript.db")
    assert not store.available and store.reason
    store.record(CHAT_A, asked="a", answered="b")  # darf nicht werfen
    assert store.search(CHAT_A, "b") == ()
    assert store.count() == 0


def test_record_swallows_late_sqlite_errors(tmp_path: Path) -> None:
    """Ein Store, der NACH dem Start kaputtgeht, darf die zugestellte Antwort nicht
    mehr gefaehrden — genau der Fall, den der Conductor-Call-Site voraussetzt."""
    store = store_with(tmp_path)
    store._conn.close()  # simuliert: Verbindung stirbt im Betrieb
    store._conn = sqlite3.connect(":memory:")  # Tabellen fehlen -> sqlite3.Error
    store.record(CHAT_A, asked="a", answered="b")  # darf nicht werfen


def test_file_permissions_are_owner_only(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    db = tmp_path / "transcript.db"
    assert (db.stat().st_mode & 0o777) == 0o600
    store.close()


# --- Runner: die Isolationsgrenze --------------------------------------------------
def test_runner_takes_conversation_from_context_never_from_args(tmp_path: Path) -> None:
    """Der scharfe Fall: das Modell schiebt ein `conversation`-Feld in die Argumente.
    Es darf nicht einmal angesehen werden — gesucht wird weiter in der eigenen."""
    store = store_with(tmp_path)
    store.record(CHAT_B, asked="geheim", answered=f"nur fuer B: {SECRET_WORD}")
    runner = runner_for(store, CHAT_A)
    out = runner(req(query=SECRET_WORD, conversation=CHAT_B))
    assert SECRET_WORD not in out
    assert "No matching turns" in out


def test_runner_finds_its_own_conversation(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    store.record(CHAT_A, asked="wie hiess es", answered=f"es hiess {SECRET_WORD}")
    runner = runner_for(store, CHAT_A)
    assert SECRET_WORD in runner(req(query=SECRET_WORD))


def test_runner_without_context_raises_instead_of_answering_empty(tmp_path: Path) -> None:
    """Kein Kontext ist ein Verdrahtungsfehler, kein Suchergebnis — ein leeres Ergebnis
    wuerde dem Modell vorgaukeln, es sei wirklich nichts gefunden worden."""
    runner = runner_for(store_with(tmp_path), None)
    with pytest.raises(ValueError, match="no conversation context"):
        runner(req(query="egal"))


def test_runner_validates_query_and_limit_like_vault_search(tmp_path: Path) -> None:
    runner = runner_for(store_with(tmp_path), CHAT_A)
    with pytest.raises(ValueError):
        runner(req(query=""))
    with pytest.raises(ValueError):
        runner(req(query="x" * (transcript.QUERY_MAX_CHARS + 1)))
    with pytest.raises(ValueError):
        runner(req(query="ok", limit=0))
    with pytest.raises(ValueError):
        runner(req(query="ok", limit=11))
    with pytest.raises(ValueError):
        runner(req(query="ok", limit=True))


# --- Verdrahtung: gebaut UND nutzbar ------------------------------------------------
def test_manifest_extractor_and_protocol_make_session_search_first_class() -> None:
    """Die „gebaut ≠ nutzbar"-Absicherung: Manifest, Kernel-Urteil und TOOL_PROTOCOL
    muessen das Werkzeug alle drei kennen — fehlt eines, existiert es praktisch nicht."""
    spec = tools.default_manifest().get("session_search")
    assert spec is not None and spec.effect is Effect.READ and spec.reversible

    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    decision = kernel.decide(req(query="frueher gesagt"))
    assert decision.verdict is Verdict.ALLOW
    assert kernel.guard_targets(req(query="x")) == ()

    text = TOOL_PROTOCOL
    assert "session_search" in text
    assert "THIS conversation" in text  # die Grenze steht im Text, den das Modell liest


def test_session_search_is_not_in_static_runners() -> None:
    """Wie `ask_operator`/`undo_last`: braucht geteilten Zustand, wird in __main__
    verdrahtet — ein statischer Eintrag waere ein Runner ohne Kontext."""
    assert "session_search" not in tools.RUNNERS
