"""skill_write — der eine, gegatete Schreibweg in die Skills-Ablage.

`skills.py` liest Skills von der Platte, sonst nichts („von der Platte lesen, sonst
nichts"). Dieses Modul ist die bewusste, schmale Ausnahme: der Agent darf einen Skill
NEU anlegen — nie aendern, nie ueberschreiben, und nie ohne den Menschen. Der Grund
steht im Manifest: `skill_write` ist irreversible deklariert, der Kernel antwortet
darum ausnahmslos NEEDS_HUMAN, und unter der UnattendedCeiling wird daraus DENY. Ein
Skill ist die haerteste Persistenz im Haus — sein Text steht ab dem naechsten Zug in
JEDEM Prompt. Koennte sich der Agent unbeaufsichtigt einen Skill schreiben, waere die
Umkehrung des Kernsatzes („das Modell schlaegt vor, der Kernel entscheidet") zur
Dauereinrichtung geworden.

Vier harte Weigerungen, alle fail-closed mit ehrlichem Grund:

  1. **Kein Ueberschreiben.** Existiert die Datei, wird abgelehnt — atomar per
     `O_EXCL`, nicht per vorherigem Blick (ein Blick ist ein Wettlauf). Verbessern
     heisst: der Betreiber loescht, danach darf neu geschrieben werden.
  2. **Kein `allowed-tools`.** Nirgends — weder im Body noch in der Beschreibung,
     die ins Frontmatter wandert. Das Feld ist woertlich eine zweite Erlaubnisquelle
     neben dem Kernel (die Doktrin von `skills.py` und `tests/test_skills.py`); ein
     geschriebener Skill darf sie nicht einmal beantragen.
  3. **Kein Geheimnis.** Was aussieht wie ein Zugangsdatum, wird ABGEWIESEN statt
     unkenntlich gemacht — dieselbe Entscheidung wie `recall.py` (`SecretRefused`):
     Redigieren muss beim ersten Durchgang vollstaendig sein, Abweisen faellt in die
     harmlose Richtung. Geprueft wird mit `recall.looks_secret`, derselben Funktion
     wie beim Langzeitgedaechtnis.
  4. **Kein Ausbruch aus der Wurzel.** Der Name ist ein gepruefter Slug in der Form,
     die der Loader in `skills.py` verlangt — strenger als das Minimum, damit ein
     geschriebener Skill auch ladbar ist — und wird genau EIN Pfadsegment. Lexikalisch
     kann er nicht aus der Wurzel heraus; geprueft wird trotzdem realpath-basiert,
     und ein Symlink als Schreibziel ist gesperrt (vault.py-Bauart).

Der Ort kommt nicht aus den Argumenten: Kernel und Runner rufen dieselbe Ableitung
(`policy.skill_write_path`) — das frame_output_path-Muster. Das Modell nennt einen
Namen, nie einen Pfad.

Sprache: Kommentare deutsch, ausgegebene Texte englisch — Ergebnis und
Ablehnungsgruende gehen an das Modell und in die Konsole (wie in `skills.py`).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from .policy import skill_write_root
from .recall import looks_secret

MAX_NAME_CHARS = 40
MAX_DESCRIPTION_CHARS = 200
MAX_BODY_CHARS = 8_000

# Dieselbe Form wie `_NAME` in `skills.py` (und `_KEBAB_NOTE` in `vault.py`): ein
# Ausdruck statt vier Einzelpruefungen. Bewusst strenger als das geforderte Minimum
# (`^[a-z0-9-]{1,40}$`) — der Loader lehnt Rand- und Doppel-Bindestriche ab, und ein
# Skill, der geschrieben, aber nie geladen wird, waere ein unehrliches Ergebnis.
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ALLOWED_TOOLS = re.compile(r"allowed-tools", re.IGNORECASE)


class SkillWriteError(ValueError):
    """Ein Skill wird nicht geschrieben. Der Text ist der ehrliche Grund."""


def _write(root: Path, args: dict) -> str:
    """Validieren und genau einmal schreiben — jede Weigerung vor dem ersten Byte."""
    name = args.get("name")
    if (
        not isinstance(name, str)
        or len(name) > MAX_NAME_CHARS
        or not _NAME.fullmatch(name)
    ):
        raise SkillWriteError(
            f"refused: name must be 1..{MAX_NAME_CHARS} chars of a-z/0-9 with single "
            "inner hyphens (the form the skills loader accepts)"
        )
    description = args.get("description")
    if not isinstance(description, str):
        raise SkillWriteError("refused: description must be text")
    # Eine Zeile, wie ein Eintrag im Langzeitgedaechtnis: die Beschreibung wandert ins
    # Frontmatter, und ein Zeilenumbruch braeche dort den Rahmen.
    description = " ".join(description.split())
    if not 1 <= len(description) <= MAX_DESCRIPTION_CHARS:
        raise SkillWriteError(
            f"refused: description must be 1..{MAX_DESCRIPTION_CHARS} characters"
        )
    body = args.get("body")
    if not isinstance(body, str) or not body.strip():
        raise SkillWriteError("refused: body must be non-empty markdown text")
    if len(body) > MAX_BODY_CHARS:
        raise SkillWriteError(f"refused: body longer than {MAX_BODY_CHARS} characters")
    if _ALLOWED_TOOLS.search(body) or _ALLOWED_TOOLS.search(description):
        raise SkillWriteError(
            "refused: 'allowed-tools' must never appear in a skill — permissions "
            "come from the kernel alone (tests/test_skills.py doctrine)"
        )
    found = looks_secret(body + " " + description)
    if found is not None:
        raise SkillWriteError(
            f"refused: this looks like a credential ({found}) — a skill on disk is "
            "read into every future prompt, so secrets are never written"
        )

    target = root / name / "SKILL.md"
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise SkillWriteError(f"refused: path escapes the skills root: {target}")
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
    if not text.endswith("\n"):
        text += "\n"
    data = text.encode("utf-8")
    _write_once(target, data)
    return (
        f"Wrote {target} ({len(data)} bytes). Skills are discovered from disk on "
        f"every turn — '{name}' becomes active on the next run, no restart needed. "
        "The file cannot be overwritten; to improve the skill, the operator deletes "
        "it first."
    )


def _write_once(target: Path, data: bytes) -> None:
    """Genau einmal schreiben — O_EXCL macht „existiert schon" atomar zur Weigerung."""
    directory = target.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.resolve(strict=True) != directory:
        raise SkillWriteError("refused: symlink as the write target")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        raise SkillWriteError(
            f"refused: {target} already exists — a skill is written once; "
            "improving means the operator deletes it first"
        ) from None
    except OSError as error:
        raise SkillWriteError(f"refused: cannot write {target}: {error}") from error
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def make_skill_write_runner(root: str | os.PathLike[str]) -> Callable[[object], str]:
    """Baut den Runner gegen eine feste Wurzel — die Naht fuer Tests und Verdrahtung."""
    wurzel = Path(root).expanduser().resolve(strict=False)

    def skill_write(req: object) -> str:
        return _write(wurzel, getattr(req, "args"))

    return skill_write


# Der produktive Runner loest seine Wurzel pro Aufruf ueber `policy.skill_write_root`
# auf — dieselbe Ableitung, ueber deren Ergebnis der Kernel gerade geurteilt hat
# (das grab_frame-Muster: der Runner baut die Regel nicht nach, er ruft sie). Pro
# Aufruf statt beim Import, damit `TALOS_SKILLS_DIRS` wie in `load_config` wirkt.
def skill_write(req: object) -> str:
    return _write(
        Path(skill_write_root()).expanduser().resolve(strict=False),
        getattr(req, "args"),
    )
