"""Der API-Reasoner ist der einzige Denk-Weg fuer eine oeffentliche Installation.

Die teuersten Fehler stehen zuerst: die Gedankenkette im Chat, der Schluessel in einer
Fehlermeldung, ein deklariertes Werkzeug am Kernel vorbei. Kein Test fasst das Netz an —
alles laeuft ueber die injizierte `http`-Abhaengigkeit.
"""
from __future__ import annotations

import json

import pytest

from talos.api_reasoner import (
    ANTHROPIC_VERSION,
    CANCELLED_TEXT,
    EMPTY_ANSWER,
    ApiReasoner,
    ReasonerFailure,
)
from talos.credentials import CredentialStore, Route
from talos.usage import UsageMeter

KEY = "sk-ant-api03-SUPERGEHEIM-0123456789"
# ⚠️ Bewusst ein ANDERER Wert. Ein Testbestand, in dem beide Anbieter denselben Schluessel
# tragen, kann den Befund vom 05.08. nicht sehen: dort ging der Anthropic-Schluessel an
# OpenAI, und mit identischen Werten stimmt jede Zusicherung trotzdem.
OPENAI_KEY = "sk-proj-OPENAI-NUR-FUER-OPENAI-987654321"


def store(*, anthropic: str = KEY, openai: str = OPENAI_KEY,
          base_url: str = "") -> CredentialStore:
    """Ein Bestand mit beiden Anbietern — so, wie eine echte Installation ihn hat."""
    routen = {}
    if anthropic:
        routen["anthropic-api"] = Route("anthropic-api", anthropic, base_url)
    if openai:
        routen["openai-api"] = Route("openai-api", openai, base_url)
    return CredentialStore(routen)


# --- Doubles ---------------------------------------------------------------------


class FakeResponse:
    """Nur die vier Namen, die `HttpResponse` verlangt."""

    def __init__(self, lines: list[str], status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self._lines = lines
        self.closed = 0
        self.on_line = None  # Callback(index) — erlaubt Abbruch mitten im Strom

    def iter_lines(self, decode_unicode: bool = False):
        for index, line in enumerate(self._lines):
            if self.on_line is not None:
                self.on_line(index)
            yield line

    def close(self) -> None:
        self.closed += 1


class FakeHttp:
    def __init__(self, response: FakeResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, *, headers, json, timeout, stream):  # noqa: A002 — Vertragsname
        self.calls.append(
            {"url": url, "headers": dict(headers), "body": json, "timeout": timeout, "stream": stream}
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    @property
    def body(self) -> dict:
        return self.calls[-1]["body"]


def sse(obj: dict) -> str:
    return "data: " + json.dumps(obj)


# Echte Reihenfolge eines Anthropic-Zuges: erst ein thinking-Block, dann Text.
ANTHROPIC_LINES = [
    "event: message_start",
    sse({"type": "message_start",
         "message": {"model": "claude-opus-5", "usage": {"input_tokens": 12, "output_tokens": 1}}}),
    "",
    ": keep-alive",
    sse({"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": ""}}),
    sse({"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "Er fragt nach dem Speicher …"}}),
    sse({"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "CAISnAIKiAEIEBgC"}}),
    sse({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
    sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Der "}}),
    sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "VPS "}}),
    sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "läuft."}}),
    sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 41}}),
    sse({"type": "message_stop"}),
]

OPENAI_LINES = [
    sse({"model": "deepseek-chat", "choices": [
        {"index": 0, "delta": {"reasoning_content": "Ich ueberlege gerade …"}}]}),
    sse({"model": "deepseek-chat", "choices": [{"index": 0, "delta": {"reasoning": "noch mehr"}}]}),
    sse({"model": "deepseek-chat", "choices": [{"index": 0, "delta": {"content": "Der "}}]}),
    sse({"model": "deepseek-chat", "choices": [{"index": 0, "delta": {"content": "VPS "}}]}),
    sse({"model": "deepseek-chat", "choices": [{"index": 0, "delta": {"content": "läuft."}}]}),
    sse({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 41}}),
    "data: [DONE]",
]


