"""Erstlauf-Assistent: Token, Identität und Denkweg einsammeln, ohne dass jemand rät.

Warum es ihn gibt: bei diesen Werten bleibt ein Tippfehler still. Ein falscher Token
meldet sich erst beim ersten Poll; eine falsche Kennung erst dann, wenn der Agent den
Betreiber nicht wiedererkennt — und eine *fremde* Kennung meldet sich nie, sie gibt nur
jemand anderem das Kommando. Ein fehlender Modellzugang wiederum lässt eine frische
Installation gar nicht erst laufen: die vorhandenen Reasoner setzen eine lokal
eingerichtete `claude`- oder `hermes`-CLI voraus, die ein fremder Nutzer nicht hat.

Deshalb wird hier nichts eingetippt, was sich beweisen lässt: der Token geht gegen
`getMe`, die Identität kommt aus einer echten Nachricht des Betreibers an seinen
eigenen Bot (`getUpdates`) statt aus einer abgetippten Zahl, und der API-Schlüssel geht
gegen das Modellverzeichnis des Anbieters. Getippt wird nur im Rückfallweg — und dann
sichtbar als der schlechtere Weg.

Bewusst NICHT gebaut: kein Vorgabe-Principal, keine leere Allowlist, kein Start.
Der Assistent schreibt eine Datei und hört auf; der Schalter bleibt beim Menschen,
genau wie bei `site/install.sh`, das ebenfalls nichts startet.

Alles Ein- und Ausgehende ist injizierbar (`stdin`, `stdout`, `http`, `runtimes`), damit
der Ablauf ohne Terminal, ohne Netz und ohne fremde Installation prüfbar ist.

Sprache: Die Ausgabe ist Englisch wie der Installer und die übrige Maschinen-
Konsole (`/help`, `/policy`, Kernel-Gründe); Kommentare bleiben Deutsch.
"""
from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import requests

from . import config
from .channel import LEGACY_CHANNEL, Principal
from .credentials import base_url_var
from .ux import SYM_FAIL, SYM_OK, SYM_TALOS

API_URL = "https://api.telegram.org/bot{token}/{method}"
API_TIMEOUT_S = 30.0
# Long-Poll-Häppchen. Kürzer als das Gesamtlimit, damit die Zeitüberschreitung
# nicht erst nach einem vollen Poll greift.
POLL_CHUNK_S = 20
DEFAULT_WAIT_S = 120.0
# Nach so vielen unbrauchbaren Eingaben ist nicht der Nutzer das Problem, sondern
# die Situation (falsches Fenster, kopierter Müll). Dann lieber sauber abbrechen.
MAX_ATTEMPTS = 5

# `123456789:AA…` — Bot-ID, Doppelpunkt, Geheimnis. Nur eine Vorprüfung; das Urteil
# fällt Telegram selbst, denn ein formgültiger Token kann längst widerrufen sein.
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
USER_ID_RE = re.compile(r"^\d+$")
# `requests` zitiert in Fehlern gern die URL — und die trägt den Token.
URL_TOKEN_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
# Die verbreitete Schlüsselform (`sk-ant-…`, `sk-proj-…`, `sk-…`). Deckt die üblichen
# Anbieter ab; wer eine eigene Form hat, wird über `_Console.hide` erfasst.
API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}")
# Kürzer darf kein Wert sein, den wir global schwärzen: ein zu kurzes „Geheimnis" käme
# in harmlosen Wörtern vor und machte die Ausgabe unlesbar.
MIN_SECRET_LEN = 12
# Ein Modellname landet als `KEY=VALUE` in einer Datei — Leerzeichen wären dort nicht
# mehr eindeutig lesbar.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:@/+-]{1,64}$")

YES_WORDS = frozenset({"y", "yes", "j", "ja"})
NO_WORDS = frozenset({"n", "no", "nein"})

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_NO_TERMINAL = 2
EXIT_USAGE = 3

TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
USERNAME_KEY = "TELEGRAM_BOT_USERNAME"
PRINCIPALS_KEY = "TALOS_ALLOWED_PRINCIPALS"
PROVIDER_KEY = "TALOS_MODEL_PROVIDER"
MODEL_KEY = "TALOS_MODEL"
ANTHROPIC_KEY = "ANTHROPIC_API_KEY"
OPENAI_KEY = "OPENAI_API_KEY"
# ⚠️ Anbietergebunden, nicht eine Adresse fuer alle. Bis 05.08. hiess sie
# `TALOS_API_BASE_URL` und galt fuer JEDEN Anbieter — wer sie fuer den einen setzte und
# auf den anderen wechselte, sprach mit der falschen Maschine. Siehe `credentials.py`.
OPENAI_BASE_URL_KEY = base_url_var("openai-api")
WRITTEN_KEYS = (
    TOKEN_KEY, USERNAME_KEY, PRINCIPALS_KEY,
    PROVIDER_KEY, MODEL_KEY, ANTHROPIC_KEY, OPENAI_KEY, OPENAI_BASE_URL_KEY,
    "TALOS_CLAUDE_BIN", "TALOS_HERMES_BIN",
)
# Werte, die nie unmaskiert auf den Bildschirm dürfen — nicht einmal beim Rückblick
# auf eine schon bestehende Datei.
SECRET_KEYS = frozenset({TOKEN_KEY, ANTHROPIC_KEY, OPENAI_KEY, "TALOS_MAIL_PASSWORD"})

# Abschnitte, die einzeln nachgezogen werden koennen — wie `hermes setup <section>`.
# Warum ueberhaupt: nach dem ersten Lauf will niemand die Kennung neu beweisen, nur weil
# er das Modell wechselt. Jeder Lauf aendert nur die ausgewaehlten Schluessel
# in einem atomaren Austausch und laesst andere Einstellungen stehen.
SECTION_IDENTITY, SECTION_MODEL, SECTION_MAIL = "identity", "model", "mail"
SECTION_TERMINAL = "terminal"
SECTIONS = (SECTION_TERMINAL, SECTION_IDENTITY, SECTION_MODEL, SECTION_MAIL)

