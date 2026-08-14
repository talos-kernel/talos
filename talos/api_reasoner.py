"""Denken ueber eine HTTP-API — der Weg fuer alle, die keine Hersteller-CLI haben.

Die beiden bestehenden Reasoner setzen eine lokale Installation voraus: `ClaudeCliReasoner`
ruft die `claude`-CLI im Print-Mode (OAuth/Abo), `HermesCliReasoner` die `hermes`-CLI. Wer
Talos oeffentlich installiert, hat weder das eine noch das andere — die oeffentliche Fassung
koennte also gar nicht denken. Dieses Modul schliesst genau diese Luecke: ein Reasoner, der
nur einen eigenen API-Schluessel braucht.

**Warum ausdruecklich kein OAuth/Abo.** Ein fremdes Abo ueber die Hersteller-CLI
mitzubenutzen waere Impersonation und brachte Sperrrisiko fuer das Konto, dem es gehoert.
Oeffentlich gilt deshalb: eigener Schluessel, sonst nichts. Eine private Installation bleibt
unveraendert beim CLI-Weg — dieses Modul ist additiv und ruehrt an den bestehenden
Reasonern nichts an.

**Der Reasoner darf NUR denken.** Es wird kein einziges Werkzeug an die API deklariert:
kein `tools`-Feld, keine Anbieter-Werkzeuge, keine Websuche. Das ist eine Sicherheitsgrenze
und kein Feintuning — Hersteller-Werkzeuge liefen VOR dem Policy-Kernel und waeren damit
ein Gate-Bypass, genau wie die CLI-Werkzeuge, die `DISALLOWED_TOOLS_ARGV` abschaltet. Was
diese Klasse anfordern darf, sagt sie weiterhin als `TOOL_CALL`-Zeile im Text; darueber
urteilt der Kernel.

**Die Denk-Falle, dieselbe wie in `stream.py`.** Ein Zug hat mehrere Bloecke, und der erste
ist oft ein *thinking*-Block, der ebenfalls Deltas sendet. Wer blind jedes Delta
durchreicht, streamt die Gedankenkette in den Chat. Beide Parser hier arbeiten deshalb mit
einer **Positivliste**: emittiert wird ausschliesslich, was in einem ausdruecklich als
`text` gestarteten Block als `text_delta` ankommt (Anthropic) bzw. was unter
`choices[].delta.content` steht (OpenAI-kompatibel). Felder wie `thinking`,
`reasoning_content` oder `reasoning` werden nie gelesen — ein neues Denk-Feld eines
Anbieters kann deshalb nicht versehentlich durchrutschen.

**Kein SDK.** `requests` steht ohnehin in `requirements.txt`; ein Anbieter-SDK haette das
Paket aufgeblaeht und Abhaengigkeiten mitgebracht, die dieses Projekt nicht pruefen kann.
Der Netzzugriff laeuft ausserdem ueber die injizierbare `http`-Abhaengigkeit, damit die
Tests ohne Netz auskommen (Vertrag: siehe `HttpTransport`).
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from .credentials import CredentialStore, Route
from . import instructions
from .reasoner import CANCELLED_TEXT, PLAN_PROTOCOL, TOOL_PROTOCOL, skills_block
from .stream import OnText
from .usage import Run, UsageMeter

__all__ = ["ApiReasoner", "HttpResponse", "HttpTransport", "SUPPORTED_PROVIDERS"]

PROVIDER_ANTHROPIC = "anthropic-api"
PROVIDER_OPENAI = "openai-api"
SUPPORTED_PROVIDERS: tuple[str, ...] = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# Anthropic verlangt `max_tokens`; der Deckel begrenzt Denken UND Antworttext zusammen.
# 16k laesst dem adaptiven Denken Luft und schneidet trotzdem eine entlaufene Antwort ab.
# Fuer OpenAI-kompatible Backends wird das Feld bewusst NICHT gesetzt: dort ist es optional,
# und ein geratener Deckel wuerde bei Modellen mit kleinerem Ausgabelimit den Aufruf kippen.
ANTHROPIC_MAX_TOKENS = 16_000

READY_MARKER = "TALOS_READY"
REDACTED = "***"
_DETAIL_CHARS = 300

# Fuenf unterscheidbare Meldungen. Ein Netzfehler darf nie wie ein falscher Schluessel
# aussehen — sonst rotiert der Betreiber einen Schluessel, der nie das Problem war.
KEY_REJECTED = "(Reasoner error: API key rejected (HTTP {status}). Check the configured key.)"
RATE_LIMITED = "(Reasoner error: rate limit or quota reached (HTTP 429). Wait and retry.)"
OVERLOADED = "(Reasoner error: provider overloaded (HTTP {status}). Retry shortly.)"
NETWORK_FAILED = "(Reasoner error: network failure — {detail})"
HTTP_FAILED = "(Reasoner error: HTTP {status} — {detail})"
TIMED_OUT = "(Timed out while thinking — please try again.)"
EMPTY_ANSWER = "(Empty answer.)"

# Alles, was nach einem Geheimnis aussieht, faellt aus jeder ausgegebenen Zeile heraus —
# nicht nur der eigene Schluessel. HTTP-Bibliotheken zitieren gern Header oder URLs, und
# ein Fehlertext eines fremden Servers kann den gesendeten Schluessel zurueckspiegeln.
_SECRETISH = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,})"
    r"|(Bearer\s+\S+)"
    r"|((?i:x-api-key)\s*[:=]\s*\S+)"
)


class HttpResponse(Protocol):
    """Was vom Antwortobjekt gebraucht wird — bewusst schmal gehalten.

    `requests.Response` erfuellt das unveraendert; ein Test-Double braucht nur diese vier
    Namen. `iter_lines` darf `str` oder `bytes` liefern.
    """

    status_code: int
    text: str

    def iter_lines(self, decode_unicode: bool = False) -> Iterable[Any]: ...

    def close(self) -> None: ...


class HttpTransport(Protocol):
    """Die einzige Netz-Naht. `requests.Session` erfuellt sie ohne Adapter."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Any,
        timeout: float,
        stream: bool,
    ) -> HttpResponse: ...