def build(
    lines: list[str] | None = None,
    *,
    provider: str = "anthropic-api",
    status: int = 200,
    text: str = "",
    error: Exception | None = None,
    meter: UsageMeter | None = None,
    skills=None,
) -> tuple[ApiReasoner, FakeHttp, FakeResponse | None]:
    response = None if error is not None else FakeResponse(lines or [], status_code=status, text=text)
    http = FakeHttp(response=response, error=error)
    reasoner = ApiReasoner(
        provider,
        "claude-opus-5" if provider == "anthropic-api" else "deepseek-chat",
        store(),
        timeout_s=30,
        meter=meter,
        skills=skills,
        http=http,
    )
    return reasoner, http, response


# --- Antwort und Streaming --------------------------------------------------------


def test_the_answer_comes_back() -> None:
    reasoner, _http, _response = build(ANTHROPIC_LINES)
    assert reasoner.reason("Wie geht es dem VPS?") == "Der VPS läuft."


def test_deltas_join_exactly_to_the_final_answer() -> None:
    """Die Live-Anzeige darf nichts anderes zeigen als die Endfassung."""
    seen: list[str] = []
    reasoner, _http, _response = build(ANTHROPIC_LINES)
    answer = reasoner.reason("Status?", seen.append)
    assert seen == ["Der ", "VPS ", "läuft."]
    assert "".join(seen) == answer


def test_thinking_never_reaches_the_sink() -> None:
    """Der teuerste Fehler: die Gedankenkette in den Chat des Betreibers streamen."""
    seen: list[str] = []
    reasoner, _http, _response = build(ANTHROPIC_LINES)
    answer = reasoner.reason("Status?", seen.append)
    assert "Er fragt" not in "".join(seen)
    assert "Er fragt" not in answer
    assert "CAISnAIK" not in answer


def test_openai_reasoning_fields_never_reach_the_sink() -> None:
    """`reasoning_content`/`reasoning` sind Denken — dieselbe Grenze wie bei Anthropic."""
    seen: list[str] = []
    reasoner, _http, _response = build(OPENAI_LINES, provider="openai-api")
    answer = reasoner.reason("Status?", seen.append)
    assert seen == ["Der ", "VPS ", "läuft."]
    assert answer == "Der VPS läuft."
    assert "ueberlege" not in answer


def test_a_broken_sink_does_not_cost_the_answer() -> None:
    def explode(_piece: str) -> None:
        raise RuntimeError("Anzeige kaputt")

    reasoner, _http, _response = build(ANTHROPIC_LINES)
    assert reasoner.reason("Status?", explode) == "Der VPS läuft."


def test_a_stream_without_text_is_not_reported_as_an_answer() -> None:
    reasoner, _http, _response = build([sse({"type": "message_stop"})])
    assert reasoner.reason("Status?") == EMPTY_ANSWER


def test_a_provider_error_inside_the_stream_beats_the_text() -> None:
    lines = [
        sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        sse({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "halb"}}),
        sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
    ]
    reasoner, _http, _response = build(lines)
    assert reasoner.reason("Status?") == "(Reasoner error: Overloaded)"


# --- Anfragekoerper: kein Werkzeug, kein Gate-Bypass -------------------------------


def test_no_tool_is_declared_to_the_api() -> None:
    """Anbieter-Werkzeuge saessen VOR dem Policy-Kernel — sie duerfen gar nicht erst existieren."""
    for provider, lines in (("anthropic-api", ANTHROPIC_LINES), ("openai-api", OPENAI_LINES)):
        reasoner, http, _response = build(lines, provider=provider)
        reasoner.reason("Lies /etc/passwd")
        body = http.body
        assert "tools" not in body
        assert "tool_choice" not in body
        assert "tool_use" not in json.dumps(body)


def test_the_prompt_carries_soul_protocol_and_skills() -> None:
    reasoner, http, _response = build(ANTHROPIC_LINES, skills=lambda: "- demo: tut etwas")
    reasoner.reason("Wie heisst du?")
    system = http.body["system"]
    assert "TOOL_CALL" in system
    assert "- demo: tut etwas" in system
    assert http.body["messages"] == [{"role": "user", "content": "Wie heisst du?"}]


def test_a_broken_skill_source_costs_the_catalogue_not_the_turn() -> None:
    def boom() -> str:
        raise RuntimeError("Katalog kaputt")

    reasoner, http, _response = build(ANTHROPIC_LINES, skills=boom)
    assert reasoner.reason("Status?") == "Der VPS läuft."
    assert "TOOL_CALL" in http.body["system"]


