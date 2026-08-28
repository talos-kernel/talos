"""Broker-WhatsApp: der SSH-Pull-Kanal — die Tests bewachen seinen Vertrag.

Kein Test fasst SSH oder `subprocess` an: der Kanal bekommt seinen einzigen
Subprozess-Aufruf injiziert (`runner`), der Cursor einen Pfad unter `tmp_path`.
Die wichtigsten Zusicherungen: der erste Lauf holt keinen Backlog, ein
fehlgeschlagener Poll rueckt den Cursor nicht vor, und eine kaputte conversation
wird abgelehnt, BEVOR irgendein Kommando laeuft.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from talos import config
from talos.channel import Button, Channel, ChannelRegistry, StructuredMessage, Trust
from talos.wabroker import (
    DEFAULT_CLI_DIR,
    DEFAULT_QUEUE_PATH,
    POLL_MAX_ENTRIES,
    BrokerError,
    BrokerWhatsAppChannel,
    split_text,
)

TARGET = "hermes"
QUEUE = DEFAULT_QUEUE_PATH
CLI_DIR = DEFAULT_CLI_DIR
TO = "whatsapp:41786676731"


class _Runner:
    """Faelscht genau den einen erlaubten Aufruf und schreibt jedes Kommando mit."""

    def __init__(self, *results: tuple[int, bytes, bytes], error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results)
        self._error = error

    def __call__(self, cmd: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(list(cmd))
        if self._error is not None:
            raise self._error
        return self._results.pop(0) if self._results else (0, b"", b"")


def _channel(runner: _Runner | None = None, tmp_path: Path | None = None, **kwargs: object) -> BrokerWhatsAppChannel:
    cursor_path = (tmp_path or Path("/nonexistent-dir")) / "cursor.json"
    return BrokerWhatsAppChannel(
        TARGET, QUEUE, CLI_DIR, runner=runner or _Runner(), cursor_path=cursor_path, **kwargs
    )


def _entry(message_id: str, text: str, sender: str = "41786676731") -> bytes:
    return json.dumps({
        "at": "2026-08-28T10:00:00.000Z",
        "atMs": 1756461600000,
        "messageId": message_id,
        "chatJid": f"{sender}@s.whatsapp.net",
        "senderNumber": sender,
        "pushName": "Ali",
        "text": text,
    }).encode() + b"\n"


def _seeded(channel: BrokerWhatsAppChannel, runner: _Runner, size: int = 100) -> None:
    """Bringt den Kanal ueber den Erstlauf hinweg: Cursor steht am Dateiende."""
    runner._results.insert(0, (0, f"{size}\n".encode(), b""))
    assert channel.poll() == []
    assert len(runner.calls) == 1


# --------------------------------------------------------------- Vertrauensstufe
def test_trust_is_full_and_cannot_be_changed(tmp_path: Path) -> None:
    channel = _channel(tmp_path=tmp_path)
    assert channel.trust is Trust.FULL
    assert ChannelRegistry((channel,)).trust_of("whatsapp") is Trust.FULL
    try:
        channel.trust = Trust.NOTIFY  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("the channel ceiling could be changed at runtime")
    assert channel.trust is Trust.FULL


def test_channel_satisfies_the_channel_protocol(tmp_path: Path) -> None:
    channel = _channel(tmp_path=tmp_path)
    assert isinstance(channel, Channel)
    assert channel.name == "whatsapp"


# --------------------------------------------------------------- Erstlauf
def test_first_run_starts_at_end_of_queue_and_returns_nothing(tmp_path: Path) -> None:
    runner = _Runner((0, b"4096\n", b""))
    channel = _channel(runner, tmp_path)

    assert channel.poll() == []
    command = runner.calls[0]
    assert command[:2] == ["ssh", "-o"]
    assert "BatchMode=yes" in command
    assert TARGET in command
    assert f"stat -c %s {QUEUE}" in command[-1]
    # Der Cursor steht persistiert am Dateiende — ein Neustart holt keinen Backlog.
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 4096}


def test_first_run_with_empty_queue_sizes_zero(tmp_path: Path) -> None:
    runner = _Runner((0, b"0\n", b""))
    assert _channel(runner, tmp_path).poll() == []
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 0}


# --------------------------------------------------------------- Abholen
def test_poll_maps_entries_and_advances_the_cursor_by_bytes(tmp_path: Path) -> None:
    payload = _entry("ABCD1234", "status der agenten") + _entry("EFGH5678", "zweite")
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._results.append((0, payload, b""))

    inbounds = channel.poll()

    assert len(inbounds) == 2
    first = inbounds[0]
    assert first.principal.channel == "whatsapp"
    assert first.principal.user_id == "41786676731"
    assert first.conversation == "whatsapp:41786676731"
    assert first.text == "status der agenten"
    assert first.dedup_key == "whatsapp:msg:ABCD1234"
    assert inbounds[1].dedup_key == "whatsapp:msg:EFGH5678"
    # tail ab Byte-Offset+1 vom geseedeten Stand 100.
    assert "tail -c +101 " in runner.calls[1][-1]
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 100 + len(payload)}
    # Zweiter Poll ohne neue Daten: leer, Cursor unveraendert.
    assert channel.poll() == []
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 100 + len(payload)}


def test_sender_falls_back_to_chat_jid_digits(tmp_path: Path) -> None:
    payload = json.dumps({
        "messageId": "JID1",
        "chatJid": "41791234567@s.whatsapp.net",
        "text": "ohne senderNumber",
    }).encode() + b"\n"
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._results.append((0, payload, b""))

    (inbound,) = channel.poll()
    assert inbound.conversation == "whatsapp:41791234567"


def test_malformed_lines_and_incomplete_entries_are_skipped(tmp_path: Path) -> None:
    payload = (
        b"das ist kein json\n"
        + json.dumps({"text": "keine messageId"}).encode() + b"\n"
        + json.dumps({"messageId": "X1", "text": "   "}).encode() + b"\n"
        + json.dumps(["kein", "objekt"]).encode() + b"\n"
        + _entry("OK1", "gilt")
    )
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._results.append((0, payload, b""))

    (inbound,) = channel.poll()
    assert inbound.dedup_key == "whatsapp:msg:OK1"


def test_a_partial_last_line_is_not_consumed(tmp_path: Path) -> None:
    """Der Broker schreibt zeilenweise: ein halber Append wartet auf den naechsten Poll."""
    complete = _entry("FULL1", "fertig")
    partial = b'{"messageId": "HALB", "text": "noch im Schrei'
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._results.append((0, complete + partial, b""))

    (inbound,) = channel.poll()
    assert inbound.dedup_key == "whatsapp:msg:FULL1"
    # Nur die komplette Zeile rueckt den Cursor vor — der Rest wird erneut gelesen.
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 100 + len(complete)}


def test_entry_cap_is_enforced_and_the_remainder_waits(tmp_path: Path) -> None:
    lines = [_entry(f"ID{i:03d}", f"nachricht {i}") for i in range(POLL_MAX_ENTRIES + 10)]
    payload = b"".join(lines)
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._results.append((0, payload, b""))

    inbounds = channel.poll()
    assert len(inbounds) == POLL_MAX_ENTRIES
    # Der Cursor steht hinter der letzten GELIEFERTEN Zeile, nicht am Dateiende.
    expected = 100 + len(b"".join(lines[:POLL_MAX_ENTRIES]))
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": expected}


# --------------------------------------------------------------- Fehlerpfad
def test_ssh_failure_raises_and_does_not_advance_the_cursor(tmp_path: Path) -> None:
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._results.append((255, b"", b"Connection refused"))

    try:
        channel.poll()
    except BrokerError as error:
        assert "255" in str(error)
        assert "Connection refused" in str(error)
    else:
        raise AssertionError("a dead ssh was reported as an empty queue")
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 100}


def test_runner_exception_raises_and_does_not_advance_the_cursor(tmp_path: Path) -> None:
    runner = _Runner()
    channel = _channel(runner, tmp_path)
    _seeded(channel, runner)
    runner._error = TimeoutError("ssh timed out")

    try:
        channel.poll()
    except BrokerError:
        pass
    else:
        raise AssertionError("a timed-out runner was swallowed")
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"cursor": 100}


def test_first_run_failure_leaves_no_cursor(tmp_path: Path) -> None:
    runner = _Runner((255, b"", b"host unknown"))
    channel = _channel(runner, tmp_path)
    try:
        channel.poll()
    except BrokerError:
        pass
    else:
        raise AssertionError("a failed first run was swallowed")
    assert not (tmp_path / "cursor.json").exists()


# --------------------------------------------------------------- Zustellung
def _b64_of(remote_command: str) -> str:
    match = re.search(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d", remote_command)
    assert match, f"no base64 payload in: {remote_command}"
    return base64.b64decode(match.group(1)).decode("utf-8")


def test_send_runs_the_broker_send_script_with_the_exact_text(tmp_path: Path) -> None:
    runner = _Runner()
    _channel(runner, tmp_path).send(TO, "Snapshot restored. Ümläute & $dollar")

    assert len(runner.calls) == 1
    command = runner.calls[0]
    assert command[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    assert command[5] == TARGET
    remote = command[6]
    assert remote.startswith(f"cd {CLI_DIR} && ")
    assert "node scripts/send.js --to 41786676731 --text \"$T\"" in remote
    assert _b64_of(remote) == "Snapshot restored. Ümläute & $dollar"


def test_malformed_conversation_is_refused_before_any_runner_call(tmp_path: Path) -> None:
    bad = [
        "telegram:41786676731",      # fremder Kanal
        "41786676731",               # ohne Kanal
        "whatsapp:+41786676731",     # '+' ist Schreibweise, keine Nummer
        "whatsapp:41 786 67 67 31",
        "whatsapp:",
        "whatsapp:12345",            # zu kurz
        "whatsapp:" + "1" * 16,      # laenger als E.164 erlaubt
    ]
    for conversation in bad:
        runner = _Runner()
        try:
            _channel(runner, tmp_path).send(conversation, "hello")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted malformed conversation: {conversation!r}")
        assert runner.calls == [], f"tried to deliver anyway: {conversation!r}"


def test_long_text_is_split_in_order_and_delivered_completely(tmp_path: Path) -> None:
    paragraph = " ".join(f"word{index:03d}" for index in range(40))
    text = "\n\n".join([paragraph] * 3)
    runner = _Runner()
    _channel(runner, tmp_path, text_limit=100).send(TO, text)

    bodies = [_b64_of(call[6]) for call in runner.calls]
    assert len(bodies) > 1
    assert all(len(body) <= 100 for body in bodies)
    # Nichts verloren, kein Wort mitten durchgeschnitten, Reihenfolge erhalten.
    assert " ".join(bodies).split() == text.split()


def test_split_prefers_paragraph_then_line_then_word_boundaries() -> None:
    assert split_text("alpha beta\ngamma delta epsilon", limit=16) == (
        "alpha beta",
        "gamma delta",
        "epsilon",
    )
    # Ein unteilbares Wort wird hart geschnitten — zustellen schlaegt schweigen.
    assert split_text("x" * 25, limit=10) == ("x" * 10, "x" * 10, "x" * 5)
    assert split_text("   \n  ") == ()


def test_send_failure_raises_with_stderr_but_no_payload(tmp_path: Path) -> None:
    runner = _Runner((1, b"", b"whatsapp-cli: not logged in"))
    try:
        _channel(runner, tmp_path).send(TO, "geheimer inhalt")
    except BrokerError as error:
        assert "not logged in" in str(error)
        assert base64.b64encode("geheimer inhalt".encode()).decode() not in str(error)
    else:
        raise AssertionError("a failed broker send was swallowed")


# --------------------------------------------------------------- Dateianhang
def test_send_file_uploads_then_sends_a_pdf_as_document(tmp_path: Path) -> None:
    document = tmp_path / "bericht final.pdf"
    document.write_bytes(b"%PDF-fake")
    runner = _Runner()

    assert _channel(runner, tmp_path).send_file(TO, str(document)) is True

    assert len(runner.calls) == 2
    scp = runner.calls[0]
    assert scp[0] == "scp"
    assert scp[-2] == str(document)
    remote_path = scp[-1]
    assert remote_path.startswith(f"{TARGET}:/tmp/wa_")
    assert remote_path.endswith("_bericht_final.pdf")  # Leerzeichen -> Unterstrich
    send = runner.calls[1][-1]
    assert f"--document {remote_path.split(':', 1)[1]}" in send
    assert "--filename 'bericht final.pdf'" in send
    assert "--mimetype application/pdf" in send
    assert "--to 41786676731" in send


def test_send_file_sends_a_png_as_image(tmp_path: Path) -> None:
    image = tmp_path / "foto.png"
    image.write_bytes(b"\x89PNG")
    runner = _Runner()

    assert _channel(runner, tmp_path).send_file(TO, str(image)) is True

    send = runner.calls[1][-1]
    assert "--image /tmp/wa_" in send
    assert "--document" not in send
    assert "foto.png" in runner.calls[0][-1]


def test_send_file_of_a_missing_local_file_fails_before_any_runner_call(tmp_path: Path) -> None:
    runner = _Runner()
    try:
        _channel(runner, tmp_path).send_file(TO, str(tmp_path / "gibts-nicht.pdf"))
    except BrokerError:
        pass
    else:
        raise AssertionError("a missing local file reached the broker")
    assert runner.calls == []


def test_failed_upload_does_not_send(tmp_path: Path) -> None:
    document = tmp_path / "a.pdf"
    document.write_bytes(b"%PDF")
    runner = _Runner((1, b"", b"scp: permission denied"))
    try:
        _channel(runner, tmp_path).send_file(TO, str(document))
    except BrokerError as error:
        assert "permission denied" in str(error)
    else:
        raise AssertionError("a failed upload was swallowed")
    assert len(runner.calls) == 1  # kein send.js hinterher


# --------------------------------------------------------------- Strukturiert
def test_structured_message_sends_text_and_button_labels_as_lines(tmp_path: Path) -> None:
    runner = _Runner()
    registry = ChannelRegistry((_channel(runner, tmp_path),))
    registry.send_structured(
        TO,
        StructuredMessage("Approve write?", ((Button("Yes", "d1"), Button("No", "d2")),)),
    )
    body = _b64_of(runner.calls[0][-1])
    assert body == "Approve write?\n[Yes]\n[No]"
    assert "d1" not in body  # Callback-Daten gehoeren nicht in die Nachricht


# --------------------------------------------------------------- Konfiguration
def test_config_defaults_to_disabled_with_documented_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TALOS_WA_BROKER_SSH", "TALOS_WA_BROKER_QUEUE", "TALOS_WA_BROKER_CLI_DIR"):
        monkeypatch.delenv(name, raising=False)
    cfg = config.load_config(require_channel=False)
    assert cfg.wa_broker_ssh == ""                # aus, bis der Betreiber opt-in gibt
    assert cfg.wa_broker_queue == DEFAULT_QUEUE_PATH
    assert cfg.wa_broker_cli_dir == DEFAULT_CLI_DIR


def test_config_is_enabled_only_by_a_non_empty_ssh_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALOS_WA_BROKER_SSH", "")
    assert config.load_config(require_channel=False).wa_broker_ssh == ""
    monkeypatch.setenv("TALOS_WA_BROKER_SSH", "  ")
    assert config.load_config(require_channel=False).wa_broker_ssh == ""
    monkeypatch.setenv("TALOS_WA_BROKER_SSH", "hermes")
    assert config.load_config(require_channel=False).wa_broker_ssh == "hermes"


def test_config_reads_custom_queue_and_cli_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALOS_WA_BROKER_SSH", "vps-2")
    monkeypatch.setenv("TALOS_WA_BROKER_QUEUE", "/var/lib/broker/queue.jsonl")
    monkeypatch.setenv("TALOS_WA_BROKER_CLI_DIR", "/opt/wa-cli")
    cfg = config.load_config(require_channel=False)
    assert cfg.wa_broker_ssh == "vps-2"
    assert cfg.wa_broker_queue == "/var/lib/broker/queue.jsonl"
    assert cfg.wa_broker_cli_dir == "/opt/wa-cli"
