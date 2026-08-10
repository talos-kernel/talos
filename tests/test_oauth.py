"""Device-Flow-Anmeldung — die Tests bewachen die Zusagen, nicht die Zeilen.

Kein Test fasst das Netz an, keiner wartet echte Sekunden: HTTP, Anzeige, Uhr und Warten
werden injiziert. Der wichtigste Fall hier ist kein Feature, sondern eine Zusicherung —
kein Token steht je in einer Ausgabe oder in einer Ausnahmekette.
"""
from __future__ import annotations

import json
import os
import stat
import traceback
from pathlib import Path

import pytest

from talos.oauth import (
    DEVICE_CODE_GRANT,
    REFRESH_TOKEN_GRANT,
    AuthorizationDeclined,
    AuthorizationExpired,
    DeviceFlow,
    NotLoggedIn,
    OAuthError,
    ProviderConfig,
    StoredToken,
    TokenStore,
    sign_in_prompt,
)

ACCESS = "xai-access-9f3c-super-secret-value"
REFRESH = "xai-refresh-1a2b-also-super-secret"
DEVICE_CODE = "device-code-7c8d-secret"
USER_CODE = "WDJB-MJHT"

CONFIG = ProviderConfig(
    name="xai",
    client_id="talos-operator-client",
    device_code_url="https://api.provider.test/oauth2/device/code",
    token_url="https://api.provider.test/oauth2/token",
    scope="api",
)


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Http:
    """Faelscht genau den einen erlaubten Aufruf und schreibt mit, was gesendet wurde."""

    def __init__(
        self,
        *responses: _Response,
        default: _Response | None = None,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)
        self._default = default
        self._error = error

    def __call__(self, url: str, *, data: dict, timeout: float) -> _Response:
        self.calls.append({"url": url, "data": dict(data), "timeout": timeout})
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        raise AssertionError(f"unexpected HTTP call to {url}")


class _Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class _Sleeper:
    """Verbrennt keine Zeit, sondern schiebt die gefaelschte Uhr — sonst laeuft nichts ab."""

    def __init__(self, clock: _Clock) -> None:
        self.calls: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._clock.now += seconds


def _device_response(**overrides: object) -> _Response:
    payload = {
        "device_code": DEVICE_CODE,
        "user_code": USER_CODE,
        "verification_uri": "https://provider.test/device",
        "expires_in": 600,
        "interval": 5,
    }
    return _Response({**payload, **overrides})


