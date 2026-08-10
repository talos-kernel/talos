"""Capability-Token — die Erlaubnis ist ein Objekt mit Ablaufdatum, keine Liste mit Namen.

Vorher entschied eine Allow-Liste: `write_file` stand drin, also durfte `write_file`
schreiben. Immer. Das ist ein **Dauerrecht an einem Namen** — es gilt fuer jede
Anfrage, die zufaellig so heisst, unbegrenzt lang, egal ob der Kernel diese konkrete
Anfrage je gesehen hat.

Ein Capability-Token dreht das um: erlaubt ist nicht ein Tool, sondern **genau eine
Handlung**, fuer **wenige Sekunden**, **einmal**.

    Kernel sagt ja  ->  Mint praegt ein Token auf DIESE Anfrage
                    ->  Runner nimmt es entgegen, prueft, entwertet
                    ->  Token ist verbraucht. Dieselbe Anfrage nochmal? Neues Urteil.

Vier Eigenschaften, jede gegen einen konkreten Fehler:

- **An die Handlung gebunden** (`action_fp`): der Fingerabdruck deckt Tool, Identitaet,
  Argumente und Ziele ab. Ein Token fuer `write_file ~/notes.md` laesst sich nicht auf
  `write_file ~/.bashrc` umbiegen — ein geaenderter Buchstabe, und die Pruefung faellt.
- **Einmalig**: nach dem Einloesen ist die ID verbraucht. Ein Replay derselben Anfrage
  laeuft nicht auf dem alten Recht, sondern muss neu durch den Kernel.
- **Kurzlebig** (`TTL_SECONDS`): ein vergessenes Token ist nach Sekunden wertlos.
- **Nicht faelschbar** (HMAC): das Geheimnis lebt im Mint und verlaesst ihn nie. Ein
  von Hand gebautes `Grant`-Objekt hat keine gueltige Signatur.

Der entscheidende Punkt ist aber die **Praegestelle**: der Mint fragt den Kernel
*selbst*. Er glaubt keinem Aufrufer, der behauptet, es sei schon erlaubt. Ein DENY
kann darum nirgends im Prozess ein Token erzeugen — auch nicht, wenn der Executor
einen Fehler haette.

**Was das NICHT ist:** eine Sandbox. Wer in diesem Prozess beliebigen Code ausfuehren
kann, kommt an den Mint. Das Token macht den *strukturellen* Vorbeiweg unmoeglich —
den vergessenen Aufruf, den Replay, das vertauschte Ziel, das Weiterreichen eines
Rechts. Isolation muss die Sandbox liefern, nicht dieses Modul.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .policy import Decision, PolicyKernel, ToolRequest, Verdict, guard_targets

# Die Lebensdauer misst nur den Weg vom Urteil bis zum Lauf — beides im selben
# Aufruf, Millisekunden auseinander. 30 s ist grosszuegig fuer langsame Platten
# und trotzdem kein Zeitfenster, in dem sich etwas planen liesse.
TTL_SECONDS = 30.0

Runner = Callable[[ToolRequest], object]


class CapabilityError(Exception):
    """Kein Token, kein Lauf. Wird vom Executor als Fehlschlag behandelt, nie ignoriert."""


def action_fingerprint(req: ToolRequest, *, derived_targets: tuple[str, ...] | None = None) -> str:
    """sha256 ueber die vollstaendige Handlung: Tool, Identitaet, Argumente, Ziele.

    Kanonisch (sortierte Schluessel), damit derselbe Aufruf denselben Abdruck ergibt —
    und jede Abweichung einen anderen. Die kernel-abgeleiteten Ziele stehen mit drin,
    nicht nur `req.targets`: was das Modell deklariert, ist Beiwerk, was der Kernel
    ableitet, ist die Wahrheit. Der Mint gibt die Ziele seines konkreten Kernels mit,
    damit ein konfigurierter Vault-Root exakt in das Recht gebunden wird.
    """
    derived = guard_targets(req) if derived_targets is None else derived_targets
    payload = {
        "tool": req.tool,
        "identity": req.identity,
        "args": req.args,
        "declared": list(req.targets),
        "derived": list(derived),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Grant:
    """Ein Recht auf genau eine Handlung. Ohne gueltige Signatur wertlos."""

    grant_id: str
    tool: str
    action_fp: str
    issued_at: float
    expires_at: float
    human_approved: bool
    level: int
    mac: str

    def short(self) -> str:
        return self.grant_id[:8]


def _payload(
    grant_id: str,
    tool: str,
    action_fp: str,
    issued_at: float,
    expires_at: float,
    human: bool,
    level: int,
) -> bytes:
    return "|".join(
        (
            grant_id,
            tool,
            action_fp,
            f"{issued_at:.6f}",
            f"{expires_at:.6f}",
            "1" if human else "0",
            str(level),
        )
    ).encode("utf-8")


class CapabilityMint:
    """Praegt und entwertet Token. Haelt das Geheimnis und das Gedaechtnis fuer Verbrauchtes.

    Der Mint entscheidet **selbst** (`policy.decide`), statt einem Aufrufer zu glauben.
    Damit ist er der eine Engpass: es gibt im Prozess keinen zweiten Weg, aus einem
    DENY ein gueltiges Token zu machen.
    """

    def __init__(
        self,
        policy: PolicyKernel,
        *,
        ttl_s: float = TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        governor: object | None = None,
    ) -> None:
        self._policy = policy
        self._governor = governor
        self._ttl_s = ttl_s
        self._clock = clock
        self._secret = secrets.token_bytes(32)  # prozesslokal, verlaesst den Mint nie
        self._spent: dict[str, float] = {}  # grant_id -> expires_at
        self._issued = 0
        self._redeemed = 0
        self._lock = threading.Lock()

    # --- praegen -----------------------------------------------------------------
    def issue(self, req: ToolRequest, *, human_approved: bool = False) -> Grant:
        """Token fuer genau diese Anfrage — oder CapabilityError, wenn der Kernel nein sagt."""
        decision: Decision = self._policy.decide(req)
        if decision.verdict is Verdict.DENY:
            raise CapabilityError(f"kein Token: {decision.reason}")
        if decision.verdict is Verdict.NEEDS_HUMAN and not human_approved:
            raise CapabilityError(f"kein Token ohne Freigabe: {decision.reason}")

        now = self._clock()
        expires_at = now + self._ttl_s
        grant_id = secrets.token_hex(16)
        action_fp = action_fingerprint(req, derived_targets=self._policy.guard_targets(req))
        level = self._level()
        mac = hmac.new(
            self._secret,
            _payload(grant_id, req.tool, action_fp, now, expires_at, human_approved, level),
            hashlib.sha256,
        ).hexdigest()
        with self._lock:
            self._issued += 1
            self._prune(now)
        return Grant(
            grant_id=grant_id,
            tool=req.tool,
            action_fp=action_fp,
            issued_at=now,
            expires_at=expires_at,
            human_approved=human_approved,
            level=level,
            mac=mac,
        )

    # --- einloesen ---------------------------------------------------------------
    def redeem(self, grant: Grant, req: ToolRequest) -> None:
        """Prueft und entwertet. Faellt eine Pruefung, fliegt CapabilityError — kein Lauf.

        Reihenfolge ist Absicht: erst Echtheit (Signatur), dann Frische (TTL), dann
        Einmaligkeit, zuletzt die Bindung an die Anfrage. Ein gefaelschtes Token
        soll nicht erst am Ziel scheitern, sondern sofort.
        """
        if not isinstance(grant, Grant):
            raise CapabilityError("kein Token vorgelegt")

        expected = hmac.new(
            self._secret,
            _payload(
                grant.grant_id,
                grant.tool,
                grant.action_fp,
                grant.issued_at,
                grant.expires_at,
                grant.human_approved,
                grant.level,
            ),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, grant.mac):
            raise CapabilityError("Token-Signatur ungueltig")

        now = self._clock()
        if now >= grant.expires_at:
            raise CapabilityError("Token abgelaufen")

        # Der Regler ist eine Notbremse: wer waehrend eines Laufs zudreht, soll nicht
        # zusehen muessen, wie ein Sekunden altes Recht noch durchgeht.
        if grant.level != self._level():
            raise CapabilityError("Autonomie-Stufe hat sich geaendert — Token entwertet")

        if grant.tool != req.tool:
            raise CapabilityError(f"Token gilt fuer {grant.tool}, nicht fuer {req.tool}")
        if not hmac.compare_digest(
            grant.action_fp,
            action_fingerprint(req, derived_targets=self._policy.guard_targets(req)),
        ):
            raise CapabilityError("Token gilt fuer eine andere Handlung")

        with self._lock:
            if grant.grant_id in self._spent:
                raise CapabilityError("Token bereits verbraucht")
            self._spent[grant.grant_id] = grant.expires_at
            self._redeemed += 1
            self._prune(now)

    # --- Auskunft ----------------------------------------------------------------
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"issued": self._issued, "redeemed": self._redeemed, "spent_kept": len(self._spent)}

    def _level(self) -> int:
        """Stand des Reglers, oder -1 wenn keiner verdrahtet ist (Tests, redteam)."""
        gov = self._governor
        return int(getattr(gov, "level", -1)) if gov is not None else -1

    def _prune(self, now: float) -> None:
        """Verbrauchte IDs vergessen, sobald sie ohnehin abgelaufen waeren (kein Leck)."""
        if len(self._spent) < 64:
            return
        self._spent = {gid: exp for gid, exp in self._spent.items() if exp > now}


@dataclass(frozen=True)
class GrantedRunner:
    """Die einzige Stelle, an der ein Tool tatsaechlich laeuft — und sie will ein Token.

    Die rohen Runner liegen *hier drin*. Wer sie ausfuehren will, muss durch `__call__`,
    und `__call__` loest zuerst ein Token ein. Ein vergessener Gate-Aufruf an anderer
    Stelle fuehrt darum nicht zu einer ungeprueften Wirkung, sondern zu gar keiner.
    """

    mint: CapabilityMint
    runners: dict[str, Runner]

    def __call__(self, req: ToolRequest, grant: Grant) -> object:
        self.mint.redeem(grant, req)
        runner = self.runners.get(req.tool)
        if runner is None:
            raise CapabilityError(f"kein Runner registriert: {req.tool}")
        return runner(req)
