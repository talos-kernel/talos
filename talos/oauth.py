"""OAuth-Anmeldung fuer Modell-Anbieter — ausschliesslich Device Authorization Grant (RFC 8628).

Warum genau ein Flow und nicht die uebliche Sammlung:

Ein Redirect-Flow mit PKCE, lokalem Empfaenger und Browser-Ruecksprung waere die
naheliegende zweite Haelfte. Er fehlt hier bewusst, denn nach Durchsicht der
Nutzungsbedingungen bleiben genau zwei Anbieter uebrig, bei denen die Anmeldung eines
Drittanbieter-Programms ueberhaupt zulaessig ist: **xAI** (dokumentierter Device-Code-Flow)
und das **Nous Portal**. Anthropic untersagt Drittanbieter-Logins ausdruecklich, GitHub
Copilot untersagt die Nutzung unveroeffentlichter APIs, und Googles Verbraucherpfad fuer
solche Anmeldungen ist abgeschaltet. Ein Redirect-Flow haette damit heute schlicht keinen
Abnehmer — und ungenutzter Sicherheitscode ist ungepruefter Sicherheitscode.

Was hier passiert, und was ausdruecklich NICHT passiert:

Hier meldet sich **der Betreiber mit seinem eigenen Konto** ueber den offiziellen Endpunkt
des Anbieters an. Er sieht die Adresse, er sieht den Code, er tippt ihn selbst ein, und der
Anbieter stellt das Token auf genau diese Zustimmung hin aus. Das ist etwas grundlegend
anderes, als die gespeicherten Anmeldedaten einer fremden Hersteller-CLI mitzubenutzen:
Dort gibt der Anbieter das Token einem anderen Programm fuer einen anderen Zweck, und
dessen Vertrauensstellung wird geliehen statt erworben. Talos leiht nichts aus.

Sorgfalt, die dieses Modul zusagt:

* Kein Token — weder Zugriff, noch Auffrischung, noch Geraetecode — steht je in einer
  Ausgabe, einer Meldung oder einer Ausnahme. Ausnahmen werden mit `from None` geworfen,
  weil sonst das unbereinigte Original als `__context__` in der Kette haengt und ein
  Traceback es doch ausdruckt (dasselbe Muster wie in `whatsapp.py`).
* Die Tokendatei entsteht direkt mit `0600`, das Verzeichnis mit `0700` — nicht per
  `chmod` hinterher, denn dazwischen laege ein Fenster mit lockeren Rechten.
* Ein abgelaufenes Zugriffstoken wird VOR Gebrauch aufgefrischt. Schlaegt das fehl, ist der
  Zustand „nicht angemeldet" — laut und sofort. Ein stiller Fehlschlag mitten im naechsten
  Denkvorgang waere der schlechteste Zeitpunkt, es zu erfahren.
* Endpunkte und Client-ID kommen als Parameter herein. Hier steht keine geratene Client-ID.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, NoReturn, Protocol, runtime_checkable

import requests

__all__ = [
    "DEVICE_CODE_GRANT",
    "REFRESH_TOKEN_GRANT",
    "DEFAULT_INTERVAL_S",
    "SLOW_DOWN_STEP_S",
    "EXPIRY_SKEW_S",
    "REDACTED",
    "OAuthError",
    "AuthorizationDeclined",
    "AuthorizationExpired",
    "NotLoggedIn",
    "HttpResponse",
    "HttpPost",
    "Notify",
    "ProviderConfig",
    "DeviceGrant",
    "StoredToken",
    "TokenStore",
    "DeviceFlow",
    "sign_in_prompt",
]

# RFC 8628 §3.4 / RFC 6749 §6 — die Grant-Typen stehen als Konstante hier, weil ein
# Tippfehler darin vom Anbieter nur als nichtssagendes `unsupported_grant_type` zurueckkommt.
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_TOKEN_GRANT = "refresh_token"

# RFC 8628 §3.5: fehlt `interval`, sind 5 s vorgesehen; auf `slow_down` wird um 5 s erhoeht.
DEFAULT_INTERVAL_S = 5.0
SLOW_DOWN_STEP_S = 5.0
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 60.0
DEFAULT_EXPIRES_IN_S = 600.0
# Obergrenze fuer die Gesamtlaufzeit. Ein Anbieter, der `expires_in: 86400` meldet, darf
# diesen Prozess nicht einen Tag lang festhalten.
MAX_FLOW_SECONDS = 1800.0
DEFAULT_TIMEOUT_S = 30.0

# Ein Token, das in 30 s ablaeuft, ist praktisch abgelaufen: es wuerde mitten in der
# naechsten Anfrage sterben. Deshalb wird frueher aufgefrischt als noetig.
EXPIRY_SKEW_S = 60.0

ERROR_PENDING = "authorization_pending"
ERROR_SLOW_DOWN = "slow_down"
ERROR_DENIED = "access_denied"
ERROR_EXPIRED = "expired_token"

REDACTED = "[REDACTED]"
_MAX_DETAIL_CHARS = 240
_MIN_SECRET_CHARS = 4  # kuerzeres „Geheimnis" wuerde beim Ersetzen halbe Woerter zerreissen
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_TOKEN_FIELD = re.compile(
    r"(?i)\"?\b((?:access|refresh|id|device)_token|device_code|client_secret)\"?\s*[:=]\s*\"?[^\"\s,&}]+"
)

# Anbietername -> Dateiname. Bewusst eng: der Name landet in einem Pfad, und ein `..`
# darin waere ein Schreibzugriff ausserhalb des Token-Verzeichnisses.
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

_DIR_MODE = 0o700
_FILE_MODE = 0o600


class OAuthError(RuntimeError):
    """Anmeldung fehlgeschlagen — laut und behandelbar, nie still."""


class AuthorizationDeclined(OAuthError):
    """Der Mensch hat im Browser abgelehnt. Kein Defekt, eine Entscheidung."""


class AuthorizationExpired(OAuthError):
    """Der Geraetecode ist abgelaufen, bevor jemand ihn bestaetigt hat."""


class NotLoggedIn(OAuthError):
    """Es gibt kein brauchbares Token. Der einzige ehrliche Zustand nach missgluecktem
    Auffrischen — ein „vielleicht doch" waere ein Fehlschlag im naechsten Denkvorgang."""


