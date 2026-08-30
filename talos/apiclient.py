"""http_request — der API-Connector: beliebige REST-Endpunkte, durch dieselbe Tuer.

`web_fetch` liest Dokumente (GET, Text, guard_url). Dieses Werkzeug ist der
bewusste naechste Schritt: beliebige Methoden mit Headern und Body — und gerade
WEIL ein POST die Welt draussen veraendert, gilt hier eine andere Vertrauensform
als beim Lesen. Fuenf Regeln, sie sind der Inhalt dieser Datei:

1. **Dieselbe Tuer wie `web_fetch`.** Jede URL — die erste und jede
   Weiterleitungs-Adresse — besteht `web.guard_url` einzeln: kein Loopback, kein
   RFC 1918, kein CGNAT/Tailscale ohne operator-benannte Adresse, nur https
   (http nur auf ausdruecklich benannte Adressen). Der Transport pinnt die
   Verbindung an die gepruefte Adresse (DNS-Rebinding frisst hier nichts).
2. **Lesemethoden laufen, Schreibmethoden fragen.** GET/HEAD/OPTIONS sind die
   Netz-Form von Lesen — der Kernel antwortet ALLOW (`policy._decide_http`).
   POST/PUT/PATCH/DELETE veraendern entfernten Zustand: ausnahmslos
   NEEDS_HUMAN, und die Attended-Auto-Freigabe greift nie (`outward` im
   Manifest — die Wirkung liegt jenseits jeder Einsperrung). Stehende Regeln
   binden an exakt (methode, url) — der Body gehoert bewusst NICHT in den
   Abdruck (die write_file-Bindung: „diese Adresse darfst du schreiben").
3. **Weiterleitungen nur fuer Lesemethoden.** Ein 302 nach einem POST wird
   gemeldet, nicht befolgt — ein blind nachgeschickter Body an eine zweite
   Adresse ist genau der Effekt, den die Freigabe nicht deckte.
4. **Header sind Eingabe, keine Konfiguration.** Das Modell darf Header setzen
   (Authorization eingeschlossen — es kennt nur, was der Betreiber ihm gab),
   aber Host/Content-Length/Connection sind gesperrt (Transport-Hoheit), und
   Anzahl und Laenge sind gedeckelt. Ins Event-Log kommen nur die
   Header-NAMEN, nie die Werte (`executor.audit_args`).
5. **Deckel ueberall.** URL-, Header-, Body- und Antwortgroesse sind begrenzt;
   die Antwort wird als Text gelesen (content-disposition und Dateinamen
   interessieren hier nicht — wer Dateien will, nutzt web_fetch/attachment).

Sprache: Kommentare deutsch, ausgegebene Texte englisch — Ergebnis und
Weigerungen gehen an das Modell und in die Konsole (Haus-Regel).
"""
from __future__ import annotations

import time
from typing import Callable, Mapping

from . import web
from .web import (
    FETCH_TIMEOUT_S,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    MAX_TEXT_CHARS,
    REDIRECT_STATUS,
    SafeUrl,
    UrlRefusedError,
    guard_url,
    parse_allowed_addresses,
)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
METHODS = SAFE_METHODS | MUTATING_METHODS

MAX_HEADERS = 20
MAX_HEADER_CHARS = 500
MAX_BODY_BYTES = 64 * 1024
# Transport-Hoheit: diese Header setzt der Client selbst — ein Modell, das sie
# faelschen koennte, koennte dem Server eine andere Laenge oder Identitaet
# des Requests vortaeuschen.
BLOCKED_HEADERS = frozenset({"host", "content-length", "connection", "transfer-encoding"})


class ApiRequestError(ValueError):
    """Ein Request wird nicht gesendet. Der Text ist der ehrliche Grund."""


