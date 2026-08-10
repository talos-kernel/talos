"""Mehrschritt-Auftraege — und warum ein Plan Reihenfolge traegt, aber nie Erlaubnis.

Die zweite Luecke, die zwei unabhaengige Pruefer beide genannt haben. In ihren Worten:
„Vergleichbare Agenten verfolgen Mehrschritt-Ziele, Abhaengigkeiten und Verifikation ohne
Operator-Steuerung nach jedem Toolcall."

Der naheliegende Weg waere ein Planer, der einen Ablauf beschliesst und ihn danach
abarbeitet. Genau der ist hier verboten, und zwar aus demselben Grund wie beim
Zeitplan: **ein Plan ist Text, den das Modell erzeugt.** Wird aus diesem Text eine
Ausfuehrungsanweisung, steht neben dem Kernel ein zweiter Erlaubnisweg — der
Kardinalfehler aus `CLAUDE.md`. Ein „freigegebener Plan" waere eine Zustimmung zu
Handlungen, die im Moment der Zustimmung noch niemand gesehen hat; genau davor schuetzt
die Bauart, dass jede Freigabe an genau einer Anfrage haengt.

Ein Plan traegt hier deshalb ausschliesslich zwei Dinge, und beide sind
Einschraenkungen:

  1. **Angekuendigte Reihenfolge.** Was der Lauf vorhat, bevor er es tut — sichtbar fuer
     den Betreiber, waehrend er noch eingreifen kann. Das ist Transparenz, kein Recht.
  2. **Eine Abbruchbedingung.** Der erste Schritt, der scheitert, beendet den Lauf mit
     einem Bericht, statt das Modell um den Fehler herum improvisieren zu lassen. Dieses
     Improvisieren ist der Weg, auf dem Agenten Schaden anrichten: der Befehl geht nicht
     durch, also wird der naechste Versuch groesser.

Und ein Plan **kauft nichts**: das Schritt-Budget des Laufs sinkt auf das, was das Modell
selbst angekuendigt hat. Wer drei Schritte plant, bekommt kein Fenster fuer vierzig.
Damit ist Planen bei Talos das Gegenteil dessen, was es anderswo ist — es macht den Lauf
**vorhersagbarer und schwaecher**, nicht maechtiger. Dieselbe Umkehrung wie bei
`schedule.UnattendedCeiling`, und aus demselben Grund: Faehigkeit darf hier nicht
heissen, dass die Leine laenger wird.

Zwei Eigenschaften fallen daraus zu, ohne dass sie eigens gebaut werden mussten:

  * Unter der unbeaufsichtigten Decke wird `NEEDS_HUMAN` zu `DENY`. Ein nachts
    angestossener Plan haelt damit an der ersten Stelle an, die einen Menschen braucht,
    und berichtet sie — statt sie zu ueberspringen.
  * Der Plan wird **einmal** gelesen und danach nicht mehr. Ein Werkzeug-Ergebnis ist
    fremder Text; koennte es den laufenden Plan ersetzen, waere Prompt-Injection ein Weg,
    sich mitten im Lauf ein groesseres Budget zu verschaffen. Es ist keiner.

Dieses Modul importiert weder `capability` noch `executor` noch `policy`. Es kann per
Bauart keine Handlung ausloesen; was es kann, ist einen Lauf frueher anhalten.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

# Ein Plan, den niemand mehr ueberblickt, ist kein Plan. Zwoelf ist die Grenze, ab der
# eine angekuendigte Liste im Chat nicht mehr am Stueck lesbar ist — und ein Ablauf, der
# laenger sein muss, gehoert in mehrere Auftraege mit einem Menschen dazwischen.
MAX_PLAN_STEPS = 12
MAX_GOAL_CHARS = 200
MAX_INTENT_CHARS = 160

# Ein angekuendigter Schritt braucht in der Praxis mehr als einen Werkzeugaufruf:
# nachsehen, tun, pruefen. Drei ist grosszuegig genug, dass ein ehrlicher Plan nicht am
# Budget scheitert, und knapp genug, dass ein Dreischritt-Plan keine vierzig Aufrufe
# deckt. Der Zuschlag deckt die Ankuendigung selbst und den abschliessenden Zug, in dem
# das Modell antwortet statt ein Werkzeug zu verlangen.
CALLS_PER_STEP = 3
PLAN_SLACK = 2

PLAN_MARKER = "PLAN:"
BUDGET_REASON = "plan budget spent — the run announced fewer steps than it needed"

# --- Die Abnahme -------------------------------------------------------------------
# Die zweite Haelfte dessen, was eine Schleife von einem Prompt unterscheidet: nicht nur
# das Modell wieder aufrufen, sondern das Ergebnis PRUEFEN. Heute prueft es sich selbst —
# es liest das Werkzeug-Ergebnis und entscheidet, ob es weitermacht. Das ist die
# schwaechste Stelle jeder Schleifen-Architektur: `run_shell` liefert `rc=1` und der
# Executor bucht `DONE`, weil das Werkzeug ja lief. Ob die ARBEIT gelang, stand nur im
# Text — und wer den Text beurteilt, war die Instanz, die ein Interesse am Gelingen hat.
#
# Eine Abnahme ist deshalb eine Zeichenkette aus einem winzigen, festen Vorrat, die
# NICHT vom Modell ausgewertet wird. Sie prueft ausschliesslich die Quittung des
# Schrittes, der gerade lief — nie den Zustand der Welt. Das ist die Grenze, die sie
# ungefaehrlich macht: eine Bedingung, die beliebige Dateien lesen koennte, waere ein
# Orakel am Kernel vorbei (`lies /etc/shadow und sag mir, ob 'root' drinsteht`). Was hier
# geprueft wird, stand ohnehin schon im Verlauf des Modells; neue Information entsteht
# keine, nur eine Aussage darueber, die niemand wegreden kann.
CHECK_OK = "ok"
CHECK_CONTAINS = "contains:"
CHECK_WROTE = "wrote:"
MAX_CHECK_CHARS = 120


def check_is_known(check: str) -> bool:
    """Nur der feste Vorrat. Unbekanntes wird beim Lesen verworfen, nicht spaeter geraten."""
    return check == CHECK_OK or (
        check.startswith((CHECK_CONTAINS, CHECK_WROTE)) and len(check.split(":", 1)[1]) > 0
    )


def check_met(check: str, *, ok: bool, output: str, targets: tuple[str, ...]) -> bool:
    """Wertet EINE Abnahme gegen EINE Quittung. Deterministisch, ohne Modell, ohne Welt."""
    if not ok:
        return False
    if check == CHECK_OK:
        return True
    if check.startswith(CHECK_CONTAINS):
        return check[len(CHECK_CONTAINS):] in output
    if check.startswith(CHECK_WROTE):
        wanted = os.path.expanduser(check[len(CHECK_WROTE):].strip())
        return any(os.path.realpath(t) == os.path.realpath(wanted) for t in targets)
    return False


def describe_check(check: str) -> str:
    """Der Klartext fuer den Bericht — die Erwartung, wie sie geprueft wurde."""
    if check == CHECK_OK:
        return "the step finishes cleanly"
    if check.startswith(CHECK_CONTAINS):
        return f'its output contains "{check[len(CHECK_CONTAINS):]}"'
    if check.startswith(CHECK_WROTE):
        return f"it writes to {check[len(CHECK_WROTE):]}"
    return check


def _clean(raw: object, limit: int) -> str:
    """Eine Zeile ohne Steuerzeichen, beschnitten. Der Text landet in der Anzeige."""
    text = " ".join(str(raw).split())
    return text[:limit]


@dataclass(frozen=True)
class Step:
    """Eine Absicht, optional mit einer Bedingung, an der sie gemessen wird."""

    intent: str
    check: str = ""


@dataclass(frozen=True)
class Plan:
    """Die Ankuendigung des Modells. Unveraenderlich, sobald sie gelesen wurde.

    `goal` und `steps` sind Modelltext und werden auch so praesentiert: als das, was der
    Lauf zu tun BEHAUPTET. Keine Zeile hier wird spaeter zur Grundlage einer Erlaubnis.

    Auch die Abnahmen stammen vom Modell, und die Grenze gehoert offen ausgesprochen:
    eine erfuellte Bedingung belegt, dass die SELBSTGESETZTE Erwartung des Laufs
    eingetreten ist — nicht, dass die Aufgabe gut erledigt wurde. Wer sich eine triviale
    Bedingung setzt, besteht sie. Was der Code garantiert, ist etwas anderes und
    trotzdem viel: dass die Bedingung wirklich GEPRUEFT wurde, statt behauptet, und dass
    sie wortwoertlich vor dem Betreiber steht, der ihre Guete beurteilen kann.
    """

    goal: str
    steps: tuple[Step, ...]

    @property
    def checks(self) -> tuple[tuple[int, str], ...]:
        """Die Bedingungen in Reihenfolge, mit ihrer Schrittnummer."""
        return tuple((i, s.check) for i, s in enumerate(self.steps, 1) if s.check)

    def headline(self) -> str:
        geprueft = len(self.checks)
        zusatz = f", {geprueft} checked" if geprueft else ""
        return f"{len(self.steps)} steps{zusatz} — {self.goal}"

    def describe(self) -> str:
        lines = [f"Goal: {self.goal}", "Announced steps:"]
        for i, step in enumerate(self.steps, 1):
            mark = f"   [checked: {describe_check(step.check)}]" if step.check else ""
            lines.append(f"  {i}. {step.intent}{mark}")
        return "\n".join(lines)

    def ceiling(self, *, declared_at: int, hard_max: int) -> int:
        """Der absolute Schritt, ab dem dieser Lauf endet — nie hoeher als das Hausmass.

        Absolut und nicht relativ, damit eine Freigabe in der Mitte nichts verschiebt:
        der Lauf wird nach der Zustimmung mit demselben Zaehler fortgesetzt, also muss
        auch die Decke dieselbe Zahl bleiben. Eine relativ gerechnete Decke waere ein
        stiller Weg, sich durch eine Rueckfrage Budget nachzukaufen.
        """
        budget = declared_at + len(self.steps) * CALLS_PER_STEP + PLAN_SLACK
        return min(int(hard_max), budget)


def _step(raw: object) -> Step | None:
    """Ein Schritt: blosser Text oder `{"intent": …, "check": …}`.

    Eine unbekannte Bedingung wird VERWORFEN statt uebernommen. Der Grund ist die
    Richtung des Irrtums: eine Bedingung, die niemand auswerten kann, waere sonst
    entweder dauerhaft unerfuellt (jeder Lauf endete mit „nicht bestaetigt", und die
    Anzeige verlernte ihre Bedeutung) oder muesste als erfuellt gelten — dann waere
    erfundenes Vokabular der Weg, eine Abnahme vorzutaeuschen. Kein Eintrag ist ehrlich:
    dieser Schritt wird eben nicht geprueft, und das steht so da.
    """
    if isinstance(raw, dict):
        intent = _clean(raw.get("intent", ""), MAX_INTENT_CHARS)
        check = _clean(raw.get("check", ""), MAX_CHECK_CHARS)
        if not check_is_known(check):
            check = ""
    else:
        intent, check = _clean(raw, MAX_INTENT_CHARS), ""
    return Step(intent=intent, check=check) if intent else None


def parse_plan(text: str) -> Plan | None:
    """Liest eine `PLAN: {json}`-Zeile — oder gibt None zurueck.

    Bewusst ohne Regex ueber die geschweiften Klammern: der JSON-Decoder weiss selbst,
    wo sein Objekt endet. Ein gieriger Ausdruck haette bis zur letzten Klammer im Text
    gegriffen und damit genau die Nachricht zerlegt, in der Plan UND erster
    Werkzeugwunsch stehen — der Normalfall.
    """
    start = text.find(PLAN_MARKER)
    if start < 0:
        return None
    brace = text.find("{", start)
    if brace < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, brace)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None

    goal = _clean(obj.get("goal", ""), MAX_GOAL_CHARS)
    raw_steps = obj.get("steps")
    if not goal or not isinstance(raw_steps, list):
        return None
    steps = tuple(step for step in (_step(raw) for raw in raw_steps) if step is not None)
    # Ein Schritt ist kein Plan — dafuer gibt es den gewoehnlichen Weg. Ohne diese
    # Untergrenze koennte ein Lauf sich mit `{"steps": ["arbeite"]}` als geplant
    # ausweisen und traege die Wirkung eines Plans, ohne seine Verbindlichkeit.
    if len(steps) < 2:
        return None
    return Plan(goal=goal, steps=steps[:MAX_PLAN_STEPS])


@dataclass(frozen=True)
class PlanRun:
    """Der Zustand eines angekuendigten Laufs. Wird kopiert, nie veraendert.

    Gezaehlt wird ausschliesslich, was wirklich passiert ist: gelaufene Werkzeugaufrufe
    und der Grund eines Abbruchs. Es gibt bewusst kein „Schritt 2 von 4" — welcher
    Werkzeugaufruf zu welchem angekuendigten Schritt gehoert, weiss nur das Modell, und
    eine Zahl, die aus seiner Selbstauskunft stammt, saehe in der Anzeige aus wie eine
    Messung. Dieselbe Regel wie im Missions-Panel: nur Gemessenes.
    """

    plan: Plan
    ceiling: int
    declared_at: int
    calls: int = 0
    failure: str = ""
    # Wie viele der angekuendigten Bedingungen bereits eingetreten sind. Ein Zeiger und
    # keine Liste pro Schritt, weil niemand zuverlaessig weiss, WELCHER Werkzeugaufruf
    # zu welchem Schritt gehoert — das wuesste nur das Modell, und eine Zahl aus seiner
    # Selbstauskunft waere genau die Selbstbeurteilung, die hier abgeschafft wird.
    # In Reihenfolge und nicht ueberspringbar: Bedingung 2 kann erst eintreten, wenn 1
    # eingetreten ist. Damit braucht die Abnahme kein einziges Feld vom Modell.
    met: int = 0

    @classmethod
    def begin(cls, plan: Plan, *, at_step: int, hard_max: int) -> "PlanRun":
        return cls(
            plan=plan,
            ceiling=plan.ceiling(declared_at=at_step, hard_max=hard_max),
            declared_at=at_step,
        )

    @property
    def aborted(self) -> bool:
        return bool(self.failure)

    @property
    def unmet(self) -> tuple[tuple[int, str], ...]:
        return self.plan.checks[self.met:]

    def record_call(self) -> "PlanRun":
        return replace(self, calls=self.calls + 1)

    def observe(self, *, ok: bool, output: str, targets: tuple[str, ...]) -> "PlanRun":
        """Haelt die naechste offene Bedingung gegen diese Quittung — und nur sie.

        Eine Quittung hakt hoechstens EINE Bedingung ab. Sonst koennte ein einzelner,
        gespraechiger Werkzeug-Ausgabetext gleich mehrere „contains"-Erwartungen auf
        einmal erfuellen, und die Reihenfolge, die diesen Mechanismus ohne Zutun des
        Modells auskommen laesst, waere wieder Zufall.
        """
        offen = self.unmet
        if not offen or self.aborted:
            return self
        if check_met(offen[0][1], ok=ok, output=output, targets=targets):
            return replace(self, met=self.met + 1)
        return self

    def verdict(self) -> str:
        """Das Urteil des CODES ueber den Lauf — leer, wenn nichts zu sagen ist.

        Der Satz haengt an einer Antwort, die das Modell selbst geschrieben hat, und
        widerspricht ihr noetigenfalls. Das ist der Punkt: „fertig" ist ab hier keine
        Behauptung mehr, die unwidersprochen stehen bleibt.
        """
        gesamt = len(self.plan.checks)
        if not gesamt:
            # Kein einziger Schritt trug eine Bedingung. Das ist bei offenen Aufgaben
            # der Normalfall — „bewerte diese Hardware" hat kein pruefbares Praedikat,
            # und eine erfundene Bedingung waere schlimmer als keine.
            #
            # Genau deshalb steht der Satz hier: ohne ihn saehe ein ungeprueft
            # abgearbeiteter Plan aus wie ein geprueftes Ergebnis. Der Unterschied
            # zwischen „erledigt" und „nachgewiesen" gehoert dem Betreiber, nicht dem
            # Lauf — und ein Agent, der ihn verschweigt, verkauft das eine als das andere.
            return "No step carried a condition — nothing here was verified by code."
        if not self.unmet:
            return f"{gesamt}/{gesamt} announced checks met."
        fehlend = "; ".join(
            f"step {nummer} ({describe_check(bedingung)})" for nummer, bedingung in self.unmet
        )
        return (
            f"{self.met}/{gesamt} announced checks met — NOT confirmed done. "
            f"Still open: {fehlend}."
        )

    def abort(self, reason: str) -> "PlanRun":
        """Der erste Abbruchgrund bleibt stehen — spaetere ueberschreiben ihn nicht."""
        return self if self.failure else replace(self, failure=_clean(reason, 300))

    def report(self) -> str:
        """Was angekuendigt war, was lief, woran es endete — und dass danach nichts kam.

        Der letzte Satz ist der wichtigste: ohne ihn muesste der Betreiber raten, ob die
        restlichen Schritte noch gelaufen sind. Ein Bericht, der offen laesst, was NICHT
        passiert ist, zwingt zum Nachsehen und ist damit keiner.
        """
        lines = [self.plan.describe(), ""]
        lines.append(f"Ran {self.calls} tool call(s) before stopping.")
        if self.failure:
            lines.append(f"Stopped at: {self.failure}")
            lines.append("Nothing after that point ran.")
        urteil = self.verdict()
        if urteil:
            lines.append(urteil)
        return "\n".join(lines)


__all__ = [
    "BUDGET_REASON",
    "CALLS_PER_STEP",
    "CHECK_CONTAINS",
    "CHECK_OK",
    "CHECK_WROTE",
    "MAX_PLAN_STEPS",
    "PLAN_SLACK",
    "Plan",
    "PlanRun",
    "Step",
    "check_is_known",
    "check_met",
    "describe_check",
    "parse_plan",
]