@runtime_checkable
class HttpResponse(Protocol):
    """Was dieses Modul von einer Antwort braucht — Status und Koerper, sonst nichts."""

    status_code: int

    def json(self) -> Any: ...


@runtime_checkable
class HttpPost(Protocol):
    """Der GESAMTE Netz-Vertrag: ein formkodierter POST, eine Antwort.

    So schmal, dass Tests ohne Netz auskommen — und dass spaeter nichts anderes durch
    diese Tuer passt als genau dieser eine Aufruf.
    """

    def __call__(self, url: str, *, data: dict[str, str], timeout: float) -> HttpResponse: ...


# Die Anzeige fuer den Menschen ist injizierbar, weil sie je nach Betrieb woanders
# hingehoert: Terminal beim Einrichten, Telegram-Kanal im Betrieb, Liste im Test.
Notify = Callable[[str], None]


def _requests_post(url: str, *, data: dict[str, str], timeout: float) -> HttpResponse:
    """Vorgabe-Transport. `requests` ist bereits Abhaengigkeit; es kommt keine dazu."""
    return requests.post(
        url, data=data, timeout=timeout, headers={"Accept": "application/json"}
    )


def _print_notice(message: str) -> None:
    """Vorgabe-Anzeige: das ist eine Aufforderung an den Menschen, kein Debug-Log."""
    print(message)


