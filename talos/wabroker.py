"""WhatsApp ueber den eigenen Broker — derselbe Kanalname, ein anderer Beweisweg.

Dieser Kanal traegt denselben Namen wie die Cloud-API-Variante in `whatsapp.py`
(`"whatsapp"`). Beide werden NIE gleichzeitig registriert — die Registry verlangt
eindeutige Namen, und `__main__.run` entscheidet: ist der Broker konfiguriert,
gewinnt er, weil er der einzige der beiden Wege ist, der auch hereinholt.

Warum diese Variante `Trust.FULL` tragen darf, waehrend die Cloud-API-Variante bei
`NOTIFY` bleiben muss — obwohl beide „WhatsApp" heissen:

Die Cloud API kennt fuer Eingehendes nur Webhooks: eine oeffentliche HTTPS-Adresse,
die von aussen erreichbar sein muss (Begruendung im Modul-Docstring von
`whatsapp.py`). Dieser Broker dreht die Richtung nicht um. Der Listener auf dem
eigenen VPS schreibt Talos-adressierte Nachrichten in eine JSONL-Queue; Talos HOLT
sie per SSH ab — jede Verbindung geht von Talos nach draussen, zu einer Maschine,
die der Betreiber selbst kontrolliert, mit seinem eigenen Schluessel. Es gibt keinen
lauschenden Socket, keinen Webhook, keine oeffentlich erreichbare Tuer. Die
Nur-ausgehend-Haltung haelt — genau wie beim Telegram-Long-Poll und beim IMAP-Abruf
des Mail-Kanals.

Der Identitaetsbeweis ist derselbe wie bei Telegram: die Absendernummer kommt aus dem
WhatsApp-Konto des Betreibers, nicht aus einem Textfeld, das der Absender selbst
tippt (anders als ein `From:`-Kopf in einer Mail). Wer die Queue faellt, kontrolliert
bereits den Broker — der Rueckweg ist nicht weicher als die erste Tuer. Deshalb
`FULL`, und deshalb als `property` ohne Setter: nicht zu heben, nicht zu senken,
auch nicht versehentlich.

Format einer Queue-Zeile (Vertrag mit dem Broker, `text` bereits prefix-bereinigt):

    {"at":"…","atMs":…,"messageId":"ABCD1234",
     "chatJid":"41786676731@s.whatsapp.net","senderNumber":"41786676731",
     "pushName":"Ali","text":"status der agenten"}
"""
from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from .channel import Inbound, Principal, StructuredMessage, Trust
from .whatsapp import CHANNEL_NAME, number_of

# Konfigurationsschluessel — hier definiert, damit `config.py` sie importiert statt
# sie zu wiederholen (dieselbe Regel wie ENV_TOKEN in `whatsapp.py`).
ENV_SSH = "TALOS_WA_BROKER_SSH"          # SSH-Ziel (Alias aus ~/.ssh/config); leer = Kanal aus
ENV_QUEUE = "TALOS_WA_BROKER_QUEUE"      # Pfad der JSONL-Queue auf dem VPS
ENV_CLI_DIR = "TALOS_WA_BROKER_CLI_DIR"  # Verzeichnis des whatsapp-cli auf dem VPS

DEFAULT_SSH_TARGET = "hermes"
DEFAULT_QUEUE_PATH = "/var/lib/wa-broker/talos-queue.jsonl"
DEFAULT_CLI_DIR = "/opt/wa-broker"

DEFAULT_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 15

# Poll-Obergrenzen. 64 Nachrichten pro Zug reichen fuer einen Burst, der Rest kommt
# im naechsten Zyklus nach — der Cursor rueckt nur so weit vor, wie wirklich gelesen
# wurde, also geht nichts verloren. 256 KB begrenzen, was ein einzelner SSH-Ruf an
# Speicher und Parsezeit kostet.
POLL_MAX_ENTRIES = 64
POLL_MAX_BYTES = 256 * 1024

