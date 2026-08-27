"""Telegram-Kanal mit sauberer Antwort und einer flüchtigen Live-Aktivität.

`TelegramClient` kapselt ausschließlich die Bot-API. `TelegramChannel` übersetzt den
kanal-neutralen Rückweg. `TelegramActivity` ist der kleine, injizierbare UX-Baustein:
eine stille Nachricht, begrenzte/gedrosselte Edits und am Ende Löschen statt Chat-Müll.
`TelegramReply` ist der zweite: die mitwachsende Antwort, die am Ende die endgültige wird.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

import requests

from .agent_loop import AgentProgress, ProgressStage
from .channel import Button, CallbackQuery, Inbound, Principal, StructuredMessage, Trust
from .identity import agent_name
from .ux import GEOMETRIC, Style, style_for

_BASE = "https://api.telegram.org/bot{token}/{method}"
CHANNEL_NAME = "telegram"
# Telegram duldet pro Chat rund einen Edit pro Sekunde. 0.8 s waren 1,25/s — bei einer
# schnellen Tool-Kaskade (bis 2x MAX_STEPS Events) reicht das fuer ein 429 mit retry_after.
# 1.2 s liegt sicher darunter und ist auf dem Handy nicht von 0.8 s zu unterscheiden.
DEFAULT_EDIT_INTERVAL_S = 1.2
# Ein Lauf hat bis zu MAX_STEPS Schritte; 5 Zeilen schnitten die Haelfte davon ab.
DEFAULT_ACTIVITY_LINES = 8
# Ohne neuen Inhalt haelt nur die Uhr die Anzeige lebendig — seltener als ein echter Edit.
DEFAULT_HEARTBEAT_S = 5.0
# Telegram nimmt hoechstens 4096 Zeichen pro Nachricht. Laengeres waere ein Aufruf, der
# sicher mit 400 zurueckkommt — waehrend des Wachsens gar nicht erst versuchen.
TELEGRAM_TEXT_LIMIT = 4096
# Uploads duerfen laenger dauern als ein Text-POST: 20 MB auf einer Pi-Leitung.
_UPLOAD_TIMEOUT_S = 120
# Der Reasoner gibt fuer einen Werkzeugwunsch GENAU eine Zeile `TOOL_CALL: {…}` aus
# (TOOL_PROTOCOL in reasoner.py, gelesen von agent_loop.parse_tool_call). Das ist
# Maschinerie und niemals Text fuer den Betreiber.
TOOL_CALL_MARKER = "TOOL_CALL"

def split_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> tuple[str, ...]:
    """Eine zu lange Antwort in mehrere Nachrichten — statt sie zu verlieren.

    ⚠️ Der Anlass ist ein echter Ausfall: die Grenze wurde bisher nur waehrend des
    WACHSENS geprueft (`_text > TELEGRAM_TEXT_LIMIT`), die fertige Antwort ging ungeteilt
    hinaus. Telegram lehnt sie mit 400 ab, `_deliver` meldet False, und der Betreiber sah
    „could not deliver the answer" — waehrend die fertige Antwort daneben lag und
    weggeworfen wurde. Ein Lauf, der gedacht, geurteilt und ausgefuehrt hat, darf nicht an
    der letzten Zeile sterben.

    Die Zustellung bleibt im Grundsatz EINE Nachricht (siehe `Conductor._deliver`): geteilt
    wird nur, was sonst gar nicht ankaeme, und dann an Absatz- vor Zeilen- vor Wortgrenzen.
    Mitten im Wort zu trennen waere die Sorte Zustellung, die man lieber nicht gehabt haette.
    """
    roh = str(text or "")
    if len(roh) <= limit:
        return (roh,)

    teile: list[str] = []
    rest = roh
    while len(rest) > limit:
        fenster = rest[:limit]
        # Absatz, dann Zeile, dann Leerzeichen — die erste Grenze, die nicht im letzten
        # Viertel des Fensters verhungert, damit ein Teil nicht fast leer bleibt.
        schnitt = -1
        for trenner in ("\n\n", "\n", " "):
            kandidat = fenster.rfind(trenner)
            if kandidat > limit // 4:
                schnitt = kandidat + (len(trenner) if trenner != " " else 1)
                break
        if schnitt <= 0:
            schnitt = limit                      # ein Wort laenger als das Fenster
        teile.append(rest[:schnitt].rstrip())
        rest = rest[schnitt:].lstrip()
    if rest:
        teile.append(rest)
    # Nummeriert, weil eine Folge ohne Zaehler wie mehrere Antworten aussieht.
    gesamt = len(teile)
    return tuple(f"{teil}\n\n_({i}/{gesamt})_" if gesamt > 1 else teil
                 for i, teil in enumerate(teile, 1))


# --- Wortschatz des Live-Feeds -------------------------------------------------------
# Bewusst an EINER Stelle: das sind die Woerter, die the operator bei jedem Lauf sieht. Keine
# i18n-Tabelle (ein Nutzer, eine Sprache) — nur benannte Konstanten statt Streuung.
# Die Zeichen sind geometrisch statt der ueblichen Chatbot-Emoji: Talos ist ein
# gravierter Bronzeautomat. Ein Zeichen, eine Bedeutung, nie doppelt belegt.
TXT_THINKING = "reasoning"
TXT_STARTING = "starting"
TXT_DONE = "done"
TXT_FAILED = "failed"
TXT_NEEDS_YOU = "needs you"
TXT_REFUSED = "refused"
TXT_EARLIER = "earlier steps"
TXT_UNKNOWN_ERROR = "unknown error"

TOOL_LABELS: dict[str, str] = {
    "read_file": "read",
    "write_file": "write",
    "run_shell": "shell",
    "undo_last": "undo",
    "vault_search": "search vault",
    "vault_get": "read vault note",
    "vault_write_note": "write vault note",
}
TOOL_LABEL_FALLBACK = "tool"
LABEL_PROTECTED = "protected file"
LABEL_SHELL_GENERIC = "shell command"


@dataclass(frozen=True)
class Update:
    update_id: int
    user_id: int
    chat_id: int
    text: str
    callback: CallbackQuery | None = None


# --- Angehaengtes: was ankommt, statt es fallen zu lassen ---------------------------
# Bis hierher galt `if text is None: continue` — ein Foto, eine Sprachnachricht, ein
# Dokument verschwand spurlos. Von aussen ist das nicht von „Bot ist tot" zu
# unterscheiden: the operator schickt ein Bild und bekommt nichts, nicht einmal ein Nein.
#
# Beschrieben wird ausschliesslich, was Telegram MITLIEFERT — Art, Groesse, Masse, Dauer,
# Dateiname. Kein Wort darueber, was zu sehen oder zu hoeren ist: das weiss hier niemand.
# Und der Satz, der die Selbsttaeuschung verhindert, steht mit drin — sonst reimt sich
# das Modell aus Dateiname und Masse eine Bildbeschreibung zusammen, und der Betreiber
# haelt sie fuer eine Wahrnehmung.
BLIND_NOTE = "Its content is not available to you — say so plainly instead of guessing."


def _kb(size: object) -> str:
    try:
        zahl = int(size)
    except (TypeError, ValueError):
        return ""
    return f"{zahl / 1024:.0f} kB" if zahl >= 1024 else f"{zahl} B"


def _join_facts(*teile: str) -> str:
    return ", ".join(t for t in teile if t)


def largest_photo(message: object) -> dict:
    """Die groesste angebotene Aufloesung — oder ein leeres Dict."""
    if not isinstance(message, dict):
        return {}
    fotos = message.get("photo")
    if not isinstance(fotos, list) or not fotos:
        return {}
    return max(
        (f for f in fotos if isinstance(f, dict)),
        key=lambda f: int(f.get("file_size") or 0),
        default={},
    )


def attachment_note(message: dict, saved: str = "") -> str:
    """Eine Zeile ueber das Angehaengte — oder leer, wenn nichts dranhaengt.

    `saved` ist der Pfad, unter dem ein Foto oder eine Sprach-/Audioaufnahme tatsaechlich
    abgelegt wurde. Steht er da, faellt der Blind-Satz weg und es steht stattdessen dort,
    WO die Datei liegt — erst damit hat Sehen oder Hoeren ein Ziel, ueber das der Kernel
    urteilen kann. Ohne Pfad bleibt es beim ehrlichen „Inhalt liegt mir nicht vor": das ist
    kein Platzhalter, sondern der Fall, in dem nichts geholt wurde (fremder Absender, zu
    gross, kein Bild, keine Aufnahme).
    """
    if not isinstance(message, dict):
        return ""
    groesstes = largest_photo(message)
    if groesstes:
        masse = ""
        if groesstes.get("width") and groesstes.get("height"):
            masse = f"{groesstes['width']}\u00d7{groesstes['height']}"
        fakten = _join_facts(masse, _kb(groesstes.get("file_size")))
        if saved:
            return f"[photo attached — {fakten}. Saved to {saved} — read it with see_image.]"
        return f"[photo attached — {fakten}. {BLIND_NOTE}]"

    for schluessel, name in (
        ("voice", "voice message"), ("audio", "audio file"), ("video", "video"),
        ("video_note", "video note"), ("animation", "animation"), ("document", "file"),
        ("sticker", "sticker"),
    ):
        teil = message.get(schluessel)
        if not isinstance(teil, dict):
            continue
        dauer = f"{int(teil['duration'])} s" if teil.get("duration") else ""
        datei = str(teil.get("file_name") or "")[:80]
        fakten = _join_facts(datei, dauer, _kb(teil.get("file_size")))
        # Ist die Aufnahme geholt worden, bekommt das Modell ein Ziel statt des
        # Blind-Satzes — genau wie beim Foto. `hear` ist READ; der Kernel urteilt ueber den
        # Pfad. Nur Sprache/Audio: Video herausschneiden waere ein weicherer Zweitweg.
        hinweis = (
            f"Saved to {saved} — transcribe it with hear."
            if saved and schluessel in ("voice", "audio")
            else BLIND_NOTE
        )
        return f"[{name} attached — {fakten}. {hinweis}]" if fakten else f"[{name} attached. {hinweis}]"
    return ""


# Wohin ein eingehendes Foto gelegt wird. Fest verdrahtet und weder vom Absender noch vom
# Modell beeinflussbar — genau das macht diesen Schreibvorgang zu Infrastruktur (wie den
# Event-Log-Eintrag) und nicht zu einer Wirkung, die eine Freigabe braeuchte. Der Name
# entsteht aus Telegrams `file_unique_id`, auf `[A-Za-z0-9_-]` reduziert; Telegrams
# `file_path` wird NIE zu einem lokalen Pfad gemacht (Verzeichniswechsel).
_INBOX_NAME = "inbox"
_FILE_BASE = "https://api.telegram.org/file/bot{token}/{path}"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
# Ein Foto, kein Datentraeger. Telegrams Bot-API laedt ohnehin nur bis 20 MB herunter.
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
_DOWNLOAD_TIMEOUT_S = 60
_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
# Sprache kommt als ogg/opus (`voice`) oder mit eigener Endung (`audio`). Die Endung wird
# nur aus einer festen Liste gewaehlt, die `hearing.SUFFIXES` deckt; der Dateiname stammt
# weiterhin aus Telegrams `file_unique_id`, nie aus einem vom Absender gewaehlten Feld.
_AUDIO_SUFFIXES = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".webm", ".mp4")
_MIME_AUDIO_SUFFIX = {
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/flac": ".flac", "audio/webm": ".webm",
}


def _audio_suffix(file_name: object, mime: object) -> str:
    """Eine Endung, die `hear` akzeptiert — aus der Dateiendung, sonst dem MIME-Typ, sonst .ogg."""
    name = str(file_name or "")
    punkt = name.rfind(".")
    if punkt != -1 and name[punkt:].lower() in _AUDIO_SUFFIXES:
        return name[punkt:].lower()
    return _MIME_AUDIO_SUFFIX.get(str(mime or "").strip().lower(), ".ogg")


def _plausible_file_path(raw: object) -> str:
    """Telegrams Antwortfeld `file_path` — oder leer, wenn es nicht harmlos aussieht.

    Es geht in eine URL, nicht in einen Dateinamen; trotzdem wird es geprueft. Ein
    Rueckgabewert mit `..`, fuehrendem `/` oder eigenem Schema koennte den Abruf aus dem
    Datei-Endpunkt herausfuehren, und der Aufrufer sieht dem Ergebnis das nicht an.
    """
    pfad = str(raw or "").strip()
    if not pfad or len(pfad) > 300:
        return ""
    if pfad.startswith("/") or "\\" in pfad or ".." in pfad or "://" in pfad:
        return ""
    return pfad


class TelegramClient:
    def __init__(
        self,
        token: str,
        poll_timeout_s: int,
        *,
        inbox: object = None,
        may_fetch: Callable[[int], bool] = lambda _user_id: False,
    ) -> None:
        self._token = token
        self._poll_timeout_s = poll_timeout_s
        # Fail-closed mit Absicht: ohne beides bleibt es beim bisherigen Verhalten. Der
        # Kanal parst Updates, BEVOR der Kernel ueber die Kennung geurteilt hat — ein
        # Abruf ohne diese Frage hiesse, dass jeder Fremde, der den Bot findet, Dateien
        # auf die Platte des Betreibers legen laesst.
        self._inbox = Path(str(inbox)) if inbox else None
        self._may_fetch = may_fetch

    def _url(self, method: str) -> str:
        return _BASE.format(token=self._token, method=method)

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<bot-token>") if self._token else text

    def _call(self, verb: Callable[..., "requests.Response"], method: str, **kw: object):
        """Der einzige Netzweg — und keine Ausnahme verlaesst ihn mit dem Token darin.

        Telegram traegt das Token im PFAD der URL. Damit stand es in der Meldung jeder
        `requests`-Ausnahme, und die Meldung wurde als `str(error)` ins Event-Log
        geschrieben — in eine Datei, die ein Update mitkopiert und jedes Backup
        einpackt. Es kam nicht durch ein Leck im Kernel dorthin, sondern weil ein
        Fehler von aussen zurueckkam und niemand ihn ansah, bevor er festgehalten wurde.

        Deshalb hier und nicht am Event-Log: das Token kennt nur diese Klasse. Eine
        Schwaerzung weiter unten muesste raten, wonach sie sucht.
        """
        try:
            resp = verb(self._url(method), **kw)
            resp.raise_for_status()
            return resp
        except requests.RequestException as error:
            # Ohne `from None` haenge die urspruengliche Ausnahme mit ihrem
            # ungeschwaerzten Text als `__cause__` daran — und ein Logger, der die
            # Kette ausgibt, schriebe das Token doch wieder hin.
            raise type(error)(self._redact(str(error))) from None

    def fetch_photo(self, message: dict, user_id: int) -> str:
        """Holt das groesste Foto und legt es ab. Gibt den Pfad zurueck — oder "".

        Ein Fehlschlag ist NIE eine Ausnahme nach oben: eine Nachricht, die wegen eines
        misslungenen Downloads gar nicht ankommt, waere schlimmer als eine ohne Bild. Der
        Aufrufer bekommt dann den bisherigen Blind-Satz.
        """
        if self._inbox is None or not self._may_fetch(int(user_id)):
            return ""
        foto = largest_photo(message)
        datei_id = str(foto.get("file_id") or "")
        eindeutig = _SAFE_NAME.sub("", str(foto.get("file_unique_id") or ""))[:48]
        if not datei_id or not eindeutig:
            return ""
        # Der angekuendigten Groesse wird nicht geglaubt, aber eine offensichtlich zu
        # grosse Datei wird gar nicht erst angefasst.
        if int(foto.get("file_size") or 0) > MAX_ATTACHMENT_BYTES:
            return ""
        try:
            antwort = self._call(requests.get, "getFile", params={"file_id": datei_id}, timeout=30)
            pfad = _plausible_file_path((antwort.json().get("result") or {}).get("file_path"))
            if not pfad:
                return ""
            roh = self._download(pfad)
        except (requests.RequestException, ValueError, OSError):
            return ""
        # An den ERSTEN BYTES gemessen, nicht am Content-Type und nicht an der Endung, die
        # Telegram mitschickt: was hier ankommt, ist Fremdinhalt und landet auf der Platte.
        from .vision import media_type

        typ = media_type(roh[:16])
        if not typ or typ not in _SUFFIX:
            return ""
        try:
            self._inbox.mkdir(parents=True, exist_ok=True)
            ziel = self._inbox / f"{eindeutig}{_SUFFIX[typ]}"
            ziel.write_bytes(roh)
        except OSError:
            return ""
        return str(ziel)

    def fetch_voice(self, message: dict, user_id: int) -> str:
        """Holt eine Sprach- oder Audionachricht und legt sie im inbox ab. Pfad — oder "".

        Dieselbe fail-closed-Absicherung wie `fetch_photo`: ohne inbox UND ohne die
        Kennung-Frage (`_may_fetch`) wird nichts geholt, sonst legt jeder Fremde, der den
        Bot findet, Dateien auf die Platte. Ein Fehlschlag ist NIE eine Ausnahme nach oben;
        der Aufrufer faellt dann auf den Blind-Satz zurueck. `voice` ist ogg/opus, `audio`
        bringt oft eine eigene Endung mit — beides landet unter einem Namen aus
        `file_unique_id`, damit weder Absender noch Modell den Ablageort waehlen.
        """
        if self._inbox is None or not self._may_fetch(int(user_id)):
            return ""
        teil = message.get("voice")
        endung = ".ogg"
        if not isinstance(teil, dict):
            teil = message.get("audio")
            if not isinstance(teil, dict):
                return ""
            endung = _audio_suffix(teil.get("file_name"), teil.get("mime_type"))
        datei_id = str(teil.get("file_id") or "")
        eindeutig = _SAFE_NAME.sub("", str(teil.get("file_unique_id") or ""))[:48]
        if not datei_id or not eindeutig:
            return ""
        if int(teil.get("file_size") or 0) > MAX_ATTACHMENT_BYTES:
            return ""
        try:
            antwort = self._call(requests.get, "getFile", params={"file_id": datei_id}, timeout=30)
            pfad = _plausible_file_path((antwort.json().get("result") or {}).get("file_path"))
            if not pfad:
                return ""
            roh = self._download(pfad)
        except (requests.RequestException, ValueError, OSError):
            return ""
        if not roh:
            return ""
        try:
            self._inbox.mkdir(parents=True, exist_ok=True)
            ziel = self._inbox / f"{eindeutig}{endung}"
            ziel.write_bytes(roh)
        except OSError:
            return ""
        return str(ziel)

    def _download(self, file_path: str) -> bytes:
        """Laedt hoechstens `MAX_ATTACHMENT_BYTES` — der Zaehler waehrend des Lesens ist
        der echte Deckel, nicht `Content-Length`."""
        url = _FILE_BASE.format(token=self._token, path=file_path)
        try:
            with requests.get(url, stream=True, timeout=_DOWNLOAD_TIMEOUT_S) as antwort:
                antwort.raise_for_status()
                teile: list[bytes] = []
                gesamt = 0
                for stueck in antwort.iter_content(64 * 1024):
                    gesamt += len(stueck or b"")
                    if gesamt > MAX_ATTACHMENT_BYTES:
                        return b""
                    teile.append(bytes(stueck or b""))
                return b"".join(teile)
        except requests.RequestException as fehler:
            raise type(fehler)(self._redact(str(fehler))) from None

    def get_updates(self, offset: int) -> list[Update]:
        """Holt neue Text-Updates ab `offset`. Long-Poll; Nicht-Text wird ignoriert."""
        resp = self._call(
            requests.get,
            "getUpdates",
            params={"offset": offset, "timeout": self._poll_timeout_s},
            timeout=self._poll_timeout_s + 10,
        )
        payload = resp.json()
        updates: list[Update] = []
        for item in payload.get("result", []):
            callback = item.get("callback_query") or {}
            if callback:
                frm = callback.get("from") or {}
                message = callback.get("message") or {}
                chat = message.get("chat") or {}
                data = callback.get("data")
                if (
                    isinstance(data, str)
                    and "id" in callback
                    and "id" in frm
                    and "id" in chat
                    and "message_id" in message
                ):
                    updates.append(
                        Update(
                            update_id=int(item["update_id"]),
                            user_id=int(frm["id"]),
                            chat_id=int(chat["id"]),
                            text="",
                            callback=CallbackQuery(
                                str(callback["id"]), data, int(message["message_id"])
                            ),
                        )
                    )
                continue
            message = item.get("message") or {}
            text = message.get("text")
            frm = message.get("from") or {}
            chat = message.get("chat") or {}
            if "id" not in frm or "id" not in chat:
                continue
            if text is None:
                # Angehaengtes statt Text. Die Bildunterschrift IST das Wort des
                # Betreibers und steht deshalb vorne; die Fakten ueber die Datei
                # kommen darunter, klar als Beobachtung erkennbar.
                notiz = attachment_note(
                    message,
                    self.fetch_photo(message, frm["id"]) or self.fetch_voice(message, frm["id"]),
                )
                if not notiz:
                    continue  # weder Text noch etwas, worueber sich reden liesse
                caption = str(message.get("caption") or "").strip()
                text = f"{caption}\n{notiz}" if caption else notiz
            updates.append(
                Update(
                    update_id=int(item["update_id"]),
                    user_id=int(frm["id"]),
                    chat_id=int(chat["id"]),
                    text=str(text),
                )
            )
        return updates

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        disable_notification: bool = False,
        parse_mode: str | None = None,
        reply_markup: object | None = None,
    ) -> int:
        data: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": disable_notification,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        resp = self._call(requests.post, "sendMessage", data=data, timeout=30)
        result = (resp.json() or {}).get("result") or {}
        if "message_id" not in result:
            raise ValueError("Telegram sendMessage ohne message_id")
        return int(result["message_id"])

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: object | None = None,
    ) -> None:
        data: dict[str, object] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        self._call(requests.post, "editMessageText", data=data, timeout=30)

    def answer_callback_query(self, query_id: str, text: str = "") -> None:
        data: dict[str, object] = {"callback_query_id": query_id}
        if text:
            data["text"] = text[:200]
        self._call(requests.post, "answerCallbackQuery", data=data, timeout=30)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._call(
            requests.post,
            "deleteMessage",
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=30,
        )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self._call(
            requests.post, "sendChatAction",
            data={"chat_id": chat_id, "action": action}, timeout=30,
        )

    def send_document(self, chat_id: int, path: str) -> None:
        """Eine Datei als Dokument. Der Pfad ist bereits gegatet (`attachment.resolve`)."""
        ziel = Path(path)
        with ziel.open("rb") as handle:
            self._call(
                requests.post,
                "sendDocument",
                data={"chat_id": chat_id},
                files={"document": (ziel.name, handle)},
                timeout=_UPLOAD_TIMEOUT_S,
            )

    def send_photo(self, chat_id: int, path: str) -> None:
        """Ein Bild als Foto. Nur fuer echte Bilder — die Wahl trifft der Aufrufer an den Bytes."""
        ziel = Path(path)
        with ziel.open("rb") as handle:
            self._call(
                requests.post,
                "sendPhoto",
                data={"chat_id": chat_id},
                files={"photo": (ziel.name, handle)},
                timeout=_UPLOAD_TIMEOUT_S,
            )


class ActivityClient(Protocol):
    """Kleine Bot-API-Oberfläche für fälschbare, netzfreie Activity-Tests."""

    def send_message(self, chat_id: int, text: str, **kwargs: object) -> int: ...

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: object
    ) -> None: ...

    def delete_message(self, chat_id: int, message_id: int) -> None: ...

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...


@dataclass
class _Line:
    """Eine Zeile des Verlaufs mit eigener Uhr. `done_at=None` heisst: laeuft noch."""

    sym: str
    text: str
    started: float
    done_at: float | None = None

    def render(self, now: float) -> str:
        span = (self.done_at if self.done_at is not None else now) - self.started
        # Unter einer Sekunde keine Zahl: sonst zappelt die Zeile bei jedem Edit.
        stamp = f"  {span:.0f}s" if span >= 1.0 else ""
        return f"{self.sym} {self.text}{stamp}"


class TelegramActivity:
    """Eine stille Statusnachricht, die den Lauf mitschreibt — und danach stehen bleibt.

    Vier bewusste Entscheidungen:
    1. **Die Statusnachricht entsteht erst beim ersten Werkzeug.** Eine gewoehnliche
       Textantwort soll sofort und allein dastehen — eine Kopfzeile ueber „Hallo, the operator."
       ist Laerm, kein Beleg. Bis dahin traegt nur der Tipp-Indikator die Wartezeit,
       genau wie bei Hermes.
    2. Die Denkphase ist sichtbar, sobald die Anzeige existiert. Sie war die laengste
       Phase eines Laufs und die einzige, die nichts zeigte.
    3. Der Verlauf wird am Ende NICHT geloescht. Wo Werkzeuge liefen, ist er der Beleg,
       was angefasst wurde — Loeschen sparte Chat-Muell und kostete die Nachvollziehbarkeit.
    4. Jede Zeile traegt ihre Dauer, der Kopf traegt Gesamtzeit und Schritt.
    """

    def __init__(
        self,
        client: ActivityClient,
        chat_id: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_edit_interval: float = DEFAULT_EDIT_INTERVAL_S,
        max_lines: int = DEFAULT_ACTIVITY_LINES,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        name: str = "",
        style: Style = GEOMETRIC,
    ) -> None:
        self._client = client
        self._style = style
        self._chat_id = chat_id
        self._clock = clock
        self._min_edit_interval = max(0.0, min_edit_interval)
        self._max_lines = max(1, max_lines)
        self._heartbeat_s = max(0.0, heartbeat_s)
        self._name = name or agent_name()
        self._lines: list[_Line] = []
        self._thinking: _Line | None = None
        self._dropped = 0
        self._step = 0
        self._max_steps = 0
        self._finished = False
        self._start = clock()
        self._lock = threading.Lock()
        # Noch KEINE Nachricht: erst ein Werkzeug rechtfertigt eine (siehe `_ensure_message`).
        self._message_id: int | None = None
        self._last_edit = clock()
        self._typing()
        # Ein Reasoner-Zug blockiert bis zu drei Minuten. Solange keine Anzeige existiert,
        # haelt der Takt den Tipp-Indikator wach (Telegram vergisst ihn nach ~5 s); danach
        # haelt er die Uhr am Laufen. Daemon-Thread: er haelt den Prozess nie auf.
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None
        if self._heartbeat_s > 0:
            self._beat = threading.Thread(target=self._heartbeat, daemon=True)
            self._beat.start()

    def _typing(self) -> None:
        try:
            self._client.send_chat_action(self._chat_id, "typing")
        except Exception:
            pass

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._heartbeat_s):
            if self._finished:
                return
            try:
                if self._message_id is None:
                    self._typing()   # nichts anzuzeigen — aber the operator soll sehen, dass es laeuft
                else:
                    self._edit(force=True)
            except Exception:
                return  # die Anzeige ist Komfort; ein toter Takt darf den Lauf nicht stoeren

    def _ensure_message(self) -> bool:
        """Legt die Statusnachricht an, sobald es etwas zu zeigen gibt. Einmal, nie erneut."""
        if self._message_id is not None:
            return True
        try:
            self._message_id = self._client.send_message(
                self._chat_id, self._render(), disable_notification=True
            )
        except Exception:
            return False
        self._last_edit = self._clock()
        return True

    # ------------------------------------------------------------------ Ereignisse
    def progress(self, event: AgentProgress) -> None:
        if self._finished:
            return
        if event.step:
            self._step = event.step
        if event.max_steps:
            self._max_steps = event.max_steps

        if event.stage is ProgressStage.THINKING:
            # Denken allein rechtfertigt keine Nachricht: sonst blitzt bei jeder reinen
            # Textantwort eine Kopfzeile auf, die gleich wieder verschwinden muesste.
            self._begin_thinking()
            if self._message_id is not None:
                self._edit()
            return
        if event.stage is ProgressStage.PLAN:
            # Die Ankuendigung steht fertig da, sobald sie gelesen ist: sie dauert nicht,
            # sie gilt. Deshalb sofort mit `done_at` — eine mitlaufende Sekundenzahl
            # neben einem Satz, der sich nie mehr aendert, waere erfundene Bewegung.
            self._end_thinking()
            now = self._clock()
            self._append(_Line(self._style.plan, event.summary, now, done_at=now))
            if self._ensure_message():
                self._edit(force=True)
            return
        if event.stage is ProgressStage.TOOL:
            self._end_thinking()
            self._append(
                _Line(
                    self._style.tool_symbol(event.tool),
                    _tool_text(event, self._style),
                    self._clock(),
                )
            )
            # Hier faellt die Entscheidung: ab jetzt gibt es etwas zu belegen.
            if self._ensure_message():
                # Ein Tool-Start ist die wichtigste Zwischeninformation — der darf nicht warten.
                self._edit(force=True)
            return
        if event.stage is ProgressStage.RESULT:
            self._close_last(event)
            self._edit(force=True)

    def tick(self) -> None:
        """Manueller Takt fuer Aufrufer ohne Thread (und fuer Tests)."""
        if self._finished:
            return
        if self._message_id is None:
            self._typing()
            return
        if self._clock() - self._last_edit >= self._heartbeat_s:
            self._edit(force=True)

    # ------------------------------------------------------------------ Abschluss
    def succeed(self, footer: str = "") -> None:
        """Verlauf einfrieren und stehen lassen. Die eigentliche Antwort folgt separat.

        Lief kein Werkzeug, existiert keine Anzeige — dann bleibt der Chat auch sauber:
        the operator sieht nur seine Frage und die Antwort.
        """
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        if self._message_id is None:
            return
        self._settle()
        self._edit(text=self._render(final=True, footer=footer), force=True)

    def fail(self, error: str) -> None:
        """Ein Fehler wird immer gemeldet — notfalls als eigene Nachricht.

        Anders als beim Erfolg: ein stiller Fehlschlag waere die eine Situation, in der
        Schweigen luegt. Deshalb legt `fail` die Anzeige notfalls selbst an.
        """
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        self._settle()
        detail = _redact(error) or TXT_UNKNOWN_ERROR
        if self._message_id is None:
            try:
                self._client.send_message(
                    self._chat_id, f"{self._style.fail} {TXT_FAILED}: {detail}", disable_notification=True
                )
            except Exception:
                pass
            return
        body = self._render(final=True)
        self._edit(text=f"{body}\n{self._style.fail} {TXT_FAILED}: {detail}", force=True)

    # ------------------------------------------------------------------ intern
    def _begin_thinking(self) -> None:
        if self._thinking is not None:
            return
        self._thinking = _Line(self._style.thinking, TXT_THINKING, self._clock())
        self._append(self._thinking)
        self._edit()

    def _end_thinking(self) -> None:
        """Die Denk-Zeile weicht dem Werkzeug, das aus ihr hervorging.

        Stehen zu lassen waere ehrlicher, macht aber bei acht Schritten eine Anzeige,
        die zur Haelfte aus „reasoning" besteht. Die Zeit steckt ohnehin im Kopf-Timer.
        """
        if self._thinking is None:
            return
        if self._thinking in self._lines:
            self._lines.remove(self._thinking)
        self._thinking = None

    def _append(self, line: _Line) -> None:
        self._lines.append(line)
        while len(self._lines) > self._max_lines:
            self._lines.pop(0)
            self._dropped += 1

    def _close_last(self, event: AgentProgress) -> None:
        if not self._lines:
            return
        line = self._lines[-1]
        line.done_at = self._clock()
        if event.status == "done":
            line.sym = self._style.ok
        elif event.status == "needs_human":
            line.sym, line.text = self._style.gate, f"{_tool_text(event, self._style)} — {TXT_NEEDS_YOU}"
        elif event.status in {"denied", "binding_changed"}:
            line.sym, line.text = self._style.blocked, f"{_tool_text(event, self._style)} — {TXT_REFUSED}"
        else:
            line.sym = self._style.fail

    def _settle(self) -> None:
        self._end_thinking()
        now = self._clock()
        for line in self._lines:
            if line.done_at is None:
                line.done_at = now

    def _render(self, *, final: bool = False, footer: str = "") -> str:
        now = self._clock()
        elapsed = now - self._start
        head = f"{self._style.talos} {self._name} · {elapsed:.0f}s"
        if not final and self._max_steps:
            head += f" · step {self._step}/{self._max_steps}"
        parts = [head]
        if self._dropped:
            parts.append(f"… {self._dropped} {TXT_EARLIER}")
        parts.extend(line.render(now) for line in self._lines)
        if final and footer:
            parts.append(footer)
        return "\n".join(parts)

    def _edit(self, *, text: str | None = None, force: bool = False) -> None:
        if self._message_id is None:
            return
        with self._lock:
            if not force and self._clock() - self._last_edit < self._min_edit_interval:
                return  # gepuffert; der naechste erlaubte Edit zeigt den gebuendelten Stand
            payload = self._render() if text is None else text
            self._last_edit = self._clock()
        try:
            self._client.edit_message_text(self._chat_id, self._message_id, payload)
        except Exception:
            # Die Anzeige ist Komfort. Ein 429 oder ein geloeschter Chat darf den Lauf
            # nicht stoppen und ihn erst recht nicht ein zweites Mal ausloesen.
            return


class _Verdict(Enum):
    """Was mit einem Reasoner-Zug geschieht, sobald genug Zeichen da sind."""

    MUTE = "mute"   # eine TOOL_CALL-Zeile: Maschinerie, gehoert nie in den Chat
    SHOW = "show"   # Prosa: darf wachsen


def _verdict(collected: str) -> _Verdict | None:
    """Entscheidet ueber einen Zug — `None` heisst: noch nicht entscheidbar.

    Die CLI teilt den Text beliebig auf; ein Delta kann mitten in `TOOL_CALL` brechen.
    Entschieden wird deshalb erst, wenn der gesammelte Anfang es beweist: solange er ein
    echtes Praefix des Markers ist (`TOOL_CA`), wird weiter zurueckgehalten. Kommt statt
    dessen irgendein anderes Zeichen — auch ein Zeilenumbruch —, ist der Marker
    ausgeschlossen und der Puffer darf raus.

    Fuehrender Leerraum zaehlt nicht: `parse_tool_call` erlaubt ihn vor der Zeile, also
    darf er hier auch keine Entscheidung ausloesen.
    """
    head = collected.lstrip()
    if not head:
        return None
    if head.startswith(TOOL_CALL_MARKER):
        return _Verdict.MUTE
    if TOOL_CALL_MARKER.startswith(head):
        return None
    return _Verdict.SHOW


class TelegramReply:
    """Die mitwachsende Antwortnachricht — am Ende ist sie die endgueltige Antwort.

    Bewusst getrennt von `TelegramActivity`: die Statusanzeige *belegt*, was lief
    (Kopfzeile, Werkzeugzeilen, Quittung). Diese Nachricht *ist* die Antwort und traegt
    darum keine Kopfzeile — eine werkzeugfreie Antwort bekommt keine, das ist ein
    Projektentscheid und keine Stilfrage. Laufen beide, bleibt der Beleg stehen und die
    Antwort ist ihre eigene Nachricht; sie kommen sich nicht ins Gehege.

    Zwei Regeln tragen den Rest:

    1. **Erst entscheiden, dann zeigen.** Der Anfang jedes Zuges wird zurueckgehalten,
       bis feststeht, ob Prosa oder eine `TOOL_CALL`-Zeile kommt (siehe `_verdict`).
       Bei Maschinerie bleibt dieser Zug vollstaendig stumm.
    2. **Genau eine Antwortnachricht.** `adopt` macht die gewachsene zur endgueltigen.
       Nur wenn das nicht geht, sendet der Aufrufer normal — nie beides.

    Die Anzeige ist Komfort, die Antwort nicht: jeder Netzfehler bleibt hier und wird
    zu einem `False`, das den Aufrufer auf den normalen Sendeweg zurueckfallen laesst.
    """

    def __init__(
        self,
        client: ActivityClient,
        chat_id: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        min_edit_interval: float = DEFAULT_EDIT_INTERVAL_S,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._clock = clock
        self._min_edit_interval = max(0.0, min_edit_interval)
        self._message_id: int | None = None
        self._buffer = ""    # zurueckgehaltener Anfang, bis der Zug entscheidbar ist
        self._text = ""      # was dieser Zug bisher gesagt hat
        self._shown = ""     # was nachweislich in der Nachricht steht
        self._muted = False
        self._decided = False
        self._done = False
        # Das erste Delta soll sofort sichtbar werden — dafuer wurde gestreamt.
        self._last_edit = clock() - self._min_edit_interval
        # Nur Zustand liegt unter dem Lock, nie ein Netzaufruf: sonst haenge der
        # Poll-Thread an einem Telegram-Timeout und `/stop` waere nur eine Behauptung.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ Ereignisse
    def begin_turn(self) -> None:
        """Neuer Reasoner-Zug, neue Entscheidung.

        Ein Lauf hat mehrere Zuege: erst Werkzeuge (stumm), am Ende Prosa. Die Nachricht
        bleibt dieselbe — der neue Zug ueberschreibt sie —, damit pro Lauf hoechstens
        eine wachsende Nachricht im Chat steht.
        """
        with self._lock:
            if self._done:
                return
            self._buffer = ""
            self._text = ""
            self._muted = False
            self._decided = False

    def push(self, delta: str) -> None:
        """Text-Delta aus dem Stream. Gedrosselt, nie blockierend, nie fatal."""
        with self._lock:
            if self._done or self._muted or not delta:
                return
            if not self._decided:
                self._buffer += delta
                verdict = _verdict(self._buffer)
                if verdict is None:
                    return                      # noch unklar: nichts nach draussen
                self._decided = True
                if verdict is _Verdict.MUTE:
                    self._muted = True
                    self._buffer = ""
                    return
                self._text, self._buffer = self._buffer, ""
            else:
                self._text += delta
            payload = self._due()
        if payload is not None:
            self._write(payload)

    # ------------------------------------------------------------------ Abschluss
    def adopt(self, text: str) -> bool:
        """Macht die gewachsene Nachricht zur endgueltigen Antwort.

        `False` heisst: es gibt keine (nichts gestreamt) oder Telegram nahm sie nicht —
        dann sendet der Aufrufer die Antwort ganz normal. Es darf nie beides passieren.
        """
        with self._lock:
            self._done = True
            message_id, shown = self._message_id, self._shown
        final = text.strip()
        if message_id is None or not final:
            return False
        try:
            # Jetzt EINMAL formatiert: der Text ist vollstaendig, ein Codeblock kann
            # nicht mehr halb offen sein (siehe `_write`).
            self._client.edit_message_text(
                self._chat_id, message_id, final, parse_mode="Markdown"
            )
        except Exception:
            return self._adopt_fallback(message_id, final, shown)
        self._shown = final
        return True

    def _adopt_fallback(self, message_id: int, final: str, shown: str) -> bool:
        """Zweiter Versuch ohne Formatierung — die Antwort ist wichtiger als ihr Satz.

        Zwei Faelle landen hier: der Text steht bereits unveraendert da (Telegram lehnt
        eine Bearbeitung ohne Aenderung mit 400 ab — dann ist die Antwort zugestellt),
        oder das Markdown der Antwort selbst ist kaputt. Im zweiten Fall geht sie
        unformatiert raus statt verloren.
        """
        if final == shown:
            return True
        try:
            self._client.edit_message_text(self._chat_id, message_id, final)
        except Exception:
            return False
        self._shown = final
        return True

    def settle(self) -> None:
        """Letzten Stand ausschreiben und einfrieren.

        Fuer Laeufe, deren Antwort einen anderen Weg nimmt (Freigabe-Dialog, Antwort auf
        einen Button). Ohne das bliebe der zuletzt gedrosselte Rest fuer immer unsichtbar.
        """
        with self._lock:
            self._done = True
            text = self._text
            pending = self._message_id is not None and text != self._shown
        if pending and text.strip():
            self._write(text)

    # ------------------------------------------------------------------ intern
    def _due(self) -> str | None:
        """Der Text, der jetzt raus darf — oder `None`, wenn die Drosselung noch laeuft.

        Aufrufer haelt das Lock; hier wird nur gerechnet und der Takt gestellt.
        """
        now = self._clock()
        if now - self._last_edit < self._min_edit_interval:
            return None                       # gebuendelt; der naechste Takt zeigt alles
        if not self._text.strip() or self._text == self._shown:
            return None
        if len(self._text) > TELEGRAM_TEXT_LIMIT:
            return None                       # sichere 400 — die Endfassung geht eigene Wege
        self._last_edit = now
        return self._text

    def _write(self, text: str) -> bool:
        """Sendet oder bearbeitet — **bewusst ohne `parse_mode`**.

        Ein halb gestreamter Text enthaelt regelmaessig einen noch offenen Codeblock oder
        ein einzelnes `*`. Telegram lehnt so eine Bearbeitung mit 400 ab, und die Antwort
        wuerde genau dann aufhoeren zu wachsen, wenn sie interessant wird. Roh gesendet
        waechst sie immer; formatiert wird ein einziges Mal, in `adopt`, wenn der Text
        vollstaendig ist.
        """
        try:
            if self._message_id is None:
                # Mit Ton: diese Nachricht IST die Antwort, und eine Antwort ohne
                # Benachrichtigung waere eine, die der Betreiber nie bemerkt.
                self._message_id = self._client.send_message(self._chat_id, text)
            else:
                self._client.edit_message_text(self._chat_id, self._message_id, text)
        except Exception:
            return False   # Komfort. Der naechste Takt schickt ohnehin den vollen Stand.
        self._shown = text
        return True


def _tool_text(event: AgentProgress, style: Style = GEOMETRIC) -> str:
    """Was das Werkzeug tut — nie Prompt, Inhalt oder voller Shell-Befehl."""
    tool = re.sub(r"[^A-Za-z0-9_.-]", "", event.tool)[:48]
    base = TOOL_LABELS.get(tool, TOOL_LABEL_FALLBACK)
    label = style.tool_label(tool, base)
    summary = _redact(event.summary)
    # Ein Shell-Summary bleibt generisch, auch wenn ein fremder Callback rohe Argumente
    # hineinschreibt. Der volle Befehl gehoert nur in den Freigabe-Dialog.
    if tool == "run_shell":
        verb = style.tool_verbs.get("run_shell")
        return f"{verb} command" if verb else LABEL_SHELL_GENERIC
    # Der Loop liefert das Basislabel manchmal mit ("write — notes.md"); es wird abgetrennt,
    # damit das gewaehlte Label nicht doppelt erscheint, sobald der Stil es umbenennt.
    if summary == base:
        summary = ""
    else:
        summary = summary.removeprefix(f"{base} — ")
    return f"{label} — {summary}" if summary else label


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|api[_-]?key|authorization|password|passwd|secret)\s*[:=]\s*\S+"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:sk|ghp|xox[baprs])-[A-Za-z0-9._-]{6,}")


def _redact(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _SECRET_TOKEN.sub("[REDACTED]", text)
    return text[:240]


def to_inbound(update: Update) -> Inbound:
    """Bot-API-Update -> kanal-neutrale Nachricht. Alle drei Schlüssel tragen den Kanal."""
    return Inbound(
        principal=Principal(CHANNEL_NAME, str(update.user_id)),
        conversation=f"{CHANNEL_NAME}:{update.chat_id}",
        text=update.text,
        dedup_key=(
            f"{CHANNEL_NAME}:callback:{update.callback.query_id}"
            if update.callback is not None
            else f"{CHANNEL_NAME}:update:{update.update_id}"
        ),
        callback=update.callback,
    )


def chat_id_of(conversation: str) -> int:
    """`telegram:12345` -> 12345. Fremder Kanal -> Fehler statt Zustellung ins Blaue."""
    name, _, raw = conversation.partition(":")
    if name != CHANNEL_NAME:
        raise ValueError(f"nicht dieser Kanal: {conversation!r}")
    return int(raw)


class TelegramChannel:
    """`Channel`-Implementierung. Hält Offset und Telegram-spezifische UX lokal."""

    name = CHANNEL_NAME
    trust = Trust.FULL

    def __init__(self, client: ActivityClient, *, status_style: str = "geometric") -> None:
        self._client = client
        self._offset = 0
        self._style = style_for(status_style)

    def poll(self) -> list[Inbound]:
        # ActivityClient ist für Tests schmal; der echte Client stellt get_updates bereit.
        get_updates = getattr(self._client, "get_updates")
        updates = get_updates(self._offset)
        for update in updates:
            self._offset = max(self._offset, update.update_id + 1)
        return [to_inbound(update) for update in updates]

    def send(self, conversation: str, text: str) -> None:
        # Markdown wird nicht umgeschrieben: insbesondere Codeblöcke bleiben exakt erhalten.
        chat = chat_id_of(conversation)
        for teil in split_for_telegram(text):
            self._client.send_message(chat, teil, parse_mode="Markdown")

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        chat_id = chat_id_of(conversation)
        keyboard = []
        for row in message.keyboard:
            rendered = []
            for button in row:
                if len(button.data.encode("utf-8")) > 64:
                    raise ValueError("Telegram callback_data exceeds 64 bytes")
                rendered.append({"text": button.label, "callback_data": button.data})
            keyboard.append(rendered)
        markup = {"inline_keyboard": keyboard}
        errors: list[str] = []
        answer = getattr(self._client, "answer_callback_query", None)
        if message.callback_query_id and answer is not None:
            try:
                answer(message.callback_query_id, message.callback_notice)
            except Exception as error:
                errors.append(f"answerCallbackQuery: {error}")
        try:
            if message.edit_message_id is not None:
                self._client.edit_message_text(
                    chat_id, message.edit_message_id, message.text, reply_markup=markup
                )
            else:
                self._client.send_message(
                    chat_id, message.text, disable_notification=True, reply_markup=markup
                )
        except Exception as error:
            errors.append(f"message delivery: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def send_file(self, conversation: str, path: str) -> None:
        """Eine Datei als echter Anhang: Bilder als Foto, alles andere als Dokument.

        Die Wahl faellt an den ersten BYTES, nicht an der Endung — eine Endung ist eine
        Behauptung des Dateinamens (dieselbe Regel wie bei `fetch_photo`). Der Pfad
        selbst ist hier schon gegatet: `attachment.resolve` hat ueber Wurzeln, Floors
        und Groesse geurteilt, bevor der Conductor an diese Stelle kommt.
        """
        chat = chat_id_of(conversation)
        from .vision import media_type

        try:
            with open(path, "rb") as handle:
                kind = media_type(handle.read(16))
        except OSError:
            kind = ""
        if kind in _SUFFIX:
            self._client.send_photo(chat, path)
        else:
            self._client.send_document(chat, path)

    def begin_activity(self, conversation: str) -> TelegramActivity:
        # Der Name wird JETZT gelesen, nicht beim Start: wer SOUL.md umbenennt, sah sonst
        # bis zum naechsten Neustart weiter den alten Namen ueber jedem Lauf stehen.
        return TelegramActivity(
            self._client, chat_id_of(conversation), name=agent_name(), style=self._style
        )

    def begin_reply(self, conversation: str) -> TelegramReply:
        """Die mitwachsende Antwort. Getrennt von `begin_activity`, weil sie etwas
        anderes ist: kein Beleg ueber den Lauf, sondern die Antwort selbst."""
        return TelegramReply(self._client, chat_id_of(conversation))
