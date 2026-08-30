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
# Die Auto-Freigabe prueft Ziele gegen DIESELBEN Floor-Funktionen des Kernels,
# statt sie nachzubauen — eine zweite, abweichende Secret-Liste waere ein Loch.
from .policy import PERSISTENCE_PREFIXES, _derived_targets, _hits, _is_secret
from .trust import TrustLookup
from .vault import VaultPathError

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


# Grund-Praefix der Attended-Auto-Freigabe. Der Executor erkennt daran, dass ein
# ALLOW nicht vom rohen Kernel kam, und schreibt sein `approval.auto_attended` —
# die Freigabe ist leise fuer the operator, aber nie unsichtbar im Log.
AUTO_ATTENDED_REASON = "attended auto-approval"


def is_auto_attended(decision: Decision) -> bool:
    """War dieses ALLOW eine Attended-Auto-Freigabe? (Fuer den Beleg im Event-Log.)"""
    return (
        decision.verdict is Verdict.ALLOW
        and decision.reason.startswith(AUTO_ATTENDED_REASON)
    )


def attended_routine(req: ToolRequest, spec: ToolSpec | None, kernel: PolicyKernel) -> bool:
    """Die Routineklasse der Attended-Auto-Freigabe — aus Spec-Eigenschaften, nie aus Namen.

    Dazu gehoeren:
      * eingesperrte oder zugangsdatenfreie Ausfuehrung: EXEC-Werkzeuge, deren
        Wirkung entweder ganz ohne Credentials auskommt (`run_shell`: Sandbox
        ohne Netz und ohne Env) ODER per Bauart hinter einer Confinement-Wand
        stattfindet (`sandbox_required` — `delegate_code`: eigener OS-User,
        wegwerfbarer Workspace, Deadline, keine Talos-Secrets im Job). Bei
        beiden ist die Einsperrung die Sicherung, die ein Prompt sonst vertritt —
        der Owner will den Worker als Default-Weg fuer jede Aufgabe, und ein
        Prompt pro Delegation war genau die Reibung, die ihn davon abhielt
        (Owner-Entscheid 27.08.).
      * reversible Werkzeuge (Snapshot/`/undo`) — aber nur, wenn ihre Ziele
        keinen Floor beruehren. Ein reversibles Schreiben auf ein Secret oder
        eine Persistenz-Stelle (`~/.bashrc`, der eigene Code) ist keine Routine,
        sondern genau der Floor, der auch attended fragt.

    Nie dazu: EXEC mit `requires_env` OHNE Confinement — Wirkung auf
    konfigurierte Infrastruktur nach aussen (Versand mit Zugangsdaten). Und nie
    dazu: `outward`-Werkzeuge — ihre Wirkung liegt jenseits jeder Einsperrung
    (ferne Maschine, fremde API), egal welche anderen Eigenschaften sie tragen.
    Weil die Klasse aus den Spec-Eigenschaften abgeleitet ist, faellt ein neues
    Werkzeug automatisch richtig ein, statt auf einer Namensliste vergessen zu
    werden.
    """
    if spec is None:
        return False
    if spec.effect is Effect.EXEC:
        # `outward` zuerst: eine Wirkung jenseits der Aussengrenze (ferne Maschine,
        # fremde API) ist per Bauart keine Routine — sie kann nicht eingesperrt
        # werden, und genau das war die Voraussetzung der Auto-Freigabe.
        if spec.outward:
            return False
        return spec.sandbox_required or not spec.requires_env
    if not spec.reversible:
        return False
    try:
        targets = set(req.targets) | set(_derived_targets(req, kernel.vault_dir))
    except VaultPathError:
        return False
    return not any(_is_secret(t) or _hits(t, PERSISTENCE_PREFIXES) for t in targets)


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
    # Attended-Auto-Freigabe (Owner-Entscheid): in einem interaktiven Lauf — eine
    # eingehende Nachricht eines erlaubten Principals, ein Mensch kann hinschauen —
    # laeuft die Routineklasse (`attended_routine`) ohne Freigabe-Prompt. Sie ist
    # keine Decke und keine zweite Erlaubnisquelle: sie greift NUR, wenn der Kernel
    # selbst NEEDS_HUMAN gesagt hat (nie bei DENY — die Mauern bleiben), keine der
    # vier Decken zugeschlagen hat (unattended/delegated haetten laengst DENY
    # gemacht) und der Regler der Anfrage freie Hand gibt (wer die Leine kuerzer
    # stellt, WILL gefragt werden). Vorgabe AUS: ein vergessener Parameter darf nur
    # weniger erlauben, nie mehr — AN stellt sie die Config (`__main__`).
    attended_autoapprove: bool = False

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
        base = self.kernel.decide(req)
        decision = self.governor.apply(base, req, spec)
        decision = channel_trust.apply(self.trust_of(req.identity.channel), decision, spec)
        if self.unattended is not None:
            decision = self.unattended.apply(decision, spec)
        if self.delegated is not None:
            decision = self.delegated.apply(decision, spec)
        # Attended-Auto-Freigabe zuletzt, NACH allen Decken: haette eine davon
        # zugeschlagen (unattended/delegated -> DENY, Regler/Kanal strenger), ist
        # das Urteil hier nicht mehr das kernel-eigene NEEDS_HUMAN — und genau nur
        # dieses darf die Routineklasse ersetzen. Der Grund traegt das Praefix,
        # an dem der Executor seinen Log-Beleg erkennt.
        if (
            self.attended_autoapprove
            and base.verdict is Verdict.NEEDS_HUMAN
            and decision.verdict is Verdict.NEEDS_HUMAN
            and ceiling(self.governor.level, req, spec).verdict is Verdict.ALLOW
            and attended_routine(req, spec, self.kernel)
        ):
            decision = Decision(Verdict.ALLOW, f"{AUTO_ATTENDED_REASON}: {base.reason}")
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
