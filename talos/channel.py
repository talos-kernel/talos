"""Kanäle — wo Talos zuhört, und wer über welchen Weg „den Betreiber" sein darf.

Bis hierher gab es genau einen Weg hinein: den Talos-Bot auf Telegram. Solange das
so bleibt, trägt eine nackte Zahl als Identität — `100000001`. In dem Moment, in dem
ein zweiter Kanal dazukommt, bricht sie an drei Stellen gleichzeitig, und zwar leise:

  1. `allowed_identities` ist eine Menge von Zahlen. Wer auf *irgendeinem* Kanal diese
     Nummer hat, ist damit the operator — Discord-Nutzer 100000001 wäre so berechtigt wie er.
  2. Der Approval-Store hängt an `chat_id`. Ein „ja" in Chat 12345 auf Kanal B löst
     die Freigabe aus, die in Chat 12345 auf Kanal A geparkt wurde.
  3. Der Idempotenz-Schlüssel heißt `tg:update:7`. Update 7 von Kanal B gilt als
     Dublette und wird stillschweigend verworfen.

Dreimal derselbe Fehler: eine Kennung ohne Namensraum. Deshalb steht am Anfang von
Schritt 4 keine neue Integration, sondern eine Regel — **eine Identität ist Kanal +
Kennung, nie eine Kennung allein**. Erst danach ist es sicher, den zweiten Kanal
überhaupt anzuschließen.

Und weil nicht jeder Kanal gleich viel beweist, trägt jeder eine Vertrauensstufe.
Ein Telegram-Update nennt eine numerische ID, die aus dem Konto des Betreibers kommt und die er
nicht selbst schreibt; ein `From:`-Header in einer Mail ist ein Textfeld, das jeder
tippen kann. Beide als „den Betreiber" zu behandeln, wäre die teuerste Zeile im Projekt. Die
Stufe wirkt darum wie der Autonomie-Regler: sie kann nur zumachen, nie aufmachen.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Protocol, runtime_checkable

# Meldeweg für einen Kanal, der beim Abholen fliegt: (Kanalname, Fehler).
ErrorSink = Callable[[str, Exception], None]

# Fällt eine Kennung ohne Kanal an (alte Config, alte Tests), wird sie diesem Kanal
# zugeschlagen. Bewusst explizit und an einer Stelle: eine stillschweigende Annahme
# über Identität gehört nicht verstreut in den Code.
LEGACY_CHANNEL = "telegram"
_SEP = ":"


class Trust(IntEnum):
    """Wie viel ein Kanal über die Identität dahinter beweist. Nur Ordnung, kein Recht.

    Die Stufe *begrenzt*, sie erlaubt nichts. Ein Kanal auf `FULL` darf nicht mehr als
    der Kernel ohnehin zulässt — er darf nur nicht weniger. Das ist dieselbe Bauart wie
    beim Autonomie-Regler (`autonomy.py`): es gibt genau eine Quelle für Erlaubnis, und
    das ist der Kernel. Alles andere darf ausschließlich verschärfen.
    """

    NOTIFY = 0
    """Nur raus. Was hereinkommt, ist kein Auftrag — bestenfalls ein Hinweis."""

    ASK = 1
    """Darf fragen und bekommt Antworten. Wirkt nicht: alles mit Effekt braucht Freigabe,
    und freigeben kann dieser Kanal nicht. Der Regler ist von hier ebenfalls tabu."""

    FULL = 2
    """Identity is cryptographic/account-bound. May do everything the operator may."""


@dataclass(frozen=True)
class Principal:
    """Wer spricht — Kanal *und* Kennung. Ohne Kanal gibt es keine Identität.

    `user_id` ist Text, nicht Zahl: eine Matrix-ID (`@user:server`) oder eine Mailadresse
    ist genauso eine Kennung wie Telegrams `100000001`. Der Typ soll nicht diktieren,
    welche Kanäle je anschließbar sind.
    """

    channel: str
    user_id: str

    def __post_init__(self) -> None:
        if not self.channel or _SEP in self.channel:
            raise ValueError(f"Kanalname leer oder mit '{_SEP}': {self.channel!r}")
        if not self.user_id:
            raise ValueError("Kennung leer")

    def __str__(self) -> str:
        return f"{self.channel}{_SEP}{self.user_id}"

    @classmethod
    def parse(cls, raw: object, *, default_channel: str = LEGACY_CHANNEL) -> "Principal":
        """`telegram:100000001` -> Principal. Eine nackte Kennung landet auf `default_channel`."""
        text = str(raw).strip()
        if not text:
            raise ValueError("empty identity")
        channel, sep, user_id = text.partition(_SEP)
        if not sep:
            return cls(default_channel, channel)
        return cls(channel, user_id)


@dataclass(frozen=True)
class CallbackQuery:
    """Opaque button click delivered by a channel.

    ``data`` is interpreted only after the Conductor has authenticated the
    principal. Adapters never execute the represented action while parsing.
    """

    query_id: str
    data: str
    message_id: int


@dataclass(frozen=True)
class Button:
    label: str
    data: str


@dataclass(frozen=True)
class StructuredMessage:
    """Channel-neutral text plus an optional inline keyboard."""

    text: str
    keyboard: tuple[tuple[Button, ...], ...] = ()
    edit_message_id: int | None = None
    callback_query_id: str | None = None
    callback_notice: str = ""


@dataclass(frozen=True)
class Inbound:
    """Eine eingegangene Nachricht — kanal-unabhängig.

    `conversation` ist der Rückweg *und* der Schlüssel für Freigaben; er trägt den
    Kanal bereits im Namen, damit zwei Kanäle sich nicht denselben Chat teilen.
    `dedup_key` ist aus demselben Grund kanal-qualifiziert.
    """

    principal: Principal
    conversation: str
    text: str
    dedup_key: str
    callback: CallbackQuery | None = None

    @property
    def channel(self) -> str:
        return self.principal.channel


@runtime_checkable
class Activity(Protocol):
    """Optionale, kanal-eigene Fortschrittsanzeige; der Agent kennt den Anbieter nicht."""

    def progress(self, event: Any) -> None: ...

    def succeed(self, footer: str = "") -> None: ...

    def fail(self, error: str) -> None: ...


@runtime_checkable
class Channel(Protocol):
    """Ein Weg hinein und hinaus. Mehr muss ein Kanal nicht können.

    Absichtlich schmal: `poll` liefert fertige `Inbound`-Objekte, `send` nimmt die
    `conversation` zurück, die dort drinstand. Alles Kanal-Eigene (Offsets, Tokens,
    Nutzlast-Formate) bleibt in der Implementierung — der Conductor kennt keinen
    Anbieter mehr, nur noch Nachrichten.
    """

    @property
    def name(self) -> str: ...

    @property
    def trust(self) -> Trust: ...

    def poll(self) -> list[Inbound]: ...

    def send(self, conversation: str, text: str) -> None: ...

    def send_structured(self, conversation: str, message: StructuredMessage) -> None: ...


class ChannelRegistry:
    """Alle angeschlossenen Kanäle. Kennt den Rückweg zu jeder `conversation`.

    Unbekannter Kanal -> Fehler, nicht „irgendwohin schicken". Ein Zustellweg, der bei
    Unsicherheit rät, ist ein Leck: die Nachricht enthält im Zweifel genau das, was
    gerade aus einer geschützten Datei kam.
    """

    def __init__(
        self,
        channels: tuple[Channel, ...] = (),
        *,
        on_error: "ErrorSink | None" = None,
    ) -> None:
        names = [c.name for c in channels]
        if len(names) != len(set(names)):
            raise ValueError(f"Kanalnamen nicht eindeutig: {names}")
        self._channels = {c.name: c for c in channels}
        self._on_error = on_error

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._channels)

    def get(self, name: str) -> Channel:
        channel = self._channels.get(name)
        if channel is None:
            raise KeyError(f"unbekannter Kanal: {name}")
        return channel

    def trust_of(self, name: str) -> Trust:
        """Vertrauensstufe eines Kanals. Unbekannt -> `NOTIFY`, also das Misstrauischste.

        Fail-closed: ein Kanal, den die Registry nicht kennt, hat nichts zu sagen.
        """
        channel = self._channels.get(name)
        return Trust.NOTIFY if channel is None else channel.trust

    def poll_all(self) -> list[Inbound]:
        """Sammelt von allen Kanälen.

        Ein defekter Kanal darf die anderen nicht anhalten — aber nur, wenn der Defekt
        *irgendwo ankommt*. Ohne `on_error` fliegt der Fehler deshalb weiter: ein
        stillschweigend verschluckter Kanal ist schlimmer als ein lauter Abbruch. Er
        sieht aus wie „keine Nachrichten", und genau so würde ein abgeklemmter Weg
        aussehen, über den the operator gerade etwas abbrechen will.
        """
        collected: list[Inbound] = []
        for name, channel in self._channels.items():
            if self._on_error is None:
                collected.extend(channel.poll())
                continue
            try:
                collected.extend(channel.poll())
            except Exception as error:
                self._on_error(name, error)
        return collected

    def send(self, conversation: str, text: str) -> None:
        name, sep, _ = conversation.partition(_SEP)
        if not sep:
            raise ValueError(f"conversation ohne Kanal: {conversation!r}")
        self.get(name).send(conversation, text)

    def begin_activity(self, conversation: str) -> Activity | None:
        """Beginnt UX nur, wenn der gewählte Kanal sie unterstützt.

        Mail/CLI und Testkanäle brauchen keine Telegram-Methoden zu imitieren; fehlende
        Unterstützung bedeutet einfach keine Live-Anzeige, nie einen Zustellfehler.
        """
        name, sep, _ = conversation.partition(_SEP)
        if not sep:
            raise ValueError(f"conversation ohne Kanal: {conversation!r}")
        channel = self.get(name)
        starter = getattr(channel, "begin_activity", None)
        return None if starter is None else starter(conversation)

    def send_file(self, conversation: str, path: str) -> bool:
        """Dateianhang, falls der Kanal das kann. `False` heisst: kann er nicht.

        Gleiche Bauart wie `begin_activity`: fehlende Unterstützung ist kein
        Zustellfehler, sondern ein ehrliches „nein", das der Aufrufer dem Betreiber
        weitersagt. Ein Kanal ohne `send_file` muss nichts imitieren.
        """
        name, sep, _ = conversation.partition(_SEP)
        if not sep:
            raise ValueError(f"conversation ohne Kanal: {conversation!r}")
        sender = getattr(self.get(name), "send_file", None)
        if sender is None:
            return False
        sender(conversation, path)
        return True

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        """Nutzt Kanal-UI, falls vorhanden; sonst wird der ehrliche Text-Fallback gesendet."""
        name, sep, _ = conversation.partition(_SEP)
        if not sep:
            raise ValueError(f"conversation ohne Kanal: {conversation!r}")
        channel = self.get(name)
        rich = getattr(channel, "send_structured", None)
        if rich is None:
            channel.send(conversation, message.text)
        else:
            rich(conversation, message)


def trust_ceiling(trust: Trust) -> str:
    """Kurztext für `/whoami` und `/policy` — was dieser Kanal höchstens darf."""
    if trust is Trust.FULL:
        return "voll (darf freigeben und den Regler stellen)"
    if trust is Trust.ASK:
        return "fragen (Antworten ja, Wirkung nein, keine Freigabe, kein Regler)"
    return "nur Zustellung (eingehend ist kein Auftrag)"
