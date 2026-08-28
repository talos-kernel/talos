"""Operator-owned MCP-Server-Registry fuer delegate_code-Jobs.

Warum sie existiert: Talos spricht selbst NIE MCP — das `claude -p`-Kind im
Job tut es. Talos erzeugt nur die MCP-Konfiguration und reicht sie per
`--mcp-config` durch. Welche Server es ueberhaupt geben darf, ist eine
Betreiber-Entscheidung und steht deklarativ in `data/mcp-servers.json`
(neben entities.json: gleiche Konvention, gleiche Fail-closed-Haertung).
Der Socket-Frame eines Jobs traegt nur NAMEN aus dieser Datei, niemals
command/args — die ausfuehrbare Wahrheit liegt ausschliesslich hier und in
der Worker-Env, und ein Modell kann sie nicht waehlen.

Haertung wie beim Entity-Registry (`intelligence.EntityRegistry`): Pflicht-
`version`, fail-closed LEER bei jedem Datei-Problem, harte Byte-/Felddeckel,
ungueltige Eintraege fallen einzeln heraus, der Rest bleibt lesbar. Zwei
zusaetzliche, hier geschaerfte Regeln:

* `command` muss ein ABSOLUTER Pfad sein (oder leer — dann startet npx das
  benannte `package`). Ein relativer Befehl loeste sich gegen den
  Arbeitsbereich des Jobs auf und liesse sich von dort aus tauschen.
* Ein `env`-Feld ist HART verboten und kostet den ganzen Eintrag: der
  MCP-Server erbt exakt das minimierte Job-Env, und eine eigene Env-Tabelle
  waere ein zweiter, ungepruefter Credential-Weg (dieselbe Regel, die
  `browser_mcp_config` von Anfang an trug).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "MAX_ARG_CHARS",
    "MAX_ARGS",
    "MAX_FILE_BYTES",
    "MAX_SERVERS",
    "SERVER_NAME",
    "McpServer",
    "McpServerRegistry",
    "mcp_config",
]

# Die Datei ist deklarativ und klein; was groesser ist, ist keine Konfiguration
# mehr, sondern ein Unfall oder ein Angriff — fail-closed leer.
MAX_FILE_BYTES = 32 * 1024
MAX_SERVERS = 16
MAX_ARGS = 32
MAX_ARG_CHARS = 400
MAX_COMMAND_CHARS = 400
MAX_PACKAGE_CHARS = 128
MAX_DESCRIPTION_CHARS = 400

# Servernamen werden zu Tool-Praefixen (`mcp__<name>`) und Dateibestandteilen —
# ein enger Zeichenvorrat, keine Ueberraschungen.
SERVER_NAME = re.compile(r"^[a-z0-9-]{1,32}$")


@dataclass(frozen=True)
class McpServer:
    """Ein freigeschaltbarer MCP-Server. `command` leer heisst: Start per
    `npx <package>` (der Job hat Netz, aber keinen schreibbaren npm-Cache
    ausserhalb seines wegwerfbaren HOME — wer das Paket nicht pro Job neu
    laden will, traegt eine fest installierte Binary als `command` ein)."""
    name: str
    command: str
    args: tuple[str, ...] = ()
    package: str = ""
    description: str = ""

    def config_entry(self) -> dict[str, Any]:
        """Der Eintrag fuer `claude --mcp-config` — reine Struktur, und
        NIEMALS ein "env"-Schluessel (Credential-Regel, siehe Modul-Kopf)."""
        if self.command:
            return {"command": self.command, "args": list(self.args)}
        return {"command": "npx", "args": [self.package, *self.args]}


def mcp_config(servers: Sequence[McpServer]) -> dict[str, Any]:
    """Die MCP-Konfiguration fuer genau die angefragten Server — nie mehr."""
    return {"mcpServers": {s.name: s.config_entry() for s in servers}}


def _server(item: object) -> McpServer | None:
    """Ein Eintrag oder None. Ungueltig faellt EINZELN heraus — ein kaputter
    Nachbar macht die guten Eintraege nicht unlesbar (entities-Konvention)."""
    if not isinstance(item, dict):
        return None
    if "env" in item:
        # Hart ablehnen, nicht still ueberlesen: wer "env" schrieb, wollte dem
        # Server Werte mitgeben — und genau dieser Weg ist verboten.
        return None
    name = item.get("name")
    if not isinstance(name, str) or not SERVER_NAME.fullmatch(name):
        return None
    command = item.get("command", "")
    if not isinstance(command, str) or len(command) > MAX_COMMAND_CHARS:
        return None
    if command and not os.path.isabs(command):
        return None
    package = item.get("package", "")
    if not isinstance(package, str) or len(package) > MAX_PACKAGE_CHARS:
        return None
    if not command and not package:
        return None  # nichts Startbares — kein Eintrag
    args = item.get("args", [])
    if (not isinstance(args, list) or len(args) > MAX_ARGS
            or not all(isinstance(a, str) and len(a) <= MAX_ARG_CHARS for a in args)):
        return None
    description = item.get("description", "")
    if not isinstance(description, str):
        return None
    return McpServer(
        name=name,
        command=command,
        args=tuple(args),
        package=package,
        description=description[:MAX_DESCRIPTION_CHARS],
    )


class McpServerRegistry:
    """Die gelesene Registry. Namen sind eindeutig; der erste Eintrag gewinnt,
    wie bei den Entitaeten — ein versehentliches Duplikat ueberschreibt nicht."""

    def __init__(self, servers: Sequence[McpServer] = ()) -> None:
        eindeutig: dict[str, McpServer] = {}
        for server in servers[:MAX_SERVERS]:
            eindeutig.setdefault(server.name, server)
        self.servers = tuple(eindeutig.values())
        self._by_name = dict(eindeutig)

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def get(self, name: str) -> McpServer | None:
        return self._by_name.get(name)

    @classmethod
    def from_mapping(cls, payload: object) -> "McpServerRegistry":
        if (not isinstance(payload, Mapping) or payload.get("version") != 1
                or not isinstance(payload.get("servers"), list)):
            return cls()
        return cls(tuple(filter(None, (_server(item) for item in payload["servers"]))))

    @classmethod
    def from_path(cls, path: Path) -> "McpServerRegistry":
        """Fail-closed: jedes Problem — fehlend, zu gross, kaputt — ist eine
        LEERE Registry, nie ein Fehler und nie ein geratener Teilststand."""
        try:
            candidate = Path(path)
            if not candidate.is_file() or candidate.stat().st_size > MAX_FILE_BYTES:
                return cls()
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            return cls.from_mapping(payload)
        except (OSError, UnicodeError, ValueError):
            return cls()
