"""Approval-Store für die menschliche Freigabe-Runde (NEEDS_HUMAN).

Sagt der Policy-Kernel NEEDS_HUMAN, hält Talos die Aktion an, fragt den Betreiber per Telegram und
parkt die **exakte** Anfrage. des Betreibers „ja" führt genau dieses eine Objekt aus, „nein" verwirft,
„immer" führt aus **und** legt eine stehende Freigabe an (siehe `standing.py`).
Bewusst simpel: höchstens eine offene Anfrage pro Chat.

Zwei Härtungen (Auflage aus der Kernel-Kritik):
- **TTL**: eine Freigabe verfällt nach `ttl_s`. Ein vergessenes „ja" im Chat ist sonst eine
  offene Tür. Nach Ablauf gibt `get()` nichts mehr zurück.
- **Fingerprint gegen TOCTOU**: beim Parken wird (realpath, sha256) je Ziel festgehalten.
  Zwischen Fragen und „ja" liegen Minuten — vor dem Ausführen wird neu gemessen und
  verglichen. Weicht ein Ziel ab, wird abgebrochen statt blind auf das getauschte Ziel
  zu schreiben. Die maßgebliche Bindung entsteht so beim Ausführen, nicht beim Fragen.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .channel import Button, Principal, StructuredMessage
from .memory import Turn
from .policy import ToolRequest, guard_targets

TTL_SECONDS = 5 * 60
_HASH_CHUNK = 65536

# Freigabe ist eine Sicherheitsbestätigung — nur ein bewusster, eindeutiger Token zählt.
# Bewusst NICHT ok/go/sure: bei offener Freigabe wuerde sonst ein beilaeufiges "ok" im
# Chat einen Shell-Lauf scharf schalten. Der Freigabe-Text verlangt ausdruecklich "yes".
_AFFIRMATIVE = {"yes"}
# Abbruch darf breit sein — mehr Wege, „nein" zu sagen, ist eine Sicherheitsreserve.
_NEGATIVE = {"no", "n", "nope", "stop", "cancel", "abort"}
# „immer" ist das ja mit Gedächtnis: es führt diese Anfrage aus UND legt eine stehende
# Freigabe für genau diese Handlung an (siehe standing.py). Genauso eng gefasst wie „ja" —
# und bewusst disjunkt davon, damit die drei Wege sich im Conductor nie überlappen.
_ALWAYS = {"always"}

Fingerprint = tuple[tuple[str, str], ...]  # ((realpath, sha256|"absent"), ...)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(targets: tuple[str, ...]) -> Fingerprint:
    """(realpath, sha256) je Ziel. Fehlt/unlesbar -> „absent"; der Hash verlässt nie den Prozess."""
    marks: list[tuple[str, str]] = []
    for target in targets:
        real = os.path.realpath(target)
        try:
            marks.append((real, _sha256(real)))
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            marks.append((real, "absent"))
    return tuple(marks)


@dataclass(frozen=True)
class Pending:
    """Eine geparkte, freigabepflichtige Anfrage — an das Objekt gebunden, nicht an die Kategorie."""

    approval_id: str
    req: ToolRequest
    targets: tuple[str, ...]
    ask_fingerprint: Fingerprint
    expires_at: float
    prompt: str
    # Fortsetzungszustand des Agent-Loops. Die Freigabe-Nachricht selbst ("ja") darf
    # nie die ursprüngliche Aufgabe oder die bisherigen Tool-Ergebnisse ersetzen.
    request_text: str = ""
    history: tuple[str, ...] = ()
    principal: str = ""
    memory_context: tuple[Turn, ...] = ()
    steps: int = 0
    resume_agent: bool = False
    # Der angekuendigte Ablauf reist mit. Er erteilt nichts — er haelt nur Budget und
    # Abbruchbedingung fest. Ginge er beim Parken verloren, waere eine Rueckfrage der
    # bequemste Weg, beides abzustreifen: nach dem „ja" stuende der Lauf wieder mit dem
    # vollen Hausmass da, obwohl er drei Schritte angekuendigt hatte.
    plan: object | None = None