MAIL_HOST_KEY = "TALOS_MAIL_HOST"
MAIL_USER_KEY = "TALOS_MAIL_USER"
MAIL_PASSWORD_KEY = "TALOS_MAIL_PASSWORD"
MAIL_SMTP_KEY = "TALOS_MAIL_SMTP_HOST"
MAIL_AUTHSERV_KEY = "TALOS_MAIL_AUTHSERV_ID"

ROUTE_ANTHROPIC = "anthropic-api"
ROUTE_OPENAI = "openai-api"
ROUTE_CLAUDE_CLI = "claude-cli"
ROUTE_HERMES = "hermes"

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# Aktuelle Modell-IDs, übernommen aus der `claude-api`-Skill (Stand 2026-08) statt
# aus dem Gedächtnis geraten: eine erfundene ID scheitert erst beim ersten Zug, und
# dann sieht es aus wie ein kaputter Agent statt wie ein Tippfehler im Assistenten.
ANTHROPIC_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
    "claude-opus-4-8",
    "claude-fable-5-1",
)
# Für OpenAI-kompatible Anbieter gibt es keine kuratierbare Liste — jeder Anbieter
# hinter einer eigenen `base_url` hat eigene Namen. Vorgeschlagen wird deshalb das,
# was Talos ohnehin als Vorgabe trägt; alles andere ist Freitext.
OPENAI_MODELS = (config.DEFAULT_MODEL,)

# 401 heisst „gilt nicht", 403 „gilt, darf aber nicht" — zwei verschiedene Ratschläge.
# Alles andere ist Transport und wird ausdrücklich NICHT dem Schlüssel angelastet.
KEY_REJECTED = {
    401: "The provider rejected this key (401). It is not valid — check your dashboard.",
    403: "This key is valid but not allowed to use the API (403) — check its permissions.",
}

FILE_HEADER = (
    "# Written by `python -m talos setup`.\n"
    "# Mode 600 on purpose: the token below is a password.\n"
)

USAGE = (
    "usage: python -m talos setup [terminal|identity|model|mail] [--out PATH] [--wait SECONDS]\n"
    "  terminal      model + this terminal identity; no Telegram account needed\n"
    "  (no section)  Telegram first run; preserves unrelated settings\n"
    "  identity      the bot token and who may command it\n"
    "  model         what the agent thinks with\n"
    "  mail          the second way in (IMAP), optional\n"
    "  --out         where to write the configuration (default: the path Talos reads)\n"
    "  --wait        how long to wait for your first message, in seconds\n"
    "  Next: talos chat (terminal), or python -m talos (Telegram service)"
)

__all__ = [
    "run_setup",
    "mask_token",
    "detect_runtimes",
    "Http",
    "HttpResponse",
    "HttpError",
    "LocalRuntimes",
    "ModelSetup",
]


# --------------------------------------------------------------------- Transport
class HttpError(RuntimeError):
    """Der Aufruf kam nicht durch. Ausdrücklich NICHT dasselbe wie ein abgelehnter Token."""


class _TokenRejected(Exception):
    """Die Gegenstelle weist das Geheimnis ab (401/403) — es gilt nicht.

    Ausdrücklich verschieden von `HttpError`: hier ist der Wert falsch, dort kam der
    Aufruf nicht durch. Wer beides gleich nennt, schickt Leute dazu, einen
    funktionierenden Schlüssel neu auszustellen. Für den Bot-Token bleibt der Grund
    leer (der Aufrufer formuliert ihn), für Anbieter-Schlüssel steht er in `str()`.
    """


class _Aborted(Exception):
    """Sauberer Abbruch mit Begründung. Es wurde nichts geschrieben."""


class _Usage(Exception):
    """Aufrufparameter unbrauchbar."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: dict[str, Any]


class Http(Protocol):
    """Die eine Netz-Oberfläche des Assistenten — schmal genug, um sie zu fälschen.

    `headers` kam mit dem Modell-Schritt dazu: Telegram trägt sein Geheimnis in der
    URL, jede Anbieter-API trägt es im Kopf. Ein zweiter Transport nur dafür wäre
    eine zweite Stelle, an der die Maskierung vergessen werden könnte.
    """

    def get(
        self,
        url: str,
        params: dict[str, Any],
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse: ...


class RequestsHttp:
    """Dünner Mantel um `requests`. Jeder Transportfehler wird zu `HttpError`.

    Die Fehlermeldung wird gesäubert, bevor sie den Aufrufer erreicht: `requests`
    hängt die angefragte URL an, und in der steht der Token im Klartext.
    """

    def get(
        self,
        url: str,
        params: dict[str, Any],
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        try:
            response = requests.get(url, params=params, timeout=timeout, headers=headers or {})
        except requests.RequestException as error:
            raise HttpError(scrub(str(error))) from None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return HttpResponse(response.status_code, payload if isinstance(payload, dict) else {})


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Entfernt Geheimnisse aus beliebigem Text: Bot-Token, API-Schlüssel, Bekanntes.

    Zwei Ebenen mit Absicht. Die Muster fangen die verbreiteten Formen — den in einer
    URL mitgeschleppten Bot-Token und `sk-…`-Schlüssel — auch dann, wenn niemand den
    konkreten Wert kennt. `secrets` fängt den Rest: sobald ein Schlüssel einmal
    eingegeben wurde, wird genau er geschwärzt, egal welche Form der Anbieter benutzt.
    """
    cleaned = URL_TOKEN_RE.sub("/bot[REDACTED]", text)
    cleaned = API_KEY_RE.sub("[REDACTED]", cleaned)
    for secret in secrets:
        if len(secret) >= MIN_SECRET_LEN:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def mask_token(token: str) -> str:
    """Erste 6 und letzte 4 Zeichen. Kurze Werte werden ganz verschwiegen.

    Die ersten sechs sind die Bot-ID (öffentlich), die letzten vier reichen zum
    Wiedererkennen. Dazwischen bleibt das Geheimnis geheim — auch auf einem
    Bildschirm, über den gerade jemand hinwegschaut oder der aufgezeichnet wird.
    """
    text = token.strip()
    if len(text) <= 12:
        return "…" if text else ""
    return f"{text[:6]}…{text[-4:]}"


