#!/usr/bin/env python3
"""Winziger Status-Dienst: liefert /status und /tailnet als JSON, sonst nichts.

Lauscht NUR auf Loopback — erreichbar wird er erst über einen Reverse-Proxy, der
allein Tailnet-Adressen durchlässt (z.B. `tailscale serve` oder Caddy mit
`remote_ip 100.64.0.0/10`). Zwei Schranken statt einer: fällt die Proxy-Regel
einmal weg, steht der Dienst trotzdem nicht im offenen Netz.

Jeder Pfad führt genau EIN festes Skript aus und gibt dessen stdout weiter.
Keine Parameter, keine Pfade aus der Anfrage, kein Shell-String — es gibt
nichts zu injizieren. Genau darauf verlässt sich `entity_status`: die Registry
zeigt auf eine operator-owned URL, und hinter der URL steht ein Befehl, den der
Betreiber wörtlich hingeschrieben hat.
"""
from __future__ import annotations

import http.server
import subprocess

SCRIPTS = {
    "/status": "/usr/local/bin/vps-status.sh",
    "/tailnet": "/usr/local/bin/tailnet-status.sh",
}
PORT = 8800
TIMEOUT_S = 10


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "status-source/1"

    def do_GET(self) -> None:  # noqa: N802
        script = SCRIPTS.get(self.path.split("?")[0])
        if script is None:
            self.send_error(404, "only /status or /tailnet")
            return
        try:
            done = subprocess.run(
                ["/bin/bash", script], capture_output=True, text=True,
                timeout=TIMEOUT_S, check=False, shell=False,
            )
            body = (done.stdout or "").strip() or '{"error":"empty output"}'
            code = 200 if done.returncode == 0 else 500
        except (OSError, subprocess.SubprocessError) as error:
            body, code = '{"error":"%s"}' % type(error).__name__, 500
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        """Still. Zugriffe stehen ohnehin im Proxy-Log, doppelt wäre nur Rauschen."""


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
