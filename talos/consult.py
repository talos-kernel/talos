"""Bounded, operator-configured read-only consultation with another agent.

The model controls only the three text fields. Endpoint and credential remain
operator-owned, redirects are not followed, and the returned advice grants no
capability inside Talos.
"""
from __future__ import annotations

import http.client
import json
import time
from typing import Callable
from urllib.parse import urlsplit

from .policy import ToolRequest

MAX_QUESTION_CHARS = 2000
MAX_DETAIL_CHARS = 1000
MAX_RESPONSE_BYTES = 32768
MAX_ANSWER_CHARS = 6000
DEFAULT_TIMEOUT_S = 210.0

Post = Callable[[str, bytes, dict[str, str], float], bytes]


def _clean(value: object, *, field: str, maximum: int, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError(f"agent_consult needs a non-empty '{field}'")
    if len(text) > maximum:
        raise ValueError(f"agent_consult '{field}' exceeds {maximum} characters")
    return text


def _post_no_redirect(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("agent_consult endpoint must be http(s)")
    if parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.path != "/consult":
        raise ValueError("agent_consult endpoint must be an exact /consult URL")
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request("POST", "/consult", body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"agent_consult bridge returned HTTP {response.status}")
        # Der Socket-Timeout gilt PRO Operation: eine Bridge, die die Antwort Byte
        # fuer Byte troepfeln laesst, haelt den Worker-Platz sonst unbegrenzt —
        # jeder einzelne read bliebe unter der Frist. Deshalb eine Gesamtfrist ueber
        # die ganze Antwort, der Rest des Budgets wird vor jedem Stueck neu gesetzt.
        stichtag = time.monotonic() + timeout
        stuecke: list[bytes] = []
        gesamt = 0
        while True:
            rest = stichtag - time.monotonic()
            if rest <= 0:
                raise ValueError("agent_consult bridge exceeded the read deadline")
            if connection.sock is not None:
                connection.sock.settimeout(rest)
            try:
                stueck = response.read(min(16384, MAX_RESPONSE_BYTES + 1 - gesamt))
            except (TimeoutError, OSError) as error:
                raise ValueError("agent_consult bridge read timed out") from error
            if not stueck:
                break
            stuecke.append(stueck)
            gesamt += len(stueck)
            if gesamt > MAX_RESPONSE_BYTES:
                raise ValueError("agent_consult response too large")
        return b"".join(stuecke)
    finally:
        connection.close()


def make_agent_consult_runner(
    endpoint: str,
    token: str,
    *,
    post: Post = _post_no_redirect,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Callable[[ToolRequest], str]:
    """Build a fixed-endpoint consultation runner.

    Advice is bounded text and therefore data only. It never becomes a capability,
    approval, executable command, endpoint, or credential inside Talos.
    """
    endpoint = str(endpoint or "").strip()
    token = str(token or "").strip()

    def consult(req: ToolRequest) -> str:
        if set(req.args) - {"question", "attempted", "failure"}:
            raise ValueError("agent_consult received unsupported arguments")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or parsed.path != "/consult"
        ):
            raise ValueError("agent_consult endpoint is not configured as an exact http(s) /consult URL")
        if not token:
            raise ValueError("agent_consult token is not configured")
        payload = {
            "question": _clean(req.args.get("question"), field="question", maximum=MAX_QUESTION_CHARS, required=True),
            "attempted": _clean(req.args.get("attempted"), field="attempted", maximum=MAX_DETAIL_CHARS),
            "failure": _clean(req.args.get("failure"), field="failure", maximum=MAX_DETAIL_CHARS),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        raw = post(
            endpoint,
            body,
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
            timeout_s,
        )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("agent_consult response too large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("agent_consult returned invalid JSON") from error
        answer = decoded.get("answer") if isinstance(decoded, dict) else None
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("agent_consult returned an empty answer")
        answer = answer.strip()
        if len(answer) > MAX_ANSWER_CHARS:
            raise ValueError("agent_consult response too large")
        return answer

    return consult