# ------------------------------------------------------------------------- Werte
@dataclass(frozen=True)
class Bot:
    token: str
    name: str
    username: str


@dataclass(frozen=True)
class Sender:
    user_id: int
    first_name: str
    username: str

    def label(self) -> str:
        handle = f" (@{self.username})" if self.username else ""
        return f"{self.first_name or 'someone'}{handle}, id {self.user_id}"


@dataclass(frozen=True)
class Options:
    out: Path
    wait_s: float
    section: str = ""


@dataclass(frozen=True)
class LocalRuntimes:
    """Was auf DIESEM Rechner schon eingerichtet ist. Leerer Pfad = nicht gefunden."""

    claude: str = ""
    hermes: str = ""


@dataclass(frozen=True)
class ModelSetup:
    """Womit der Agent denkt. Leerer `key_name` heisst: es wird kein Schlüssel geschrieben."""

    provider: str
    model: str
    key_name: str = ""
    key: str = ""
    base_url: str = ""
    binary_name: str = ""
    binary: str = ""

    def values(self) -> tuple[tuple[str, str], ...]:
        values = [(PROVIDER_KEY, self.provider), (MODEL_KEY, self.model)]
        if self.key_name and self.key:
            values.append((self.key_name, self.key))
        if self.base_url:
            values.append((OPENAI_BASE_URL_KEY, self.base_url))
        if self.binary_name and self.binary:
            values.append((self.binary_name, self.binary))
        return tuple(values)


@dataclass(frozen=True)
class _Route:
    key: str
    label: str
    note: str = ""


# ----------------------------------------------------------------------- Konsole
class _Console:
    """Ein- und Ausgabe an genau einer Stelle — deshalb ist der Ablauf testbar.

    `say` säubert jede Zeile: der Assistent zeigt Fehlermeldungen aus dem Netz an,
    und eine davon trägt im Rohzustand den Token. Eine einzige vergessene
    Maskierung an einer beliebigen Aufrufstelle wäre sonst das Leck.
    """

    def __init__(self, stdin: Any, stdout: Any) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._secrets: tuple[str, ...] = ()

    def hide(self, secret: str) -> None:
        """Lernt ein Geheimnis dazu, das ab jetzt in JEDER Zeile geschwärzt wird.

        Die Muster in `scrub` kennen nur die üblichen Formen. Ein Anbieter mit eigener
        Schlüsselform würde durchrutschen — und zwar genau dort, wo es weh tut: in
        einer zitierten Fehlermeldung aus dem Netz. Deshalb merkt sich die Konsole den
        echten Wert, sobald er einmal eingegeben wurde, und zwar *bevor* damit
        irgendetwas versucht wird.
        """
        text = secret.strip()
        if len(text) >= MIN_SECRET_LEN and text not in self._secrets:
            self._secrets = (*self._secrets, text)

    @property
    def interactive(self) -> bool:
        """Fail-closed: was kein `isatty` hat oder dabei fliegt, gilt als nicht interaktiv."""
        isatty = getattr(self._stdin, "isatty", None)
        if isatty is None:
            return False
        try:
            return bool(isatty())
        except Exception:
            return False

    def say(self, text: str = "") -> None:
        self._write(f"{text}\n")

    def ask(self, prompt: str) -> str:
        self._write(prompt)
        line = self._stdin.readline()
        if line == "":
            raise _Aborted("input ended — nothing was written.")
        return line.strip()

    def ask_secret(self, prompt: str) -> str:
        """Wie `ask`, aber ohne Echo, sobald wirklich ein Terminal dranhängt.

        Ein Schlüssel, der beim Tippen auf dem Bildschirm steht, steht auch im
        Scrollback, in der Bildschirmaufnahme und über der Schulter. Hängt kein echtes
        Terminal dran (Test, Pipe), bleibt es beim normalen Lesen — sonst wäre genau
        der Zweig, der Geheimnisse anfasst, der einzige ungeprüfte im ganzen Ablauf.
        """
        if self._stdin is sys.stdin and self.interactive:
            try:
                return getpass.getpass(scrub(prompt, self._secrets)).strip()
            except EOFError:
                raise _Aborted("input ended — nothing was written.") from None
        return self.ask(prompt)

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        """Ja/Nein mit sicherer Vorgabe. `ja` gilt neben `yes` — additiv, nie ersetzend."""
        suffix = "[Y/n]" if default else "[y/N]"
        for _ in range(MAX_ATTEMPTS):
            answer = self.ask(f"{prompt} {suffix} ").lower()
            if not answer:
                return default
            if answer in YES_WORDS:
                return True
            if answer in NO_WORDS:
                return False
            self.say("    please answer yes or no.")
        return default

    def _write(self, text: str) -> None:
        from .terminalui import paint

        text = scrub(text, self._secrets)
        if text.lstrip().startswith(SYM_FAIL):
            text = paint(text, "fail", out=self._stdout)
        elif text.lstrip().startswith(SYM_OK):
            text = paint(text, "ok", out=self._stdout)
        elif text.rstrip().endswith(("]", "?", ":")):
            text = paint(text, out=self._stdout)
        self._stdout.write(text)
        flush = getattr(self._stdout, "flush", None)
        if flush is not None:
            flush()


