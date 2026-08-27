"""WhatsApp — ein Melde-Weg nach draussen. Mehr ist dieser Kanal nicht, und mehr darf er nicht sein.

Warum `Trust.NOTIFY` hier nicht verhandelbar ist — und zwar aus Architektur, nicht aus
Bequemlichkeit:

Telegram holt seine Nachrichten per Long-Polling ab. Jede Verbindung geht von Talos *nach
draussen*; nichts muss ihn erreichen koennen. Genau das ist der Grund, warum dieser Agent
auf einem Raspberry Pi hinter einem Heimrouter laufen kann — ohne Portfreigabe, ohne
Tunnel, ohne oeffentlich aufloesbaren Namen. Die Angriffsflaeche von aussen ist null,
weil es von aussen keine Tuer gibt.

Die offizielle WhatsApp Business Cloud API kennt fuer eingehende Nachrichten **nur
Webhooks**: Meta stellt an eine oeffentliche HTTPS-Adresse zu, die der Betreiber
betreiben, erreichbar halten und absichern muesste. Eingehendes WhatsApp wuerde Talos
also von „nur ausgehend" auf „oeffentlich erreichbar" umstellen. Das ist das groesste
Zugestaendnis, das dieses Projekt machen koennte, und nichts, was als Nebenwirkung einer
neuen Integration passieren darf.

Ausgehend braucht davon nichts: ein einziger HTTPS-Aufruf an die Cloud API genuegt.
Deshalb kann dieser Kanal genau das — und deshalb traegt er die misstrauischste Stufe.
`Trust.NOTIFY` heisst laut `channel.py`: „Nur raus. Was hereinkommt, ist kein Auftrag."
Der Conductor lehnt auf dieser Stufe Freigaben und den Autonomie-Regler ohnehin ab
(`conductor.py`, `trust.py`). Die Stufe hier anzuheben waere kein Feature-Flag, sondern
die stillschweigende Behauptung, es gaebe einen geprueften Rueckweg — den es nicht gibt.
Sie ist darum eine `property` ohne Setter: nicht zu heben, auch nicht versehentlich.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, NoReturn, Protocol, runtime_checkable

import requests

from .channel import Inbound, StructuredMessage, Trust

CHANNEL_NAME = "whatsapp"

# Konfigurationsschluessel. Hier definiert, damit `config.py` sie importieren kann statt
# sie zu wiederholen: ein zweiter Ort fuer denselben Namen ist ein zweiter Ort zum Vertippen.
ENV_TOKEN = "WHATSAPP_TOKEN"          # Zugangstoken der Cloud API (Bearer)
ENV_PHONE_ID = "WHATSAPP_PHONE_ID"    # ID der Absender-Nummer (nicht die Nummer selbst)
ENV_TO = "WHATSAPP_TO"                # Zielnummer, nur Ziffern, im internationalen Format

GRAPH_API_VERSION = "v21.0"
_ENDPOINT = "https://graph.facebook.com/{version}/{phone_id}/messages"
_UPLOAD_ENDPOINT = "https://graph.facebook.com/{version}/{phone_id}/media"
DEFAULT_TIMEOUT_S = 30.0
# Ein Upload darf laenger dauern als eine Text-Meldung: 20 MB auf einer Pi-Leitung.
UPLOAD_TIMEOUT_S = 120.0

# Die Cloud API nimmt hoechstens 4096 Zeichen im Textkoerper. Laengeres kommt garantiert
# als 400 zurueck — deshalb wird geteilt statt gesendet und gehofft.
WHATSAPP_TEXT_LIMIT = 4096

# E.164 erlaubt bis zu 15 Ziffern; unter 8 ist keine international waehlbare Nummer.
MIN_NUMBER_DIGITS = 8
MAX_NUMBER_DIGITS = 15
# Bewusst eine Zeichenklasse statt `str.isdigit()`: das haelt "²" (hochgestellt) und
# arabisch-indische Ziffern fuer Ziffern, und beides wuerde die Cloud API nur verwirren.
_NUMBER = re.compile("[0-9]{%d,%d}" % (MIN_NUMBER_DIGITS, MAX_NUMBER_DIGITS))

_SEP = ":"
REDACTED = "[REDACTED]"
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_ACCESS_TOKEN_PARAM = re.compile(r"(?i)access_token=[^&\s]+")
# Fehlertexte sind fuer Menschen, nicht fuer Forensik: 240 Zeichen reichen, um zu
# verstehen was los ist, und sind kurz genug, um versehentlich nichts mitzuschleppen.
_MAX_DETAIL_CHARS = 240

# Meta meldet das geschlossene Kundenfenster mit festen Codes: 131047 ist die heutige
# „Re-engagement message", 470 der aeltere „outside the allowed window". Beide bedeuten
# dasselbe und beide sind KEIN Defekt — sie sind der Normalfall, sobald der Betreiber
# 24 h nicht geantwortet hat.
WINDOW_ERROR_CODES = frozenset({470, 131047})
WINDOW_MESSAGE = (
    "WhatsApp refused the message: outside the 24-hour window — WhatsApp only "
    "accepts approved templates then."
)

__all__ = [
    "CHANNEL_NAME",
    "ENV_TOKEN",
    "ENV_PHONE_ID",
    "ENV_TO",
    "GRAPH_API_VERSION",
    "DEFAULT_TIMEOUT_S",
    "WHATSAPP_TEXT_LIMIT",
    "WINDOW_ERROR_CODES",
    "WINDOW_MESSAGE",
    "HttpResponse",
    "HttpPost",
    "HttpUpload",
    "WhatsAppError",
    "OutsideWindowError",
    "WhatsAppChannel",
    "conversation_for",
    "number_of",
    "split_text",
]


class WhatsAppError(RuntimeError):
    """Zustellung fehlgeschlagen — laut und behandelbar.

    Bewusst eine Ausnahme und kein `False`: `ChannelRegistry.poll_all` und der Conductor
    zeigen, wie das Projekt mit defekten Kanaelen umgeht. Ein stillschweigend
    verschluckter Zustellfehler sieht aus wie eine zugestellte Nachricht — und dieser
    Kanal existiert ausschliesslich, um Nachrichten zuzustellen.
    """


class OutsideWindowError(WhatsAppError):
    """Das 24-Stunden-Fenster ist zu. Kein Defekt, sondern eine Regel von Meta."""


@runtime_checkable
class HttpResponse(Protocol):
    """Was dieser Kanal von einer Antwort braucht — Status und Koerper, sonst nichts."""

    status_code: int

    def json(self) -> Any: ...


@runtime_checkable
class HttpPost(Protocol):
    """Der GESAMTE Netz-Vertrag dieses Kanals: ein POST, eine Antwort.

    So schmal wie moeglich, damit Tests ohne Netz und ohne `requests`-Doppel auskommen —
    und damit spaeter nichts anderes durch diese Tuer passt als genau dieser eine Aufruf.
    """

    def __call__(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse: ...


def _requests_post(
    url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
) -> HttpResponse:
    """Vorgabe-Transport. `requests` ist bereits Abhaengigkeit; es kommt keine dazu."""
    return requests.post(url, json=json, headers=headers, timeout=timeout)


@runtime_checkable
class HttpUpload(Protocol):
    """Der ZWEITE Netz-Vertrag: ein Multipart-POST auf den Media-Endpunkt.

    Eigenes Protokoll statt `HttpPost` aufzuweiten: ein Datei-Upload ist ein anderer
    Aufruf (Bytes statt JSON), und wer nur Text sendet, soll ihn gar nicht erst in
    der Hand halten. Dieselbe Schmalheit wie `HttpPost` — genau dieser eine Aufruf.
    """

    def __call__(
        self,
        url: str,
        *,
        data: dict[str, str],
        files: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse: ...


def _requests_upload(
    url: str,
    *,
    data: dict[str, str],
    files: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> HttpResponse:
    """Vorgabe-Transport fuer Uploads. `requests` ist bereits Abhaengigkeit."""
    return requests.post(url, data=data, files=files, headers=headers, timeout=timeout)


def number_of(conversation: str) -> str:
    """`whatsapp:41791234567` -> `41791234567`. Alles andere ist ein Fehler.

    Streng, weil eine halb erkannte Nummer eine Nachricht an jemand anderen waere: der
    Rueckweg traegt den Kanal im Namen (wie bei Telegram, `chat_id_of`), und die Nummer
    muss rein numerisch und plausibel lang sein. Ein `+`, ein Leerzeichen oder ein
    Bindestrich ist Schreibweise, nicht Nummer — hier wird nicht geraten, hier wird
    abgelehnt.
    """
    name, sep, raw = conversation.partition(_SEP)
    if not sep or name != CHANNEL_NAME:
        raise ValueError(f"not this channel: {conversation!r}")
    if _NUMBER.fullmatch(raw) is None:
        raise ValueError(
            f"malformed WhatsApp number: {raw!r} — expected {MIN_NUMBER_DIGITS}"
            f"-{MAX_NUMBER_DIGITS} digits, no '+' and no separators"
        )
    return raw


def conversation_for(number: str) -> str:
    """Zielnummer -> Rueckweg. Validiert beim Bauen, nicht erst beim Senden."""
    conversation = f"{CHANNEL_NAME}{_SEP}{str(number).strip()}"
    number_of(conversation)
    return conversation


def split_text(text: str, limit: int = WHATSAPP_TEXT_LIMIT) -> tuple[str, ...]:
    """Teilt an Zeilen-, sonst an Wortgrenzen. Nichts wird abgeschnitten, nichts abgelehnt.

    Eine Meldung, die genau dann verstummt, wenn sie lang (also interessant) wird, waere
    der schlechteste Ausgang: der Betreiber sieht nichts und haelt das fuer Ruhe. Getrennt
    wird darum an der letzten Zeilen-, ersatzweise Wortgrenze innerhalb des Limits; nur
    ein einzelnes ueberlanges Wort wird hart geschnitten, weil es keine Grenze hat.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    rest = text.strip()
    parts: list[str] = []
    while len(rest) > limit:
        # Ein Zeichen ueber das Limit hinaus ansehen: liegt die Grenze exakt auf dem
        # Limit, soll sie noch als Trennstelle zaehlen.
        head = rest[: limit + 1]
        cut = head.rfind("\n")
        if cut <= 0:
            cut = head.rfind(" ")
        if cut <= 0:
            cut = limit  # unteilbar — hart schneiden ist besser als gar nicht zustellen
        chunk = rest[:cut].rstrip()
        if chunk:
            parts.append(chunk)
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return tuple(parts)


