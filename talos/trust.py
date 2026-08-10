"""Kanal-Decke — was ueber einen Kanal hoechstens Wirkung entfalten darf.

`channel.py` haelt die Stufen und sagt, was sie *bedeuten* sollen. Hier stehen sie
als Urteil — sonst ist eine Vertrauensstufe nur ein Kommentar. Genau das war sie in
der ersten Fassung von Schritt 4: `Trust.ASK` versprach im Docstring „Wirkt nicht",
im Code kam eine ASK-Nachricht aber ungebremst im Agent-Loop an. Auf Autonomie 5
haette der Kernel ein Schreiben erlaubt, und es waere gelaufen — auf Zuruf eines
Kanals, dessen Identitaet ein selbst getipptes Textfeld sein kann.

Zwei Decken, eine Bauart (`policy.stricter`): der Regler beantwortet *"wie viel soll
heute ohne Rueckfrage laufen?"*, die Kanal-Decke *"wie viel beweist der Weg, auf dem
das hereinkam?"*. Beide sehen das Kernel-Urteil und duerfen es nur verschaerfen. Die
hoechste Stufe ist damit exakt der ungefilterte Kernel — keine Decke vergibt Rechte.

**Warum ASK bei Wirkung DENY sagt und nicht NEEDS_HUMAN.** Freigeben kann dieser
Kanal per Definition nicht. Ein NEEDS_HUMAN wuerde eine Anfrage parken, die dort
niemand loesen kann: Talos wartet, der Nutzer sieht „Freigabe noetig" und es kommt
bis zum Ablauf der TTL nichts — eine Selbstblockade, die wie ein Defekt aussieht.
DENY ist die ehrliche Antwort: dieser Weg wirkt nicht.
"""
from __future__ import annotations

from typing import Callable

from .channel import Trust
from .manifest import Effect, ToolSpec
from .policy import Decision, Verdict, stricter

TrustLookup = Callable[[str], Trust]


def ceiling(trust: object, spec: ToolSpec | None) -> Decision:
    """Das Freizuegigste, was dieser Kanal zulaesst — unabhaengig vom Kernel-Urteil.

    `trust` ist bewusst `object`: die Stufe kommt aus einer Registry, die zur Laufzeit
    bestueckt wird. Was sich nicht als Stufe lesen laesst, ist keine — und bekommt
    das strengste Urteil, nicht das gewohnte.
    """
    try:
        level = Trust(trust)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return Decision(Verdict.DENY, f"unbekannte Vertrauensstufe: {trust!r}")

    if level is Trust.FULL:
        return Decision(Verdict.ALLOW, "")
    if level is Trust.NOTIFY:
        return Decision(Verdict.DENY, "Kanal nur Zustellung — eingehend ist kein Auftrag")

    # ASK: lesen ja, wirken nein. Ein unbekanntes Tool (spec is None) faellt hier
    # ebenfalls auf DENY — der Kernel lehnt es zwar ohnehin ab, aber eine Decke, die
    # bei Unkenntnis durchlaesst, ist keine.
    if spec is not None and spec.effect is Effect.READ:
        return Decision(Verdict.ALLOW, "")
    return Decision(Verdict.DENY, "Kanal darf fragen, nicht wirken (und nicht freigeben)")


def apply(trust: object, decision: Decision, spec: ToolSpec | None) -> Decision:
    """Verrechnet Kernel-Urteil und Kanal-Decke: es gilt das strengere."""
    return stricter(decision, ceiling(trust, spec))