# ------------------------------------------------------------------- Bot-API
def _call(
    http: Http,
    token: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = API_TIMEOUT_S,
) -> dict:
    """Ein Bot-API-Aufruf. 401 heisst „Token ungültig", alles andere ist Transport.

    Die Unterscheidung ist der ganze Zweck dieser Funktion: „Telegram sagt nein" und
    „ich komme nicht ans Netz" führen zu völlig verschiedenen Ratschlägen. Ein
    Assistent, der beides „ungültiger Token" nennt, schickt Leute zum falschen Fehler
    — und im schlimmsten Fall dazu, einen funktionierenden Token neu auszustellen.
    """
    response = http.get(API_URL.format(token=token, method=method), params, timeout)
    if response.status == 401:
        raise _TokenRejected()
    if response.status == 409:
        raise HttpError(
            "another poller already uses this token (409) — stop the running agent first"
        )
    if response.status != 200 or not response.payload.get("ok"):
        raise HttpError(f"unexpected reply from Telegram (HTTP {response.status})")
    return response.payload


def _fetch_bot(http: Http, token: str) -> Bot:
    result = _call(http, token, "getMe", {}).get("result") or {}
    return Bot(
        token=token,
        name=str(result.get("first_name") or "unnamed"),
        username=str(result.get("username") or ""),
    )


def _senders_from(updates: Any) -> tuple[Sender, ...]:
    """Absender aus Updates, ohne Dubletten und ohne Bots, in Eingangsreihenfolge."""
    found: dict[int, Sender] = {}
    for item in updates if isinstance(updates, list) else []:
        entry = item if isinstance(item, dict) else {}
        message = entry.get("message") or entry.get("edited_message") or {}
        frm = message.get("from") or {}
        user_id = frm.get("id")
        if not isinstance(user_id, int) or frm.get("is_bot"):
            continue
        found.setdefault(
            user_id,
            Sender(user_id, str(frm.get("first_name") or ""), str(frm.get("username") or "")),
        )
    return tuple(found.values())


def _wait_for_senders(http: Http, token: str, wait_s: float) -> tuple[Sender, ...]:
    """Long-Poll bis zur ersten Nachricht oder bis das Zeitlimit erreicht ist.

    Der erste Abruf passiert immer — auch bei `wait_s = 0`. Sonst wäre eine
    Nachricht, die längst wartet, allein wegen der Uhr unsichtbar.
    """
    deadline = time.monotonic() + max(0.0, wait_s)
    offset = 0
    while True:
        payload = _call(
            http, token, "getUpdates",
            {"offset": offset, "timeout": POLL_CHUNK_S},
            timeout=POLL_CHUNK_S + 10,
        )
        updates = payload.get("result")
        senders = _senders_from(updates)
        if senders:
            return senders
        offset = _next_offset(updates, offset)
        if time.monotonic() >= deadline:
            return ()


def _next_offset(updates: Any, current: int) -> int:
    ids = [
        int(item["update_id"])
        for item in (updates if isinstance(updates, list) else [])
        if isinstance(item, dict) and isinstance(item.get("update_id"), int)
    ]
    return max(ids) + 1 if ids else current


# ------------------------------------------------------------------- Schritte
def _intro(console: _Console, out: Path) -> None:
    from .terminalui import heading

    console.say(heading("T A L O S  /  SETUP", "01 Connect  ·  02 Identity  ·  03 Model",
                        out=console._stdout))
    console.say("")
    console.say("  Three things are missing before it can run: the bot token, who is")
    console.say("  allowed to command it, and what it thinks with. This asks for all")
    console.say(f"  three, checks each one against the real service, and writes {out}.")
    console.say("  It starts nothing.")


def _explain_without_terminal(console: _Console, out: Path) -> None:
    """Ohne Terminal gibt es niemanden zu fragen — also den Weg von Hand zeigen."""
    console.say("")
    console.say(f"  {SYM_FAIL} stopped: this needs a terminal, and there is nobody to ask.")
    console.say("  Set the same values by hand — all of them, or it starts incomplete:")
    console.say("")
    console.say(f"    install -m 600 /dev/null {out}")
    console.say(f"    printf '{TOKEN_KEY}=%s\\n' '<token from @BotFather>' >> {out}")
    console.say(f"    printf '{PRINCIPALS_KEY}=telegram:%s\\n' '<your numeric id>' >> {out}")
    console.say(f"    printf '{PROVIDER_KEY}=%s\\n' '{ROUTE_ANTHROPIC}' >> {out}")
    console.say(f"    printf '{MODEL_KEY}=%s\\n' '{ANTHROPIC_MODELS[0]}' >> {out}")
    console.say(f"    printf '{ANTHROPIC_KEY}=%s\\n' '<your own api key>' >> {out}")
    console.say("")
    console.say(f"  OpenAI-compatible instead: {PROVIDER_KEY}={ROUTE_OPENAI} with {OPENAI_KEY},")
    console.say(f"  plus {OPENAI_BASE_URL_KEY} if it is not OpenAI itself. An already set up local")
    console.say(f"  CLI is {PROVIDER_KEY}={ROUTE_CLAUDE_CLI} or"
                f" {PROVIDER_KEY}={config.DEFAULT_MODEL_PROVIDER} — no key needed.")
    console.say("")
    console.say("  The same names work as environment variables.")
    console.say("  There is no default identity: an empty allowlist is refused on start.")


def _existing_values(path: Path) -> dict[str, str]:
    """Belegte Werte in der Zieldatei. Eigener Leser, weil hier ein halb gefülltes
    File normal ist — `load_config` würde daran zu Recht scheitern."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() in WRITTEN_KEYS and value.strip():
            values[key.strip()] = value.strip()
    return values


def _may_overwrite(console: _Console, out: Path) -> bool:
    """Bestehendes wird nie still ersetzt — und die Vorgabe ist „lass es liegen"."""
    present = _existing_values(out)
    if not present:
        return True
    console.say("")
    console.say(f"  {out} already holds values:")
    for key, value in present.items():
        console.say(f"    {_masked(key, value)}")
    if console.confirm("  Overwrite them?", default=False):
        return True
    console.say("  Nothing changed.")
    return False