def test_anthropic_request_shape() -> None:
    reasoner, http, _response = build(ANTHROPIC_LINES)
    reasoner.reason("Status?")
    call = http.calls[-1]
    assert call["url"].endswith("/v1/messages")
    assert call["headers"]["x-api-key"] == KEY
    assert call["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert call["stream"] is True
    assert http.body["stream"] is True
    assert http.body["model"] == "claude-opus-5"


def test_openai_request_shape() -> None:
    reasoner, http, _response = build(OPENAI_LINES, provider="openai-api")
    reasoner.reason("Status?")
    call = http.calls[-1]
    assert call["url"].endswith("/chat/completions")
    # ⚠️ Der OpenAI-Schluessel, nicht "irgendein Schluessel". Bis 05.08. stand hier der
    # Anthropic-Wert — die Zusicherung war gruen, weil der Test nur einen Wert kannte.
    assert call["headers"]["authorization"] == f"Bearer {OPENAI_KEY}"
    assert [m["role"] for m in http.body["messages"]] == ["system", "user"]


def test_a_custom_base_url_is_used() -> None:
    http = FakeHttp(response=FakeResponse(OPENAI_LINES))
    reasoner = ApiReasoner(
        "openai-api", "qwen3", store(base_url="https://hydra.example/v1/"), timeout_s=30, http=http
    )
    reasoner.reason("Status?")
    assert http.calls[-1]["url"] == "https://hydra.example/v1/chat/completions"


# --- Fehler ehrlich unterscheiden --------------------------------------------------


def test_four_causes_give_four_different_messages() -> None:
    """Ein Netzfehler darf nie wie ein falscher Schluessel aussehen."""
    bad_key, _http, _r = build([], status=401, text='{"error":{"message":"invalid x-api-key"}}')
    limited, _http, _r = build([], status=429, text="rate limit")
    overloaded, _http, _r = build([], status=529, text="overloaded")
    offline, _http, _r = build(error=OSError("Name or service not known"))

    answers = [
        bad_key.reason("x"),
        limited.reason("x"),
        overloaded.reason("x"),
        offline.reason("x"),
    ]
    assert len(set(answers)) == 4
    assert "401" in answers[0] and "key" in answers[0].lower()
    assert "429" in answers[1]
    assert "529" in answers[2]
    assert "network" in answers[3].lower()
    assert "key" not in answers[3].lower()


def test_an_unexpected_status_is_reported_with_its_code() -> None:
    reasoner, _http, _response = build([], status=418, text="teapot")
    assert "418" in reasoner.reason("x")


def test_a_dripping_stream_hits_the_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """`timeout` einer HTTP-Bibliothek ist eine Pause-zwischen-Paketen — die Wanduhr nicht."""

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            self.now += 0.5
            return self.now

        def time(self) -> float:
            return 1_700_000_000.0

    monkeypatch.setattr("talos.api_reasoner.time", Clock())
    http = FakeHttp(response=FakeResponse(ANTHROPIC_LINES))
    reasoner = ApiReasoner("anthropic-api", "claude-opus-5", store(), timeout_s=1, http=http)
    assert "Timed out" in reasoner.reason("Status?")


def test_a_stream_that_dies_midway_is_a_network_failure() -> None:
    class Dying(FakeResponse):
        def iter_lines(self, decode_unicode: bool = False):
            yield ANTHROPIC_LINES[1]
            raise ConnectionResetError("peer went away")

    http = FakeHttp(response=Dying([]))
    reasoner = ApiReasoner("anthropic-api", "claude-opus-5", store(), timeout_s=30, http=http)
    assert "network" in reasoner.reason("x").lower()


# --- Der Schluessel taucht nirgends auf --------------------------------------------


def test_the_key_never_appears_in_any_output() -> None:
    """Der Server spiegelt den Header zurueck, die Bibliothek zitiert ihn — beides faellt raus."""
    echoed = f'{{"error":{{"message":"invalid api key: {KEY} for https://api.anthropic.com"}}}}'
    reasoner, http, _response = build([], status=400, text=echoed)
    answer = reasoner.reason("Status?")
    # Der Schluessel geht wirklich raus — sonst wuerde der Test nichts beweisen.
    assert http.calls[-1]["headers"]["x-api-key"] == KEY
    assert KEY not in answer
    assert "SUPERGEHEIM" not in answer


def test_the_key_never_appears_when_the_transport_raises() -> None:
    boom = OSError(f"connection failed, sent header x-api-key: {KEY}")
    reasoner, _http, _response = build(error=boom)
    answer = reasoner.reason("Status?")
    assert KEY not in answer
    assert "SUPERGEHEIM" not in answer


def test_the_key_never_appears_in_a_raised_exception() -> None:
    """`validate()` wirft — auch dieser Text geht ins Log."""
    reasoner, _http, _response = build([], status=401, text=f"bad key {KEY}")
    with pytest.raises(RuntimeError) as caught:
        reasoner.validate()
    assert KEY not in str(caught.value)


def test_the_key_never_appears_in_the_usage_note() -> None:
    meter = UsageMeter()
    reasoner, _http, _response = build(error=OSError(f"Bearer {KEY}"), meter=meter)
    reasoner.reason("Status?")
    last = meter.snapshot().last
    assert last is not None
    assert KEY not in last.note


# --- Abbruch ----------------------------------------------------------------------


def test_cancel_stops_a_running_turn() -> None:
    seen: list[str] = []
    reasoner, _http, response = build(ANTHROPIC_LINES)
    assert response is not None
    first_text_delta = ANTHROPIC_LINES.index(
        sse({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "VPS "}})
    )
    response.on_line = lambda index: reasoner.cancel() if index == first_text_delta else None

    answer = reasoner.reason("Status?", seen.append)
    assert answer == CANCELLED_TEXT
    assert seen == ["Der "]  # was nach dem Abbruch kam, wurde nie angezeigt
    assert response.closed >= 1