# Die Cloud API nimmt 4096 Zeichen; der Broker haengt am selben WhatsApp, also gilt
# hier dieselbe Groessenordnung mit etwas Reserve nach unten.
TEXT_LIMIT = 4000

CURSOR_FILENAME = "wa-broker-cursor.json"

_DIGITS = re.compile("[^0-9]")

_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
_MIME_BY_EXTENSION = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "csv": "text/csv",
    "txt": "text/plain",
    "zip": "application/zip",
}

# Fehlertexte sind fuer Menschen: 240 Zeichen reichen, um zu verstehen was los ist.
_MAX_DETAIL_CHARS = 240

__all__ = [
    "CURSOR_FILENAME",
    "DEFAULT_CLI_DIR",
    "DEFAULT_QUEUE_PATH",
    "DEFAULT_SSH_TARGET",
    "DEFAULT_TIMEOUT_S",
    "ENV_CLI_DIR",
    "ENV_QUEUE",
    "ENV_SSH",
    "POLL_MAX_BYTES",
    "POLL_MAX_ENTRIES",
    "TEXT_LIMIT",
    "BrokerError",
    "BrokerWhatsAppChannel",
    "CursorStore",
    "JsonCursorStore",
    "Runner",
    "split_text",
]


class BrokerError(RuntimeError):
    """Broker-Zugriff oder Zustellung fehlgeschlagen — laut und behandelbar.

    Bewusst eine Ausnahme und kein `False` (dieselbe Regel wie `WhatsAppError`):
    `ChannelRegistry.poll_all` meldet ihn als `channel.error`, und ein still
    verschluckter Ausfall saehe aus wie „keine Nachrichten" — genau so saehe aber
    auch ein abgeklemmter Weg aus, ueber den the operator gerade etwas abbrechen will.
    """


@runtime_checkable
class Runner(Protocol):
    """Der GESAMTE Subprozess-Vertrag dieses Kanals: ein Kommando, ein Ergebnis.

    So schmal wie moeglich, damit Tests ohne SSH und ohne `subprocess`-Doppel
    auskommen — und damit durch diese Tuer nichts anderes passt als genau das:
    Kommando rein, (rc, stdout, stderr) raus.
    """

    def __call__(self, cmd: list[str]) -> tuple[int, bytes, bytes]: ...


