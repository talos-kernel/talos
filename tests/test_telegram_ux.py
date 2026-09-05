"""Telegram-UX: kurzer Start und genau eine verschwindende Live-Aktivität."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from talos.agent_loop import AgentProgress, ProgressStage
from talos.approval import ApprovalStore
from talos.capability import CapabilityMint
from talos.channel import Principal
from talos.identity import agent_name
from talos.commands import CommandCenter, HELP
from talos.eventlog import EventLog
from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel
from talos.telegram import TelegramActivity, TelegramChannel, TelegramReply
from talos.ux import SYM_TALOS

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:4242"


class _Reasoner:
    def cancel(self) -> bool:
        return False


class _Worker:
    def pending(self) -> int:
        return 0

    def busy(self) -> bool:
        return False

    def drain(self) -> int:
        return 0


def _center(tmp_path: Path, status=None) -> CommandCenter:
    manifest = ToolManifest().with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
    policy = PolicyKernel(manifest, frozenset({OWNER}))
    return CommandCenter(
        log=EventLog(tmp_path / "events.db"),
        approvals=ApprovalStore(),
        policy=policy,
        started_at=0,
        bot_username="Talos_bot",
        reasoner=_Reasoner(),
        worker=_Worker(),
        repo_dir=tmp_path,
        mint=CapabilityMint(policy),
        start_status=status,
    )


def test_start_is_personal_short_and_separate_from_help(tmp_path: Path) -> None:
    center = _center(tmp_path)
    start = center.dispatch("start", "", principal=OWNER, conversation=CHAT).reply or ""
    help_text = center.dispatch("help", "", principal=OWNER, conversation=CHAT).reply or ""

    assert start.startswith("Hallo 👋")
    # Der Name steht in SOUL.md, nicht im Quelltext: nach einer Umbenennung stellte
    # sich der Waechter sonst weiter unter dem alten vor.
    assert agent_name() in start
    assert agent_name() in help_text
    assert "**Kurzstatus**" in start
    assert "Dienst: ✅ aktiv" in start
    assert "/model, /status, /help" in start
    assert "/stop" not in start
    assert help_text == HELP.format(name=agent_name())
    assert "/stop" in help_text


def test_start_uses_only_status_facts_the_callback_actually_provides(tmp_path: Path) -> None:
    center = _center(
        tmp_path,
        status=lambda: {
            "model": "claude-sonnet-4",
            "vault": "✅ bereit",
            "vault_search": "🔒 gesperrt",
            "location": "Pi",
        },
    )
    start = center.dispatch("start", "", principal=OWNER, conversation=CHAT).reply or ""

    assert "Läuft auf: Pi" in start
    assert "Modell: claude-sonnet-4" in start
    assert "Vault: ✅ bereit" in start
    assert "Vault search: 🔒 gesperrt" in start


def test_start_status_failure_is_truthful_and_small(tmp_path: Path) -> None:
    def unavailable():
        raise RuntimeError("probe down")

    start = _center(tmp_path, status=unavailable).dispatch(
        "start", "", principal=OWNER, conversation=CHAT
    ).reply or ""
    assert "Dienst: ✅ aktiv" in start
    assert "Weitere Statusdaten: werden geprüft" in start
    assert "VPS" not in start and "Trading" not in start


@dataclass
class FakeTelegramClient:
    now: list[float] = field(default_factory=lambda: [0.0])
    sent: list[tuple[int, str, dict]] = field(default_factory=list)
    edited: list[tuple[int, int, str, dict]] = field(default_factory=list)
    deleted: list[tuple[int, int]] = field(default_factory=list)
    actions: list[tuple[int, str]] = field(default_factory=list)
    fail_delete: bool = False

    def send_message(self, chat_id: int, text: str, **kwargs) -> int:
        self.sent.append((chat_id, text, kwargs))
        return 77

    def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs) -> None:
        self.edited.append((chat_id, message_id, text, kwargs))

    def delete_message(self, chat_id: int, message_id: int) -> None:
        if self.fail_delete:
            raise RuntimeError("delete forbidden")
        self.deleted.append((chat_id, message_id))

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))


def _tool(tool: str, *, summary: str, step: int = 1, max_steps: int = 8) -> AgentProgress:
    return AgentProgress(
        ProgressStage.TOOL, tool=tool, status="running", summary=summary,
        step=step, max_steps=max_steps,
    )


def _result(tool: str, status: str, *, step: int = 1, max_steps: int = 8) -> AgentProgress:
    return AgentProgress(
        ProgressStage.RESULT, tool=tool, status=status, step=step, max_steps=max_steps
    )


def test_plain_answer_never_gets_a_status_message() -> None:
    """Eine Kopfzeile ueber „Hallo, the operator." ist Laerm, kein Beleg.

    Bis ein Werkzeug laeuft, traegt nur der Tipp-Indikator die Wartezeit — genau wie
    bei Hermes, wo Tool-Progress auf Telegram per Vorgabe gar nicht erscheint.
    """
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)

    assert client.sent == []                 # noch nichts zu zeigen
    assert client.actions == [(42, "typing")]

    client.now[0] = 2.0
    activity.progress(AgentProgress(ProgressStage.THINKING, step=1, max_steps=8))
    assert client.sent == []                 # Denken allein rechtfertigt keine Nachricht

    activity.succeed("✓ 2s")
    assert client.sent == []                 # und am Ende bleibt der Chat sauber
    assert client.edited == []


def test_success_leaves_the_trail_standing_instead_of_deleting_it() -> None:
    """Der Verlauf ist der Beleg, was Talos angefasst hat — Loeschen kostete genau den."""
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)
    activity.progress(_tool("read_file", summary="read — notes.md", step=1, max_steps=8))
    client.now[0] = 2.0
    activity.progress(_result("read_file", "done", step=1, max_steps=8))

    activity.succeed("✓ 1 tool · 2s")
    final = client.edited[-1][2]
    assert client.deleted == []          # nichts wird weggeraeumt
    assert final.startswith(f"◉ {agent_name()}")
    assert "✓ read — notes.md" in final
    assert "✓ 1 tool · 2s" in final      # die Quittung haengt darunter
    assert "step" not in final           # im Endstand kein laufender Zaehler mehr


def test_footer_is_optional_and_absent_when_nothing_was_measured() -> None:
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)
    activity.progress(_tool("read_file", summary="read — notes.md"))
    activity.succeed()
    assert client.edited[-1][2].startswith(f"◉ {agent_name()}")


def test_thinking_is_visible_and_yields_to_the_tool_it_produced() -> None:
    """Die Denkphase war die laengste und die einzige unsichtbare — das war der Fehler."""
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)

    # Der erste Werkzeuglauf legt die Anzeige an; ab da ist auch das Denken sichtbar.
    activity.progress(_tool("read_file", summary="read — notes.md", step=1, max_steps=8))
    activity.progress(_result("read_file", "done", step=1, max_steps=8))
    assert client.sent and "▸ read — notes.md" in client.sent[0][1]

    client.now[0] = 2.0   # Drosselung abgelaufen, sonst bleibt der Stand gepuffert
    activity.progress(AgentProgress(ProgressStage.THINKING, step=2, max_steps=8))
    rendered = client.edited[-1][2]
    assert "◈ reasoning" in rendered
    assert "step 2/8" in rendered

    activity.progress(_tool("write_file", summary="write — out.md", step=2, max_steps=8))
    rendered = client.edited[-1][2]
    assert "◈ reasoning" not in rendered   # weicht dem Werkzeug, das aus ihr hervorging
    assert "▸ write — out.md" in rendered


def test_failure_is_reported_even_without_a_status_message() -> None:
    """Der eine Fall, in dem Schweigen luegen wuerde — also notfalls eigene Nachricht."""
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)
    activity.fail("Timeout mit token=super-secret-value")

    assert client.edited == []
    final = client.sent[-1][1]
    assert "✕ failed: Timeout" in final
    assert "super-secret-value" not in final
    assert "[REDACTED]" in final
    assert client.deleted == []


def test_activity_failure_is_final_and_precise_but_redacted() -> None:
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)
    activity.progress(_tool("run_shell", summary="shell"))
    activity.fail("Timeout mit token=super-secret-value")

    final = client.edited[-1][2]
    assert "✕ failed: Timeout" in final
    assert "super-secret-value" not in final
    assert "[REDACTED]" in final
    assert client.deleted == []


def test_tool_updates_are_sanitized_bounded_and_coalesced() -> None:
    client = FakeTelegramClient()
    activity = TelegramActivity(
        client, 42, clock=lambda: client.now[0], min_edit_interval=1.0, max_lines=3, heartbeat_s=0
    )
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz"

    for index in range(5):
        activity.progress(
            AgentProgress(
                ProgressStage.TOOL,
                tool="run_shell",
                status="running",
                summary=f"curl -H Authorization:{secret} https://example.test/{index}",
            )
        )
    # Ein Tool-Start ist die wichtigste Zwischeninfo und wird sofort gezeigt; entscheidend
    # ist, dass jeder Edit nur EINEN Aufruf absetzt statt fuenf Nachrichten zu fluten.
    assert len(client.edited) == 5
    rendered = client.edited[-1][2]
    assert secret not in rendered
    assert "curl -H" not in rendered  # kein voller Shell-Befehl
    assert "shell command" in rendered  # generisch statt roher Befehl

    client.now[0] = 1.1
    activity.progress(AgentProgress(ProgressStage.RESULT, tool="run_shell", status="done"))
    rendered = client.edited[-1][2]
    assert len(rendered.splitlines()) <= 5  # Kopf + Hinweis auf Gekuerztes + max_lines
    assert "✓ shell command" in rendered


def test_channel_exposes_activity_without_leaking_telegram_into_conductor() -> None:
    client = FakeTelegramClient()
    channel = TelegramChannel(client)
    activity = channel.begin_activity("telegram:42")
    assert isinstance(activity, TelegramActivity)
    activity.progress(_tool("read_file", summary="read — notes.md"))
    assert client.sent[0][0] == 42


def test_failure_method_is_idempotent_after_success() -> None:
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, clock=lambda: client.now[0], heartbeat_s=0)
    activity.succeed()
    before = len(client.edited)
    activity.fail("zu spät")
    assert len(client.edited) == before   # ein abgeschlossener Lauf schreibt nicht nach


# --- Statusanzeige und mitwachsende Antwort nebeneinander ----------------------------
@dataclass
class NumberedClient(FakeTelegramClient):
    """Wie oben, aber mit unterscheidbaren Nachrichten-IDs — hier stehen zwei im Chat."""

    next_id: int = 900

    def send_message(self, chat_id: int, text: str, **kwargs) -> int:
        super().send_message(chat_id, text, **kwargs)
        self.next_id += 1
        return self.next_id


def test_growing_answer_carries_no_header_and_stands_beside_the_trail() -> None:
    """Der Beleg bleibt der Beleg, die Antwort bleibt die Antwort — zwei Nachrichten.

    Die Kopfzeile gehoert zur Statusanzeige; eine Antwort bekommt keine (Projektentscheid).
    """
    client = NumberedClient()
    clock = lambda: client.now[0]  # noqa: E731
    activity = TelegramActivity(client, 42, clock=clock, heartbeat_s=0)
    reply = TelegramReply(client, 42, clock=clock, min_edit_interval=0.0)

    activity.progress(_tool("read_file", summary="read — notes.md"))
    activity.progress(_result("read_file", "done"))
    reply.push("In der Datei ")
    reply.push("steht nichts Besonderes.")
    assert reply.adopt("In der Datei steht nichts Besonderes.") is True
    activity.succeed("✓ 1 tool · 2s")

    trail, answer = client.sent[0], client.sent[1]
    assert trail[1].startswith(f"◉ {agent_name()}")     # der Beleg traegt die Kopfzeile
    assert answer[1] == "In der Datei "                 # die Antwort faengt nackt an
    assert answer[2].get("parse_mode") is None          # halbes Markdown waere ein 400

    final_answer = [edit for edit in client.edited if edit[1] == 902][-1]
    assert final_answer[2] == "In der Datei steht nichts Besonderes."
    assert SYM_TALOS not in final_answer[2] and agent_name() not in final_answer[2]
    assert client.deleted == []


# --- Eingehende Fotos: erst ein Ziel, dann Sehen ------------------------------------
class _FakeAntwort:
    def __init__(self, *, json_daten=None, stuecke=None) -> None:
        self._json = json_daten or {}
        self._stuecke = stuecke or []
    def raise_for_status(self): return None
    def json(self): return self._json
    def iter_content(self, groesse=1): return iter(self._stuecke)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _png_bytes() -> bytes:
    import struct, zlib
    def chunk(typ, daten):
        k = typ + daten
        return struct.pack(">I", len(daten)) + k + struct.pack(">I", zlib.crc32(k) & 0xffffffff)
    roh = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(roh)) + chunk(b"IEND", b""))


def _foto_nachricht(groesse: int = 92_000) -> dict:
    return {"photo": [{"file_id": "AgAC-gross", "file_unique_id": "AQADuniq_1",
                       "width": 960, "height": 1280, "file_size": groesse}]}


def _client(tmp_path, *, inhalt: bytes | None = None, erlaubt: bool = True, monkeypatch=None):
    from talos import telegram as tg
    klient = tg.TelegramClient("123:abc", 1, inbox=tmp_path / "inbox",
                               may_fetch=lambda uid: erlaubt)
    gesehen: list = []

    def fake_call(verb, method, **kw):
        gesehen.append(method)
        return _FakeAntwort(json_daten={"ok": True, "result": {"file_path": "photos/file_7.jpg"}})

    klient._call = fake_call                                    # type: ignore[assignment]
    klient._download = lambda pfad: (gesehen.append(pfad) or (inhalt if inhalt is not None else _png_bytes()))  # type: ignore[assignment]
    return klient, gesehen


def test_a_photo_from_a_stranger_is_never_written_to_disk(tmp_path) -> None:
    """Der Kanal parst Updates, BEVOR der Kernel ueber die Kennung geurteilt hat. Ohne
    diese Frage koennte jeder Fremde, der den Bot findet, Dateien auf der Platte des
    Betreibers ablegen lassen — ohne je etwas zu duerfen."""
    klient, gesehen = _client(tmp_path, erlaubt=False)
    assert klient.fetch_photo(_foto_nachricht(), 999) == ""
    assert gesehen == []                       # es wurde nicht einmal gefragt
    assert not (tmp_path / "inbox").exists()


def test_without_an_inbox_nothing_is_fetched(tmp_path) -> None:
    """Fail-closed: ohne verdrahteten Ablageort bleibt es beim bisherigen Verhalten."""
    from talos import telegram as tg
    klient = tg.TelegramClient("123:abc", 1, may_fetch=lambda uid: True)
    assert klient.fetch_photo(_foto_nachricht(), 1) == ""


def test_an_allowed_photo_lands_under_a_name_we_chose(tmp_path) -> None:
    """Der Name kommt aus `file_unique_id`, auf Harmloses reduziert — NIE aus Telegrams
    `file_path`, sonst waere ein Verzeichniswechsel eine Antwortzeile entfernt."""
    klient, _ = _client(tmp_path)
    pfad = klient.fetch_photo(_foto_nachricht(), 1)
    assert pfad.endswith("AQADuniq_1.png")
    assert (tmp_path / "inbox" / "AQADuniq_1.png").read_bytes().startswith(b"\x89PNG")


def test_something_that_is_not_an_image_is_not_kept(tmp_path) -> None:
    """Gemessen an den ERSTEN BYTES, nicht am Content-Type und nicht an der Endung, die
    Telegram mitschickt."""
    klient, _ = _client(tmp_path, inhalt=b"%PDF-1.7 gar kein Bild")
    assert klient.fetch_photo(_foto_nachricht(), 1) == ""
    assert list((tmp_path / "inbox").glob("*")) == [] if (tmp_path / "inbox").exists() else True


def test_an_oversized_photo_is_not_even_requested(tmp_path) -> None:
    klient, gesehen = _client(tmp_path)
    from talos import telegram as tg
    assert klient.fetch_photo(_foto_nachricht(tg.MAX_ATTACHMENT_BYTES + 1), 1) == ""
    assert gesehen == []


def test_a_failed_download_costs_the_picture_not_the_message(tmp_path) -> None:
    """Eine Nachricht, die wegen eines misslungenen Downloads gar nicht ankommt, waere
    schlimmer als eine ohne Bild."""
    import requests
    klient, _ = _client(tmp_path)
    def kaputt(_pfad): raise requests.RequestException("weg")
    klient._download = kaputt                                   # type: ignore[assignment]
    assert klient.fetch_photo(_foto_nachricht(), 1) == ""


def test_the_note_names_the_path_once_there_is_one() -> None:
    """Ohne Pfad bleibt der ehrliche Blind-Satz; mit Pfad steht dort ein ZIEL, ueber das
    der Kernel urteilen kann."""
    from talos import telegram as tg
    ohne = tg.attachment_note(_foto_nachricht())
    assert tg.BLIND_NOTE in ohne and "960×1280" in ohne
    mit = tg.attachment_note(_foto_nachricht(), "/w/inbox/a.png")
    assert tg.BLIND_NOTE not in mit
    assert "/w/inbox/a.png" in mit and "see_image" in mit


def test_a_telegram_file_path_that_tries_to_escape_is_refused() -> None:
    from talos import telegram as tg
    assert tg._plausible_file_path("photos/file_7.jpg") == "photos/file_7.jpg"
    for boese in ("../../etc/passwd", "/etc/passwd", "https://anderswo.example/x", "a\\b", "", None):
        assert tg._plausible_file_path(boese) == ""


# --- Ein Abbruchbericht geht raus, auch wenn Telegram sein HTML ablehnt ---------------
@dataclass
class HtmlRejectingClient(FakeTelegramClient):
    """Bildet die echte Bot-API nach: einen Teil, dessen HTML sie ablehnt -> 400.

    Unter legacy-Markdown war der Ausloeser Alltag (`read_file` mit ungerader
    Unterstrich-Zahl); unter HTML ist er selten geworden, aber die Notbremse
    bleibt dieselbe: die Antwort ist wichtiger als ihr Satz.
    """

    def send_message(self, chat_id: int, text: str, **kwargs) -> int:
        if kwargs.get("parse_mode") == "HTML" and "FORCE400" in text:
            raise RuntimeError("400 Client Error: Bad Request (can't parse entities)")
        return super().send_message(chat_id, text, **kwargs)


def test_a_report_telegrams_html_rejects_still_goes_out_plain() -> None:
    """Zustellung schlaegt Formatierung: ohne parse_mode raus statt gar nicht.

    Dieselbe Bauart wie `TelegramReply._adopt_fallback`: die Antwort ist wichtiger
    als ihr Satz. Ohne den Fallback verlor der Kanal genau die Berichte, die der
    Betreiber am dringendsten braucht — die ueber einen Fehlschlag.
    """
    client = HtmlRejectingClient()
    channel = TelegramChannel(client)

    bericht = (
        "**FORCE400** Stopped at: read_file — error: [Errno 2] No such file or directory"
    )
    channel.send("telegram:42", bericht)

    assert len(client.sent) == 1
    _, text, kwargs = client.sent[0]
    assert text == bericht
    assert "parse_mode" not in kwargs        # unformatiert zugestellt, nicht verloren


def test_well_formed_markdown_keeps_its_formatting() -> None:
    """Der Fallback ist Notbremse, nicht Regel: gueltiges Markdown wird HTML-Satz."""
    client = HtmlRejectingClient()
    channel = TelegramChannel(client)

    channel.send("telegram:42", "Plan **abgebrochen**, Rest lief nicht.")

    _, text, kwargs = client.sent[0]
    assert kwargs.get("parse_mode") == "HTML"
    assert text == "Plan <b>abgebrochen</b>, Rest lief nicht."


# --- Eine zu lange Antwort geht raus, statt verloren zu gehen ---------------------------
def test_a_long_answer_is_split_instead_of_being_dropped() -> None:
    """⚠️ Der echte Ausfall: „could not deliver the answer", waehrend die fertige
    Antwort daneben lag.

    Die Grenze wurde nur beim WACHSEN geprueft; die fertige Antwort ging ungeteilt raus,
    Telegram lehnte mit 400 ab, und ein Lauf, der gedacht, geurteilt und ausgefuehrt
    hatte, starb an der letzten Zeile.
    """
    from talos.telegram import TELEGRAM_TEXT_LIMIT, split_for_telegram

    lang = ("Ein Absatz mit Inhalt. " * 40 + "\n\n") * 12
    teile = split_for_telegram(lang)

    assert len(teile) > 1
    assert all(len(t) <= TELEGRAM_TEXT_LIMIT for t in teile)
    assert "(1/" in teile[0]                       # eine Folge, keine losen Antworten


def test_a_normal_answer_stays_exactly_one_message() -> None:
    """Die Zustellung bleibt im Grundsatz EINE Nachricht — geteilt wird nur der Notfall."""
    from talos.telegram import split_for_telegram

    assert split_for_telegram("Der VPS läuft.") == ("Der VPS läuft.",)


def test_the_split_never_falls_inside_a_word() -> None:
    from talos.telegram import split_for_telegram

    text = " ".join(f"wort{i:04d}" for i in range(2000))
    teile = split_for_telegram(text)
    zusammen = " ".join(t.split("\n\n_(")[0] for t in teile)
    assert "wort0500" in zusammen and "wort1500" in zusammen
    for teil in teile:
        assert not teil.split("\n\n_(")[0].endswith("wort")


def test_a_single_word_longer_than_the_window_still_goes_out() -> None:
    """Ein 9000 Zeichen langer Token ohne Leerzeichen darf nicht zur Endlosschleife werden."""
    from talos.telegram import TELEGRAM_TEXT_LIMIT, split_for_telegram

    teile = split_for_telegram("A" * 9000)
    assert len(teile) >= 3
    assert all(len(t) <= TELEGRAM_TEXT_LIMIT + 20 for t in teile)


def test_expressive_status_style_uses_emoji_and_verbs() -> None:
    """Ausdrucksvoll zeichnet Werkzeuge mit Emoji und Verb; geometrisch bleibt unveraendert."""
    from talos.telegram import _tool_text
    from talos.ux import EXPRESSIVE, GEOMETRIC, SYM_TOOL, style_for

    read = AgentProgress(ProgressStage.TOOL, tool="read_file", summary="SOUL.md")
    assert _tool_text(read, GEOMETRIC) == "read — SOUL.md"
    assert _tool_text(read, EXPRESSIVE) == "Reading — SOUL.md"
    assert GEOMETRIC.tool_symbol("read_file") == SYM_TOOL
    assert EXPRESSIVE.tool_symbol("read_file") == "📖"

    # Der Shell-Befehl bleibt in beiden Stilen generisch — nie der rohe Befehl.
    shell = AgentProgress(ProgressStage.TOOL, tool="run_shell", summary="rm -rf /tmp/x")
    assert _tool_text(shell, GEOMETRIC) == "shell command"
    assert _tool_text(shell, EXPRESSIVE) == "Running command"

    assert style_for("expressive") is EXPRESSIVE
    assert style_for("nonsense") is GEOMETRIC   # unbekannt kippt die Vorgabe nie


def test_mission_retains_counts_when_the_visible_trail_is_truncated() -> None:
    from talos.ux import EXPRESSIVE
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, style=EXPRESSIVE, max_lines=2,
                                clock=lambda: client.now[0], heartbeat_s=0)
    for index in range(4):
        activity.progress(_tool("read_file", summary=f"read — file-{index}.txt"))
        activity.progress(_result("read_file", "done"))
    client.now[0] = 65
    activity.tick()
    text = client.edited[-1][2]
    assert "1m 05s" in text and "4 tool calls" in text
    assert "2 earlier events" in text and "file-0.txt" not in text
    assert "limit 8" in text and "%" not in text and "ETA" not in text
    assert len(client.sent) == 1 and client.deleted == []


def test_mission_does_not_call_a_delegated_job_complete_at_turn_end() -> None:
    from talos.ux import EXPRESSIVE
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, style=EXPRESSIVE, heartbeat_s=0)
    activity.progress(_tool("delegate_codex", summary=""))
    activity.progress(_result("delegate_codex", "done"))
    activity.succeed()
    text = client.edited[-1][2]
    assert "TURN FINISHED" in text and "Delegating to Codex" in text
    assert "job complete" not in text.lower() and "100%" not in text


def test_mission_approval_and_failure_never_get_a_success_header() -> None:
    from talos.ux import EXPRESSIVE
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, style=EXPRESSIVE, heartbeat_s=0)
    activity.progress(_tool("remote_exec", summary=""))
    activity.progress(_result("remote_exec", "needs_human"))
    activity.succeed()
    assert "NEEDS YOUR APPROVAL" in client.edited[-1][2]
    assert "TURN FINISHED" not in client.edited[-1][2]
    failed = TelegramActivity(client, 42, style=EXPRESSIVE, heartbeat_s=0)
    failed.progress(_tool("read_file", summary=""))
    failed.progress(_result("read_file", "denied"))
    failed.succeed()
    assert "TURN FINISHED WITH ISSUES" in client.edited[-1][2]
    assert "1 tool call refused or failed" in client.edited[-1][2]


def test_mission_escapes_operator_and_tool_text_and_redacts_failures() -> None:
    from talos.ux import EXPRESSIVE
    client = FakeTelegramClient()
    activity = TelegramActivity(client, 42, style=EXPRESSIVE, name='<a href="bad">Agent</a>', heartbeat_s=0)
    activity.progress(_tool("read_file", summary="read — <b>notes&more</b>"))
    activity.fail("token=never-show-this <script>boom</script>")
    text = client.edited[-1][2]
    assert client.sent[0][2]["parse_mode"] == "HTML"
    assert client.edited[-1][3]["parse_mode"] == "HTML"
    assert "<a " not in text and "<script>" not in text
    assert "&lt;b&gt;notes&amp;more&lt;/b&gt;" in text
    assert "never-show-this" not in text and "[REDACTED]" in text
    assert "STOPPED" in text
