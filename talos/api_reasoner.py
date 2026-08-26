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

**Schlüssellose Anbieter.** Der Katalog kennt lokale Anbieter (`auth="local"`, etwa
Ollama), die bewusst keinen Schluessel haben: ihre Route aus `credentials.py` traegt nur
eine Adresse, und die Anfrage geht ohne `Authorization`-Header raus — ein leerer Bearer
waere ein mitgeschicktes Leer-Geheimnis. Schluesselpflichtige Anbieter bleiben
fail-closed: ohne Schluessel wirft `CredentialStore.route` schon beim Bauen.

**Fehler als Ausnahme, Text unveraendert.** Ein klassifizierter Fehlschlag (falscher
Schluessel, Kontingent, Ueberlast, Netz, Timeout) wird intern als `ReasonerFailure`
geworfen; `reason()` faengt sie und liefert EXAKT den bisherigen Meldungstext. Wer die
Fehlerart maschinell braucht — die Laufzeit-Fallback-Kette in `fallback.py` — ruft
`reason_strict()`. So bleibt der Vertrag mit e2e/redteam wortgleich, ohne dass die
Kette an Texten raten muss.

**Transport-Naht: direct oder socket.** Die Vorgabe ist der Direktweg (HTTP aus diesem
Prozess). `TALOS_MODEL_WORKER=socket:///run/talos/model.sock` schaltet auf den
UID-getrennten Modell-Worker (`modelworker.py`) um: die Anfrage geht als JSON-Zeile
ueber einen Unix-Socket, der Schluessel liegt in der Worker-Env hinter einer anderen
UID — der Agent haelt dann gar keinen mehr. Fail-closed in beide Richtungen: ein
unerreichbarer Socket ist ein klassifizierter Netzfehler (die Fallback-Kette greift),
NIE ein stiller Rueckfall auf einen Schluessel im Agent-Env; und ein gesetzter, aber
unbekannter Variablenwert ist ein Fehler beim Bauen, keine stille Vorgabe.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Protocol

from . import catalog, instructions
from .credentials import WORKER_ENV_VAR, CredentialStore, Route, parse_worker_socket
from .reasoner import CANCELLED_TEXT, PLAN_PROTOCOL, TOOL_PROTOCOL, skills_block
from .stream import OnText
from .usage import Run, UsageMeter

__all__ = [
    "ApiReasoner",
    "FALLBACKABLE_KINDS",
    "HttpResponse",
    "HttpTransport",
    "ReasonerFailure",
    "SUPPORTED_PROVIDERS",
]

PROVIDER_ANTHROPIC = "anthropic-api"
PROVIDER_OPENAI = "openai-api"
# Neben den beiden klassischen API-Wegen die Katalog-Anbieter, die derselbe Reasoner
# ohne Zusatzcode sprechen kann: alles OpenAI-kompatible. `ollama` ist der lokale,
# schlüssellose Fall — seine Route traegt nur eine Adresse (siehe `credentials.py`).
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    PROVIDER_ANTHROPIC,
    PROVIDER_OPENAI,
    "ollama",
    "nvidia-nim",
    "kimi",
)

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

# Dieselben Ursachen als maschinenlesbare Art. Die Meldung ist fuer den Menschen und
# bleibt wortgleich; das `kind` ist fuer die Fallback-Kette (`fallback.py`), die danach
# entscheidet, ob ein zweiter Anbieter ueberhaupt eine Chance hat.
KIND_KEY_REJECTED = "key_rejected"
KIND_RATE_LIMITED = "rate_limited"
KIND_OVERLOADED = "overloaded"
KIND_NETWORK_FAILED = "network_failed"
KIND_TIMED_OUT = "timed_out"
KIND_HTTP_FAILED = "http_failed"

# Ein 4xx-Fachfehler (HTTP_FAILED) loest die Kette bewusst NICHT aus: das Modell hat die
# Anfrage verstanden und fachlich abgelehnt — der naechste Anbieter bekaeme dieselbe
# Anfrage und loeste denselben Fehler aus, nur teurer. Alles andere ist Infrastruktur.
FALLBACKABLE_KINDS: frozenset[str] = frozenset({
    KIND_KEY_REJECTED,
    KIND_RATE_LIMITED,
    KIND_OVERLOADED,
    KIND_NETWORK_FAILED,
    KIND_TIMED_OUT,
})

# Die Arten, die der Modell-Worker ueber den Socket melden darf — exakt dieselbe
# Taxonomie, damit die Kette ueber den Socket unveraendert funktioniert. Eine Art
# ausserhalb (etwa `invalid_request` des Workers) ist kein Anbieter-Urteil und wird
# als HTTP_FAILED behandelt: nicht fallbackbar, weil jeder Hop denselben Frame
# ablehnen wuerde.
_WORKER_KINDS: frozenset[str] = frozenset({
    KIND_KEY_REJECTED,
    KIND_RATE_LIMITED,
    KIND_OVERLOADED,
    KIND_NETWORK_FAILED,
    KIND_TIMED_OUT,
    KIND_HTTP_FAILED,
})

