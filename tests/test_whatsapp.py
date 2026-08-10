"""WhatsApp ist ein Melde-Weg — die Tests bewachen genau diese Grenze.

Kein Test fasst das Netz an: der Kanal bekommt seinen einzigen HTTP-Aufruf injiziert.
Der wichtigste Fall hier ist kein Feature, sondern eine Zusicherung — der Zugangstoken
darf in keiner Ausgabe und in keiner Ausnahme auftauchen.
"""
from __future__ import annotations

import traceback

from talos.channel import Button, Channel, ChannelRegistry, StructuredMessage, Trust
from talos.whatsapp import (
    GRAPH_API_VERSION,
    OutsideWindowError,
    WhatsAppChannel,
    WhatsAppError,
    conversation_for,
    number_of,
    split_text,
)

TOKEN = "EAAGm0PX4ZCpsBO-super-secret-access-token"
PHONE_ID = "109876543210987"
TO = "whatsapp:41791234567"


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = {"messages": [{"id": "wamid.1"}]} if payload is None else payload

    def json(self) -> dict:
        return self._payload


class _Http:
    """Faelscht genau den einen erlaubten Aufruf und schreibt mit, was gesendet wurde."""

    def __init__(self, *responses: _Response, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)
        self._error = error

    def __call__(self, url: str, *, json: dict, headers: dict, timeout: float) -> _Response:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._responses.pop(0) if self._responses else _Response(200)


def _channel(http: _Http | None = None, **kwargs: object) -> WhatsAppChannel:
    return WhatsAppChannel(TOKEN, PHONE_ID, post=http or _Http(), **kwargs)


# --------------------------------------------------------------- Vertrauensstufe
def test_trust_is_notify_and_cannot_be_raised() -> None:
    channel = _channel()
    assert channel.trust is Trust.NOTIFY
    assert ChannelRegistry((channel,)).trust_of("whatsapp") is Trust.NOTIFY
    try:
        channel.trust = Trust.FULL   # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("the channel ceiling could be raised at runtime")
    assert channel.trust is Trust.NOTIFY


def test_channel_satisfies_the_channel_protocol() -> None:
    assert isinstance(_channel(), Channel)
    assert _channel().name == "whatsapp"


# --------------------------------------------------------------- kein Rueckweg
def test_poll_is_empty_and_never_touches_the_network() -> None:
    http = _Http()
    channel = _channel(http)
    assert channel.poll() == []
    assert channel.poll() == []
    assert http.calls == []


# --------------------------------------------------------------- Zustellung
def test_message_goes_out_with_the_right_body_and_number() -> None:
    http = _Http()
    _channel(http).send(TO, "Snapshot restored.")

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == (
        f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_ID}/messages"
    )
    assert call["json"] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "41791234567",
        "type": "text",
        "text": {"preview_url": False, "body": "Snapshot restored."},
    }
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["timeout"] > 0


def test_empty_text_sends_nothing_instead_of_an_empty_message() -> None:
    http = _Http()
    _channel(http).send(TO, "   \n  ")
    assert http.calls == []


# --------------------------------------------------------------- Nummernpruefung
def test_malformed_conversation_or_number_is_refused_without_a_request() -> None:
    bad = [
        "telegram:41791234567",     # fremder Kanal
        "41791234567",              # ohne Kanal
        "whatsapp:+41791234567",    # '+' ist Schreibweise, keine Nummer
        "whatsapp:41 79 123 45 67",
        "whatsapp:41-79-123-45-67",
        "whatsapp:",
        "whatsapp:12345",           # zu kurz
        "whatsapp:" + "1" * 16,     # laenger als E.164 erlaubt
        "whatsapp:41791234567a",
        "whatsapp:٤١٧٩١٢٣٤٥٦٧",      # arabisch-indische Ziffern (str.isdigit() saehe True)
    ]
    for conversation in bad:
        http = _Http()
        try:
            _channel(http).send(conversation, "hello")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted malformed conversation: {conversation!r}")
        assert http.calls == [], f"tried to deliver anyway: {conversation!r}"


def test_conversation_for_validates_while_building() -> None:
    assert conversation_for("41791234567") == TO
    assert number_of(TO) == "41791234567"
    try:
        conversation_for("+41 79 123 45 67")
    except ValueError:
        pass
    else:
        raise AssertionError("built a conversation from an unusable number")


# --------------------------------------------------------------- langer Text
def test_split_prefers_line_then_word_boundaries() -> None:
    assert split_text("alpha beta\ngamma delta epsilon", limit=16) == (
        "alpha beta",
        "gamma delta",
        "epsilon",
    )
    # Ein unteilbares Wort wird hart geschnitten — zustellen schlaegt schweigen.
    assert split_text("x" * 25, limit=10) == ("x" * 10, "x" * 10, "x" * 5)