def _ask_token(console: _Console, http: Http) -> Bot:
    console.say("")
    console.say("  1) The bot token.")
    console.say("     Telegram → @BotFather → /newbot (or /token for an existing bot).")
    console.say("     It looks like 123456789:AA… — treat it as a password.")
    for _ in range(MAX_ATTEMPTS):
        raw = console.ask("  token: ")
        if not raw:
            raise _Aborted("no token given — nothing was written.")
        if not TOKEN_RE.match(raw):
            console.say("    That is not a token shape (digits, ':', then the secret).")
            continue
        bot = _try_token(console, http, raw)
        if bot is not None:
            console.say(f"    {SYM_OK} {bot.name} (@{bot.username})")
            return bot
    raise _Aborted("too many attempts — nothing was written.")


def _try_token(console: _Console, http: Http, token: str) -> Bot | None:
    """Ein Versuch gegen `getMe`. `None` heisst „nochmal fragen", nie „egal"."""
    try:
        return _fetch_bot(http, token)
    except _TokenRejected:
        console.say("    Telegram rejected this token (401). It is not valid — ask @BotFather.")
    except HttpError as error:
        console.say(f"    Could not reach Telegram: {error}")
        console.say("    That is a network problem, not a wrong token. Trying again is safe.")
    return None


def _ask_identity(console: _Console, http: Http, token: str, wait_s: float) -> Principal:
    """Identität einfangen statt abtippen — der Bot bezeugt, wer geschrieben hat."""
    console.say("")
    console.say("  2) Who may command this agent.")
    console.say("     Open Telegram, write your bot anything — I am waiting.")
    console.say(f"     (up to {wait_s:.0f}s; nothing is written until you confirm)")
    try:
        senders = _wait_for_senders(http, token, wait_s)
    except HttpError as error:
        raise _Aborted(f"could not listen for messages: {error}") from None
    if not senders:
        return _manual_identity(console, wait_s)
    chosen = _choose_sender(console, senders)
    if chosen is None:
        raise _Aborted(
            "identity not confirmed — nothing was written. "
            "An unconfirmed id in the allowlist would hand the agent to someone else."
        )
    return Principal(LEGACY_CHANNEL, str(chosen.user_id))


def _choose_sender(console: _Console, senders: tuple[Sender, ...]) -> Sender | None:
    """Ein ausdrückliches Ja bzw. eine getippte Nummer. Beides ist eine Entscheidung."""
    if len(senders) == 1:
        console.say(f"    That was {senders[0].label()}.")
        return senders[0] if console.confirm("  Is that you?") else None
    console.say("    More than one person wrote:")
    for index, sender in enumerate(senders, start=1):
        console.say(f"      {index}) {sender.label()}")
    answer = console.ask("  Which one are you? [number, empty = none] ")
    if not answer.isdigit() or not 1 <= int(answer) <= len(senders):
        return None
    return senders[int(answer) - 1]


def _manual_identity(console: _Console, wait_s: float) -> Principal:
    """Rückfallweg nach Zeitüberschreitung — bewusst sichtbar der schlechtere Weg.

    Er bleibt trotzdem drin: die Alternative wäre ein von Hand geschriebenes
    env-File, also derselbe Tippfehler, nur ganz ohne Assistenten.
    """
    console.say(f"    Nobody wrote within {wait_s:.0f}s.")
    console.say("    Fallback: type your numeric Telegram id (@userinfobot tells you).")
    raw = console.ask("  your telegram id [empty = stop]: ")
    if not raw:
        raise _Aborted("no identity — an empty allowlist would be open to everyone.")
    if not USER_ID_RE.match(raw):
        raise _Aborted(f"{raw!r} is not a numeric telegram id — nothing was written.")
    return Principal(LEGACY_CHANNEL, raw)


# ------------------------------------------------------------------- Modell
def detect_runtimes(values: dict[str, str] | None = None) -> LocalRuntimes:
    """Was an Modell-Laufzeiten auf diesem Rechner wirklich benutzbar ist."""
    values = values or {}
    native = Path.home() / ".local/bin/claude"
    claude = os.environ.get("TALOS_CLAUDE_BIN") or values.get("TALOS_CLAUDE_BIN")
    hermes = os.environ.get("TALOS_HERMES_BIN") or values.get("TALOS_HERMES_BIN")
    return LocalRuntimes(
        claude=_executable(claude or (str(native) if native.is_file() and
                           os.access(native, os.X_OK) else config.CLAUDE_BIN), "claude"),
        hermes=_executable(hermes or config.HERMES_BIN, "hermes"),
    )


def _executable(configured: str, name: str) -> str:
    """Der konfigurierte Pfad, sonst der aus dem PATH. Nur Ausführbares zählt.

    Geprüft wird auf das X-Bit, nicht auf blosse Existenz: eine Datei, die nur
    dasteht, ist keine Laufzeit. Sie als Vorschlag anzubieten hiesse, eine
    Einrichtung zu bestätigen, die beim ersten Zug bricht.
    """
    candidate = Path(configured).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(name) or ""


def _routes(runtimes: LocalRuntimes) -> tuple[_Route, ...]:
    """API-Schluessel oder die eigene, bereits angemeldete lokale CLI."""
    local = [
        _Route(key, f"the {name} CLI already set up here ({path})", note)
        for key, name, path, note in (
            (ROUTE_CLAUDE_CLI, "claude", runtimes.claude,
             "uses your local sign-in; keep the CLI up to date"),
            (ROUTE_HERMES, "hermes", runtimes.hermes,
             "uses your existing local provider configuration"),
        )
        if path
    ]
    return (
        *local,
        _Route(ROUTE_ANTHROPIC, "Claude through an API key of your own"),
        _Route(ROUTE_OPENAI, "OpenAI-compatible — Codex and any provider with its own base url"),
    )


