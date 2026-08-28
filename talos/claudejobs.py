"""Client fuer den Claude-Worker-Socket.

Fail-closed wie der Modell-Worker-Weg in `api_reasoner`: ein unerreichbarer
Worker ist ein benannter Fehler (`unavailable`), NIE ein stiller Rueckfall
darauf, `claude` im Agent-Prozess laufen zu lassen — hier liegen weder die
Sandbox noch das dedizierte Worker-Home. Eine Antwort, die das Protokoll
nicht spricht, ist kaputte Infrastruktur und faellt in denselben Topf.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Callable, Sequence

# Die Naht: Tests injizieren ein Double, das konservierte Antwort-Zeilen
# liefert; Produktion nimmt `_default_exchange` (Socket path, Frame, Frist
# in Sekunden → Antwort-Zeile).
Exchange = Callable[[str, bytes, float], bytes]

# Lesescheiben von hoechstens einer Sekunde, damit die Gesamtfrist ueber ALLEM
# steht — ein Worker, der Bytes tropfelt, knackt sonst jeden pro-recv-Timeout.
READ_SLICE_S = 1.0
# Prompts und Summaries sind groesser als Modell-Frames; der Deckel schuetzt
# den Client gegen einen Worker, der unbegrenzt Bytes kippt.
MAX_LINE = 1 << 20

_KIND_UNAVAILABLE = "unavailable"


def _default_exchange(socket_path: str, frame: bytes, timeout_s: float) -> bytes:
    """Ein Frame raus, eine Zeile rein. Wanduhr ab JETZT, nicht pro recv."""
    deadline = time.monotonic() + timeout_s
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(max(0.1, deadline - time.monotonic()))
        conn.connect(socket_path)
        conn.sendall(frame + b"\n")
        conn.shutdown(socket.SHUT_WR)
        data = b""
        while not data.endswith(b"\n"):
            rest = deadline - time.monotonic()
            if rest <= 0:
                raise TimeoutError("claude worker read deadline")
            conn.settimeout(min(READ_SLICE_S, rest))
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_LINE:
                raise ValueError("claude worker frame too large")
    return data


def _unavailable(detail: str) -> dict:
    return {"ok": False, "kind": _KIND_UNAVAILABLE,
            "message": f"(Claude worker unavailable — {detail[:200]})"}


def _anfrage(socket_path: str, obj: dict, *, timeout_s: float,
             exchange: Exchange | None) -> dict:
    """Schickt einen Frame und liefert die Antwort — wirft NIE.

    Der Runner soll formatieren, nicht retten: Transport- UND Protokollfehler
    kommen von hier bereits als benannter `unavailable`-Frame zurueck.
    """
    tausch = exchange if exchange is not None else _default_exchange
    try:
        roh = tausch(socket_path, json.dumps(obj).encode("utf-8"), timeout_s)
    except (OSError, TimeoutError, ValueError) as fehler:
        return _unavailable(str(fehler))
    try:
        antwort = json.loads(roh)
    except ValueError:
        return _unavailable("unreadable frame")
    # Vertrauen endet am Socket: nur Frames mit echtem ok-Flag sind Antworten.
    if not isinstance(antwort, dict) or not isinstance(antwort.get("ok"), bool):
        return _unavailable("frame without ok flag")
    return antwort


def submit_job(socket_path: str, job_id: str, prompt: str, workspace: str, *,
               timeout_s: float = 30.0, exchange: Exchange | None = None,
               browser_mcp: bool = False,
               mcp_servers: Sequence[str] = ()) -> dict:
    """Meldet einen Job an. Die Antwort ist der Worker-Frame (accepted/busy/…).

    `browser_mcp=True` fordert chrome-devtools-mcp IM Job an — das Flag steht
    nur dann im Frame, wenn es gemeint ist: der Vorgabe-Frame bleibt Byte fuer
    Byte der alte, und der Worker lehnt eine Anforderung ab, die er nicht
    freigeschaltet hat. `mcp_servers` ist die generische Form: eine Liste von
    NAMEN aus der operator-owned Registry (`data/mcp-servers.json`) — der
    Frame traegt niemals command/args, denn die ausfuehrbare Wahrheit gehoert
    dem Worker, nicht der Leitung. Auch diese Liste steht nur im Frame, wenn
    sie nicht leer ist.
    """
    frame = {"op": "submit", "job_id": job_id, "prompt": prompt,
             "workspace": workspace}
    if browser_mcp:
        frame["browser_mcp"] = True
    if mcp_servers:
        frame["mcp_servers"] = list(mcp_servers)
    return _anfrage(socket_path, frame, timeout_s=timeout_s, exchange=exchange)


def job_status(socket_path: str, job_id: str, *, timeout_s: float = 30.0,
               exchange: Exchange | None = None) -> dict:
    """Fragt den Stand eines Jobs ab (running/done/failed/timeout/unknown_job)."""
    return _anfrage(
        socket_path,
        {"op": "status", "job_id": job_id},
        timeout_s=timeout_s, exchange=exchange,
    )