class ApprovalStore:
    """Hält je Chat höchstens eine offene Anfrage, mit TTL und Änderungs-Erkennung.

    Seit dem Worker-Thread lesen zwei Threads hier: der Poll-Thread beantwortet
    Kommandos und Freigaben, der Worker parkt sie beim Denken. Der RLock ist
    reentrant, weil `get()` bei Ablauf selbst `clear()` ruft.
    """

    def __init__(
        self,
        ttl_s: int = TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._pending: dict[str, Pending] = {}
        self._ttl_s = ttl_s
        self._clock = clock
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(12))
        self._lock = threading.RLock()

    def park(
        self,
        conversation: str,
        req: ToolRequest,
        prompt: str,
        *,
        request_text: str = "",
        history: tuple[str, ...] = (),
        principal: object | None = None,
        memory_context: tuple[Turn, ...] = (),
        steps: int = 0,
        resume_agent: bool = False,
        plan: object | None = None,
    ) -> Pending:
        targets = guard_targets(req)
        rec = Pending(
            approval_id=self._nonce_factory(),
            req=req,
            targets=targets,
            ask_fingerprint=fingerprint(targets),
            expires_at=self._clock() + self._ttl_s,
            prompt=prompt,
            request_text=request_text,
            history=history,
            principal=str(req.identity if principal is None else principal),
            memory_context=memory_context,
            steps=steps,
            resume_agent=resume_agent,
            plan=plan,
        )
        with self._lock:
            self._pending = {**self._pending, conversation: rec}
        return rec

    def get(self, conversation: str) -> Pending | None:
        """Offene Anfrage des Chats — oder None, wenn keine da ist oder sie abgelaufen ist."""
        with self._lock:
            rec = self._pending.get(conversation)
            if rec is None:
                return None
            if self._clock() >= rec.expires_at:
                self.clear(conversation)
                return None
            return rec

    def claim_if_current(
        self, conversation: str, expected: Pending, *, principal: object | None = None
    ) -> Pending | None:
        """Atomically consume exactly the shown approval, only for its requester."""
        with self._lock:
            current = self._pending.get(conversation)
            if current is None:
                return None
            if self._clock() >= current.expires_at:
                self._pending = {k: v for k, v in self._pending.items() if k != conversation}
                return None
            if current is not expected or current.approval_id != expected.approval_id:
                return None
            if principal is not None and current.principal != str(principal):
                return None
            self._pending = {k: v for k, v in self._pending.items() if k != conversation}
            return current

    def clear(self, conversation: str) -> None:
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if k != conversation}

    def target_unchanged(self, rec: Pending) -> bool:
        """Exec-Zeit-Neubindung: kein Ziel hat sich seit dem Fragen geändert."""
        return fingerprint(rec.targets) == rec.ask_fingerprint


@dataclass(frozen=True)
class _ApprovalButtonState:
    principal: Principal
    conversation: str
    pending: Pending
    decision: str
    expires_at: float


class ApprovalPicker:
    """Hermes-style approval buttons backed by opaque, one-use server-side tokens."""

    PREFIX = "ap:"

    def __init__(
        self,
        *,
        ttl_s: int = TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._ttl_s = max(1, int(ttl_s))
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(9))
        self._states: dict[str, _ApprovalButtonState] = {}
        self._lock = threading.Lock()

    def open(
        self,
        text: str,
        pending: Pending,
        *,
        principal: Principal,
        conversation: str,
    ) -> StructuredMessage:
        now = self._clock()
        expires_at = min(pending.expires_at, now + self._ttl_s)
        buttons: list[Button] = []
        with self._lock:
            self._states = {
                token: state for token, state in self._states.items() if state.expires_at > now
            }
            for label, decision in (
                ("✓ Allow once", "yes"),
                ("∞ Always allow", "always"),
                ("✕ Deny", "no"),
            ):
                token = self._unique_token()
                self._states[token] = _ApprovalButtonState(
                    principal=principal,
                    conversation=conversation,
                    pending=pending,
                    decision=decision,
                    expires_at=expires_at,
                )
                buttons.append(Button(label, self.PREFIX + token))
        return StructuredMessage(text, ((buttons[0], buttons[1]), (buttons[2],)))

    def consume(
        self,
        data: str,
        *,
        principal: Principal,
        conversation: str,
        pending: Pending | None,
    ) -> str | None:
        if not data.startswith(self.PREFIX):
            return None
        token = data[len(self.PREFIX):]
        if not token or ":" in token:
            return None
        with self._lock:
            now = self._clock()
            self._states = {
                key: value for key, value in self._states.items() if value.expires_at > now
            }
            state = self._states.get(token)
            if state is None:
                return None
            # A fremde click must not invalidate the operator's still-valid button.
            if state.principal != principal or state.conversation != conversation:
                return None
            # Bind the click to the exact object parked when the keyboard was rendered.
            if pending is None or state.pending.approval_id != pending.approval_id:
                self._states.pop(token, None)
                return None
            decision = state.decision
            self._states = {
                key: value
                for key, value in self._states.items()
                if not (
                    value.principal == principal
                    and value.conversation == conversation
                    and value.pending.approval_id == pending.approval_id
                )
            }
            return decision

    def discard(self, pending: Pending, *, principal: Principal, conversation: str) -> None:
        """Invalidate sibling buttons when the same decision is typed instead of clicked."""
        with self._lock:
            self._states = {
                key: value
                for key, value in self._states.items()
                if not (
                    value.principal == principal
                    and value.conversation == conversation
                    and value.pending.approval_id == pending.approval_id
                )
            }

    def _unique_token(self) -> str:
        for _ in range(16):
            token = self._token_factory().strip()
            if token and ":" not in token and token not in self._states:
                return token
        raise RuntimeError("could not allocate unique approval callback token")


def is_affirmative(text: str) -> bool:
    return text.strip().lower().rstrip("!.") in _AFFIRMATIVE


def is_negative(text: str) -> bool:
    return text.strip().lower().rstrip("!.") in _NEGATIVE


def is_always(text: str) -> bool:
    """des Betreibers „immer": run once and create the standing approval."""
    return text.strip().lower().rstrip("!.") in _ALWAYS