def _scrub(value: object, token: str) -> str:
    """Entfernt alles, was nach Zugang aussieht — zuerst den echten Token.

    HTTP-Bibliotheken zitieren in Ausnahmen gern URL und Header. Diese Funktion laeuft
    darum ueber JEDEN Text, der diesen Modul verlaesst.
    """
    text = " ".join(str(value or "").split())
    if token:
        text = text.replace(token, REDACTED)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _ACCESS_TOKEN_PARAM.sub(f"access_token={REDACTED}", text)
    return text[:_MAX_DETAIL_CHARS]


def _api_error(response: HttpResponse) -> tuple[frozenset[int], str]:
    """Codes und Klartext aus der Graph-Antwort. Defensiv: ein Fehler hat oft keinen Body."""
    try:
        payload = response.json()
    except Exception:
        return frozenset(), ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return frozenset(), ""
    codes: set[int] = set()
    # `code` traegt den Fall meistens, `error_subcode` in einigen Antworten stattdessen.
    for key in ("code", "error_subcode"):
        try:
            codes.add(int(error[key]))
        except (KeyError, TypeError, ValueError):
            continue
    return frozenset(codes), str(error.get("message") or "")


class WhatsAppChannel:
    """`Channel`-Implementierung: sendet, hoert nicht zu.

    Der Token bleibt vollstaendig hier drin. Er steht in keinem Attribut, das ein `repr`
    zeigt, in keiner Fehlermeldung und in keiner Ausnahme (siehe `_scrub` und `_fail`).
    """

    name = CHANNEL_NAME

    def __init__(
        self,
        token: str,
        phone_id: str,
        *,
        post: HttpPost = _requests_post,
        upload: HttpUpload = _requests_upload,
        api_version: str = GRAPH_API_VERSION,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        text_limit: int = WHATSAPP_TEXT_LIMIT,
    ) -> None:
        if not str(token).strip():
            raise ValueError(f"{ENV_TOKEN} is empty")
        if _NUMBER.fullmatch(str(phone_id).strip()) is None:
            raise ValueError(f"{ENV_PHONE_ID} must be the numeric phone number ID")
        self._token = str(token).strip()
        self._phone_id = str(phone_id).strip()
        self._post = post
        self._upload = upload
        self._url = _ENDPOINT.format(version=api_version, phone_id=self._phone_id)
        self._upload_url = _UPLOAD_ENDPOINT.format(version=api_version, phone_id=self._phone_id)
        self._timeout_s = float(timeout_s)
        self._text_limit = max(1, int(text_limit))

    def __repr__(self) -> str:
        # Explizit, damit kein spaeterer Umbau auf `dataclass` den Token in Logs traegt.
        return f"WhatsAppChannel(phone_id={self._phone_id!r})"

    @property
    def trust(self) -> Trust:
        """Immer `NOTIFY` — ohne Setter, damit die Stufe nirgends angehoben werden kann.

        Begruendung im Modul-Docstring: es gibt keinen eingehenden Weg, also gibt es
        nichts, was eine hoehere Stufe beweisen koennte.
        """
        return Trust.NOTIFY

    def poll(self) -> list[Inbound]:
        """Immer leer — absichtlich, nicht „noch nicht gebaut".

        Eingehendes WhatsApp gaebe es nur ueber einen oeffentlichen Webhook-Endpunkt
        (Modul-Docstring). Solange der nicht existiert und bewusst beschlossen ist, waere
        jede andere Rueckgabe eine Luege im Vertrauensmodell: sie wuerde behaupten, hier
        koenne etwas hereinkommen, dessen Herkunft geprueft wurde.
        """
        return []

    def send(self, conversation: str, text: str) -> None:
        """Stellt zu — notfalls in mehreren Teilen, aber vollstaendig.

        Faellt ein Teil aus, fliegt der Fehler sofort: eine halb zugestellte Meldung mit
        lautem Fehler ist besser als eine leise verlorene.
        """
        number = number_of(conversation)
        for part in split_text(text, self._text_limit):
            self._deliver(number, part)

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        """Faellt auf den Textteil zurueck. Knoepfe werden bewusst NICHT nachgebaut.

        Auf einem `NOTIFY`-Kanal koennte niemand etwas freigeben — der Conductor lehnt
        Freigaben hier vor jeder UI ab. Ein Knopf, der garantiert nichts bewirkt, waere
        also kein Komfort, sondern eine falsche Zusage im wichtigsten Moment.
        """
        self.send(conversation, message.text)

    def send_file(self, conversation: str, path: str) -> None:
        """Ein Dokument als echter Anhang: erst der Upload, dann die Dokument-Meldung.

        Die Cloud API nimmt Dateien nur in zwei Schritten — Upload gegen den
        Media-Endpunkt, dann eine Nachricht mit der vergebene Medien-Kennung. Der Pfad
        ist hier laengst gegatet (`attachment.resolve`); dieser Kanal liest die Datei
        nur noch und schiebt sie hinaus. Fehler fliegen wie beim Text: laut.
        """
        number = number_of(conversation)
        media_id = self._upload_media(Path(path))
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "document",
            "document": {"id": media_id, "filename": Path(path).name},
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._post(
                self._url, json=payload, headers=headers, timeout=self._timeout_s
            )
        except Exception as error:
            self._fail(f"WhatsApp delivery failed: {_scrub(error, self._token)}")
        self._check(response)

    def _upload_media(self, path: Path) -> str:
        """Laedt die Datei hoch und gibt die Medien-Kennung zurueck — oder wirft."""
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            response = self._upload(
                self._upload_url,
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (path.name, path.read_bytes(), mime)},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=UPLOAD_TIMEOUT_S,
            )
        except Exception as error:
            self._fail(f"WhatsApp media upload failed: {_scrub(error, self._token)}")
        if not 200 <= self._status_of(response) < 300:
            codes, detail = _api_error(response)
            if codes & WINDOW_ERROR_CODES:
                raise OutsideWindowError(WINDOW_MESSAGE)
            self._fail(f"WhatsApp media upload failed: {_scrub(detail, self._token)}")
        try:
            media_id = str((response.json() or {}).get("id") or "")
        except Exception:
            media_id = ""
        if not media_id:
            self._fail("WhatsApp media upload returned no media id")
        return media_id

    # ------------------------------------------------------------------ intern
    def _deliver(self, number: str, text: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": number,
            "type": "text",
            # Keine Link-Vorschau: eine Meldung soll melden, nicht nebenbei fremde URLs
            # von Metas Servern abrufen lassen.
            "text": {"preview_url": False, "body": text},
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._post(
                self._url, json=payload, headers=headers, timeout=self._timeout_s
            )
        except Exception as error:
            self._fail(f"WhatsApp delivery failed: {_scrub(error, self._token)}")
        self._check(response)

    def _check(self, response: HttpResponse) -> None:
        status = self._status_of(response)
        if 200 <= status < 300:
            return
        codes, detail = _api_error(response)
        if codes & WINDOW_ERROR_CODES:
            # Der haeufigste Stolperstein dieses Kanals. Der rohe Text lautet
            # „(#131047) Re-engagement message" und erklaert niemandem, was zu tun ist.
            raise OutsideWindowError(WINDOW_MESSAGE)
        clean = _scrub(detail, self._token)
        suffix = f": {clean}" if clean else ""
        self._fail(f"WhatsApp API rejected the message (HTTP {status}){suffix}")

    @staticmethod
    def _status_of(response: HttpResponse) -> int:
        try:
            return int(response.status_code)
        except (AttributeError, TypeError, ValueError):
            return 0  # unlesbare Antwort gilt als Fehlschlag, nie als Zustellung

    @staticmethod
    def _fail(message: str) -> NoReturn:
        """Wirft bereinigt — und **ohne** Ursachenkette (`from None`).

        Das ist Absicht: ein `raise ... from error` haengt die Original-Ausnahme an, und
        genau die traegt bei HTTP-Bibliotheken den Authorization-Header oder die volle
        URL. Ein Traceback wuerde den Token dann trotz sauberer Meldung ausdrucken.
        """
        raise WhatsAppError(message) from None