def _ask_model(
    console: _Console, http: Http, out: Path, runtimes: LocalRuntimes | None
) -> ModelSetup:
    """Womit der Agent denkt. Ohne diesen Wert läuft eine frische Installation nicht.

    Zuerst wird gezeigt, was schon da ist — sonst entscheidet der Betreiber blind
    und richtet womöglich ein zweites Mal ein, was längst funktioniert.
    """
    present = _existing_values(out)
    found = detect_runtimes(present) if runtimes is None else runtimes
    console.say("")
    console.say("  Model connection — what the agent thinks with.")
    console.say("     Already on this machine:")
    console.say(f"       claude CLI: {found.claude or 'not found'}")
    console.say(f"       hermes CLI: {found.hermes or 'not found'}")
    for name in (ANTHROPIC_KEY, OPENAI_KEY):
        console.say(f"       {name}: {_key_source(name, present)}")
    route = _choose_route(console, found)
    if route.key in {ROUTE_CLAUDE_CLI, ROUTE_HERMES}:
        return _local_setup(console, route, found)
    return _api_setup(console, http, route)


def _key_source(name: str, present: dict[str, str]) -> str:
    if os.environ.get(name):
        return "set in the environment"
    value = present.get(name, "")
    return f"in the target file already ({mask_token(value)})" if value else "not set"


def _choose_route(console: _Console, runtimes: LocalRuntimes) -> _Route:
    """Nummer oder Name. Vorschlag ist die lokale Einrichtung, sonst der API-Weg."""
    routes = _routes(runtimes)
    default = routes[0]
    console.say("     How should it reason?")
    for index, route in enumerate(routes, start=1):
        console.say(f"       {index}) {route.key} — {route.label}")
        if route.note:
            console.say(f"          {route.note}")
    for _ in range(MAX_ATTEMPTS):
        answer = console.ask(f"  which one? [number or name, empty = {default.key}] ")
        if not answer:
            return default
        chosen = _match_route(routes, answer)
        if chosen is not None:
            return chosen
        console.say("    Not one of the options.")
    raise _Aborted("no way to reason chosen — nothing was written.")


def _match_route(routes: tuple[_Route, ...], answer: str) -> _Route | None:
    if answer.isdigit() and 1 <= int(answer) <= len(routes):
        return routes[int(answer) - 1]
    wanted = answer.casefold()
    return next((route for route in routes if route.key == wanted), None)


def _local_setup(console: _Console, route: _Route, runtimes: LocalRuntimes) -> ModelSetup:
    """Der lokale Weg bleibt, wie er ist: Anbieter und Modell, aber KEIN Schlüssel.

    Für Hermes wird der Anbietername genommen, den Talos ohnehin als Vorgabe trägt —
    raten wäre hier besonders teuer, weil ein falscher Anbietername erst beim ersten
    Zug auffällt.
    """
    if route.key == ROUTE_CLAUDE_CLI:
        provider, models = ROUTE_CLAUDE_CLI, ANTHROPIC_MODELS
    else:
        provider, models = config.DEFAULT_MODEL_PROVIDER, (config.DEFAULT_MODEL,)
    model = _choose_model(console, models)
    console.say(f"    {SYM_OK} keeping the local setup — no key is written.")
    claude = route.key == ROUTE_CLAUDE_CLI
    return ModelSetup(provider=provider, model=model,
                      binary_name="TALOS_CLAUDE_BIN" if claude else "TALOS_HERMES_BIN",
                      binary=runtimes.claude if claude else runtimes.hermes)


def _api_setup(console: _Console, http: Http, route: _Route) -> ModelSetup:
    """Der öffentliche Weg: eigener Schlüssel, gegen die echte API bewiesen."""
    anthropic = route.key == ROUTE_ANTHROPIC
    base_url = ANTHROPIC_BASE_URL if anthropic else _ask_base_url(console)
    key = _ask_api_key(console, http, route, base_url)
    model = _choose_model(console, ANTHROPIC_MODELS if anthropic else OPENAI_MODELS)
    return ModelSetup(
        provider=route.key,
        model=model,
        key_name=ANTHROPIC_KEY if anthropic else OPENAI_KEY,
        key=key,
        # Anthropic hat genau eine Adresse; sie zu schreiben wäre eine Zeile, die nur
        # falsch werden kann. Bei OpenAI-kompatiblen Anbietern ist sie die halbe Miete.
        base_url="" if anthropic else base_url,
    )


def _ask_base_url(console: _Console) -> str:
    answer = console.ask(f"  base url [empty = {OPENAI_BASE_URL}] ")
    if not answer:
        return OPENAI_BASE_URL
    if not answer.startswith(("http://", "https://")):
        raise _Aborted(f"{answer!r} is not a base url — nothing was written.")
    return answer.rstrip("/")


def _ask_api_key(console: _Console, http: Http, route: _Route, base_url: str) -> str:
    console.say("     The key stays on this machine, is written mode 600, and is")
    console.say("     never printed in full — not even in an error from the provider.")
    for _ in range(MAX_ATTEMPTS):
        raw = console.ask_secret("  api key: ")
        if not raw:
            raise _Aborted("no api key given — nothing was written.")
        # Erst merken, dann benutzen: die Fehlermeldung des nächsten Aufrufs kann ihn
        # zitieren, und dann ist es für die Maskierung zu spät.
        console.hide(raw)
        if _try_api_key(console, http, route, raw, base_url):
            console.say(f"    {SYM_OK} {mask_token(raw)} accepted by {base_url}")
            return raw
    raise _Aborted("too many attempts — nothing was written.")


def _try_api_key(
    console: _Console, http: Http, route: _Route, key: str, base_url: str
) -> bool:
    """Ein Versuch gegen die echte API. `False` heisst „nochmal fragen", nie „egal"."""
    try:
        _verify_key(http, route, key, base_url)
        return True
    except _TokenRejected as rejected:
        console.say(f"    {rejected}")
    except HttpError as error:
        console.say(f"    Could not reach {base_url}: {error}")
        console.say("    That is a network problem, not a wrong key. Trying again is safe.")
    return False