def _scrub(value: object, secrets: Iterable[str] = ()) -> str:
    """Entfernt alles, was nach Zugang aussieht — zuerst die bekannten Geheimnisse.

    HTTP-Bibliotheken zitieren in Ausnahmen gern URL, Formularkoerper und Header. Diese
    Funktion laeuft darum ueber JEDEN Text, der dieses Modul verlaesst.
    """
    text = " ".join(str(value or "").split())
    for secret in secrets:
        cleaned = str(secret or "")
        if len(cleaned) >= _MIN_SECRET_CHARS:
            text = text.replace(cleaned, REDACTED)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _TOKEN_FIELD.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return text[:_MAX_DETAIL_CHARS]


def _safe_provider(name: str) -> str:
    """Anbietername -> Dateiname-tauglicher Schluessel. Wird nicht geraten, wird abgelehnt."""
    cleaned = str(name).strip().lower()
    if _SAFE_NAME.fullmatch(cleaned) is None:
        raise ValueError(
            f"unsafe provider name: {name!r} — use letters, digits, '-' or '_' only"
        )
    return cleaned


def _clamp(value: object, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return max(low, min(high, number))


@dataclass(frozen=True)
class ProviderConfig:
    """Endpunkte und Client-ID eines Anbieters. Kommen von aussen, nie aus diesem Modul."""

    name: str
    client_id: str
    device_code_url: str
    token_url: str
    scope: str = ""

    def __post_init__(self) -> None:
        _safe_provider(self.name)
        if not str(self.client_id).strip():
            raise ValueError(f"{self.name}: client_id is empty")
        for field, url in (("device_code_url", self.device_code_url), ("token_url", self.token_url)):
            # HTTPS ist keine Stilfrage: ueber `http` gaebe dieser Flow Geraetecode und
            # Token an jeden weiter, der auf dem Weg mithoert.
            if not str(url).startswith("https://"):
                raise ValueError(f"{self.name}: {field} must be an https:// URL")


@dataclass(frozen=True, repr=False)
class DeviceGrant:
    """Antwort des Geraetecode-Endpunkts. `device_code` ist geheim — deshalb kein `repr`."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str = ""
    interval: float = DEFAULT_INTERVAL_S
    expires_in: float = DEFAULT_EXPIRES_IN_S

    def __repr__(self) -> str:
        return f"DeviceGrant(user_code={self.user_code!r}, expires_in={self.expires_in!r})"


@dataclass(frozen=True, repr=False)
class StoredToken:
    """Das, was auf Platte liegt. Kein `repr`, weil sonst jedes Log den Zugang traegt."""

    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    token_type: str = "Bearer"
    scope: str = ""

    def __repr__(self) -> str:
        return (
            f"StoredToken(token_type={self.token_type!r}, expires_at={self.expires_at!r}, "
            f"refreshable={bool(self.refresh_token)})"
        )

    def is_expired(self, now: float, *, skew: float = EXPIRY_SKEW_S) -> bool:
        """`expires_at == 0` heisst „ohne Ablaufangabe" — dann wird nichts unterstellt."""
        return self.expires_at > 0.0 and (now + skew) >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StoredToken | None:
        access = str(payload.get("access_token") or "")
        if not access:
            return None  # ohne Zugriffstoken ist die Datei wertlos, nicht halb gueltig
        return cls(
            access_token=access,
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=_clamp(payload.get("expires_at"), 0.0, float("inf"), 0.0),
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=str(payload.get("scope") or ""),
        )


class TokenStore:
    """Eine Datei je Anbieter unter einem uebergebenen Verzeichnis.

    Das Verzeichnis kommt von aussen (Konfiguration), damit dieses Modul nicht heimlich
    entscheidet, wo die Zugaenge des Betreibers liegen.
    """

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, provider: str) -> Path:
        return self._directory / f"{_safe_provider(provider)}.json"

    def load(self, provider: str) -> StoredToken | None:
        """Fehlend, unlesbar oder kaputt sind derselbe Zustand: nicht angemeldet."""
        try:
            raw = self.path_for(provider).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return StoredToken.from_dict(payload) if isinstance(payload, dict) else None

    def save(self, provider: str, token: StoredToken) -> Path:
        """Schreibt atomar und von Anfang an mit `0600`.

        `O_CREAT | O_EXCL | O_WRONLY` mit Modus statt `open()` + `chmod`: nur so gibt es
        keinen Moment, in dem die Datei mit den lockeren Rechten der `umask` existiert.
        `O_EXCL` kommt dazu, damit nicht durch einen untergeschobenen Symlink geschrieben
        wird. Erst `os.replace` macht die fertige Datei sichtbar — ein Abbruch mittendrin
        hinterlaesst so nie ein halbes Token.
        """
        path = self.path_for(provider)
        self._ensure_directory()
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(token.as_dict(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return path

    def delete(self, provider: str) -> bool:
        """`True`, wenn wirklich etwas entfernt wurde."""
        try:
            self.path_for(provider).unlink()
        except FileNotFoundError:
            return False
        return True

    def _ensure_directory(self) -> None:
        """Legt das Verzeichnis mit `0700` an — und zieht ein fremdes nach.

        Nur beim Anlegen ist der Modus atomar. Existiert es schon und steht offen, ist ein
        `chmod` das kleinere Uebel: die Datei selbst ist `0600`, aber ein lesbares
        Verzeichnis verraet, bei welchen Anbietern der Betreiber ein Konto hat.
        """
        try:
            self._directory.mkdir(mode=_DIR_MODE, parents=True, exist_ok=False)
            return
        except FileExistsError:
            pass
        if stat.S_IMODE(self._directory.stat().st_mode) & 0o077:
            os.chmod(self._directory, _DIR_MODE)


def sign_in_prompt(provider: str, grant: DeviceGrant) -> str:
    """Der Text, den der Mensch sieht. Enthaelt den Benutzercode, nie den Geraetecode.

    Der `verification_uri_complete` traegt den Code bereits in der Adresse; er wird
    bevorzugt gezeigt, der Code aber trotzdem genannt — damit der Betreiber auf der Seite
    des Anbieters vergleichen kann, was er da eigentlich bestaetigt.
    """
    target = grant.verification_uri_complete or grant.verification_uri
    return (
        f"Sign in to {provider}: open {target} and enter the code {grant.user_code}. "
        f"Waiting for your confirmation..."
    )


class DeviceFlow:
    """Device Authorization Grant fuer genau einen Anbieter.

    Alles Aussenherum ist injizierbar (HTTP, Anzeige, Uhr, Warten), damit die Tests weder
    Netz noch Wanduhr brauchen — und damit hier nichts Zeit verbrennt, was niemand sieht.
    """

    def __init__(
        self,
        config: ProviderConfig,
        store: TokenStore,
        *,
        post: HttpPost = _requests_post,
        notify: Notify = _print_notice,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._store = store
        self._post = post
        self._notify = notify
        self._sleep = sleep
        # Wanduhr, nicht `monotonic`: der Ablaufzeitpunkt muss einen Neustart ueberleben.
        self._clock = clock
        self._timeout_s = float(timeout_s)

    def __repr__(self) -> str:
        return f"DeviceFlow(provider={self._config.name!r})"

    @property
    def provider(self) -> str:
        return self._config.name

    # ------------------------------------------------------------------ oeffentlich
    def login(self) -> StoredToken:
        """Fordert den Geraetecode an, zeigt ihn, wartet auf die Bestaetigung, legt ab."""
        grant = self._request_device_code()
        self._notify(sign_in_prompt(self._config.name, grant))
        token = self._await_token(grant)
        self._store.save(self._config.name, token)
        return token

    def is_logged_in(self) -> bool:
        return self._store.load(self._config.name) is not None

    def logout(self) -> bool:
        """Entfernt das Token. `True`, wenn es eines gab."""
        return self._store.delete(self._config.name)

    def access_token(self) -> str:
        """Ein benutzbares Zugriffstoken — notfalls frisch geholt.

        Der Ablauf wird VOR dem Gebrauch geprueft, nicht danach interpretiert: ein 401
        mitten im Werkzeuglauf ist derselbe Fehler, nur teurer und spaeter.
        """
        stored = self._store.load(self._config.name)
        if stored is None:
            raise NotLoggedIn(
                f"Not signed in to {self._config.name}. Run the sign-in first."
            ) from None
        if not stored.is_expired(self._clock()):
            return stored.access_token
        return self._refresh(stored).access_token

    # ------------------------------------------------------------------ intern
    def _request_device_code(self) -> DeviceGrant:
        data = {"client_id": self._config.client_id}
        if self._config.scope:
            data["scope"] = self._config.scope
        payload = self._form_post(self._config.device_code_url, data, secrets=())
        error = str(payload.get("error") or "")
        if error:
            self._fail(f"{self._config.name} refused to start the sign-in ({error})")
        grant = DeviceGrant(
            device_code=str(payload.get("device_code") or ""),
            user_code=str(payload.get("user_code") or ""),
            verification_uri=str(payload.get("verification_uri") or ""),
            verification_uri_complete=str(payload.get("verification_uri_complete") or ""),
            interval=_clamp(
                payload.get("interval"), MIN_INTERVAL_S, MAX_INTERVAL_S, DEFAULT_INTERVAL_S
            ),
            expires_in=_clamp(
                payload.get("expires_in"), MIN_INTERVAL_S, MAX_FLOW_SECONDS, DEFAULT_EXPIRES_IN_S
            ),
        )
        if not (grant.device_code and grant.user_code and grant.verification_uri):
            # Halb geratene Felder wuerden den Menschen auf eine leere Seite schicken.
            self._fail(f"{self._config.name} sent an incomplete device-code response")
        return grant

    def _await_token(self, grant: DeviceGrant) -> StoredToken:
        """Pollt geduldig, aber nicht ewig — und respektiert `slow_down`.

        Wer auf `slow_down` weiter im alten Takt fragt, wird vom Anbieter gesperrt. Das
        Intervall wird darum erhoeht und nie wieder gesenkt.
        """
        interval = grant.interval
        deadline = self._clock() + grant.expires_in
        while True:
            self._sleep(interval)
            if self._clock() >= deadline:
                raise AuthorizationExpired(
                    f"Sign-in to {self._config.name} timed out after "
                    f"{int(grant.expires_in)}s without confirmation. Start it again."
                ) from None
            outcome = self._exchange(grant.device_code)
            if isinstance(outcome, StoredToken):
                return outcome
            if outcome == ERROR_SLOW_DOWN:
                interval = min(MAX_INTERVAL_S, interval + SLOW_DOWN_STEP_S)
            # ERROR_PENDING: der Mensch tippt noch. Genau dafuer ist dieser Flow gebaut.

    def _exchange(self, device_code: str) -> StoredToken | str:
        """Ein Poll-Durchgang: Token, oder der nicht-endgueltige Fehlercode als Text."""
        data = {
            "grant_type": DEVICE_CODE_GRANT,
            "device_code": device_code,
            "client_id": self._config.client_id,
        }
        payload = self._form_post(self._config.token_url, data, secrets=(device_code,))
        error = str(payload.get("error") or "")
        if not error:
            return self._token_from(payload)
        if error in (ERROR_PENDING, ERROR_SLOW_DOWN):
            return error
        self._raise_terminal(error)

    def _raise_terminal(self, error: str) -> NoReturn:
        """Endgueltige Fehler bekommen unterscheidbare Klassen UND unterscheidbare Saetze.

        „Abgelehnt" und „abgelaufen" fuehren zu verschiedenen Handgriffen: einmal noch
        einmal versuchen und zustimmen, einmal ueberhaupt erst rechtzeitig sein.
        """
        if error == ERROR_DENIED:
            raise AuthorizationDeclined(
                f"Sign-in to {self._config.name} was declined in the browser."
            ) from None
        if error == ERROR_EXPIRED:
            raise AuthorizationExpired(
                f"The device code for {self._config.name} expired before it was "
                f"confirmed. Start the sign-in again."
            ) from None
        raise OAuthError(f"{self._config.name} rejected the sign-in ({_scrub(error)})") from None

    def _refresh(self, stored: StoredToken) -> StoredToken:
        """Frischt auf — oder erklaert den Zustand zu „nicht angemeldet".

        Das Token wird dabei geloescht. Ein nicht auffrischbares Token IST keine Anmeldung;
        es liegen zu lassen hiesse, den Fehlschlag auf den naechsten Denkvorgang zu
        vertagen. Der Preis ist eine erneute Anmeldung nach einer Netzstoerung — laut und
        behebbar, und damit das bessere Ende.
        """
        name = self._config.name
        if not stored.refresh_token:
            self._store.delete(name)
            raise NotLoggedIn(
                f"The {name} access token expired and there is no refresh token. Sign in again."
            ) from None
        data = {
            "grant_type": REFRESH_TOKEN_GRANT,
            "refresh_token": stored.refresh_token,
            "client_id": self._config.client_id,
        }
        try:
            payload = self._form_post(
                self._config.token_url, data, secrets=(stored.refresh_token,)
            )
            if payload.get("error"):
                self._fail(f"{name} rejected the refresh token")
            fresh = self._token_from(payload, fallback_refresh=stored.refresh_token)
        except OAuthError:
            self._store.delete(name)
            raise NotLoggedIn(
                f"Refreshing the {name} sign-in failed — treat this as signed out "
                f"and sign in again."
            ) from None
        self._store.save(name, fresh)
        return fresh

    def _token_from(self, payload: dict[str, Any], *, fallback_refresh: str = "") -> StoredToken:
        """Baut das Token. `fallback_refresh` haelt eine Anmeldung am Leben, wenn der
        Anbieter beim Auffrischen kein neues `refresh_token` mitschickt (die Mehrheit)."""
        access = str(payload.get("access_token") or "")
        if not access:
            self._fail(f"{self._config.name} returned no access token")
        lifetime = _clamp(payload.get("expires_in"), 0.0, MAX_FLOW_SECONDS * 48, 0.0)
        return StoredToken(
            access_token=access,
            refresh_token=str(payload.get("refresh_token") or fallback_refresh),
            expires_at=(self._clock() + lifetime) if lifetime > 0 else 0.0,
            token_type=str(payload.get("token_type") or "Bearer"),
            scope=str(payload.get("scope") or self._config.scope),
        )

    def _form_post(
        self, url: str, data: dict[str, str], *, secrets: tuple[str, ...]
    ) -> dict[str, Any]:
        """Ein POST, eine gelesene JSON-Antwort — beides bereinigt.

        Der Koerper wird auch bei 4xx gelesen: RFC 8628 liefert `authorization_pending`
        ausgerechnet mit HTTP 400, ein Status allein taugt hier also nicht als Urteil.
        """
        try:
            response = self._post(url, data=dict(data), timeout=self._timeout_s)
        except Exception as error:
            self._fail(f"{self._config.name}: sign-in request failed: {_scrub(error, secrets)}")
        try:
            payload = response.json()
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            status = _status_of(response)
            self._fail(f"{self._config.name} sent an unreadable response (HTTP {status})")
        return payload

    @staticmethod
    def _fail(message: str) -> NoReturn:
        """Wirft bereinigt und **ohne** Ursachenkette (`from None`).

        Absicht: `raise ... from error` haengt das Original an, und genau das traegt bei
        HTTP-Bibliotheken den Formularkoerper mit `device_code`/`refresh_token`. Ein
        Traceback wuerde das Geheimnis dann trotz sauberer Meldung ausdrucken.
        """
        raise OAuthError(message) from None


def _status_of(response: HttpResponse) -> int:
    try:
        return int(response.status_code)
    except (AttributeError, TypeError, ValueError):
        return 0  # unlesbare Antwort gilt als Fehlschlag, nie als Erfolg