def _token_response(**overrides: object) -> _Response:
    payload = {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    return _Response({**payload, **overrides})


def _error_response(error: str, status_code: int = 400) -> _Response:
    return _Response({"error": error, "error_description": f"provider says: {error}"}, status_code)


def _flow(
    tmp_path: Path, http: _Http, *, clock: _Clock | None = None, notices: list[str] | None = None
) -> tuple[DeviceFlow, _Sleeper, _Clock, list[str]]:
    the_clock = clock or _Clock()
    sleeper = _Sleeper(the_clock)
    seen = notices if notices is not None else []
    flow = DeviceFlow(
        CONFIG,
        TokenStore(tmp_path / "oauth"),
        post=http,
        notify=seen.append,
        sleep=sleeper,
        clock=the_clock,
    )
    return flow, sleeper, the_clock, seen


# ------------------------------------------------------------------ glatter Durchlauf
def test_successful_login_stores_the_token(tmp_path: Path) -> None:
    http = _Http(_device_response(), _token_response())
    flow, sleeper, clock, notices = _flow(tmp_path, http)

    token = flow.login()

    assert token.access_token == ACCESS
    assert token.refresh_token == REFRESH
    assert token.expires_at == pytest.approx(clock.now + 3600)
    assert flow.is_logged_in()
    assert flow.access_token() == ACCESS
    # Erst der Geraetecode-Endpunkt, dann der Token-Endpunkt — mit den Feldern aus RFC 8628.
    assert http.calls[0]["url"] == CONFIG.device_code_url
    assert http.calls[0]["data"] == {"client_id": CONFIG.client_id, "scope": "api"}
    assert http.calls[1]["url"] == CONFIG.token_url
    assert http.calls[1]["data"]["grant_type"] == DEVICE_CODE_GRANT
    assert http.calls[1]["data"]["device_code"] == DEVICE_CODE
    # Vor dem ersten Poll wird gewartet — der Mensch hat den Code noch gar nicht getippt.
    assert sleeper.calls == [5.0]
    assert len(notices) == 1 and USER_CODE in notices[0]


def test_prompt_shows_the_complete_uri_and_the_user_code(tmp_path: Path) -> None:
    http = _Http(
        _device_response(verification_uri_complete="https://provider.test/device?code=WDJB-MJHT"),
        _token_response(),
    )
    flow, _, _, notices = _flow(tmp_path, http)

    flow.login()

    assert "https://provider.test/device?code=WDJB-MJHT" in notices[0]
    assert USER_CODE in notices[0]


# ------------------------------------------------------------------ Poll-Verhalten
def test_authorization_pending_keeps_polling(tmp_path: Path) -> None:
    http = _Http(
        _device_response(),
        _error_response("authorization_pending"),
        _error_response("authorization_pending"),
        _token_response(),
    )
    flow, sleeper, _, _ = _flow(tmp_path, http)

    assert flow.login().access_token == ACCESS
    assert len(http.calls) == 4  # 1x Geraetecode + 3x Token-Endpunkt
    assert sleeper.calls == [5.0, 5.0, 5.0]  # geduldig, ohne das Intervall anzufassen


def test_slow_down_increases_the_interval(tmp_path: Path) -> None:
    """Wer auf `slow_down` im alten Takt weiterfragt, wird vom Anbieter gesperrt."""
    http = _Http(
        _device_response(),
        _error_response("authorization_pending"),
        _error_response("slow_down"),
        _error_response("authorization_pending"),
        _token_response(),
    )
    flow, sleeper, _, _ = _flow(tmp_path, http)

    flow.login()

    assert sleeper.calls == [5.0, 5.0, 10.0, 10.0]  # nach dem slow_down dauerhaft langsamer


def test_expired_token_and_access_denied_are_distinguishable(tmp_path: Path) -> None:
    denied_http = _Http(_device_response(), _error_response("access_denied"))
    denied_flow, _, _, _ = _flow(tmp_path / "a", denied_http)
    with pytest.raises(AuthorizationDeclined) as declined:
        denied_flow.login()

    expired_http = _Http(_device_response(), _error_response("expired_token"))
    expired_flow, _, _, _ = _flow(tmp_path / "b", expired_http)
    with pytest.raises(AuthorizationExpired) as expired:
        expired_flow.login()

    assert "declined" in str(declined.value)
    assert "expired" in str(expired.value)
    assert str(declined.value) != str(expired.value)
    # Unterschiedliche Klassen, damit ein Aufrufer sie ohne Textvergleich trennen kann.
    assert not isinstance(declined.value, AuthorizationExpired)
    assert not isinstance(expired.value, AuthorizationDeclined)
    assert not denied_flow.is_logged_in() and not expired_flow.is_logged_in()


def test_unknown_error_aborts_with_the_provider_code(tmp_path: Path) -> None:
    http = _Http(_device_response(), _error_response("invalid_client"))
    flow, _, _, _ = _flow(tmp_path, http)

    with pytest.raises(OAuthError) as failure:
        flow.login()

    assert "invalid_client" in str(failure.value)
    assert not isinstance(failure.value, (AuthorizationDeclined, AuthorizationExpired))


def test_overall_deadline_aborts_the_flow(tmp_path: Path) -> None:
    """Der Anbieter setzt die Frist; danach ist der Geraetecode wertlos."""
    http = _Http(
        _device_response(expires_in=30),
        default=_error_response("authorization_pending"),
    )
    flow, sleeper, _, _ = _flow(tmp_path, http)

    with pytest.raises(AuthorizationExpired) as expired:
        flow.login()

    assert "timed out" in str(expired.value)
    assert sum(sleeper.calls) <= 35.0  # bricht an der Frist ab, nicht irgendwann
    assert not flow.is_logged_in()


def test_incomplete_device_response_is_refused(tmp_path: Path) -> None:
    http = _Http(_device_response(verification_uri=""))
    flow, _, _, notices = _flow(tmp_path, http)

    with pytest.raises(OAuthError, match="incomplete"):
        flow.login()
    assert notices == []  # niemand wird auf eine leere Seite geschickt


def test_unreadable_response_is_a_failure_not_a_login(tmp_path: Path) -> None:
    http = _Http(_Response(ValueError("no json here"), status_code=502))
    flow, _, _, _ = _flow(tmp_path, http)

    with pytest.raises(OAuthError, match="unreadable"):
        flow.login()


# ------------------------------------------------------------------ Ablage
def test_token_file_is_0600_and_directory_0700(tmp_path: Path) -> None:
    http = _Http(_device_response(), _token_response())
    store = TokenStore(tmp_path / "nested" / "oauth")
    flow = DeviceFlow(
        CONFIG, store, post=http, notify=lambda _: None, sleep=lambda _: None, clock=_Clock()
    )

    flow.login()

    path = store.path_for("xai")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == ACCESS
    assert list(store.directory.iterdir()) == [path]  # keine .tmp-Reste


def test_existing_loose_directory_is_tightened(tmp_path: Path) -> None:
    directory = tmp_path / "oauth"
    directory.mkdir(mode=0o755)
    store = TokenStore(directory)

    store.save("xai", StoredToken(access_token=ACCESS))

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_provider_name_cannot_escape_the_directory(tmp_path: Path) -> None:
    """Der Name landet in einem Pfad — `..` waere ein Schreibzugriff nach draussen."""
    store = TokenStore(tmp_path / "oauth")
    for evil in ("../../etc/passwd", "..", "x/y", "with space", ""):
        with pytest.raises(ValueError):
            store.path_for(evil)


def test_store_treats_broken_files_as_signed_out(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "oauth")
    assert store.load("xai") is None  # gar keine Datei

    store.save("xai", StoredToken(access_token=ACCESS))
    store.path_for("xai").write_text("{not json", encoding="utf-8")
    assert store.load("xai") is None

    store.path_for("xai").write_text(json.dumps({"refresh_token": REFRESH}), encoding="utf-8")
    assert store.load("xai") is None  # ohne Zugriffstoken ist die Datei wertlos


# ------------------------------------------------------------------ Auffrischen
def test_expired_access_token_is_refreshed_before_use(tmp_path: Path) -> None:
    clock = _Clock()
    store = TokenStore(tmp_path / "oauth")
    store.save(
        "xai",
        StoredToken(access_token="stale-token", refresh_token=REFRESH, expires_at=clock.now - 1),
    )
    http = _Http(_token_response(access_token="fresh-access", refresh_token=""))
    flow = DeviceFlow(
        CONFIG, store, post=http, notify=lambda _: None, sleep=lambda _: None, clock=clock
    )

    assert flow.access_token() == "fresh-access"
    assert http.calls[0]["data"] == {
        "grant_type": REFRESH_TOKEN_GRANT,
        "refresh_token": REFRESH,
        "client_id": CONFIG.client_id,
    }
    # Ohne neues refresh_token bleibt das alte gueltig — sonst waere die Anmeldung tot.
    stored = store.load("xai")
    assert stored is not None
    assert stored.access_token == "fresh-access" and stored.refresh_token == REFRESH
    assert flow.access_token() == "fresh-access"  # zweiter Aufruf ohne weiteren HTTP-Aufruf
    assert len(http.calls) == 1


def test_token_close_to_expiry_is_refreshed_early(tmp_path: Path) -> None:
    clock = _Clock()
    store = TokenStore(tmp_path / "oauth")
    store.save(
        "xai",
        StoredToken(access_token="stale", refresh_token=REFRESH, expires_at=clock.now + 30),
    )
    http = _Http(_token_response(access_token="fresh"))
    flow = DeviceFlow(
        CONFIG, store, post=http, notify=lambda _: None, sleep=lambda _: None, clock=clock
    )

    assert flow.access_token() == "fresh"  # 30 s Restlaufzeit sterben mitten in der Anfrage


def test_valid_token_is_used_without_any_request(tmp_path: Path) -> None:
    clock = _Clock()
    store = TokenStore(tmp_path / "oauth")
    store.save("xai", StoredToken(access_token=ACCESS, expires_at=clock.now + 3600))
    http = _Http()  # jeder Aufruf waere ein AssertionError
    flow = DeviceFlow(
        CONFIG, store, post=http, notify=lambda _: None, sleep=lambda _: None, clock=clock
    )

    assert flow.access_token() == ACCESS
    assert http.calls == []


def test_failed_refresh_means_not_signed_in(tmp_path: Path) -> None:
    """Ein nicht auffrischbares Token IST keine Anmeldung — laut jetzt statt still spaeter."""
    clock = _Clock()
    store = TokenStore(tmp_path / "oauth")
    store.save(
        "xai",
        StoredToken(access_token="stale", refresh_token=REFRESH, expires_at=clock.now - 1),
    )
    http = _Http(_error_response("invalid_grant"))
    flow = DeviceFlow(
        CONFIG, store, post=http, notify=lambda _: None, sleep=lambda _: None, clock=clock
    )

    with pytest.raises(NotLoggedIn):
        flow.access_token()

    assert not flow.is_logged_in()
    assert store.load("xai") is None
    with pytest.raises(NotLoggedIn):
        flow.access_token()  # bleibt abgemeldet, kein zweiter Versuch mit totem Token


def test_expired_token_without_refresh_token_is_not_signed_in(tmp_path: Path) -> None:
    clock = _Clock()
    store = TokenStore(tmp_path / "oauth")
    store.save("xai", StoredToken(access_token="stale", expires_at=clock.now - 1))
    http = _Http()
    flow = DeviceFlow(
        CONFIG, store, post=http, notify=lambda _: None, sleep=lambda _: None, clock=clock
    )

    with pytest.raises(NotLoggedIn, match="no refresh token"):
        flow.access_token()
    assert http.calls == []
    assert not flow.is_logged_in()


def test_access_token_without_any_login_says_so(tmp_path: Path) -> None:
    flow, _, _, _ = _flow(tmp_path, _Http())

    with pytest.raises(NotLoggedIn, match="Not signed in"):
        flow.access_token()


# ------------------------------------------------------------------ Abmelden
def test_logout_removes_the_file(tmp_path: Path) -> None:
    http = _Http(_device_response(), _token_response())
    flow, _, _, _ = _flow(tmp_path, http)
    flow.login()
    store = TokenStore(tmp_path / "oauth")

    assert flow.logout() is True
    assert not store.path_for("xai").exists()
    assert not flow.is_logged_in()
    assert flow.logout() is False  # zweimal abmelden ist kein Fehler, nur folgenlos


# ------------------------------------------------------------------ Geheimhaltung
def _rendered_chain(error: BaseException) -> str:
    """Die VOLLE Kette — `__cause__`, `__context__`, alles, was ein Traceback zeigen wuerde."""
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


def test_no_secret_in_output_or_exception_chain_when_the_transport_leaks(tmp_path: Path) -> None:
    """HTTP-Bibliotheken zitieren den Formularkoerper in ihren Ausnahmen. Genau hier faellt
    ein `raise ... from error` auf: das Original haenge sonst als `__context__` in der Kette."""
    leak = RuntimeError(
        f"POST /oauth2/token failed: refresh_token={REFRESH}&device_code={DEVICE_CODE}"
    )
    clock = _Clock()
    store = TokenStore(tmp_path / "oauth")
    store.save(
        "xai",
        StoredToken(access_token=ACCESS, refresh_token=REFRESH, expires_at=clock.now - 1),
    )
    notices: list[str] = []
    flow = DeviceFlow(
        CONFIG,
        store,
        post=_Http(error=leak),
        notify=notices.append,
        sleep=lambda _: None,
        clock=clock,
    )

    with pytest.raises(NotLoggedIn) as failure:
        flow.access_token()

    rendered = _rendered_chain(failure.value)
    for secret in (ACCESS, REFRESH, DEVICE_CODE):
        assert secret not in rendered
        assert secret not in str(failure.value)
        assert secret not in "".join(notices)


class _FailsAtTokenEndpoint(_Http):
    """Der Geraetecode kommt sauber, dann bricht der Transport mit dem Code im Text zusammen."""

    def __call__(self, url: str, *, data: dict, timeout: float) -> _Response:
        if url == CONFIG.token_url:
            raise RuntimeError(f"connection reset while sending device_code={DEVICE_CODE}")
        return super().__call__(url, data=data, timeout=timeout)


def test_no_device_code_in_the_chain_during_polling(tmp_path: Path) -> None:
    flow, _, _, notices = _flow(tmp_path, _FailsAtTokenEndpoint(_device_response()))

    with pytest.raises(OAuthError) as failure:
        flow.login()

    assert DEVICE_CODE not in _rendered_chain(failure.value)
    assert notices and DEVICE_CODE not in "".join(notices)  # angezeigt wurde, geleakt nicht
    assert "[REDACTED]" in str(failure.value)


def test_error_body_from_the_provider_cannot_smuggle_a_token(tmp_path: Path) -> None:
    """Auch der Anbieter darf uns keinen Token in die Meldung schreiben."""
    http = _Http(
        _device_response(),
        _Response({"error": f"invalid_request access_token={ACCESS}"}, 400),
    )
    flow, _, _, _ = _flow(tmp_path, http)

    with pytest.raises(OAuthError) as failure:
        flow.login()

    assert ACCESS not in _rendered_chain(failure.value)


def test_reprs_never_carry_a_secret(tmp_path: Path) -> None:
    token = StoredToken(access_token=ACCESS, refresh_token=REFRESH, expires_at=42.0)
    flow, _, _, _ = _flow(tmp_path, _Http())

    for text in (repr(token), str(token), repr(flow)):
        assert ACCESS not in text and REFRESH not in text
    assert "refreshable=True" in repr(token)
    assert "xai" in repr(flow)


def test_prompt_never_shows_the_device_code() -> None:
    from talos.oauth import DeviceGrant

    grant = DeviceGrant(
        device_code=DEVICE_CODE,
        user_code=USER_CODE,
        verification_uri="https://provider.test/device",
    )
    prompt = sign_in_prompt("xai", grant)

    assert USER_CODE in prompt and "https://provider.test/device" in prompt
    assert DEVICE_CODE not in prompt
    assert DEVICE_CODE not in repr(grant)


# ------------------------------------------------------------------ Konfiguration
def test_endpoints_and_client_id_must_be_supplied() -> None:
    with pytest.raises(ValueError, match="client_id"):
        ProviderConfig("xai", "", "https://a.test/d", "https://a.test/t")
    with pytest.raises(ValueError, match="https"):
        ProviderConfig("xai", "id", "http://a.test/d", "https://a.test/t")
    with pytest.raises(ValueError, match="https"):
        ProviderConfig("xai", "id", "https://a.test/d", "http://a.test/t")
    with pytest.raises(ValueError, match="provider name"):
        ProviderConfig("../evil", "id", "https://a.test/d", "https://a.test/t")


def test_hostile_interval_values_are_clamped(tmp_path: Path) -> None:
    """Ein Anbieter darf uns weder in eine Endlosschleife noch in einen Sturm schicken."""
    http = _Http(_device_response(interval="not a number", expires_in=99_999), _token_response())
    flow, sleeper, _, _ = _flow(tmp_path, http)

    flow.login()

    assert sleeper.calls == [5.0]  # unlesbares Intervall -> RFC-Vorgabe

    http2 = _Http(_device_response(interval=0), _token_response())
    flow2, sleeper2, _, _ = _flow(tmp_path / "second", http2)
    flow2.login()
    assert sleeper2.calls == [1.0]  # 0 s waere ein Anfragensturm


def test_store_directory_is_supplied_from_outside(tmp_path: Path) -> None:
    """Dieses Modul entscheidet nicht heimlich, wo die Zugaenge des Betreibers liegen."""
    store = TokenStore(tmp_path / "chosen-by-the-operator")
    store.save("xai", StoredToken(access_token=ACCESS))

    assert store.path_for("xai") == tmp_path / "chosen-by-the-operator" / "xai.json"
    assert os.path.commonpath([str(tmp_path), str(store.path_for("xai"))]) == str(tmp_path)