def _verify_key(http: Http, route: _Route, key: str, base_url: str) -> None:
    """Billigster Beweis, dass der Schlüssel gilt: das Modellverzeichnis lesen.

    Es kostet keine Token und erzeugt nichts — und trotzdem antwortet der Anbieter
    genau dann mit 401, wenn der Schlüssel nicht gilt. Ein Testprompt wäre der
    teurere Weg zur selben Antwort.
    """
    if route.key == ROUTE_ANTHROPIC:
        url = f"{base_url}/v1/models"
        headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    else:
        url = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {key}"}
    response = http.get(url, {"limit": 1}, API_TIMEOUT_S, headers=headers)
    rejected = KEY_REJECTED.get(response.status)
    if rejected:
        raise _TokenRejected(rejected)
    if response.status != 200:
        raise HttpError(f"unexpected reply (HTTP {response.status})")


def _choose_model(console: _Console, models: tuple[str, ...]) -> str:
    """Kuratierte Liste plus Freitext — die Liste altert, der Anbieter entscheidet."""
    console.say("     Model:")
    for index, name in enumerate(models, start=1):
        console.say(f"       {index}) {name}")
    for _ in range(MAX_ATTEMPTS):
        answer = console.ask(f"  model [number, name, empty = {models[0]}] ")
        if not answer:
            return models[0]
        if answer.isdigit() and 1 <= int(answer) <= len(models):
            return models[int(answer) - 1]
        if MODEL_NAME_RE.match(answer):
            return answer
        console.say("    That is not a model name.")
    raise _Aborted("no model chosen — nothing was written.")


def _masked(key: str, value: str) -> str:
    return f"{key}={mask_token(value)}" if key in SECRET_KEYS else f"{key}={value}"


def _write_env(
    out: Path, bot: Bot, principal: Principal, setup: ModelSetup
) -> tuple[tuple[str, str], ...]:
    """Ersetzt die bestaetigten Setup-Werte atomar; andere Einstellungen bleiben."""
    values = (
        (TOKEN_KEY, bot.token),
        (USERNAME_KEY, bot.username),
        (PRINCIPALS_KEY, str(principal)),
        *setup.values(),
    )
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return _write_section(out, values)


def _report(console: _Console, out: Path, values: tuple[tuple[str, str], ...],
            *, terminal: bool = False) -> None:
    console.say("")
    console.say(f"  Wrote {out}, mode 600:")
    for key, value in values:
        console.say(f"    {_masked(key, value)}")
    console.say("")
    console.say("  Done — and it is not running.")
    command = "python -m talos chat" if terminal else "python -m talos"
    if out.resolve() not in {config.LOCAL_ENV.resolve(), config.SECRETS_ENV.resolve()}:
        command = f"TALOS_SECRETS_ENV={shlex.quote(str(out.resolve()))} {command}"
    console.say(f"  Start it yourself:  {command}")
    console.say("  Change model later: talos setup model")
    console.say(f"  Check configuration: talos config validate --file {shlex.quote(str(out))}")
    console.say("  Diagnose prerequisites: talos doctor")
    console.say("  A running service keeps its current settings until restarted.")


# --------------------------------------------------------------------- Aufruf
def _parse_args(argv: list[str]) -> Options:
    """`--out PATH`, `--wait SECONDS`. Ein führendes `setup` ist erlaubt und egal."""
    rest = list(argv)
    if rest and rest[0] == "setup":
        rest.pop(0)
    out = Path(config.SECRETS_ENV)
    wait_s = DEFAULT_WAIT_S
    section = ""
    if rest and not rest[0].startswith("-"):
        section = rest.pop(0)
        if section not in SECTIONS:
            raise _Usage(f"unknown section: {section}")
    while rest:
        flag = rest.pop(0)
        if flag in {"-h", "--help"}:
            raise _Usage("")
        if flag not in {"--out", "--wait"}:
            raise _Usage(f"unknown argument: {flag}")
        if not rest:
            raise _Usage(f"{flag} needs a value")
        value = rest.pop(0)
        if flag == "--out":
            out = Path(value).expanduser()
            continue
        try:
            wait_s = max(0.0, float(value))
        except ValueError:
            raise _Usage(f"--wait needs a number of seconds, got {value!r}") from None
    return Options(out=out, wait_s=wait_s, section=section)


def _ask_mail(console: _Console, out: Path) -> tuple[tuple[str, str], ...]:
    """Der zweite Eingang. Er HOLT ab (IMAP) — ein empfangender Server waere ein Tor
    von aussen, und genau deshalb gibt es hier keinen Webhook zu konfigurieren.

    Die Stufe bleibt `Trust.ASK` und ist nicht einstellbar: eine Adresse beweist kein
    Konto. Wer darueber schreibt, darf gefragt werden und antworten — freigeben nie.
    """
    console.say("")
    console.say("  Mail — the second way in. It fetches over IMAP; nothing listens.")
    console.say("  Leave the host empty to skip.")
    host = console.ask("    IMAP host (e.g. imap.example.com): ").strip()
    if not host:
        console.say("    skipped — mail stays off.")
        return ()
    user = console.ask("    IMAP user (the full address): ").strip()
    password = console.ask_secret("    IMAP password: ")
    if not user or not password:
        raise _Aborted("mail needs host, user and password — nothing was written.")
    smtp = console.ask("    SMTP host for replies (empty: no replies): ").strip()
    console.say("")
    console.say("    One more, and it matters: the name YOUR receiving server stamps")
    console.say("    into Authentication-Results. Talos trusts only that header, and only")
    console.say("    the topmost one — a From: line proves nothing, and neither does a")
    console.say("    header nobody can attribute to a server.")
    console.say("    Leave it empty and the channel exists but discards every mail:")
    console.say("    without this name there is nothing left to prove a sender with.")
    authserv = console.ask("    authserv-id (e.g. mx.example.com): ").strip()
    werte = [(MAIL_HOST_KEY, host), (MAIL_USER_KEY, user), (MAIL_PASSWORD_KEY, password)]
    if smtp:
        werte.append((MAIL_SMTP_KEY, smtp))
    if authserv:
        werte.append((MAIL_AUTHSERV_KEY, authserv))
    return tuple(werte)