class _ApiFailure(Exception):
    """Ein Fehlschlag mit fertiger, bereits bereinigter Meldung fuer den Betreiber."""

    def __init__(self, message: str, note: str) -> None:
        super().__init__(note)
        self.message = message
        self.note = note


class _TextSink:
    """Sammelt den Antworttext und reicht jedes Stueck an die Live-Anzeige weiter.

    Ein kaputter Sink darf den Lauf nie mitnehmen: die Anzeige ist Komfort, die Antwort
    nicht — dieselbe Regel wie in `stream.py`.
    """

    def __init__(self, on_text: OnText | None) -> None:
        self._on_text = on_text
        self._parts: list[str] = []

    def emit(self, piece: object) -> None:
        if not isinstance(piece, str) or not piece:
            return
        self._parts.append(piece)
        if self._on_text is None:
            return
        try:
            self._on_text(piece)
        except Exception:
            pass

    @property
    def text(self) -> str:
        return "".join(self._parts)


class _AnthropicStream:
    """SSE der Messages-API. Emittiert nur Text-Deltas aus Text-Bloecken."""

    def __init__(self, sink: _TextSink) -> None:
        self.sink = sink
        self._text_blocks: set[int] = set()
        self.model = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.error = ""

    def feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            message = message if isinstance(message, dict) else {}
            self.model = str(message.get("model") or "")
            usage = message.get("usage")
            if isinstance(usage, dict):
                self.input_tokens = _int(usage.get("input_tokens"))
                self.output_tokens = _int(usage.get("output_tokens"))
            return
        if kind == "content_block_start":
            block = event.get("content_block")
            index = event.get("index")
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(index, int):
                self._text_blocks.add(index)
            return
        if kind == "content_block_delta":
            index = event.get("index")
            if not isinstance(index, int) or index not in self._text_blocks:
                return  # thinking/signature — gehoert nicht in den Chat
            delta = event.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            if delta.get("type") == "text_delta":
                self.sink.emit(delta.get("text"))
            return
        if kind == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.output_tokens = _int(usage.get("output_tokens")) or self.output_tokens
            return
        if kind == "error":
            self.error = _error_detail(event.get("error"))


class _OpenAiStream:
    """SSE der Chat-Completions-API. Liest ausschliesslich `delta.content`.

    `reasoning_content`/`reasoning` (DeepSeek, Qwen und andere) werden nie gelesen — die
    Positivliste ist die Sicherung, nicht eine Liste verbotener Feldnamen.
    """

    def __init__(self, sink: _TextSink) -> None:
        self.sink = sink
        self.model = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.error = ""

    def feed(self, chunk: dict) -> None:
        if chunk.get("error"):
            self.error = _error_detail(chunk.get("error"))
            return
        model = chunk.get("model")
        if isinstance(model, str) and model:
            self.model = model
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.input_tokens = _int(usage.get("prompt_tokens")) or self.input_tokens
            self.output_tokens = _int(usage.get("completion_tokens")) or self.output_tokens
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                self.sink.emit(delta.get("content"))


