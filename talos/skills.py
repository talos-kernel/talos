"""Agent Skills — fremder Anweisungstext, gelesen ohne die Herrschaft abzugeben.

Ein Skill ist ein Verzeichnis mit einer `SKILL.md`: YAML-Frontmatter (Name, Beschreibung)
und darunter Markdown-Anweisungen; daneben duerfen `scripts/`, `references/`, `assets/`
liegen. Der offene Standard dahinter ist agentskills.io/specification. Dieses Modul
implementiert davon genau den Teil, den ein Waechter verantworten kann: **von der Platte
lesen, sonst nichts.**

Warum das Modul so misstrauisch ist: Talos' Kernsatz lautet „das Modell schlaegt vor, der
Kernel entscheidet". Ein Skill dreht diesen Satz absichtlich um — er ist Text von Fremden,
der in den Prompt wandert und dem Modell sagt, was es tun soll. Tragbar ist das nur unter
fuenf Bedingungen; sie sind der eigentliche Inhalt dieser Datei:

1. **`allowed-tools` wird gelesen, aber NIE befolgt.** Das Feld ist woertlich eine zweite
   Erlaubnisquelle neben dem Kernel — genau das, was `CLAUDE.md` verbietet. Es landet
   ausschliesslich als `Skill.requested_tools` auf dem Datenobjekt, damit `/skills` dem
   Betreiber zeigen kann, was ein Skill *gerne haette*. Es fliesst in keine Freigabe, in
   keine Vorab-Genehmigung und in keine Tool-Auswahl; `render()` gibt es bewusst nicht in
   den Prompt, sonst laese das Modell dort „vorgenehmigt: run_shell" und bekaeme Ideen.
   In diesem Modul gibt es deshalb keinen einzigen oeffentlichen Namen, der eine Erlaubnis
   verspricht — ein Test haelt das fest.
2. **Kein Ausbruch aus dem Wurzelverzeichnis.** Weder `name` noch ein Symlink duerfen zu
   einem Pfad ausserhalb der konfigurierten Wurzel fuehren. Geprueft wird realpath-basiert
   (`_contained`), fail-closed. Und der Name eines Skills wird nie zu einem Pfad: er ist
   nur ein Schluessel in ein bereits geprueftes Verzeichnis (`SkillCatalog.get`).
3. **Deckel gegen Kontextfrass.** Katalogzeile, Katalog gesamt und Body sind begrenzt
   (dieselbe Logik wie `MAX_SOUL_CHARS` in `identity.py`). Ein Skill, der den Deckel
   reisst, wird beschnitten und sagt das — er wird nicht stillschweigend verschluckt.
4. **Ein kaputter Skill darf nie den Start verhindern.** Ungueltiges Frontmatter, fehlende
   Pflichtfelder, Namensverstoss, unlesbare Datei: dieser eine Skill faellt raus, mit
   nachvollziehbarem Grund in `SkillCatalog.rejected`; alle anderen laufen weiter.
5. **Kein Netz, kein Download, keine Installation.** Gelesen wird nur ein Verzeichnis, das
   der Nutzer schon hat. Fremde Skills haben eigene Autoren und eigene Lizenzen —
   mitliefern oder nachladen waere die einzige rechtlich heikle Variante.

Progressive Disclosure wie in der Spec: beim Start nur `name` + `description` (der
Katalog), der Body erst bei Aktivierung, `scripts/`/`references/` ueberhaupt nicht durch
dieses Modul — die liest das Modell bei Bedarf ueber normale Tools, und damit durch den
Kernel. Das ist der Punkt: der Umweg ueber die Gate-Kette bleibt erhalten.

Sprache: Kommentare deutsch, alle ausgegebenen Texte englisch — Katalog und Body gehen in
den Prompt, die Ablehnungsgruende in die Maschinenkonsole (`/skills`), und die bleibt laut
`CLAUDE.md` englisch.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SKILL_FILENAME = "SKILL.md"

# Grenzen aus der Spec, wo sie eine nennt.
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1_024
MAX_COMPATIBILITY_CHARS = 500

# Grenzen, die dieses Modul zusaetzlich zieht — die Spec empfiehlt <5000 Tokens fuer den
# Body; als Zeichen gerechnet sind das grosszuegige 20k. Der Rest schuetzt davor, dass ein
# einzelnes Verzeichnis den Prompt oder den Start auffrisst.
MAX_SKILL_FILE_BYTES = 256 * 1024
MAX_FRONTMATTER_LINES = 200
MAX_BODY_CHARS = 20_000
MAX_CATALOG_DESCRIPTION_CHARS = 200
MAX_CATALOG_CHARS = 4_000
MAX_SKILLS = 500

BODY_TRUNCATED = "\n…[skill truncated]"
DESCRIPTION_TRUNCATED = "…"

# Vorschlag fuer die Verdrahtung; die Wurzel gehoert in die Config, nicht hierher.
DEFAULT_SKILLS_DIR = Path.home() / ".talos" / "skills"

# Spec: 1..64 Zeichen aus a-z, 0-9, `-`, nicht am Rand und nie doppelt. Dieselbe Form wie
# `_KEBAB_NOTE` in `vault.py` — ein Ausdruck statt vier Einzelpruefungen.
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SCALAR = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
_NESTED = re.compile(r"^[ \t]+([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
# Block-Skalare (`|`, `>`, jeweils auch mit `-`/`+`) sind gaengige YAML-Schreibweise fuer
# lange Beschreibungen und in echten Skills sehr haeufig. Der Wert steht dann in den
# folgenden, staerker eingerueckten Zeilen.
_BLOCK_START = ("|", ">")
# Flow-Collections und Anker/Alias kann dieser Parser nicht — und soll er auch nicht.
# Steht so etwas in einem PFLICHTFELD, faellt der Skill; in einem optionalen Feld wird es
# uebersprungen. Ein Skill wegen seines dekorativen `metadata`-Blocks wegzuwerfen war der
# Grund, warum von 216 echten Skills 132 durchfielen — die Strenge traf die Falschen.
_UNSUPPORTED_VALUE_START = ("[", "{", "&", "*")
_REQUIRED_FIELDS = ("name", "description")

RootLike = str | os.PathLike[str]


class SkillError(ValueError):
    """Ein Skill ist unbrauchbar. Der Text ist der Grund, den `/skills` zeigt."""


@dataclass(frozen=True)
class Skill:
    """Ein gueltiger Skill. Unveraenderlich; der Body kommt erst auf Zuruf von der Platte."""

    name: str
    description: str
    path: Path
    license: str = ""
    compatibility: str = ""
    # Der rohe `allowed-tools`-Wert. Ausdruecklich UNBEFOLGT: nur Anzeige fuer den
    # Betreiber. Wer diesen Wert je in eine Freigabe einspeist, hebt den Kernel auf.
    requested_tools: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    def load_body(self, *, max_chars: int = MAX_BODY_CHARS) -> str | None:
        """Die Anweisungen unter dem Frontmatter, begrenzt. Datei weg oder kaputt -> None."""
        body = _body_from_disk(self.path)
        return None if body is None else _bounded(body, max_chars, BODY_TRUNCATED)

    def catalog_line(self, *, max_description: int = MAX_CATALOG_DESCRIPTION_CHARS) -> str:
        """Eine Zeile fuer den Systemprompt — Name und Beschreibung, sonst nichts."""
        return f"- {self.name} — {_bounded(self.description, max_description, DESCRIPTION_TRUNCATED)}"


@dataclass(frozen=True)
class SkillRejection:
    """Warum ein Verzeichnis kein Skill wurde. Fuer ein spaeteres `/skills`."""

    path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


@dataclass(frozen=True)
class SkillCatalog:
    """Das Ergebnis einer Entdeckungsrunde: was gilt, und was warum nicht."""

    skills: tuple[Skill, ...] = ()
    rejected: tuple[SkillRejection, ...] = ()

    def get(self, name: object) -> Skill | None:
        """Skill zum Namen. `name` kommt womoeglich vom Modell — darum `object`, nie ein Pfad."""
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def body(self, name: object, *, max_chars: int = MAX_BODY_CHARS) -> str | None:
        """Body genau eines Skills. Unbekannter Name -> `None`, kein Fehler."""
        skill = self.get(name)
        return None if skill is None else skill.load_body(max_chars=max_chars)

    def render(self, *, max_chars: int = MAX_CATALOG_CHARS, query: str = "") -> str:
        """Der Katalog fuer den Systemprompt, mit ehrlichem Hinweis beim Abschneiden.

        Absteigend probiert statt gierig gefuellt, weil der Hinweis selbst Platz kostet:
        so bleibt die Ausgabe garantiert unter dem Deckel — inklusive Hinweis.
        """
        # Match the current request BEFORE applying the context cap. Alphabetical
        # truncation otherwise makes later skills permanently invisible.
        terms = set(re.findall(r"\w{3,}", query.casefold()[-6000:]))
        def score(skill: Skill) -> int:
            name = set(re.findall(r"\w{3,}", skill.name.casefold()))
            description = set(re.findall(r"\w{3,}", skill.description.casefold()))
            return 10 * len(name & terms) + len(description & terms)
        ordered = sorted(self.skills, key=lambda skill: (-score(skill), skill.name)) if terms else self.skills
        lines = tuple(f"{skill.catalog_line()}\n  path: {skill.path}" for skill in ordered)
        for kept in range(len(lines), -1, -1):
            text = _catalog_text(lines, kept, max_chars)
            if len(text) <= max_chars:
                return text
        # Selbst der Hinweis passt nicht mehr. Dann lieber gar kein Katalog als ein
        # halber: ein leerer Katalog heisst „keine Skills" und ist damit harmlos.
        return ""


class SkillSource:
    """Live discovery with request-based ranking; no skill body is loaded eagerly."""

    def __init__(self, roots: Iterable[RootLike]) -> None:
        self.roots = tuple(roots)

    def __call__(self) -> str:
        return discover_skills(self.roots).render()

    def for_prompt(self, prompt: str) -> str:
        request = prompt.split("[Tool results so far]", 1)[0]
        return discover_skills(self.roots).render(query=request)


def discover_skills(
    roots: RootLike | Iterable[RootLike],
    *,
    max_skills: int = MAX_SKILLS,
) -> SkillCatalog:
    """Alle gueltigen Skills unter einer oder mehreren Wurzeln, stabil sortiert.

    Doppelte Namen loest die Reihenfolge der Wurzeln auf — die erste gewinnt, die zweite
    wird als Ablehnung protokolliert. Fehlende Wurzeln sind der Normalfall (die meisten
    Installationen haben gar keine Skills) und darum still.
    """
    accepted: dict[str, Skill] = {}
    rejected: list[SkillRejection] = []
    for root in _roots(roots):
        if not root.is_dir():
            continue
        try:
            entries = _skill_dirs(root)
        except OSError as error:
            rejected.append(SkillRejection(root, f"skills root not readable: {error}"))
            continue
        for entry in entries:
            _admit(root, entry, accepted, rejected, max_skills)
    ordered = tuple(sorted(accepted.values(), key=lambda skill: skill.name))
    return SkillCatalog(skills=ordered, rejected=tuple(rejected))


def _admit(
    root: Path,
    entry: Path,
    accepted: dict[str, Skill],
    rejected: list[SkillRejection],
    max_skills: int,
) -> None:
    """Einen Kandidaten pruefen und einsortieren. Jeder Fehlschlag bekommt einen Grund."""
    if len(accepted) >= max_skills:
        rejected.append(SkillRejection(entry, f"skill limit of {max_skills} reached"))
        return
    try:
        skill = _load_skill(root, entry)
    except SkillError as error:
        rejected.append(SkillRejection(entry, str(error)))
        return
    first = accepted.get(skill.name)
    if first is not None:
        rejected.append(
            SkillRejection(entry, f"duplicate name '{skill.name}', already loaded from {first.path}")
        )
        return
    accepted[skill.name] = skill


def _roots(roots: RootLike | Iterable[RootLike]) -> tuple[Path, ...]:
    """Wurzeln kanonisieren und entdoppeln — dieselbe Wurzel zweimal ergaebe Scheinduplikate."""
    candidates = [roots] if isinstance(roots, (str, os.PathLike)) else list(roots)
    unique: list[Path] = []
    for raw in candidates:
        root = Path(raw).expanduser().resolve(strict=False)
        if root not in unique:
            unique.append(root)
    return tuple(unique)


def _skill_dirs(root: Path) -> tuple[Path, ...]:
    """Kandidaten unter einer Wurzel: sichtbare Eintraege mit einer `SKILL.md`, nach Namen."""
    children = sorted(root.iterdir(), key=lambda child: child.name)
    return tuple(
        child
        for child in children
        if not child.name.startswith(".") and (child / SKILL_FILENAME).is_file()
    )


def _contained(root: Path, candidate: Path) -> Path:
    """realpath des Ziels — oder Abbruch, wenn es aus der Wurzel herausfuehrt.

    Realpath-basiert, weil ein Symlink lexikalisch harmlos aussieht und trotzdem nach
    `/etc` zeigen kann. `root` ist bereits aufgeloest (siehe `_roots`).
    """
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise SkillError(f"path escapes the skills root: {candidate}")
    return resolved


def _load_skill(root: Path, directory: Path) -> Skill:
    """Ein Verzeichnis zu einem Skill machen — oder mit Grund ablehnen."""
    _contained(root, directory)
    skill_file = _contained(root, directory / SKILL_FILENAME)
    head, _ = _split_frontmatter(_read_capped(skill_file))
    fields, metadata = _parse_frontmatter(head)
    # Geprueft wird gegen den Namen des Eintrags, den der Betreiber sieht — nicht gegen
    # das Symlink-Ziel. Ein Symlink auf ein anderes Skill-Verzeichnis faellt damit
    # automatisch durch die Namensregel.
    name, description = _validated_identity(fields, directory.name)
    compatibility = fields.get("compatibility", "")
    if len(compatibility) > MAX_COMPATIBILITY_CHARS:
        raise SkillError(f"compatibility longer than {MAX_COMPATIBILITY_CHARS} characters")
    return Skill(
        name=name,
        description=description,
        path=skill_file,
        license=fields.get("license", ""),
        compatibility=compatibility,
        requested_tools=fields.get("allowed-tools", ""),
        metadata=metadata,
    )


def _validated_identity(fields: dict[str, str], directory_name: str) -> tuple[str, str]:
    """Die beiden Pflichtfelder, streng nach Spec geprueft."""
    name = fields.get("name", "")
    if not name:
        raise SkillError("required field 'name' is missing or empty")
    if len(name) > MAX_NAME_CHARS or not _NAME.fullmatch(name):
        raise SkillError(
            f"invalid skill name {name!r} "
            f"(1..{MAX_NAME_CHARS} chars, a-z/0-9 and single inner hyphens only)"
        )
    if name != directory_name:
        raise SkillError(f"name {name!r} does not match its directory {directory_name!r}")
    description = fields.get("description", "")
    if not description:
        raise SkillError("required field 'description' is missing or empty")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillError(f"description longer than {MAX_DESCRIPTION_CHARS} characters")
    return name, description


def _read_capped(path: Path) -> str:
    """SKILL.md lesen, aber nur wenn sie eine gewoehnliche Datei in vernuenftiger Groesse ist."""
    try:
        info = path.stat()
    except OSError as error:
        raise SkillError(f"{SKILL_FILENAME} not readable: {error}") from None
    if not stat.S_ISREG(info.st_mode):
        raise SkillError(f"{SKILL_FILENAME} is not a regular file")
    if info.st_size > MAX_SKILL_FILE_BYTES:
        raise SkillError(f"{SKILL_FILENAME} larger than {MAX_SKILL_FILE_BYTES} bytes")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillError(f"{SKILL_FILENAME} not readable as UTF-8: {error}") from None


def _split_frontmatter(text: str) -> tuple[tuple[str, ...], str]:
    """In Frontmatter-Zeilen und Body trennen. Ohne sauberen Rahmen kein Skill."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"frontmatter missing ({SKILL_FILENAME} must start with '---')")
    limit = min(len(lines), MAX_FRONTMATTER_LINES + 1)
    for index in range(1, limit):
        if lines[index].strip() in {"---", "..."}:
            return tuple(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillError(f"frontmatter not closed within {MAX_FRONTMATTER_LINES} lines")


def _parse_frontmatter(lines: tuple[str, ...]) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """Ein bewusst enger YAML-Ausschnitt: flache `key: value`-Paare, `metadata` eine Ebene tief.

    Eng, weil PyYAML nicht in `requirements.txt` steht und eine neue Abhaengigkeit fuer
    Fremdtext die falsche Richtung waere — ein voller YAML-Parser bringt Tags, Aliase und
    Billion-Laughs mit, und nichts davon braucht die Spec: ihr Frontmatter ist eine flache
    String-Map plus eine einstufige `metadata`-Map. Was dieser Parser nicht sicher versteht,
    lehnt er ab, statt so zu tun, als haette er es verstanden (fail-closed pro Skill —
    andere Skills bleiben davon unberuehrt).

    `metadata` wird flach eingesammelt: die Tiefe wird nicht mitgezaehlt, weil `metadata`
    rein dekorativ ist und in keine Entscheidung eingeht.
    """
    fields: dict[str, str] = {}
    metadata: list[tuple[str, str]] = []
    current = ""
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        nested = _NESTED.match(raw)
        if nested is not None and current == "metadata":
            # `metadata` ist rein dekorativ und geht in keine Entscheidung ein. Ein Eintrag,
            # den dieser Parser nicht versteht, wird uebersprungen — nicht der Skill.
            try:
                metadata.append((nested.group(1), _scalar(nested.group(2))))
            except SkillError:
                pass
            continue
        if raw.lstrip().startswith("- ") and current:
            continue   # Listenform eines Feldes, das Talos nicht auswertet
        match = _SCALAR.match(raw)
        if match is None or (nested is not None and current in fields):
            # Kein neues Feld, sondern die Fortsetzung des vorigen: YAML erlaubt einen
            # Wert ueber mehrere eingerueckte Zeilen, und genau so schreiben echte Skills
            # ihre langen Beschreibungen. Frueher flog der Skill hier raus.
            if current in fields:
                fields[current] = " ".join((fields[current] + " " + raw.strip()).split())
                continue
            raise SkillError(f"unsupported frontmatter line: {raw.strip()[:60]!r}")
        current = match.group(1).casefold()
        if current in fields:
            raise SkillError(f"duplicate frontmatter field: {current}")
        value = (match.group(2) or "").strip()
        if value.startswith(_BLOCK_START):
            fields[current] = ""   # der Inhalt kommt aus den Folgezeilen
            continue
        if current == "metadata":
            continue               # die Unterzeilen sammelt der `_NESTED`-Zweig
        try:
            fields[current] = _scalar(value)
        except SkillError:
            # Ein Pflichtfeld muss lesbar sein. Ein optionales darf unverstaendlich sein,
            # ohne den ganzen Skill mitzunehmen — Talos trifft damit keine Entscheidung.
            if current in _REQUIRED_FIELDS:
                raise
            current = ""
    return fields, tuple(metadata)


def _scalar(raw: str | None) -> str:
    """Ein einzeiliger Wert. Anfuehrungszeichen fallen weg, Whitespace wird eingeebnet."""
    value = (raw or "").strip()
    if value.startswith(_UNSUPPORTED_VALUE_START):
        raise SkillError(f"unsupported YAML construct in frontmatter value: {value[:20]!r}")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return " ".join(value.split())


def _bounded(text: str, maximum: int, marker: str) -> str:
    """Beschneiden statt verschlucken — der Marker sagt, dass etwas fehlt."""
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - len(marker))] + marker


def _catalog_text(lines: tuple[str, ...], kept: int, max_chars: int) -> str:
    hidden = len(lines) - kept
    shown = list(lines[:kept])
    if hidden > 0:
        shown.append(f"({hidden} more skill(s) omitted — catalog capped at {max_chars} characters)")
    return "\n".join(shown)


# Der einzige globale Zustand des Moduls. Gehaengt an den Dateizeitstempel, nicht an die
# Prozesslaufzeit — genau wie in `identity.py`: ein bearbeiteter Skill wirkt sofort, ein
# unveraenderter kostet einen `stat` statt eines Lesevorgangs.
_BODY_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


def _body_from_disk(path: Path) -> str | None:
    """Body aus dem Cache, wenn die Datei sich nicht geruehrt hat. Fehlschlag -> `None`."""
    try:
        stamp = path.stat()
    except OSError:
        return None
    key = (stamp.st_mtime_ns, stamp.st_size)
    hit = _BODY_CACHE.get(str(path))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        _, body = _split_frontmatter(_read_capped(path))
    except SkillError:
        return None
    _BODY_CACHE[str(path)] = (key, body.strip())
    return _BODY_CACHE[str(path)][1]