def _write_section(out: Path, values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """Alle Werte in EINEM atomaren Austausch, damit kein halber Abschnitt entsteht."""
    from .configcli import write_keys

    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_keys(out, dict(values))
    return values


def _terminal(console: _Console, http: Http, options: Options,
              runtimes: LocalRuntimes | None) -> int:
    from .askcli import refuse_in_sandbox
    from .chatcli import attended
    from .configcli import read_file

    if refuse_in_sandbox() or not attended(console._stdin, console._stdout):
        raise _Aborted("terminal setup needs a real input AND output terminal, outside the sandbox.")
    from .terminalui import heading

    console.say(heading("T A L O S  /  TERMINAL SETUP",
                        "01 Your identity  ·  02 Your model  ·  Then: talos chat",
                        out=console._stdout))
    principal = str(Principal("cli", str(os.getuid())))
    existing = {}
    if options.out.resolve() in {config.LOCAL_ENV.resolve(), config.SECRETS_ENV.resolve()}:
        existing.update(read_file(config.LOCAL_ENV))
        existing.update(read_file(config.SECRETS_ENV))
    existing.update(read_file(options.out))
    raw = existing.get(PRINCIPALS_KEY) or existing.get("TALOS_ALLOWED_USER_IDS", "")
    identities = list(dict.fromkeys(str(Principal.parse(part)) for part in
                                   raw.replace(",", " ").split()))
    override = os.environ.get(PRINCIPALS_KEY) or os.environ.get("TALOS_ALLOWED_USER_IDS", "")
    if override and principal not in {str(Principal.parse(p)) for p in override.replace(",", " ").split()}:
        raise _Aborted(f"the environment overrides {PRINCIPALS_KEY} and excludes this terminal; "
                       "update that override first. Nothing was written.")
    console.say(f"  1) Allow this local user ({principal}) to give Talos instructions.")
    console.say("     Tool permissions still come from the kernel; existing identities stay.")
    if not console.confirm("  Enable this terminal?", default=False):
        console.say("  Cancelled — nothing was written.")
        return EXIT_OK
    if principal not in identities:
        identities.append(principal)
    console.say("  2) Choose a model connection.")
    model = _ask_model(console, http, options.out, runtimes)
    values = ((PRINCIPALS_KEY, ",".join(identities)), *model.values())
    _report(console, options.out, _write_section(options.out, values), terminal=True)
    return EXIT_OK


def _wizard(
    console: _Console, http: Http, options: Options, runtimes: LocalRuntimes | None
) -> int:
    if options.section:
        return _section(console, http, options, runtimes)
    _intro(console, options.out)
    if not console.interactive:
        _explain_without_terminal(console, options.out)
        return EXIT_NO_TERMINAL
    if not _may_overwrite(console, options.out):
        return EXIT_OK
    bot = _ask_token(console, http)
    principal = _ask_identity(console, http, bot.token, options.wait_s)
    setup = _ask_model(console, http, options.out, runtimes)
    _report(console, options.out, _write_env(options.out, bot, principal, setup))
    return EXIT_OK


def _section(
    console: _Console, http: Http, options: Options, runtimes: LocalRuntimes | None
) -> int:
    """Ein einzelner Abschnitt — er ruehrt ausschliesslich seine eigenen Schluessel an."""
    if options.section == SECTION_TERMINAL:
        return _terminal(console, http, options, runtimes)
    if not console.interactive:
        _explain_without_terminal(console, options.out)
        return EXIT_NO_TERMINAL
    console.say("")
    console.say(f"  {SYM_TALOS} talos setup {options.section} — {options.out}")

    if options.section == SECTION_IDENTITY:
        bot = _ask_token(console, http)
        principal = _ask_identity(console, http, bot.token, options.wait_s)
        werte = ((TOKEN_KEY, bot.token), (USERNAME_KEY, bot.username),
                 (PRINCIPALS_KEY, str(principal)))
    elif options.section == SECTION_MODEL:
        werte = _ask_model(console, http, options.out, runtimes).values()
    else:
        werte = _ask_mail(console, options.out)
        if not werte:
            return EXIT_OK

    _report(console, options.out, _write_section(options.out, werte))
    return EXIT_OK


def run_setup(
    argv: list[str] | None = None,
    *,
    stdin: Any = None,
    stdout: Any = None,
    http: Http | None = None,
    runtimes: LocalRuntimes | None = None,
) -> int:
    """Führt durch die Einrichtung und gibt den Exit-Code zurück. Startet nichts."""
    console = _Console(
        sys.stdin if stdin is None else stdin,
        sys.stdout if stdout is None else stdout,
    )
    try:
        options = _parse_args(list(sys.argv[1:] if argv is None else argv))
    except _Usage as problem:
        if str(problem):
            console.say(f"  {SYM_FAIL} {problem}")
            console.say(USAGE)
            return EXIT_USAGE
        console.say(USAGE)
        return EXIT_OK
    try:
        return _wizard(console, RequestsHttp() if http is None else http, options, runtimes)
    except _Aborted as stop:
        console.say("")
        console.say(f"  {SYM_FAIL} stopped: {stop}")
        return EXIT_ABORTED
    except OSError as error:
        # Unschreibbares Ziel ist ein Bedienfehler, kein Absturz — und der Hinweis
        # muss sagen, dass wirklich nichts entstanden ist.
        console.say("")
        console.say(f"  {SYM_FAIL} stopped: could not write {options.out}: {error}")
        return EXIT_ABORTED
    except KeyboardInterrupt:
        console.say("")
        console.say(f"  {SYM_FAIL} stopped: interrupted — nothing was written.")
        return EXIT_ABORTED
