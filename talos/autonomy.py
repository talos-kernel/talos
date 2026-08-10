"""Autonomie-Regler 0-5 — wie weit the operator den Waechter von der Leine laesst.

Der Kernel beantwortet die Frage *"ist das erlaubt?"*. Der Regler beantwortet eine
andere: *"wie viel soll heute ohne Rueckfrage laufen?"*. Die Trennung ist Absicht.
Die Floors sind Sicherheit und gehoeren niemandem — der Regler ist des Betreibers Ermessen.

**Der Regler kann nur zumachen, nie aufmachen.** Er sieht das Urteil des Kernels und
darf es verschaerfen (ALLOW -> NEEDS_HUMAN -> DENY), niemals abschwaechen. Ein DENY
des Kernels bleibt DENY auf jeder Stufe, auch auf 5. Damit ist die hoechste Stufe
exakt der ungefilterte Kernel — nicht etwa "alles darf". Ein Regler, der nach oben
hin Rechte *vergibt*, waere eine zweite Erlaubnisquelle neben dem Kernel; genau die
haben wir mit der Allow-Liste gerade abgeschafft.

| Stufe | Name           | Lesen        | Schreiben                    | Shell |
|-------|----------------|--------------|------------------------------|-------|
| 0     | aus            | abgelehnt    | abgelehnt                    | abgelehnt |
| 1     | angeleint      | fragt        | abgelehnt                    | abgelehnt |
| 2     | lesen          | frei         | abgelehnt                    | abgelehnt |
| 3     | fragen         | frei         | fragt                        | fragt |
| 4     | arbeitsbereich | frei         | frei im Arbeitsbereich, sonst fragt | fragt |
| 5     | voll           | frei         | wie der Kernel               | fragt* |

*Shell fragt auf **jeder** Stufe, solange `shell_needs_human` steht — der Regler
hebt das nicht auf, er kann es nur zusaetzlich verbieten (Stufen 0-2).

**Der Regler ist kein Tool.** Es gibt bewusst kein `set_autonomy` im Manifest: das
Modell schlaegt Tool-Calls vor, und ein Agent, der seine eigene Leine verlaengern
kann, hat keine. Gestellt wird der Regler ausschliesslich ueber `/autonomy <n>` im
Kommando-Pfad — deterministisch, an des Betreibers Identitaet gebunden, im Log belegt.

Startwert ist Stufe 5 — also genau das Verhalten, das es vor dem Regler gab. Der
Regler aendert von sich aus nichts; er gibt the operator nur einen Hebel, mit dem er kuerzer
anleinen kann, ohne den Kernel anzufassen.

Die Stufe ueberlebt einen Neustart, weil sie im Event-Log steht (`autonomy.set`).
Ein Regler, der bei jedem Absturz stillschweigend auf "mehr" zurueckspringt, waere
eine Falle; `restore_level()` liest darum den letzten gesetzten Stand zurueck.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from . import trust as channel_trust
from .channel import Principal
from .manifest import Effect, ToolManifest, ToolSpec
from .policy import (
    HOME,
    WORKSPACE_DIR,
    Decision,
    PolicyKernel,
    ToolRequest,
    Verdict,
    guard_targets,
    stricter,
)
from .trust import TrustLookup

MIN_LEVEL = 0
MAX_LEVEL = 5
# Startwert ist der Status quo: der ungefilterte Kernel. Das ist bewusst nicht der
# "sicherste" Wert, denn der Regler ist keine Sicherheitsgrenze — das sind die Floors,
# und die stehen auf jeder Stufe. Ein niedriger Startwert wuerde beim ersten Start
# stillschweigend etwas anderes tun als das, was the operator geprueft und freigegeben hat:
# auf Stufe 2 laeuft auch mit "ja" nichts mehr, die Freigabe-Runde waere tot. Wer
# kuerzer angeleint sein will, sagt es einmal — und der Stand ueberlebt den Neustart.
DEFAULT_LEVEL = 5

# Freier Schreibbereich fuer Stufe 4. Bewusst NICHT das Installationsverzeichnis —
# der eigene Quellcode steht im Persistenz-Floor und fragt auf jeder Stufe.
# Der Pfad kommt aus `policy.WORKSPACE_DIR`, also aus dem Modulpfad: eine hier
# ausgeschriebene Zeichenkette zeigte nach einer Umbenennung ins Leere, und Stufe 4
# haette dann jede Schreibarbeit als „ausserhalb des Arbeitsbereichs" abgewiesen.
WORKSPACE = WORKSPACE_DIR

LEVEL_NAMES: dict[int, str] = {
    0: "off",
    1: "leashed",
    2: "read",
    3: "ask",
    4: "workspace",
    5: "full",
}

LEVEL_HELP: dict[int, str] = {
    0: "no tool runs — Talos only answers",
    1: "even reading needs your approval, no effects",
    2: "reading is free, every effect is refused",
    3: "reading is free, every effect asks you",
    4: f"writing is free under {WORKSPACE}, otherwise it asks",
    5: "the kernel decides alone (floors stay)",
}

class AutonomyError(ValueError):
    """Ungueltige Stufe oder nicht zugelassene Identitaet. Der Stand bleibt unveraendert."""


def clamp(level: object) -> int:
    """Alles, was keine Stufe ist, wird zur sichersten Stufe — nie zur hoechsten."""
    try:
        value = int(level)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MIN_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, value))


def _inside(path: str, root: Path) -> bool:
    """Realpath-basiert wie der Floor — sonst traegt `~/talos/workspace/../.bashrc` frei."""
    real = os.path.realpath(os.path.expanduser(os.path.expandvars(str(path))))
    base = os.path.realpath(str(root))
    return real == base or real.startswith(base + os.sep)


def ceiling(level: int, req: ToolRequest, spec: ToolSpec | None) -> Decision:
    """Das Freizuegigste, was diese Stufe zulaesst — unabhaengig vom Kernel-Urteil.

    Wird spaeter mit dem Kernel-Urteil verrechnet: es gilt immer das strengere.
    """
    if level <= 0:
        return Decision(Verdict.DENY, "autonomy 0 (off) — no tool runs")

    reading = spec is not None and spec.effect is Effect.READ
    if level == 1:
        if reading:
            return Decision(Verdict.NEEDS_HUMAN, "autonomy 1 (leashed) — even reading asks")
        return Decision(Verdict.DENY, "autonomy 1 (leashed) — no effects")

    if reading:
        return Decision(Verdict.ALLOW, "")

    if level == 2:
        return Decision(Verdict.DENY, "autonomy 2 (read) — no effects")
    if level == 3:
        return Decision(Verdict.NEEDS_HUMAN, "autonomy 3 (ask) — every effect asks you")
    if level == 4:
        return _workspace_ceiling(req, spec)
    return Decision(Verdict.ALLOW, "")


def _workspace_ceiling(req: ToolRequest, spec: ToolSpec | None) -> Decision:
    """Stufe 4: freie Hand im Arbeitsbereich, alles andere fragt.

    Shell bleibt freigabepflichtig: ein Kommando hat keine ableitbaren Ziele, also
    laesst sich gar nicht feststellen, ob es im Arbeitsbereich bleibt.
    """
    if spec is not None and spec.effect is Effect.EXEC:
        return Decision(Verdict.NEEDS_HUMAN, "autonomy 4 — shell asks (no derivable target)")
    targets = guard_targets(req)
    if not targets:
        return Decision(
            Verdict.NEEDS_HUMAN, "autonomy 4 — no free effect without a derivable target"
        )
    outside = [t for t in targets if not _inside(t, WORKSPACE)]
    if outside:
        return Decision(
            Verdict.NEEDS_HUMAN,
            "autonomy 4 — outside the workspace: " + ", ".join(outside),
        )
    return Decision(Verdict.ALLOW, "")


class AutonomyGovernor:
    """Haelt die Stufe. Thread-sicher, weil Poll-Thread stellt und Worker liest."""

    def __init__(self, level: int = DEFAULT_LEVEL) -> None:
        self._level = clamp(level)
        self._lock = threading.Lock()

    @property
    def level(self) -> int:
        with self._lock:
            return self._level

    def set_level(self, level: object, *, principal: Principal, allowed_identities: frozenset[Principal]) -> tuple[int, int]:
        """Stellt die Stufe. Gibt (vorher, nachher). Fehler lassen den Stand unberuehrt."""
        if principal not in allowed_identities:
            raise AutonomyError(f"identity not allowed: {principal}")
        try:
            value = int(str(level).strip())
        except (TypeError, ValueError):
            raise AutonomyError(f"not a level: {level}") from None
        if not MIN_LEVEL <= value <= MAX_LEVEL:
            raise AutonomyError(f"level outside {MIN_LEVEL}-{MAX_LEVEL}: {value}")
        with self._lock:
            before, self._level = self._level, value
        return before, value

    def apply(self, decision: Decision, req: ToolRequest, spec: ToolSpec | None) -> Decision:
        """Verrechnet Kernel-Urteil und Stufen-Decke: es gilt das strengere."""
        return stricter(decision, ceiling(self.level, req, spec))

    def describe(self) -> str:
        level = self.level
        return f"{level} ({LEVEL_NAMES[level]}) — {LEVEL_HELP[level]}"


@dataclass(frozen=True)
class GovernedKernel:
    """Der Kernel mit vorgeschalteten Decken — nach aussen derselbe `decide()`.

    Absichtlich als Huelle und nicht als Feld im Kernel: so gibt es im ganzen Prozess
    genau eine Stelle, an der ein Urteil entsteht, und jeder Aufrufer (Executor, Mint,
    Kommandos, Freigabe-Text) sieht dasselbe. Wer den Regler umgehen wollte, muesste
    sich den ungefilterten Kernel besorgen — der ist hier eingeschlossen.

    Zwei Decken liegen darueber, beide nur verschaerfend: der Autonomie-Regler (wie
    weit laesst the operator heute von der Leine) und die Kanal-Decke (wie viel beweist der
    Weg, auf dem die Anfrage hereinkam). Die Reihenfolge ist gleichgueltig — `stricter`
    ist kommutativ im Ergebnis; nur der mitgelieferte Grund haengt daran.

    `trust_of` hat bewusst **keinen Vorgabewert**. Ein Vorgabewert waere hier immer
    entweder unbrauchbar (alles zu) oder gefaehrlich (alles offen), und ein vergessener
    Parameter wuerde still die Decke abschalten. Ohne ihn gibt es kein Objekt.
    """

    kernel: PolicyKernel
    governor: AutonomyGovernor
    trust_of: TrustLookup
    # Die dritte Decke: waehrend eines zeitgesteuerten Laufs ist niemand da, der eine
    # Freigabe geben koennte — `NEEDS_HUMAN` wird deshalb zu `DENY` statt zu parken.
    # Optional, weil ein Prozess ohne Zeitplaene sie nicht braucht; fehlt sie, aendert
    # sich nichts. Wie die beiden anderen kann sie ausschliesslich verschaerfen.
    unattended: object | None = None
    # Die vierte Decke: waehrend eines delegierten Laufs darf nur gelesen werden. Auch
    # sie ist optional und auch sie kann ausschliesslich verschaerfen — beide gehen
    # durch dieselbe `stricter`-Stelle wie der Regler und die Kanal-Stufe.
    delegated: object | None = None

    @property
    def manifest(self) -> ToolManifest:
        return self.kernel.manifest

    @property
    def allowed_identities(self) -> frozenset[Principal]:
        return self.kernel.allowed_identities

    @property
    def shell_needs_human(self) -> bool:
        return self.kernel.shell_needs_human

    @property
    def vault_dir(self) -> Path:
        return self.kernel.vault_dir

    def guard_targets(self, req: ToolRequest) -> tuple[str, ...]:
        return self.kernel.guard_targets(req)

    def decide(self, req: ToolRequest) -> Decision:
        spec = self.kernel.manifest.get(req.tool)
        decision = self.governor.apply(self.kernel.decide(req), req, spec)
        decision = channel_trust.apply(self.trust_of(req.identity.channel), decision, spec)
        if self.unattended is not None:
            decision = self.unattended.apply(decision, spec)
        if self.delegated is not None:
            decision = self.delegated.apply(decision, spec)
        return decision


def restore_level(log, default: int = DEFAULT_LEVEL) -> int:
    """Letzte gesetzte Stufe aus dem Event-Log.

    Zwei Faelle, die man nicht verwechseln darf: ein **leeres** Log ist der erste
    Start — da gilt `default`. Ein **unlesbares** Log ist ein Defekt: dann ist auch
    nicht mehr feststellbar, ob the operator zuletzt zugedreht hat, und ohne Log gibt es
    ausserdem keinen Audit-Trail mehr. Beides zusammen heisst: nicht hochfahren.
    Wir fallen auf die sicherste Stufe, nicht auf die gewohnte.
    """
    try:
        rows = log.recent(1, ("autonomy.set",))
    except Exception:
        return MIN_LEVEL
    if not rows:
        return clamp(default)
    payload = rows[-1].get("payload") or {}
    if "level" not in payload:
        return clamp(default)
    return clamp(payload.get("level"))


def table() -> str:
    """Die Leiter als Text — fuer `/autonomy` ohne Argument."""
    return "\n".join(f"{n} {LEVEL_NAMES[n]:<15} {LEVEL_HELP[n]}" for n in range(MIN_LEVEL, MAX_LEVEL + 1))
