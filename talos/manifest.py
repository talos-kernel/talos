"""Tool-Manifest — jedes Tool deklariert seinen Effekt und seine Reversibilität.

Kernel-Spec §8: `name, effect(read|write|exec), reversible, requires_env, sandbox_required`.
Der Policy-Kernel (policy.py) berechnet daraus objektiv das Gating — kein LLM-Raten.
Das Manifest ist unveränderlich; `with_tool` liefert eine neue Kopie (Immutability-Regel).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Effect(str, Enum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"


@dataclass(frozen=True)
class ToolSpec:
    """Deklarierte Eigenschaften eines Tools. Quelle der Wahrheit fürs Gating."""

    name: str
    effect: Effect
    reversible: bool
    requires_env: frozenset[str] = frozenset()
    sandbox_required: bool = False
    # Die Wirkung geht nach aussen (entfernte Maschine, fremde API) — keine
    # Sandbox und keine Credential-Freiheit kann sie einfangen. Die
    # Attended-Auto-Freigabe (`autonomy.attended_routine`) schliesst solche
    # Werkzeuge per Bauart aus: ihre Routineklasse endet an der Aussengrenze.
    outward: bool = False


@dataclass(frozen=True)
class ToolManifest:
    """Unveränderliche Registry deklarierter Tools."""

    tools: tuple[ToolSpec, ...] = ()

    def get(self, name: str) -> ToolSpec | None:
        for spec in self.tools:
            if spec.name == name:
                return spec
        return None

    def with_tool(self, spec: ToolSpec) -> "ToolManifest":
        """Gibt ein NEUES Manifest mit dem zusätzlichen Tool zurück (nie mutieren)."""
        if self.get(spec.name) is not None:
            raise ValueError(f"Tool bereits registriert: {spec.name}")
        return ToolManifest(tools=self.tools + (spec,))