class _SubprocessRunner:
    """Vorgabe-Transport: `subprocess` mit einer Deadline pro Aufruf.

    Ein `ssh`, der haengt, darf den Poll-Loop nicht mit sich ziehen — der Timeout
    wird zum Kanalfehler, nicht zum Stillstand. In die Ausnahme kommt nur der
    Programmname, nie das Kommando: das kann die base64-Nutzlast enthalten.
    """

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = float(timeout_s)

    def __call__(self, cmd: list[str]) -> tuple[int, bytes, bytes]:
        try:
            done = subprocess.run(cmd, capture_output=True, timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            raise BrokerError(
                f"broker command timed out after {self._timeout_s:.0f}s: {cmd[0]}"
            ) from None
        except OSError as error:
            raise BrokerError(f"broker command failed to start: {cmd[0]}: {error}") from None
        return done.returncode, done.stdout, done.stderr


@runtime_checkable
class CursorStore(Protocol):
    """Wo der Lese-Stand liegt. `None` heisst: dieser Kanal lief noch nie."""

    def load(self) -> int | None: ...

    def save(self, offset: int) -> None: ...


class JsonCursorStore:
    """Byte-Offset in der fernen Queue-Datei, als kleine JSON-Datei im Talos-Datenverzeichnis.

    Eine unlesbare oder halb geschriebene Datei gilt als „noch nie gelaufen" — das
    ist die sichere Richtung: der Kanal springt ans Dateiende statt Backlog
    nachzuholen, und der naechste erfolgreiche Poll schreibt einen sauberen Stand.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> int | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            offset = int(data["cursor"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return offset if offset >= 0 else None

    def save(self, offset: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"cursor": int(offset)}), encoding="utf-8")


def _default_cursor_path() -> Path:
    # Spaet importiert: `config` liest beim Import Prozess-Env, und dieser Kanal soll
    # auch ladbar bleiben, wenn gerade keine Konfiguration existiert (Tests, doctor).
    from .config import DATA_DIR

    return DATA_DIR / CURSOR_FILENAME


def split_text(text: str, limit: int = TEXT_LIMIT) -> tuple[str, ...]:
    """Teilt an Absatz-, dann Zeilen-, dann Wortgrenzen. Nichts geht verloren.

    Nach `split_for_telegram` gebaut und bewusst lokal gehalten (dieser Kanal
    nummeriert die Teile nicht — WhatsApp zeigt sie als eigene Nachrichten, eine
    Folge ist dort lesbar). Ein einzelnes ueberlanges Wort wird hart geschnitten,
    weil es keine Grenze hat: zustellen schlaegt schweigen.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    rest = str(text or "").strip()
    parts: list[str] = []
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for separator in ("\n\n", "\n", " "):
            candidate = window.rfind(separator)
            # Die erste Grenze, die nicht im letzten Viertel des Fensters verhungert,
            # damit ein Teil nicht fast leer bleibt.
            if candidate > limit // 4:
                cut = candidate + (len(separator) if separator != " " else 1)
                break
        if cut <= 0:
            cut = limit  # unteilbar — hart schneiden ist besser als gar nicht zustellen
        chunk = rest[:cut].rstrip()
        if chunk:
            parts.append(chunk)
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return tuple(parts)


def _to_inbound(raw: bytes) -> Inbound | None:
    """Eine Queue-Zeile -> `Inbound`. Alles Unvollstaendige faellt still heraus.

    „Still" ist hier richtig: eine kaputte Zeile ist kein Kanalfehler, sondern Daten,
    die der Broker nie vollstaendig geschrieben hat — und der Cursor ist ohnehin
    darueber hinweg, ein Fehler danach koennte nichts mehr reparieren.
    """
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    message_id = str(entry.get("messageId") or "").strip()
    text = str(entry.get("text") or "").strip()
    if not message_id or not text:
        return None
    # senderNumber ist eine nackte Nummer; chatJid traegt das „@s.whatsapp.net" noch
    # mit. Auf Ziffern reduziert sind beide dieselbe Kennung — Schreibweise, nicht Nummer.
    sender = _DIGITS.sub("", str(entry.get("senderNumber") or entry.get("chatJid") or ""))
    if not sender:
        return None
    return Inbound(
        principal=Principal(CHANNEL_NAME, sender),
        conversation=f"{CHANNEL_NAME}:{sender}",
        text=text,
        dedup_key=f"{CHANNEL_NAME}:msg:{message_id}",
    )


def _short(value: bytes | str) -> str:
    """stderr auf eine lesbare Laenge. Kein Geheimnis hier — aber ein Log hat ein Mass."""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return " ".join(text.split())[:_MAX_DETAIL_CHARS]


class BrokerWhatsAppChannel:
    """`Channel`-Implementierung: holt per SSH ab, sendet per SSH. Kein Socket.

    Der Cursor (Byte-Offset in der fernen Queue) gehoert diesem Kanal und wird nur
    nach einem ERFOLGREICHEN Abruf geschrieben: ein Poll-Fehler rueckt ihn nicht vor,
    der naechste Poll liest dieselben Bytes noch einmal.
    """

    name = CHANNEL_NAME

    def __init__(
        self,
        ssh_target: str = DEFAULT_SSH_TARGET,
        queue_path: str = DEFAULT_QUEUE_PATH,
        cli_dir: str = DEFAULT_CLI_DIR,
        *,
        runner: Runner | None = None,
        cursor_store: CursorStore | None = None,
        cursor_path: Path | str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        text_limit: int = TEXT_LIMIT,
    ) -> None:
        self._target = str(ssh_target).strip() or DEFAULT_SSH_TARGET
        if not str(queue_path).strip():
            raise ValueError(f"{ENV_QUEUE} is empty")
        if not str(cli_dir).strip():
            raise ValueError(f"{ENV_CLI_DIR} is empty")
        self._queue = str(queue_path).strip()
        self._cli_dir = str(cli_dir).strip()
        self._runner: Runner = runner if runner is not None else _SubprocessRunner(timeout_s)
        if cursor_store is None:
            cursor_store = JsonCursorStore(
                cursor_path if cursor_path is not None else _default_cursor_path()
            )
        self._cursor = cursor_store
        self._text_limit = max(1, int(text_limit))

    def __repr__(self) -> str:
        return f"BrokerWhatsAppChannel(ssh_target={self._target!r})"

    @property
    def trust(self) -> Trust:
        """Immer `FULL` — ohne Setter. Begruendung im Modul-Docstring."""
        return Trust.FULL

    def poll(self) -> list[Inbound]:
        """Holt neue Queue-Zeilen ab. Der erste Lauf ueberhaupt holt NICHTS.

        Ohne Cursor-Datei gibt es keinen belegten Stand — die Queue auf eine frische
        Installation nachzuholen hiesse, jede jemals an Talos gerichtete Nachricht
        noch einmal als Auftrag zu stellen. Stattdessen springt der Cursor ans
        Dateiende; nur was danach ankommt, gilt.
        """
        cursor = self._cursor.load()
        if cursor is None:
            out = self._ssh(f"stat -c %s {shlex.quote(self._queue)}")
            try:
                size = int(out.strip())
            except ValueError:
                raise BrokerError(
                    f"broker queue size unreadable: {_short(out)!r}"
                ) from None
            self._cursor.save(size)
            return []
        data = self._ssh(f"tail -c +{cursor + 1} {shlex.quote(self._queue)}")
        if not data:
            return []
        inbounds, consumed = self._parse(data)
        if consumed:
            self._cursor.save(cursor + consumed)
        return inbounds

    def send(self, conversation: str, text: str) -> None:
        """Stellt zu — notfalls in mehreren Teilen, aber vollstaendig.

        Die Nummer wird geprueft, BEVOR irgendein Subprozess startet: eine halb
        erkannte conversation ist eine Nachricht an jemand anderen.
        """
        number = number_of(conversation)
        for part in split_text(text, self._text_limit):
            self._deliver(number, part)

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        """Text plus Knopf-Beschriftungen als Textzeilen. Keine echten Knoepfe.

        send.js kennt kein Inline-Keyboard. Die Beschriftungen gehen mit, damit eine
        Freigabe-Frage lesbar bleibt — geantwortet wird wie immer mit Text.
        """
        lines = [message.text]
        for row in message.keyboard:
            for button in row:
                lines.append(f"[{button.label}]")
        self.send(conversation, "\n".join(lines))

    def send_file(self, conversation: str, path: str) -> bool:
        """Ein Anhang in zwei Spruengen: `scp` auf den VPS, dann send.js von dort.

        Der Broker erreicht nur Dateien auf seiner eigenen Maschine. Bilder gehen als
        `--image`, alles andere als `--document` mit Mime aus der Endung (der Vertrag
        des Brokers — er ist aelter als die byte-basierte Erkennung in `telegram.py`
        und bleibt wie deployed). Fehler fliegen wie beim Text: laut.
        """
        number = number_of(conversation)
        local = Path(path)
        if not local.is_file():
            # VOR jedem Subprozess: ein fehlender Pfad ist ein Aufrufer-Fehler, kein
            # Broker-Fehler, und darf nicht erst nach dem scp auffallen.
            raise BrokerError(f"broker send_file: not a file: {path!r}")
        safe_name = re.sub(r"[ /]+", "_", local.name).strip("_") or "file"
        remote = f"/tmp/wa_{int(time.time())}_{safe_name}"
        rc, _, stderr = self._run(
            [
                "scp",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={CONNECT_TIMEOUT_S}",
                str(local),
                f"{self._target}:{remote}",
            ]
        )
        if rc != 0:
            raise BrokerError(f"broker upload failed (rc {rc}): {_short(stderr)}")
        extension = local.suffix.lower().lstrip(".")
        if extension in _IMAGE_EXTENSIONS:
            attachment = f"--image {shlex.quote(remote)}"
        else:
            mime = _MIME_BY_EXTENSION.get(extension, "application/octet-stream")
            attachment = (
                f"--document {shlex.quote(remote)}"
                f" --filename {shlex.quote(local.name)}"
                f" --mimetype {shlex.quote(mime)}"
            )
        rc, _, stderr = self._run_ssh(
            f"cd {shlex.quote(self._cli_dir)}"
            f" && node scripts/send.js --to {shlex.quote(number)} {attachment}"
        )
        if rc != 0:
            raise BrokerError(f"broker send_file failed (rc {rc}): {_short(stderr)}")
        return True

    # ------------------------------------------------------------------ intern
    def _deliver(self, number: str, text: str) -> None:
        # Der Text reist base64-kodiert: so ueberlebt er Quoting, Newlines und
        # Sonderzeichen, ohne dass eine Shell ihn je als Syntax sieht. In Fehler
        # gehoert er trotzdem nicht — die Meldung unten nennt rc und stderr.
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        rc, _, stderr = self._run_ssh(
            f"cd {shlex.quote(self._cli_dir)}"
            f" && T=$(echo '{encoded}' | base64 -d)"
            f" && node scripts/send.js --to {shlex.quote(number)} --text \"$T\""
        )
        if rc != 0:
            raise BrokerError(f"broker send failed (rc {rc}): {_short(stderr)}")

    def _run(self, cmd: list[str]) -> tuple[int, bytes, bytes]:
        try:
            return self._runner(cmd)
        except BrokerError:
            raise
        except Exception as error:
            raise BrokerError(f"broker command failed: {cmd[0]}: {_short(str(error))}") from None

    def _run_ssh(self, remote_command: str) -> tuple[int, bytes, bytes]:
        return self._run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={CONNECT_TIMEOUT_S}",
                self._target,
                remote_command,
            ]
        )

    def _ssh(self, remote_command: str) -> bytes:
        """Ein SSH-Ruf mit geprueftem rc. Scheitert er, ruehrt sich der Cursor nicht."""
        rc, out, stderr = self._run_ssh(remote_command)
        if rc != 0:
            raise BrokerError(f"broker ssh failed (rc {rc}): {_short(stderr)}")
        return out

    @staticmethod
    def _parse(data: bytes) -> tuple[list[Inbound], int]:
        """JSONL -> Inbounds plus verbrauchte Bytes. Die Obergrenzen stehen oben.

        Zurueckgegeben wird, wie viele Bytes der Cursor vorruecken darf — das ist
        weniger als `len(data)`, wenn eine Obergrenze mitten im Strom stoppt; der
        Rest wird im naechsten Poll erneut gelesen. Eine einzelne Zeile jenseits des
        Byte-Limits wird verbraucht und uebersprungen: sonst klemmte der Cursor fuer
        immer vor derselben Giftzeile.
        """
        inbounds: list[Inbound] = []
        consumed = 0
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b"\n"):
                # Der Broker haengt zeilenweise an: eine letzte Zeile ohne Newline
                # kann ein halb geschriebener Append sein. Sie wird NICHT verbraucht
                # — der naechste Poll liest sie komplett, statt eine Nachricht als
                # „kaputte Zeile" fuer immer zu verlieren.
                break
            if len(inbounds) >= POLL_MAX_ENTRIES:
                break
            if consumed + len(raw) > POLL_MAX_BYTES:
                if consumed == 0:
                    consumed = len(raw)
                break
            consumed += len(raw)
            inbound = _to_inbound(raw)
            if inbound is not None:
                inbounds.append(inbound)
        return inbounds, consumed