class ApiReasoner:
    """Denkt ueber eine HTTP-API — Anthropic-nativ oder OpenAI-kompatibel.

    Abbrechbar ohne Lock ueber dem Netz: der Lock schuetzt nur die Zustandsfelder. `cancel()`
    setzt die Flagge, gibt den Lock frei und schliesst danach die offene Antwort — sonst
    haenge ein `/stop` an derselben Leitung fest, die es beenden soll.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        credentials: CredentialStore,
        *,
        timeout_s: int,
        meter: UsageMeter | None = None,
        skills: Callable[[], str] | None = None,
        http: HttpTransport | None = None,
    ) -> None:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unbekannter API-Anbieter: {provider!r}")
        if not model.strip():
            raise ValueError("Modellname fehlt")
        self.provider = provider
        self.model = model.strip()
        # ⚠️ Der BESTAND, nicht der Wert. Aufgeloest wird in `_route()` bei jedem Aufruf:
        # ein eingefrorener Schluessel gehoert dem Anbieter, der beim Bauen ausgewaehlt
        # war — und `/model` wechselt den im laufenden Prozess.
        self._credentials = credentials
        # Fail closed, und zwar SOFORT: der Router baut vor dem Umschalten (`_build_validated`),
        # also faellt ein fehlender Schluessel als abgelehnter Wechsel auf, nicht als
        # kaputter Zug mitten im Gespraech. Die alte Auswahl bleibt dabei stehen.
        credentials.route(provider)
        self._timeout_s = timeout_s
        self._meter = meter
        self._skills = skills
        self._http = http if http is not None else _default_http()
        self._lock = threading.Lock()
        self._active = False
        self._cancelled = False
        self._response: HttpResponse | None = None

    @property
    def timeout_s(self) -> int:
        return self._timeout_s

    # --- Prompt ------------------------------------------------------------------

    def _skills_text(self) -> str:
        """Der Skill-Katalog fuer diesen Zug — leer, wenn keine Quelle verdrahtet ist.

        Injiziert statt selbst entdeckt, wie bei den CLI-Reasonern: ein Fehler in der
        Quelle kostet den Katalog, nie den Zug.
        """
        if self._skills is None:
            return ""
        try:
            return skills_block(self._skills())
        except Exception:
            return ""

    def _compose(self, prompt: str) -> tuple[str, str]:
        """(stehende Anweisungen, Nachricht) — pro Zug gelesen, nicht beim Start eingefroren.

        Derselbe Builder wie im CLI-Pfad ordnet SOUL, AGENTS, USER, Werkzeug-/Planprotokoll
        und Skill-Katalog. Hier gehen die stehenden Anweisungen ins System-Feld statt in
        den Nutzerzug. Grund ist nicht Kosmetik: liegen Persona, Werkzeugprotokoll und die
        Nachricht des Betreibers im selben Nutzerzug, kann das Modell beides nicht mehr
        auseinanderhalten — und fremder Nachrichtentext sieht dann wie eine stehende Regel aus.
        """
        return instructions.assemble_system_prompt(
            tool_protocol=TOOL_PROTOCOL,
            plan_protocol=PLAN_PROTOCOL,
            skills=self._skills_text(),
        ), prompt

    def _route(self) -> Route:
        """Schluessel und Adresse dieses Anbieters — bei JEDEM Aufruf frisch aufgeloest.

        Der Grund steht in `credentials.py`: ein Wert, der im Objekt liegt, gehoert dem
        Anbieter von damals. Hier gehoert er dem, an den gerade gesprochen wird.
        """
        eintrag = self._credentials.route(self.provider)
        vorgabe = ANTHROPIC_BASE_URL if self.provider == PROVIDER_ANTHROPIC else OPENAI_BASE_URL
        return Route(eintrag.provider, eintrag.api_key,
                     (eintrag.base_url or vorgabe).rstrip("/"))

    def _request(self, system: str, message: str) -> tuple[str, dict[str, str], dict]:
        """URL, Header und Koerper. Enthaelt bewusst KEIN `tools`-Feld (Gate-Bypass)."""
        route = self._route()
        if self.provider == PROVIDER_ANTHROPIC:
            url = f"{route.base_url}/v1/messages"
            headers = {
                "x-api-key": route.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
                "accept": "text/event-stream",
            }
            body: dict[str, Any] = {
                "model": self.model,
                "max_tokens": ANTHROPIC_MAX_TOKENS,
                "messages": [{"role": "user", "content": message}],
                "stream": True,
            }
            if system:
                body["system"] = system
            return url, headers, body
        url = f"{route.base_url}/chat/completions"
        headers = {
            "authorization": f"Bearer {route.api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": message})
        return url, headers, {
            "model": self.model,
            "messages": messages,
            "stream": True,
            # Ohne dieses Feld liefert ein OpenAI-kompatibler Strom gar keinen Verbrauch,
            # und `/usage` fiele auf Behauptungen zurueck. Es gehoert zum offiziellen
            # Schema; ein Backend, das es nicht kennt, muss ueber `base_url` erreichbar
            # bleiben — dann ist der Fehler sichtbar und nicht still.
            "stream_options": {"include_usage": True},
        }

    # --- Lauf --------------------------------------------------------------------

    def reason(self, prompt: str, on_text: OnText | None = None) -> str:
        system, message = self._compose(prompt)
        return self._run(system, message, on_text)

    def _run(self, system: str, message: str, on_text: OnText | None) -> str:
        with self._lock:
            if self._active:
                raise RuntimeError("API-Reasoner laeuft bereits")
            self._active = True
            self._cancelled = False
        started = time.monotonic()
        try:
            parser = self._exchange(system, message, _TextSink(on_text))
        except _ApiFailure as failure:
            self._record(started, ok=False, note=failure.note)
            return failure.message
        finally:
            with self._lock:
                self._active = False
                self._response = None
        if parser is None:
            self._record(started, ok=False, note="abgebrochen")
            return CANCELLED_TEXT
        text, note = _finish(parser)
        self._record(started, ok=not note, parser=parser, note=note)
        return text

    def _exchange(
        self, system: str, message: str, sink: _TextSink
    ) -> _AnthropicStream | _OpenAiStream | None:
        """Ein Aufruf. `None` heisst: abgebrochen. Fehler kommen als `_ApiFailure`."""
        url, headers, body = self._request(system, message)
        # Die Zeitgrenze laeuft ab dem Absenden, nicht ab der ersten Zeile: `timeout` einer
        # HTTP-Bibliothek ist eine Pause-zwischen-Paketen und beginnt bei jedem Byte neu.
        # Ein tropfender Strom haengt damit beliebig lange — die Wanduhr hier tut es nicht.
        deadline = time.monotonic() + max(1.0, float(self._timeout_s))
        try:
            response = self._http.post(
                url, headers=headers, json=body, timeout=float(self._timeout_s), stream=True
            )
        except Exception as error:  # Transportfehler jeder Bibliothek
            # `from None` ist hier Absicht und keine Nachlaessigkeit: die urspruengliche
            # Ausnahme kann den Header samt Schluessel tragen. Bliebe sie als `__cause__`
            # haengen, stuende sie im naechsten Traceback — bereinigt waere dann nur der
            # Satz, den ohnehin niemand liest.
            raise _ApiFailure(NETWORK_FAILED.format(detail=self._scrub(error)), "Netzfehler") from None
        with self._lock:
            if self._cancelled:
                _close(response)
                return None
            self._response = response
        try:
            status = _int(getattr(response, "status_code", 0))
            if status != 200:
                raise _ApiFailure(*_classify(status, self._scrub(_body_of(response))))
            return self._consume(response, sink, deadline)
        finally:
            _close(response)

    def _consume(
        self, response: HttpResponse, sink: _TextSink, deadline: float
    ) -> _AnthropicStream | _OpenAiStream | None:
        """Liest den SSE-Strom Zeile fuer Zeile und laesst nur Antworttext durch."""
        parser: _AnthropicStream | _OpenAiStream = (
            _AnthropicStream(sink) if self.provider == PROVIDER_ANTHROPIC else _OpenAiStream(sink)
        )
        try:
            for raw in response.iter_lines(decode_unicode=True):
                with self._lock:
                    cancelled = self._cancelled
                if cancelled:
                    return None
                if time.monotonic() > deadline:
                    raise _ApiFailure(TIMED_OUT, "Zeitüberschreitung")
                payload = _sse_payload(raw)
                if payload is None:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue  # eine unlesbare Zeile ist kein Grund, den Lauf zu verlieren
                if isinstance(event, dict):
                    parser.feed(event)
        except _ApiFailure:
            raise
        except Exception as error:
            with self._lock:
                cancelled = self._cancelled
            if cancelled:
                return None  # `cancel()` hat die Leitung geschlossen — das ist kein Fehler
            raise _ApiFailure(
                NETWORK_FAILED.format(detail=self._scrub(error)), "Netzfehler"
            ) from None
        return parser

    def validate(self) -> None:
        """Beweist Schluessel, Modell und Erreichbarkeit, bevor der Router umschaltet."""
        with self._lock:
            if self._active:
                raise RuntimeError("API-Reasoner laeuft bereits")
        answer = self._run("", f"Answer with exactly {READY_MARKER}.", None)
        if READY_MARKER not in answer:
            raise RuntimeError(f"API-Modellprobe ohne Bereitschaftsmarker: {answer[:160]}")

    def cancel(self) -> bool:
        """True, wenn wirklich ein Lauf abgeschossen wurde. False heisst: es lief nichts."""
        with self._lock:
            if not self._active:
                return False
            self._cancelled = True
            response = self._response
        # Ausserhalb des Locks: `close()` fasst die offene Verbindung an, und ein Lock
        # ueber einem Netzaufruf haette genau den Thread blockiert, der abbrechen will.
        if response is not None:
            _close(response)
        return True

    # --- Messung und Bereinigung --------------------------------------------------

    def _record(
        self,
        started: float,
        *,
        ok: bool,
        parser: _AnthropicStream | _OpenAiStream | None = None,
        note: str = "",
    ) -> None:
        """Zaehlt den Lauf. Ohne Meter passiert nichts — der Reasoner bleibt benutzbar.

        Gemessen, nicht geschaetzt: Token kommen aus der Antwort des Anbieters, die Dauer
        von der eigenen Uhr. Fehlt etwas, bleibt es 0 — `/usage` soll keine Zahl zeigen,
        fuer die es keine Quelle gibt. Kosten bleiben leer: der Preis pro Token ist
        Anbieter- und Tarifsache und waere hier geraten.
        """
        if self._meter is None:
            return
        measured = max(0.0, time.monotonic() - started)
        model = (parser.model if parser is not None else "") or self.model
        self._meter.record(
            Run(
                at=time.time(),
                ok=ok,
                duration_s=measured,
                model=model,
                models=(model,) if model else (),
                input_tokens=parser.input_tokens if parser is not None else 0,
                output_tokens=parser.output_tokens if parser is not None else 0,
                num_turns=1 if ok else 0,
                note=note,
            )
        )

    def _scrub(self, value: object) -> str:
        """Der Schluessel darf in keiner Meldung, keinem Log und keiner Ausnahme stehen.

        Zuerst JEDER hinterlegte Schluessel woertlich — nicht nur der aktive. Ein fremder
        Server spiegelt den Header zurueck, den er bekommen hat, und der Fall, gegen den
        `credentials.py` gebaut ist, waere sonst genau der eine, der ungeschwaerzt im Log
        steht. Danach alles, was ueberhaupt nach einem Geheimnis aussieht.
        """
        text = str(value)
        for schluessel in self._credentials.all_keys():
            text = text.replace(schluessel, REDACTED)
        return _SECRETISH.sub(REDACTED, text).strip()[:_DETAIL_CHARS]


def _default_http() -> HttpTransport:
    """`requests` erst beim Bauen importieren — Tests brauchen die Bibliothek nie."""
    import requests

    return requests.Session()  # type: ignore[return-value]


def _sse_payload(raw: object) -> str | None:
    """Der Nutzteil einer SSE-Zeile — `None` fuer Kommentare, Leerzeilen und `event:`."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    line = raw.strip()
    if not line.startswith("data:"):
        return None
    return line[len("data:"):].strip() or None