def test_cancel_without_a_running_turn_is_false() -> None:
    reasoner, _http, _response = build(ANTHROPIC_LINES)
    assert reasoner.cancel() is False


def test_a_cancelled_turn_is_still_counted() -> None:
    """Fehlversuche zaehlen mit — sonst verschwindet, wonach man sucht, wenn es klemmt."""
    meter = UsageMeter()
    reasoner, _http, response = build(ANTHROPIC_LINES, meter=meter)
    assert response is not None
    response.on_line = lambda index: reasoner.cancel() if index == 0 else None
    reasoner.reason("Status?")
    snapshot = meter.snapshot()
    assert snapshot.runs == 1
    assert snapshot.failed == 1


# --- Verbrauch ---------------------------------------------------------------------


def test_usage_is_measured_not_claimed() -> None:
    meter = UsageMeter()
    reasoner, _http, _response = build(ANTHROPIC_LINES, meter=meter)
    reasoner.reason("Status?")
    last = meter.snapshot().last
    assert last is not None
    assert last.ok is True
    assert last.model == "claude-opus-5"
    assert last.input_tokens == 12
    assert last.output_tokens == 41
    assert last.duration_s >= 0.0
    assert last.cost_usd == 0.0  # nie geraten


def test_openai_usage_lands_in_the_meter() -> None:
    meter = UsageMeter()
    reasoner, _http, _response = build(OPENAI_LINES, provider="openai-api", meter=meter)
    reasoner.reason("Status?")
    last = meter.snapshot().last
    assert last is not None
    assert (last.input_tokens, last.output_tokens) == (12, 41)


def test_without_a_meter_the_reasoner_still_works() -> None:
    reasoner, _http, _response = build(ANTHROPIC_LINES)
    assert reasoner.reason("Status?") == "Der VPS läuft."


# --- Konstruktion und Probe --------------------------------------------------------


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError):
        ApiReasoner("gibts-nicht", "qwen3", store(), timeout_s=30, http=FakeHttp())


def test_a_missing_key_is_refused_before_anything_is_sent() -> None:
    with pytest.raises(ValueError):
        ApiReasoner("anthropic-api", "claude-opus-5", store(anthropic=""),
                    timeout_s=30, http=FakeHttp())


