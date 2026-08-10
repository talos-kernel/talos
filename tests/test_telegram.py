from __future__ import annotations

from unittest.mock import Mock

from talos.channel import Button, CallbackQuery, StructuredMessage
from talos.telegram import TelegramChannel, TelegramClient, Update, to_inbound


def _response(payload=None):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload or {"result": []}
    return response


def test_callback_query_is_parsed_as_inbound_data_not_executed(monkeypatch) -> None:
    payload = {
        "result": [{
            "update_id": 12,
            "callback_query": {
                "id": "cb-1",
                "data": "tm:tok:p:0",
                "from": {"id": 7},
                "message": {"message_id": 99, "chat": {"id": 42}},
            },
        }]
    }
    monkeypatch.setattr("talos.telegram.requests.get", lambda *a, **k: _response(payload))
    update = TelegramClient("secret", 1).get_updates(0)[0]
    inbound = to_inbound(update)
    assert inbound.text == ""
    assert inbound.callback == CallbackQuery("cb-1", "tm:tok:p:0", 99)
    assert inbound.principal.user_id == "7"
    assert inbound.dedup_key == "telegram:callback:cb-1"


def test_structured_message_serializes_inline_keyboard_and_edits_callback(monkeypatch) -> None:
    calls = []

    def post(url, data, timeout):
        calls.append((url, data))
        return _response()

    monkeypatch.setattr("talos.telegram.requests.post", post)
    channel = TelegramChannel(TelegramClient("secret", 1))
    ui = StructuredMessage(
        "Choose",
        ((Button("Alpha", "tm:tok:p:0"), Button("Beta", "tm:tok:p:1")),),
        edit_message_id=99,
        callback_query_id="cb-1",
    )
    channel.send_structured("telegram:42", ui)

    assert calls[0][0].endswith("/answerCallbackQuery")
    assert calls[1][0].endswith("/editMessageText")
    assert '"callback_data": "tm:tok:p:0"' in calls[1][1]["reply_markup"]


def test_callback_answer_failure_does_not_prevent_keyboard_clearing_edit(monkeypatch) -> None:
    calls = []

    def post(url, data, timeout):
        calls.append((url, data))
        response = _response()
        if url.endswith("/answerCallbackQuery"):
            response.raise_for_status.side_effect = RuntimeError("callback too old")
        return response

    monkeypatch.setattr("talos.telegram.requests.post", post)
    channel = TelegramChannel(TelegramClient("secret", 1))
    ui = StructuredMessage(
        "Working",
        (),
        edit_message_id=99,
        callback_query_id="cb-old",
    )
    try:
        channel.send_structured("telegram:42", ui)
    except RuntimeError as error:
        assert "answerCallbackQuery" in str(error)
    else:
        raise AssertionError("transport error was hidden")

    assert [url.rsplit("/", 1)[-1] for url, _data in calls] == [
        "answerCallbackQuery",
        "editMessageText",
    ]
    assert calls[1][1]["reply_markup"] == '{"inline_keyboard": []}'


def test_callback_data_over_telegram_limit_is_rejected() -> None:
    channel = TelegramChannel(TelegramClient("secret", 1))
    ui = StructuredMessage("bad", ((Button("x", "x" * 65),),))
    try:
        channel.send_structured("telegram:42", ui)
    except ValueError as error:
        assert "64" in str(error)
    else:
        raise AssertionError("oversize callback data accepted")


# --- Angehaengtes: nicht mehr lautlos fallen lassen ---------------------------------
def _update_mit(message: dict) -> list:
    """Ein Telegram-Update mit beliebigem Inhalt, durch den echten Parser."""
    from talos.telegram import TelegramClient

    client = TelegramClient("t", 1)
    payload = {"result": [{
        "update_id": 7,
        "message": {"from": {"id": 100000001}, "chat": {"id": 100000001}, **message},
    }]}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return payload

    client._call = lambda *_a, **_k: _Resp()  # type: ignore[assignment]
    return client.get_updates(0)


def test_a_photo_no_longer_disappears_without_a_word() -> None:
    """Vorher galt `if text is None: continue` — ein Bild verschwand spurlos, und von
    aussen war das nicht von einem toten Bot zu unterscheiden."""
    updates = _update_mit({"photo": [
        {"width": 320, "height": 180, "file_size": 8000},
        {"width": 1280, "height": 720, "file_size": 86000},
    ]})
    assert len(updates) == 1
    text = updates[0].text
    assert "photo attached" in text
    assert "1280" in text and "720" in text      # die GROESSTE Fassung, nicht die erste
    assert "84 kB" in text


def test_the_caption_is_the_operators_word_and_comes_first() -> None:
    """Die Bildunterschrift ist das, was der Mensch gesagt hat. Die Dateifakten sind
    Beobachtung und stehen darunter."""
    updates = _update_mit({
        "photo": [{"width": 100, "height": 100, "file_size": 2048}],
        "caption": "Was steht auf dem Schild?",
    })
    zeilen = updates[0].text.split("\n")
    assert zeilen[0] == "Was steht auf dem Schild?"
    assert zeilen[1].startswith("[photo attached")


def test_the_note_admits_that_the_content_is_not_visible() -> None:
    """Der Satz, der die Selbsttaeuschung verhindert: sonst reimt sich das Modell aus
    Dateiname und Massen eine Bildbeschreibung zusammen, und sie liest sich wie eine
    Wahrnehmung."""
    from talos.telegram import BLIND_NOTE

    for anhang in (
        {"photo": [{"width": 10, "height": 10, "file_size": 100}]},
        {"voice": {"duration": 7, "file_size": 12000}},
        {"document": {"file_name": "bericht.pdf", "file_size": 240000}},
    ):
        assert BLIND_NOTE in _update_mit(anhang)[0].text


def test_voice_and_documents_are_described_with_measured_values() -> None:
    stimme = _update_mit({"voice": {"duration": 7, "file_size": 12288}})[0].text
    assert "voice message" in stimme and "7 s" in stimme and "12 kB" in stimme

    datei = _update_mit({"document": {"file_name": "bericht.pdf", "file_size": 240000}})[0].text
    assert "bericht.pdf" in datei and "234 kB" in datei


def test_a_message_with_neither_text_nor_attachment_is_still_skipped() -> None:
    """Ein Beitritts- oder Pin-Ereignis ist keine Nachricht an Talos."""
    assert _update_mit({"new_chat_members": [{"id": 5}]}) == []