def _finish(parser: _AnthropicStream | _OpenAiStream) -> tuple[str, str]:
    """(Antworttext, Auffaelligkeit). Ein Fehlerereignis im Strom schlaegt den Text."""
    if parser.error:
        return f"(Reasoner error: {parser.error})", "Fehler laut Anbieter"
    text = parser.sink.text.strip()
    if not text:
        return EMPTY_ANSWER, "leere Antwort"
    return text, ""


def _classify(status: int, detail: str) -> tuple[str, str]:
    """Vier Ursachen, vier Meldungen. Wer sie zusammenlegt, schickt den Betreiber irre."""
    if status in (401, 403):
        return KEY_REJECTED.format(status=status), f"HTTP {status}"
    if status == 429:
        return RATE_LIMITED, "HTTP 429"
    if status in (500, 502, 503, 529):
        return OVERLOADED.format(status=status), f"HTTP {status}"
    return HTTP_FAILED.format(status=status, detail=detail or "unknown"), f"HTTP {status}"


def _error_detail(error: object) -> str:
    if isinstance(error, dict):
        message = error.get("message") or error.get("type") or ""
        return str(message)[:_DETAIL_CHARS] or "unknown"
    return str(error or "unknown")[:_DETAIL_CHARS]


def _body_of(response: HttpResponse) -> str:
    try:
        return str(getattr(response, "text", "") or "")
    except Exception:
        return ""


def _close(response: HttpResponse | None) -> None:
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