def test_validate_accepts_the_ready_marker() -> None:
    lines = [
        sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        sse({"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "TALOS_READY"}}),
    ]
    reasoner, http, _response = build(lines)
    reasoner.validate()
    assert "system" not in http.body  # die Probe braucht die Persona nicht


def test_validate_rejects_a_reply_without_the_marker() -> None:
    reasoner, _http, _response = build(ANTHROPIC_LINES)
    with pytest.raises(RuntimeError):
        reasoner.validate()


# --- Die Katalog-Anbieter: ollama, nvidia-nim, kimi ---------------------------------


def test_ollama_builds_without_a_key_and_sends_no_authorization_header() -> None:
    """Ein lokaler Anbieter hat keinen Schluessel — und bekommt deshalb keinen Header.

    Ein leerer `Bearer` waere kein neutraler Zustand: manche Server lehnen genau ihn ab,
    und ein mitgeschicktes Leer-Geheimnis ist eins zu viel.
    """
    bestand = CredentialStore({"ollama": Route("ollama", "", "http://localhost:11434/v1")})
    http = FakeHttp(response=FakeResponse(OPENAI_LINES))
    reasoner = ApiReasoner("ollama", "qwen3:27b", bestand, timeout_s=30, http=http)
    assert reasoner.reason("Status?") == "Der VPS läuft."
    call = http.calls[-1]
    assert call["url"] == "http://localhost:11434/v1/chat/completions"
    assert "authorization" not in call["headers"]
    assert http.body["model"] == "qwen3:27b"


def test_ollama_falls_back_to_the_catalog_address_when_the_route_has_none() -> None:
    """Ein handgebauter Bestand ohne Adresse landet beim Katalog, nicht bei OpenAI."""
    bestand = CredentialStore({"ollama": Route("ollama", "", "")})
    http = FakeHttp(response=FakeResponse(OPENAI_LINES))
    ApiReasoner("ollama", "qwen3:27b", bestand, timeout_s=30, http=http).reason("x")
    assert http.calls[-1]["url"] == "http://localhost:11434/v1/chat/completions"


def test_kimi_and_nvidia_build_with_their_own_keys() -> None:
    for slug, key, base in (
        ("kimi", "kimi-eigener-key-123", "https://api.kimi.com/coding/v1"),
        ("nvidia-nim", "nvapi-eigener-key-456", "https://integrate.api.nvidia.com/v1"),
    ):
        bestand = CredentialStore({slug: Route(slug, key, base)})
        http = FakeHttp(response=FakeResponse(OPENAI_LINES))
        reasoner = ApiReasoner(slug, "irgendein-modell", bestand, timeout_s=30, http=http)
        assert reasoner.reason("x") == "Der VPS läuft."
        assert http.calls[-1]["url"] == f"{base}/chat/completions"
        assert http.calls[-1]["headers"]["authorization"] == f"Bearer {key}"


def test_their_base_url_is_overridable_per_provider_variable() -> None:
    """Dieselbe Konvention wie ueberall: `TALOS_BASE_URL_<PROVIDER>` schlaegt den Katalog."""
    bestand = CredentialStore({
        "kimi": Route("kimi", "kimi-eigener-key-123", "https://gateway.example/kimi/"),
    })
    http = FakeHttp(response=FakeResponse(OPENAI_LINES))
    ApiReasoner("kimi", "k2", bestand, timeout_s=30, http=http).reason("x")
    assert http.calls[-1]["url"] == "https://gateway.example/kimi/chat/completions"


def test_a_keyed_catalog_provider_without_a_key_stays_fail_closed() -> None:
    with pytest.raises(ValueError):
        ApiReasoner("kimi", "k2", store(), timeout_s=30, http=FakeHttp())


# --- Strukturierte Fehler: dieselbe Ursache, als Ausnahme -----------------------------


def test_reason_strict_raises_the_classified_failure_reason_returns_its_text() -> None:
    """`reason()` bleibt wortgleich; `reason_strict()` traegt die Art derselben Ursache."""
    reasoner, _http, _r = build([], status=429, text="rate limit")
    with pytest.raises(ReasonerFailure) as caught:
        reasoner.reason_strict("x")
    assert caught.value.kind == "rate_limited"
    assert reasoner.reason("x") == caught.value.message


def test_every_classified_failure_carries_its_kind() -> None:
    for status, kind in ((401, "key_rejected"), (429, "rate_limited"),
                         (503, "overloaded"), (418, "http_failed")):
        reasoner, _h, _r = build([], status=status, text="x")
        with pytest.raises(ReasonerFailure) as caught:
            reasoner.reason_strict("x")
        assert caught.value.kind == kind, status
    offline, _h, _r = build(error=OSError("Name or service not known"))
    with pytest.raises(ReasonerFailure) as caught:
        offline.reason_strict("x")
    assert caught.value.kind == "network_failed"


def test_a_failure_text_never_carries_the_key_along_the_exception_path() -> None:
    reasoner, _http, _r = build([], status=400, text=f"invalid api key: {KEY}")
    with pytest.raises(ReasonerFailure) as caught:
        reasoner.reason_strict("Status?")
    assert KEY not in str(caught.value)
    assert "SUPERGEHEIM" not in caught.value.message
