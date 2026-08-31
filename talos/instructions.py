"""Live gelesene, begrenzte Operator-Anweisungen fuer jeden Reasoner-Zug."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .identity import FALLBACK_PREAMBLE, SOUL_PATH

INSTALL_DIR = Path(__file__).resolve().parent.parent
AGENTS_PATH = INSTALL_DIR / "AGENTS.md"
USER_PATH = INSTALL_DIR / "USER.md"

# Jede Quelle und auch ihre Summe haben eine harte, sichtbare Grenze. Der Summendeckel
# ist kleiner als drei Einzeldeckel: so bleibt auch bei drei legitimen, grossen Dateien
# Platz fuer Werkzeugprotokoll, Skills und die eigentliche Nachricht.
MAX_SOURCE_CHARS = 8_000
MAX_INSTRUCTION_CONTEXT_CHARS = 16_000

# Antwortformat: der Kanal rendert Telegram-HTML aus Markdown (tgmarkup) — ohne
# diese Zeilen schreibt das Modell Fliesstext, mit ihnen nutzt es die Form, die
# der Kanal kann. Bewusst knapp gehalten: Stilrichtung, kein Korsett.
ANSWER_FORMAT = (
    "\n\nAntwortformat: Gliedere laengere Antworten mit **fetten** Zwischentiteln, "
    "`inline code` fuer Pfade, Befehle und Dateinamen, ```-Codebloecken fuer "
    "mehrzeilige Befehle oder Ausgaben und > fuer Zitate. Setze sparsame, treffende "
    "Emojis als Abschnittsmarken (✅ erledigt, ❌ fehlgeschlagen, ⚠️ Warnung, "
    "🛠 Werkzeug, 📊 Zahlen). Kurze Antworten bleiben schlicht."
)

_cache: dict[str, tuple[bytes, str]] = {}


def _read(path: Path, *, fallback: str | None) -> str | None:
    """Liest operator-owned Text; gleicher Inhalt darf billig aus dem Cache kommen.

    Ein Fingerprint statt nur mtime+Groesse ist Absicht: atomare oder sehr schnelle
    Schreibvorgaenge koennen dieselbe Laenge und denselben sichtbaren Zeitstempel haben.
    Prompt-Aenderungen muessen trotzdem im naechsten Zug wirken.
    """
    try:
        # Bereits der Read ist begrenzt; eine versehentlich kopierte Logdatei darf
        # weder Speicher noch I/O eines jeden Turns aufblasen. TextIO zaehlt hier
        # Zeichen statt Bytes und trennt daher kein UTF-8-Zeichen in der Mitte.
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read(MAX_SOURCE_CHARS + 1)
        overflow = len(raw) > MAX_SOURCE_CHARS
        text = raw.strip()
    except (OSError, UnicodeDecodeError):
        return fallback
    value = text or fallback
    if value is None:
        return None
    value = _truncate(value, MAX_SOURCE_CHARS, path.name, force=overflow)
    fingerprint = hashlib.sha256(value.encode("utf-8")).digest()
    cached = _cache.get(str(path))
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    _cache[str(path)] = (fingerprint, value)
    return value


def _truncate(text: str, limit: int, label: str, *, force: bool = False) -> str:
    marker = f"\n[{label} TRUNCATED]"
    if len(text) <= limit and not force:
        return text
    return text[: max(0, limit - len(marker))] + marker


def load_instruction_context(
    *,
    soul_path: Path = SOUL_PATH,
    agents_path: Path = AGENTS_PATH,
    user_path: Path = USER_PATH,
) -> str:
    """SOUL, AGENTS, USER klar getrennt und in dieser Autoritaetsreihenfolge."""
    sources = (
        ("SOUL.md", _read(soul_path, fallback=FALLBACK_PREAMBLE)),
        ("AGENTS.md", _read(agents_path, fallback=None)),
        ("USER.md", _read(user_path, fallback=None)),
    )
    present = [
        (name, _truncate(text, MAX_SOURCE_CHARS, name))
        for name, text in sources
        if text is not None
    ]
    wrappers = sum(
        len(f"<<< BEGIN {name} >>>\n\n<<< END {name} >>>\n\n") for name, _ in present
    )
    combined_marker = "[COMBINED INSTRUCTION CONTEXT TRUNCATED]\n"
    available = MAX_INSTRUCTION_CONTEXT_CHARS - wrappers
    combined = sum(len(text) for _, text in present) > available
    if combined:
        available -= len(combined_marker)
    quota = max(0, available // len(present)) if present else 0
    sections: list[str] = []
    for name, text in present:
        body = _truncate(text, quota, name) if combined else text
        sections.append(f"<<< BEGIN {name} >>>\n{body}\n<<< END {name} >>>\n")
    result = "\n".join(sections)
    if combined:
        result += combined_marker
    return result[:MAX_INSTRUCTION_CONTEXT_CHARS]


def assemble_system_prompt(
    *,
    tool_protocol: str,
    plan_protocol: str,
    skills: str = "",
    final_protocol: str = "",
    soul_path: Path = SOUL_PATH,
    agents_path: Path = AGENTS_PATH,
    user_path: Path = USER_PATH,
) -> str:
    """Einziger Aufbaupfad fuer CLI- und API-Reasoner."""
    return (
        load_instruction_context(
            soul_path=soul_path, agents_path=agents_path, user_path=user_path
        )
        + tool_protocol
        + plan_protocol
        + skills
        + final_protocol
        + ANSWER_FORMAT
    )