def _validated(args: Mapping) -> tuple[str, str, dict[str, str], bytes | None]:
    """Methode, URL, Header und Body pruefen — jede Weigerung vor dem ersten Byte."""
    method = str(args.get("method") or "GET").strip().upper()
    if method not in METHODS:
        raise ApiRequestError(
            f"refused: method {method!r} — one of {', '.join(sorted(METHODS))}"
        )
    url = str(args.get("url") or "").strip()
    if not url:
        raise ApiRequestError("refused: url must be non-empty text")
    raw_headers = args.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ApiRequestError("refused: headers must be a name→value object")
    if len(raw_headers) > MAX_HEADERS:
        raise ApiRequestError(f"refused: more than {MAX_HEADERS} headers")
    headers: dict[str, str] = {}
    for name, value in raw_headers.items():
        key = str(name).strip()
        if not key:
            raise ApiRequestError("refused: empty header name")
        if key.lower() in BLOCKED_HEADERS:
            raise ApiRequestError(
                f"refused: header {key!r} belongs to the transport, not the caller"
            )
        text = str(value)
        if len(text) > MAX_HEADER_CHARS:
            raise ApiRequestError(f"refused: header {key!r} longer than {MAX_HEADER_CHARS}")
        headers[key] = text
    body = args.get("body")
    data: bytes | None = None
    if body is not None:
        if not isinstance(body, str):
            raise ApiRequestError("refused: body must be text")
        data = body.encode("utf-8")
        if len(data) > MAX_BODY_BYTES:
            raise ApiRequestError(f"refused: body longer than {MAX_BODY_BYTES} bytes")
        if method in SAFE_METHODS:
            raise ApiRequestError(f"refused: {method} with a body is not a request this tool sends")
    return method, url, headers, data


def make_http_request_runner(
    *,
    get: web.HttpGet | None = None,
    allow_http: bool = False,
    resolve: web.Resolver | None = None,
    clock: web.Clock = time.monotonic,
    timeout_s: float = FETCH_TIMEOUT_S,
    allowed_addresses: frozenset[str] = frozenset(),
) -> Callable[[object], str]:
    """Baut `http_request` gegen eine Produktionskonfiguration (make_web_runners-Muster)."""
    transport = get or web._requests_get

    def http_request(req: object) -> str:
        args = getattr(req, "args")
        method, url, headers, data = _validated(args)
        requested = guard_url(
            url, allow_http=allow_http, resolve=resolve, allowed_addresses=allowed_addresses
        )
        deadline = clock() + float(timeout_s)
        current = requested
        hops = 0
        while True:
            response = transport(
                current.url,
                method=method,
                headers=headers,
                data=data,
                timeout=web._remaining(deadline, clock),
                stream=True,
                pin=current.addresses,
            )
            try:
                status = int(getattr(response, "status_code", 0))
                if status in REDIRECT_STATUS:
                    if method not in SAFE_METHODS:
                        return (
                            f"HTTP {status} from {current.url} — redirect NOT followed: "
                            "a mutating request is never re-sent to a second address. "
                            "Ask for the new location directly if it is intended."
                        )
                    hops += 1
                    if hops > MAX_REDIRECTS:
                        raise UrlRefusedError(
                            f"more than {MAX_REDIRECTS} redirects starting at {requested.url}"
                        )
                    current = web._next_hop(
                        response, current, allow_http=allow_http, resolve=resolve,
                        allowed_addresses=allowed_addresses,
                    )
                    continue
                content_type = web._header(response, "Content-Type")
                body = web._read_capped(response, MAX_RESPONSE_BYTES, deadline, clock)
            finally:
                response.close()
            text = web._decode(body, content_type)
            clipped = text[:MAX_TEXT_CHARS]
            cut = " […response truncated]" if len(text) > len(clipped) else ""
            kind = content_type.split(";")[0].strip().lower() or "unknown"
            return f"HTTP {status} {method} {current.url} [{kind}]\n{clipped}{cut}"
    return http_request


# Der produktive Runner loest seine Freigabe-Adressen pro Aufruf aus der
# Umgebung — dieselbe Ableitung wie `config.load_config` (TALOS_WEB_ALLOWED_ADDRESSES),
# damit die Verdrahtung in __main__ ihn einfach ersetzen kann (skill_write-Muster).
def http_request(req: object) -> str:
    import os

    runner = make_http_request_runner(
        allow_http=os.environ.get("TALOS_WEB_ALLOW_HTTP", "") == "1",
        allowed_addresses=parse_allowed_addresses(
            os.environ.get("TALOS_WEB_ALLOWED_ADDRESSES", "")
        ),
    )
    return runner(req)
