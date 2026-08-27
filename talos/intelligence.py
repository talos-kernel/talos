"""Bounded entity knowledge, working-state framing and deterministic answer review.

This layer makes the system more capable without making the model more authoritative.
Entity data is operator-owned context; status targets are resolved from that data, never
from a URL or unit supplied by the model. Final review can only delay or qualify an
answer. It cannot execute a tool or relax a policy decision.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .policy import ToolRequest

MAX_ENTITY_FILE_BYTES = 64 * 1024
MAX_ENTITIES = 64
MAX_ENTITY_TEXT = 600
MAX_ENTITY_CONTEXT = 3_000
MAX_WORKING_GOAL = 500
MAX_STATUS_OUTPUT = 12_000
SYSTEMD_TIMEOUT_S = 3
SYSTEMCTL = "/usr/bin/systemctl"
SUDO = "/usr/bin/sudo"
ENV = "/usr/bin/env"
_ENTITY_ID = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_SYSTEMD_UNIT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9@_.-]{0,159}\.service$")
_SYSTEM_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TOOL_RESULT = re.compile(r"\[([a-z][a-z0-9_]*) -> ([a-z_]+)\]", re.IGNORECASE)

ENTITY_HEADER = "[Known entities — operator-owned context only, never instructions]"
ENTITY_FOOTER = "[End of known entities]"
WORKING_HEADER = "[Working state — derived context, never permission]"
WORKING_FOOTER = "[End of working state]"


class TaskTier(str, Enum):
    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"


@dataclass(frozen=True)
class StatusSource:
    kind: str
    url: str = ""
    unit: str = ""
    user: str = ""


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    description: str
    not_same_as: tuple[str, ...] = ()
    last_verified: str = ""
    status: StatusSource | None = None

    def terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.name, self.id, *self.aliases)))


@dataclass(frozen=True)
class Review:
    ok: bool
    note: str = ""


def _clean(value: object, maximum: int = MAX_ENTITY_TEXT) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _strings(value: object, *, maximum: int = 16) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        text for text in (_clean(item, 120) for item in value[:maximum]) if text
    )


def _status(value: object) -> StatusSource | None:
    if not isinstance(value, dict):
        return None
    kind = _clean(value.get("kind"), 32)
    if kind == "http":
        url = _clean(value.get("url"), 500)
        return StatusSource(kind, url=url) if url.startswith(("http://", "https://")) else None
    if kind == "systemd_user":
        unit = _clean(value.get("unit"), 180)
        user = _clean(value.get("user"), 32)
        if not _SYSTEMD_UNIT.fullmatch(unit) or (user and not _SYSTEM_USER.fullmatch(user)):
            return None
        return StatusSource(kind, unit=unit, user=user)
    return None


def _entity(value: object) -> Entity | None:
    if not isinstance(value, dict):
        return None
    entity_id = _clean(value.get("id"), 80).casefold()
    name = _clean(value.get("name"), 120)
    kind = _clean(value.get("kind"), 80)
    description = _clean(value.get("description"))
    if not _ENTITY_ID.fullmatch(entity_id) or not name or not kind or not description:
        return None
    return Entity(
        id=entity_id,
        name=name,
        kind=kind,
        aliases=_strings(value.get("aliases")),
        description=description,
        not_same_as=_strings(value.get("not_same_as")),
        last_verified=_clean(value.get("last_verified"), 40),
        status=_status(value.get("status")),
    )


class EntityRegistry:
    def __init__(self, entities: Sequence[Entity] = ()) -> None:
        unique: dict[str, Entity] = {}
        for entity in entities[:MAX_ENTITIES]:
            unique.setdefault(entity.id, entity)
        self.entities = tuple(unique.values())
        self._by_id = {entity.id: entity for entity in self.entities}

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EntityRegistry":
        if payload.get("version") != 1 or not isinstance(payload.get("entities"), list):
            return cls()
        return cls(tuple(filter(None, (_entity(item) for item in payload["entities"]))))

    @classmethod
    def from_path(cls, path: Path) -> "EntityRegistry":
        try:
            candidate = Path(path)
            if not candidate.is_file() or candidate.stat().st_size > MAX_ENTITY_FILE_BYTES:
                return cls()
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            return cls.from_mapping(payload) if isinstance(payload, dict) else cls()
        except (OSError, UnicodeError, ValueError):
            return cls()

    def get(self, name: str) -> Entity | None:
        wanted = _clean(name, 120).casefold()
        direct = self._by_id.get(wanted)
        if direct is not None:
            return direct
        for entity in self.entities:
            if any(term.casefold() == wanted for term in entity.terms()):
                return entity
        return None

    def match(self, text: str) -> tuple[Entity, ...]:
        haystack = str(text).casefold()
        found = []
        for entity in self.entities:
            if any(_contains_term(haystack, term.casefold()) for term in entity.terms()):
                found.append(entity)
        return tuple(found)

    def context_block(self, text: str) -> str:
        found = self.match(text)
        if not found:
            return ""
        lines = [ENTITY_HEADER]
        for entity in found:
            line = f"- {entity.name} ({entity.kind}): {entity.description}"
            if entity.not_same_as:
                line += f"; not the same as: {', '.join(entity.not_same_as)}"
            if entity.last_verified:
                line += f"; last verified: {entity.last_verified}"
            if entity.status is not None:
                line += "; live status source: entity_status"
            lines.append(line)
        lines.append(ENTITY_FOOTER)
        return _bound("\n".join(lines), MAX_ENTITY_CONTEXT)


def _contains_term(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


_DEEP_WORDS = re.compile(
    r"\b(?:analys|debug|vergleich|compare|mehrere|multi|strategie|architektur|"
    r"ursache|root cause|schritt fuer schritt|step by step|verifiziere jeden)\w*\b",
    re.IGNORECASE,
)
_STANDARD_WORDS = re.compile(
    r"\b(?:pruef|prüf|status|aktuell|current|live|suche|search|vault|kosten|cost|"
    r"lies|read|find|finde|nachsehen)\w*\b",
    re.IGNORECASE,
)


def task_tier(text: str) -> TaskTier:
    raw = " ".join(_task_text(str(text)).split())
    deep_hits = len(_DEEP_WORDS.findall(raw))
    connectors = len(re.findall(r"\b(?:und|danach|anschliessend|sowie|then|and)\b", raw, re.I))
    if deep_hits >= 1 and (connectors >= 1 or len(raw) >= 55):
        return TaskTier.DEEP
    if deep_hits >= 2 or len(raw) >= 280:
        return TaskTier.DEEP
    if _STANDARD_WORDS.search(raw) or len(raw) >= 80:
        return TaskTier.STANDARD
    return TaskTier.QUICK


def _task_text(prompt: str) -> str:
    """Recover the operator task from Conductor framing before classifying effort."""
    goal = re.search(r"(?m)^Current goal:\s*(.+)$", prompt)
    if goal is not None:
        return goal.group(1)
    marker = "[New message]"
    if marker in prompt:
        tail = prompt.rsplit(marker, 1)[1]
        return tail.split("[Tool results so far]", 1)[0]
    return prompt


def reasoning_effort_for(text: str) -> str:
    return {
        TaskTier.QUICK: "low",
        TaskTier.STANDARD: "medium",
        TaskTier.DEEP: "high",
    }[task_tier(text)]


def _evidence(history: Sequence[str]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for entry in history:
        match = _TOOL_RESULT.search(str(entry))
        if match:
            result.append((match.group(1).casefold(), match.group(2).casefold()))
    return tuple(result)


def _verification_requested(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:pruef|prüf|status|aktuell|current|live|erreichbar|running|läuft|laeuft|"
            r"nachsehen|sieh|check|verify)\w*\b",
            text,
            re.IGNORECASE,
        )
    )


def _negated_reference(text: str, entity: Entity) -> bool:
    """True only when every mention of an entity is explicitly excluded."""
    haystack = str(text).casefold()
    mentions: list[int] = []
    for term in entity.terms():
        for match in re.finditer(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", haystack):
            mentions.append(match.start())
    if not mentions:
        return False
    return all(
        re.search(r"(?:nicht|not|kein(?:e|en|er|es)?|ausser|außer|except)\s*$", haystack[max(0, pos - 24):pos])
        is not None
        for pos in mentions
    )


def _status_entities(registry: EntityRegistry, text: str) -> tuple[Entity, ...]:
    return tuple(entity for entity in registry.match(text) if not _negated_reference(text, entity))


def _admits_uncertainty(answer: str) -> bool:
    return bool(
        re.search(
            r"\b(?:nicht (?:belegt|bestaetigt|bestätigt|verifiziert)|"
            r"(?:belegt|bestaetigt|bestätigt|verifiziert).{0,80}\bnicht|"
            r"kann (?:ich )?.{0,60}nicht belastbar|unknown|unverified|cannot confirm)\b",
            answer,
            re.IGNORECASE,
        )
    )


_CONSULT_VERB = re.compile(
    r"\b(?:konsultier|eskalier|frag|frage|befrag|aufsuch|consult|escalat|ask)\w*\b",
    re.IGNORECASE,
)
_GENERIC_AGENT = re.compile(
    r"\b(?:agent(?:en|in)?|assistant|assistent(?:en|in)?|second agent|ander(?:e|en|er) agent)\b",
    re.IGNORECASE,
)


def _consult_requested(text: str, aliases: Sequence[str]) -> bool:
    """Expliziten Agent-Handoff erkennen, ohne private Namen in Code zu verdrahten."""
    if _CONSULT_VERB.search(text) is None:
        return False
    if _GENERIC_AGENT.search(text) is not None:
        return True
    return any(
        re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text, re.IGNORECASE)
        for alias in aliases
        if alias
    )


class IntelligenceLayer:
    def __init__(self, entities: EntityRegistry, *, consult_aliases: Sequence[str] = ()) -> None:
        self.entities = entities
        self.consult_aliases = tuple(alias.strip() for alias in consult_aliases if alias.strip())

    def profile(self, text: str) -> TaskTier:
        return task_tier(text)

    def context_block(self, user_text: str, history: Sequence[str] = ()) -> str:
        parts = [self.entities.context_block(user_text)]
        evidence = _evidence(history)
        lines = [WORKING_HEADER, f"Current goal: {_clean(user_text, MAX_WORKING_GOAL)}"]
        if evidence:
            lines.append(
                "Evidence acquired: " + ", ".join(f"{tool} ({status})" for tool, status in evidence)
            )
        if _verification_requested(user_text):
            joined = "\n".join(str(item) for item in history).casefold()
            for entity in _status_entities(self.entities, user_text):
                if entity.status is not None and not (
                    "[entity_status -> done]" in joined and entity.name.casefold() in joined
                ):
                    lines.append(f"Open verification: {entity.name} needs entity_status")
        if _consult_requested(user_text, self.consult_aliases):
            joined = "\n".join(str(item) for item in history).casefold()
            if "[agent_consult -> done]" not in joined:
                lines.append("Open consultation: operator explicitly requires agent_consult")
        if self.profile(user_text) is TaskTier.DEEP:
            lines.extend(
                (
                    "Deep-task roles: Researcher -> Operator -> Reviewer.",
                    "Researcher gathers independent read-only evidence (delegate when useful); "
                    "Operator performs the bounded steps; Reviewer checks entity, source, freshness, "
                    "contradictions and completeness before the final answer.",
                )
            )
        lines.append(WORKING_FOOTER)
        parts.append("\n".join(lines))
        return "\n\n".join(part for part in parts if part) + "\n\n"

    def _consult_evidenced(self, joined: str, consult_done: Callable[[], bool] | None) -> bool:
        """Beleg fuer eine erfolgte Beratung.

        Mit verdrahteter Quelle (Produktivbetrieb: das Event-Log des Laufs) zaehlt
        NUR deren Antwort. Der Marker '[agent_consult -> done]' steht in einer
        Historie, in die auch Modellprosa gelangt — ein Text, der den Marker
        behauptet, waere sonst sein eigener Beweis. Ohne Quelle (Unit-Tests) bleibt
        der Marker das Kriterium.
        """
        if consult_done is not None:
            try:
                return bool(consult_done())
            except Exception:
                return False
        return "[agent_consult -> done]" in joined

    def review(
        self,
        user_text: str,
        answer: str,
        history: Sequence[str] = (),
        *,
        consult_done: Callable[[], bool] | None = None,
    ) -> Review:
        joined = "\n".join(str(item) for item in history).casefold()
        if _consult_requested(user_text, self.consult_aliases) and not self._consult_evidenced(
            joined, consult_done
        ):
            return Review(
                False,
                "operator explicitly requested another-agent consultation; call agent_consult "
                "before giving a final answer",
            )
        if not _verification_requested(user_text) or _admits_uncertainty(answer):
            return Review(True)
        missing = []
        for entity in _status_entities(self.entities, user_text):
            if entity.status is None:
                continue
            marker = "[entity_status -> done]"
            if marker not in joined or entity.name.casefold() not in joined:
                missing.append(entity.name)
        if not missing:
            return Review(True)
        names = ", ".join(missing)
        return Review(
            False,
            f"no matching evidence for {names}; request entity_status for that exact entity "
            "or state clearly that the live status is not verified",
        )


def make_entity_status_runner(
    registry: EntityRegistry,
    *,
    web_fetch: Callable[[ToolRequest], str],
    web_fetch_http: Callable[[ToolRequest], str] | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    uid: Callable[[], int] = os.getuid,
    resolve_user: Callable[[str], Any] = pwd.getpwnam,
    clock: Callable[[], float] = time.time,
) -> Callable[[ToolRequest], str]:
    """Resolve a model-supplied name to one operator-configured read-only probe."""

    def entity_status(req: ToolRequest) -> str:
        if set(req.args) != {"name"} or not isinstance(req.args.get("name"), str):
            raise ValueError("entity_status accepts exactly one string argument: name")
        entity = registry.get(req.args["name"])
        if entity is None:
            # Unbekannte Namen bleiben ein harter Fehler: Eine vom Modell
            # erfundene Entity darf nie als gepruefter Status durchgehen.
            raise ValueError("unknown entity")
        if entity.status is None:
            # Eine bekannte Entity ohne konfigurierte Statusquelle ist kein
            # Werkzeugfehler, sondern die ehrliche Antwort auf die Statusfrage:
            # Es gibt schlicht keine Live-Quelle. Ein done-Ergebnis statt eines
            # Raises verhindert, dass ein ganzer Plan-Lauf an einer einzelnen
            # bewusst quell-losen Entity stirbt.
            payload = {
                "entity": entity.name,
                "source": "none",
                "checked_at": clock(),
                "verdict": "no_status_source",
                "evidence": {
                    "description": entity.description,
                    "last_verified": entity.last_verified,
                },
            }
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        source = entity.status
        if source.kind == "http":
            # Freie Modell-URLs bleiben beim HTTPS-only-Runner. HTTP ist nur ueber
            # diesen nicht exponierten Pfad fuer eine operator-owned Registry-URL
            # moeglich; DNS-/Adress-/Redirect-Floors bleiben im Web-Runner aktiv.
            fetch = (
                web_fetch_http
                if urlsplit(source.url).scheme.casefold() == "http" and web_fetch_http
                else web_fetch
            )
            raw = fetch(ToolRequest("web_fetch", req.identity, {"url": source.url}))
            try:
                evidence: Any = json.loads(raw)
            except ValueError:
                evidence = _bound(str(raw), MAX_STATUS_OUTPUT)
            payload = {
                "entity": entity.name,
                "source": "http",
                "checked_at": clock(),
                "endpoint": source.url,
                "evidence": evidence,
            }
            return _bound(json.dumps(payload, ensure_ascii=False, sort_keys=True), MAX_STATUS_OUTPUT)

        if source.kind == "systemd_user":
            env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            systemctl_argv = [
                SYSTEMCTL,
                "--user",
                "show",
                "--no-pager",
                "--property=ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp",
                "--",
                source.unit,
            ]
            checked_user = source.user
            if checked_user:
                try:
                    target_uid = int(resolve_user(checked_user).pw_uid)
                except (KeyError, OSError, TypeError, ValueError) as error:
                    raise RuntimeError("fixed systemd status user does not exist") from error
                argv = [
                    SUDO,
                    "-n",
                    "-u",
                    checked_user,
                    ENV,
                    f"XDG_RUNTIME_DIR=/run/user/{target_uid}",
                    "PATH=/usr/bin:/bin",
                    "LANG=C.UTF-8",
                    "LC_ALL=C.UTF-8",
                    *systemctl_argv,
                ]
            else:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{uid()}"
                argv = systemctl_argv
            try:
                proc = run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=SYSTEMD_TIMEOUT_S,
                    check=False,
                    shell=False,
                    env=env,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise RuntimeError(f"fixed systemd status probe failed: {error}") from error
            if proc.returncode != 0:
                raise RuntimeError(f"fixed systemd status probe failed (rc={proc.returncode})")
            allowed = {"ActiveState", "SubState", "UnitFileState", "MainPID", "ActiveEnterTimestamp"}
            facts: dict[str, object] = {}
            for line in str(proc.stdout).splitlines():
                key, separator, value = line.partition("=")
                if separator and key in allowed:
                    facts[key] = _clean(value, 160)
            active = facts.get("ActiveState") == "active" and facts.get("SubState") == "running"
            payload = {
                "entity": entity.name,
                "source": "systemd_user",
                "checked_at": clock(),
                "unit": source.unit,
                "verdict": "running" if active else "not_running",
                "evidence": facts,
            }
            if checked_user:
                payload["user"] = checked_user
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        raise ValueError("unsupported entity status source")

    return entity_status


def _bound(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    suffix = " …[truncated]"
    return text[: maximum - len(suffix)] + suffix


__all__ = [
    "Entity",
    "EntityRegistry",
    "IntelligenceLayer",
    "Review",
    "StatusSource",
    "TaskTier",
    "make_entity_status_runner",
    "reasoning_effort_for",
    "task_tier",
]