def test_long_text_is_split_at_sensible_boundaries_and_delivered_completely() -> None:
    paragraph = " ".join(f"word{index:03d}" for index in range(60))
    text = "\n".join([paragraph] * 4)
    http = _Http()
    _channel(http, text_limit=100).send(TO, text)

    bodies = [call["json"]["text"]["body"] for call in http.calls]
    assert len(bodies) > 1
    assert all(len(body) <= 100 for body in bodies)
    # Nichts verloren und kein Wort mitten durchgeschnitten.
    assert " ".join(bodies).split() == text.split()
    # An der Zeilengrenze getrennt: ein Absatz endet auch als Teil auf seinem letzten Wort.
    assert any(body.endswith("word059") for body in bodies)


# --------------------------------------------------------------- 24-Stunden-Fenster
def test_outside_the_24_hour_window_is_reported_in_plain_language() -> None:
    payload = {
        "error": {
            "message": "(#131047) Re-engagement message",
            "type": "OAuthException",
            "code": 131047,
        }
    }
    try:
        _channel(_Http(_Response(400, payload))).send(TO, "status")
    except OutsideWindowError as error:
        assert "outside the 24-hour window" in str(error)
        assert "approved templates" in str(error)
        assert "131047" not in str(error)          # kein roher API-Fehler
        assert isinstance(error, WhatsAppError)    # die Registry sieht einen Zustellfehler
    else:
        raise AssertionError("the closed customer window was not recognised")


def test_legacy_window_code_is_recognised_too() -> None:
    payload = {"error": {"message": "outside the allowed window", "code": 470}}
    try:
        _channel(_Http(_Response(400, payload))).send(TO, "status")
    except OutsideWindowError as error:
        assert "24-hour window" in str(error)
    else:
        raise AssertionError("legacy window code 470 was not recognised")


# --------------------------------------------------------------- Token-Dichtheit
def test_token_appears_in_no_output_and_in_no_exception() -> None:
    # Genau der Fall, den HTTP-Bibliotheken verursachen: die Ausnahme zitiert den Header.
    leaky = RuntimeError(
        f"POST https://graph.facebook.com/... failed "
        f"(headers={{'Authorization': 'Bearer {TOKEN}'}})"
    )
    channel = _channel(_Http(error=leaky))
    assert TOKEN not in repr(channel) and TOKEN not in str(channel)
    try:
        channel.send(TO, "status")
    except WhatsAppError as error:
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        assert TOKEN not in str(error)
        assert TOKEN not in repr(error)
        assert TOKEN not in rendered, "token leaked through the exception chain"
        assert "[REDACTED]" in str(error)
    else:
        raise AssertionError("a dead transport was swallowed")


def test_token_echoed_by_the_api_is_redacted_as_well() -> None:
    payload = {"error": {"message": f"Invalid OAuth access token: {TOKEN}", "code": 190}}
    try:
        _channel(_Http(_Response(401, payload))).send(TO, "status")
    except WhatsAppError as error:
        assert TOKEN not in str(error)
        assert "[REDACTED]" in str(error)
    else:
        raise AssertionError("an authentication failure was swallowed")


# --------------------------------------------------------------- laut statt still
def test_delivery_failure_raises_visibly_through_the_registry() -> None:
    registry = ChannelRegistry((_channel(_Http(_Response(500, {}))),))
    try:
        registry.send(TO, "important")
    except WhatsAppError as error:
        assert "500" in str(error)
    else:
        raise AssertionError("an HTTP 500 was reported as a delivered message")


def test_unreadable_response_counts_as_failure_not_as_delivery() -> None:
    class _Broken:
        status_code = "not a number"

        def json(self) -> dict:
            raise ValueError("no body")

    try:
        _channel(_Http(_Broken())).send(TO, "status")   # type: ignore[arg-type]
    except WhatsAppError:
        pass
    else:
        raise AssertionError("an unreadable response passed as a delivered message")


# --------------------------------------------------------------- keine Knoepfe
def test_structured_message_falls_back_to_text_and_renders_no_buttons() -> None:
    http = _Http()
    registry = ChannelRegistry((_channel(http),))
    registry.send_structured(
        TO,
        StructuredMessage("Approve write?", ((Button("Yes", "tm:t:p:0"),),)),
    )

    call = http.calls[0]
    assert call["json"]["type"] == "text"
    assert call["json"]["text"]["body"] == "Approve write?"
    assert "tm:t:p:0" not in repr(call), "a button was rebuilt on a notify-only channel"


# --------------------------------------------------------------- Konfiguration
def test_unusable_credentials_are_refused_at_construction() -> None:
    for token, phone_id in (("", PHONE_ID), ("  ", PHONE_ID), (TOKEN, ""), (TOKEN, "phone-1")):
        try:
            WhatsAppChannel(token, phone_id, post=_Http())
        except ValueError:
            continue
        raise AssertionError(f"accepted unusable credentials: {token!r}/{phone_id!r}")