# Antwort-Deckel der Worker-Leitung. Der fertige Antworttext reist als EINE JSON-Zeile;
# die Grenze ist Puffer gegen einen Worker, der unbegrenzt kippt — keine inhaltliche
# Begrenzung (16 MiB Text sind weit ueber jedem Modell-Output).
_WORKER_MAX_LINE = 16 * 1024 * 1024

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


class ReasonerFailure(Exception):
    """Ein klassifizierter Denk-Fehlschlag — strukturiert, ohne den Text zu aendern.

    `message` ist EXAKT die bisherige, bereits bereinigte Betreiber-Meldung: Wer die
    Ausnahme nur abfaengt und ihren Text ausliefert, verhaelt sich wortgleich wie vor
    ihrer Einfuehrung (e2e/redteam assertieren auf diese Texte). `kind` ist die
    maschinelle Form derselben Ursache, damit eine Fallback-Kette entscheiden kann,
    ob ein weiterer Versuch sinnvoll ist — ohne an der Meldung zu raten.
    """

    def __init__(self, message: str, *, kind: str, note: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.note = note


class _ApiFailure(Exception):
    """Ein Fehlschlag mit fertiger, bereits bereinigter Meldung fuer den Betreiber."""

    def __init__(self, message: str, note: str, kind: str = KIND_HTTP_FAILED) -> None:
        super().__init__(note)
        self.message = message
        self.note = note
        self.kind = kind


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
        worker: str | None = None,
    ) -> None:
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unbekannter API-Anbieter: {provider!r}")
        if not model.strip():
            raise ValueError("Modellname fehlt")
        self.provider = provider
        self.model = model.strip()
        # Die Transport-Naht. `worker=None` heisst: die Umgebung entscheidet
        # (`TALOS_MODEL_WORKER`); ein expliziter Wert gewinnt — auch der leere, mit
        # dem der Modell-Worker selbst den Direktweg ERZwingt (sonst kettete ein
        # versehentlich gesetztes TALOS_MODEL_WORKER in seiner Umgebung Anfragen
        # ueber einen Socket weiter, und die UID-Trennung loefe im Kreis).
        self._worker_socket = parse_worker_socket(
            os.environ.get(WORKER_ENV_VAR, "") if worker is None else worker
        )
        # ⚠️ Der BESTAND, nicht der Wert. Aufgeloest wird in `_route()` bei jedem Aufruf:
        # ein eingefrorener Schluessel gehoert dem Anbieter, der beim Bauen ausgewaehlt
        # war — und `/model` wechselt den im laufenden Prozess.
        self._credentials = credentials
        if not self._worker_socket:
            # Fail closed, und zwar SOFORT: der Router baut vor dem Umschalten
            # (`_build_validated`), also faellt ein fehlender Schluessel als abgelehnter
            # Wechsel auf, nicht als kaputter Zug mitten im Gespraech. Die alte Auswahl
            # bleibt dabei stehen. Im Worker-Modus entfaellt die Pruefung absichtlich:
            # dort ist ein Bestand OHNE Schluessel der Soll-Zustand (der Schluessel
            # liegt in der Worker-Env hinter einer anderen UID), und die Pruefung wuerde
            # genau diesen Zustand als abgelehnten Wechsel melden.
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
        if self._worker_socket:
            # Laut statt still: im Worker-Modus HAT der Agent keine Route — ein Aufruf
            # hier waere ein Programmfehler, und ein Rueckfall auf einen Schluessel im
            # Agent-Env waere genau der Zustand, den der Worker abschafft.
            raise RuntimeError("Worker-Modus: der Agent-Prozess haelt keine Provider-Route")
        eintrag = self._credentials.route(self.provider)
        if self.provider == PROVIDER_ANTHROPIC:
            vorgabe = ANTHROPIC_BASE_URL
        else:
            # Die Vorgabe des Katalogs gilt vor der des Protokolls: ein Katalog-Anbieter
            # (ollama, nvidia-nim, kimi) traegt seine Adresse bereits, und ein
            # handgebauter Bestand ohne Adresse soll dort landen — nicht bei OpenAI.
            info = catalog.get(self.provider)
            vorgabe = (info.base_url if info is not None else "") or OPENAI_BASE_URL
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
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
        # ⚠️ Lokale Anbieter (ollama) haben BEWUSST keinen Schluessel: ein leerer
        # `Bearer`-Header waere keine Neutralitaet, sondern ein mitgeschicktes
        # Leer-Geheimnis — und manche Server lehnen genau das ab. Kein Schluessel,
        # kein Header. Schluesselpflichtige Anbieter kommen ohnehin gar nicht ohne
        # Schluessel durch `credentials.route`.
        if route.api_key:
            headers["authorization"] = f"Bearer {route.api_key}"
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
        """Der bisherige Vertrag: ein klassifizierter Fehler kommt als Meldungstext.

        Wer die Kette braucht, ruft `reason_strict` — derselbe Lauf, aber der Fehler
        fliegt als `ReasonerFailure`, statt hier zum Text zu werden.
        """
        try:
            return self.reason_strict(prompt, on_text)
        except ReasonerFailure as failure:
            return failure.message

    def reason_strict(self, prompt: str, on_text: OnText | None = None) -> str:
        """Wie `reason`, aber ein klassifizierter Fehlschlag wird GEWORFEN.

        Fuer die Laufzeit-Fallback-Kette: nur eine Ausnahme traegt die Fehlerart
        (`kind`) unverfaelscht — am Meldungstext entlangzuparsen waere Raten.
        """
        system, message = self._compose(prompt)
        return self._run(system, message, on_text)

    def reason_composed(self, system: str, message: str) -> str:
        """Ein Zug, dessen System- und Nutzerteil BEREITS komponiert sind.

        Fuer den Modell-Worker (`modelworker.py`): Persona, Werkzeugprotokoll und
        Skill-Katalog hat der Agent hineingeschrieben, BEVOR die Anfrage ueber den
        Socket ging — `_compose` wuerde sie hier ein zweites Mal ankleben. Fehler
        fliegen als `ReasonerFailure`, damit der Worker die Art ueber das Protokoll
        zurueckmelden kann.
        """
        return self._run(system, message, None)

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
            # Geworfen, nicht zurueckgegeben: der Aufrufer entscheidet, ob er den Text
            # ausliefert (`reason`) oder die Kette weiterschaltet (`fallback.py`).
            # `from None` wie unten beim Transportfehler: die Note duerfen Logs sehen,
            # eine verkettete Ausnahme mit Transport-Details nicht.
            raise ReasonerFailure(failure.message, kind=failure.kind, note=failure.note) from None
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
        if self._worker_socket:
            return self._exchange_via_worker(system, message, sink)
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
            raise _ApiFailure(
                NETWORK_FAILED.format(detail=self._scrub(error)), "Netzfehler",
                KIND_NETWORK_FAILED,
            ) from None
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
                    raise _ApiFailure(TIMED_OUT, "Zeitüberschreitung", KIND_TIMED_OUT)
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
                NETWORK_FAILED.format(detail=self._scrub(error)), "Netzfehler",
                KIND_NETWORK_FAILED,
            ) from None
        return parser

    # --- Worker-Transport ----------------------------------------------------------

    def _exchange_via_worker(
        self, system: str, message: str, sink: _TextSink
    ) -> _OpenAiStream | None:
        """Ein Aufruf ueber den Modell-Worker statt direkt ans Netz.

        Dieselbe Fehler-Taxonomie wie der Direktweg: der Worker antwortet mit Text
        oder einer klassifizierten Art (`modelworker.py`), und beides wird hier in
        genau die `_ApiFailure`-Form uebersetzt, die `_run`, `_record` und
        `fallback.py` bereits sprechen. Fail-closed: ein unerreichbarer Socket ist
        ein Netzfehler — NIE ein stiller Rueckfall auf einen Schluessel im
        Agent-Env (dort liegt im Worker-Modus ohnehin keiner).
        """
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": message})
        frame = json.dumps({
            "provider": self.provider,
            "model": self.model,
            "messages": messages,
            "params": {"timeout_s": self._timeout_s},
        }).encode("utf-8") + b"\n"
        # Wie beim Direktweg laeuft die Wanduhr ab dem Absenden: `settimeout` allein
        # waere eine Pause-zwischen-Paketen und beginnt bei jedem recv neu.
        deadline = time.monotonic() + max(1.0, float(self._timeout_s))
        verbindung = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            verbindung.settimeout(max(1.0, deadline - time.monotonic()))
            try:
                verbindung.connect(self._worker_socket)
                verbindung.sendall(frame)
            except OSError as error:
                raise _ApiFailure(
                    NETWORK_FAILED.format(detail=self._scrub(error)),
                    "Worker nicht erreichbar",
                    KIND_NETWORK_FAILED,
                ) from None
            with self._lock:
                if self._cancelled:
                    return None
                # In `self._response`, damit `cancel()` die Leitung schliessen kann —
                # `_close` spricht duck-typisiert `close()`, und das kann ein Socket.
                self._response = verbindung  # type: ignore[assignment]
            roh = self._read_worker_line(verbindung, deadline)
        finally:
            _close(verbindung)  # type: ignore[arg-type]
        if roh is None:
            return None  # abgebrochen
        try:
            antwort = json.loads(roh)
        except ValueError:
            antwort = None
        if not isinstance(antwort, dict):
            # Ein Worker, der kein Protokoll spricht, ist kaputte Infrastruktur —
            # dieselbe Klasse wie eine Leitung, die mitten im Satz abbricht.
            raise _ApiFailure(
                NETWORK_FAILED.format(detail="unreadable frame from the model worker"),
                "Worker-Protokollfehler",
                KIND_NETWORK_FAILED,
            )
        if antwort.get("ok") is True:
            text = antwort.get("text")
            if not isinstance(text, str):
                raise _ApiFailure(
                    NETWORK_FAILED.format(detail="model worker answer without text"),
                    "Worker-Protokollfehler",
                    KIND_NETWORK_FAILED,
                )
            sink.emit(text)
            # Der synthetische Parser traegt Text und Modell in dieselbe Form, die
            # `_finish` und `_record` vom Direktweg kennen. Token-Zaehler bleiben 0:
            # ueber den Socket reist nur Text (dokumentiert in docs/model-worker.md).
            parser = _OpenAiStream(sink)
            modell = antwort.get("model")
            if isinstance(modell, str) and modell:
                parser.model = modell
            return parser
        kind = antwort.get("kind")
        kind = kind if kind in _WORKER_KINDS else KIND_HTTP_FAILED
        # `_scrub` auch hier: die Worker-Meldung ist bereits bereinigt gebaut, aber
        # die Leitung ist eine Grenze — Vertrauen endet am Socket, nicht am Format.
        meldung = self._scrub(antwort.get("message") or "") or (
            f"(Reasoner error: model worker failed — {kind})"
        )
        raise _ApiFailure(meldung, f"Worker: {kind}", kind)

    def _read_worker_line(self, verbindung: socket.socket, deadline: float) -> bytes | None:
        """Eine Antwort-Zeile vom Worker. `None` heisst: abgebrochen."""
        puffer = bytearray()
        while len(puffer) <= _WORKER_MAX_LINE:
            with self._lock:
                if self._cancelled:
                    return None
            rest = deadline - time.monotonic()
            if rest <= 0:
                raise _ApiFailure(TIMED_OUT, "Zeitüberschreitung", KIND_TIMED_OUT)
            # Kurze Scheiben, damit `cancel()` nicht eine ganze Timeout-Laenge haengt.
            verbindung.settimeout(min(rest, 1.0))
            try:
                stueck = verbindung.recv(65536)
            except socket.timeout:
                continue
            except OSError as error:
                with self._lock:
                    if self._cancelled:
                        return None  # `cancel()` hat die Leitung geschlossen
                raise _ApiFailure(
                    NETWORK_FAILED.format(detail=self._scrub(error)), "Netzfehler",
                    KIND_NETWORK_FAILED,
                ) from None
            if not stueck:
                raise _ApiFailure(
                    NETWORK_FAILED.format(
                        detail="model worker closed the connection without an answer"
                    ),
                    "Netzfehler",
                    KIND_NETWORK_FAILED,
                )
            puffer += stueck
            if b"\n" in puffer:
                return bytes(puffer.split(b"\n", 1)[0])
        raise _ApiFailure(
            NETWORK_FAILED.format(detail="model worker answer exceeded the frame limit"),
            "Netzfehler",
            KIND_NETWORK_FAILED,
        )

    def validate(self) -> None:
        """Beweist Schluessel, Modell und Erreichbarkeit, bevor der Router umschaltet."""
        with self._lock:
            if self._active:
                raise RuntimeError("API-Reasoner laeuft bereits")
        try:
            answer = self._run("", f"Answer with exactly {READY_MARKER}.", None)
        except ReasonerFailure as failure:
            # Dieselbe Meldung wie vor der Ausnahme: die Probe lieferte Fehlertext statt
            # des Markers. Der Router liest nur "Validation gescheitert" — der Wortlaut
            # des Grundes bleibt dabei exakt der alte.
            answer = failure.message
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


def _classify(status: int, detail: str) -> tuple[str, str, str]:
    """Vier Ursachen, vier Meldungen, vier Arten. Wer sie zusammenlegt, schickt den
    Betreiber irre — und die Fallback-Kette auf eine falsche Faehrte."""
    if status in (401, 403):
        return KEY_REJECTED.format(status=status), f"HTTP {status}", KIND_KEY_REJECTED
    if status == 429:
        return RATE_LIMITED, "HTTP 429", KIND_RATE_LIMITED
    if status in (500, 502, 503, 529):
        return OVERLOADED.format(status=status), f"HTTP {status}", KIND_OVERLOADED
    return (
        HTTP_FAILED.format(status=status, detail=detail or "unknown"),
        f"HTTP {status}",
        KIND_HTTP_FAILED,
    )


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
