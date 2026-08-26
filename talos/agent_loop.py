"""Agent-Loop — der Reasoner schlägt Tool-Calls VOR, der Policy-Kernel führt aus.

Kernel-Spec v0.2 §1: „Reasoner strikt beratend, produziert typisierte Pläne, nie Tool-Calls.
Nur der Policy-Kernel führt aus." Genau das ist hier verdrahtet:
  propose(history) -> Text.  Enthält der Text eine `TOOL_CALL: {json}`-Zeile, wird sie
  geparst und durch den Executor gegated. Sonst ist der Text die finale Antwort.

Der Loop kennt weder Telegram noch claude — beides ist injiziert/darüber. So bleibt er testbar.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .channel import Principal
from .executor import Executor, Status
from .plan import BUDGET_REASON, PlanRun, parse_plan
from .policy import ToolRequest, guard_targets
from .redirect import Redirect
from .ux import SYM_FAIL, SYM_OK

Propose = Callable[[list[str]], str]
Progress = Callable[["AgentProgress"], None]
FinalCheck = Callable[[str, tuple[str, ...]], tuple[bool, str]]

# 8 war zu knapp fuer echte Mehrschritt-Auftraege: der Lauf brach
# mitten in legitimen Werkzeug-Kaskaden ab. 100 ist eine Notbremse gegen Endlosschleifen,
# keine Arbeitsgrenze — jeder einzelne Schritt passiert weiterhin einzeln den Kernel, und
# `/stop` bleibt jederzeit moeglich. Mehr Schritte = mehr Wirkungen pro Auftrag, aber
# keine neuen Rechte.
MAX_STEPS = 100
MAX_TOOL_RESULT_CHARS = 12_000
TOOL_RESULT_CUT = " […tool output truncated]"
_TOOL_RE = re.compile(r"^\s*TOOL_CALL:\s*(\{.*\})\s*$", re.MULTILINE | re.DOTALL)

# --- Fremdsyntax: der Ausfall, der wie eine Antwort aussieht ------------------------
# Der Reasoner laeuft als Subprozess einer CLI, die ihre EIGENE Werkzeug-Schreibweise
# kennt. Rutscht das Modell in die (`Read(/tmp/x)`, `<invoke name="Read">`), passiert
# genau nichts — die Zeile ist kein `TOOL_CALL`, also faellt sie durch als „finale
# Antwort", und der Betreiber bekommt eine Werkzeug-Notation vorgelegt, als waere sie
# das Ergebnis. Im e2e-Lauf gegen das echte Modell ist das reproduzierbar der erste
# Fall, und es ist der gefaehrlichste Fehlermodus ueberhaupt: er sieht aus wie Erfolg.
#
# Erkannt wird nur, was die GANZE Antwort ausmacht — eine Prosa-Antwort, die `Read(...)`
# beilaeufig erwaehnt, ist keine Fremdsyntax. Lieber einen Ausrutscher durchlassen als
# eine echte Antwort verschlucken.
_FOREIGN_XML = re.compile(r"^\s*<\s*(invoke|function_calls|antml:)", re.IGNORECASE)
_FOREIGN_CALL = re.compile(
    r"^\s*(?:Read|Write|Edit|MultiEdit|Bash|Glob|Grep|WebFetch|WebSearch|Task|NotebookEdit)"
    r"\s*\([^\n]*\)\s*$",
    re.IGNORECASE,
)
# Eine Fremdnotation ist kurz. Alles darueber ist Prosa, die zufaellig so anfaengt.
MAX_FOREIGN_CHARS = 300
# Zwei Nachfassversuche. Danach wird die Antwort ausgeliefert wie sie ist — eine
# Endlosschleife waere schlimmer als eine schiefe Antwort, und der Zaehler des Laufs
# laeuft ohnehin mit.
MAX_FOREIGN_RETRIES = 2
FOREIGN_NOTE = (
    "[Your last reply used another tool syntax. Nothing ran — this system reads only a "
    "single line 'TOOL_CALL: {\"tool\": ..., \"args\": {...}}'. Send that line, or answer "
    "in prose if you need no tool.]"
)


def looks_foreign(text: str) -> bool:
    """Ist diese Antwort in Wahrheit ein Werkzeugaufruf einer FREMDEN Notation?"""
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_FOREIGN_CHARS:
        return False
    return bool(_FOREIGN_XML.match(stripped) or _FOREIGN_CALL.match(stripped))


# Der zweite misslungene Zug — und der teurere, weil er wie eine Entscheidung aussieht:
# das Modell LEHNT DIE AUFGABE AB und begruendet es mit einer Schranke SEINES EIGENEN
# Prozesses („Plan-Modus ist aktiv und blockiert jede Tool-Ausfuehrung", „meine Tools sind
# deaktiviert", „ich laufe in einer Sandbox"). Genau die Schranken legt Talos ihm mit
# Absicht an (`reasoner.CLAUDE_ISOLATION_ARGV`), damit er nichts ausfuehren KANN. Er
# verwechselt „ich darf nichts ausfuehren" mit „ich darf nicht danach fragen".
#
# ⚠️ Der Prompt allein reicht nicht. Der Absatz in `reasoner.TOOL_PROTOCOL` steht genau
# dagegen und wurde gemessen: ohne ihn 1 von 4 Laeufen mit Werkzeugwunsch, mit ihm 2 von 4.
# Im e2e-Lauf auf der echten Maschine fielen daran 7 von 44 Faellen — Y1 lehnte ab, und
# Y2 bis Y7 kippten hinterher, weil ohne offene Freigabe nichts mehr zu beantworten war.
# Ein Prompt, der eine Neigung des Modells nur verschiebt, ist keine Grenze. Die Korrektur
# gehoert dorthin, wo der misslungene Zug ERKANNT wird — hierher, neben `looks_foreign`.
#
# Erkannt wird nur die Kombination aus Weigerung UND Selbstbezug. „Ich kann das nicht"
# allein ist oft die richtige Antwort (fehlende Angabe, unmoegliche Aufgabe); erst der
# Verweis auf die eigene Prozess-Schranke macht daraus den Fehler.
_SELF_BLOCK_REFUSAL = re.compile(
    r"\b(?:kann|darf|werde|geht)\s+(?:ich\s+)?(?:das\s+|dies\s+|es\s+)?nicht\b"
    r"|\bfuehre\s+.{0,40}\bnicht\s+aus\b"
    r"|\bführe\s+.{0,40}\bnicht\s+aus\b"
    r"|\b(?:i\s+)?(?:can(?:no|')t|cannot|am\s+unable\s+to|will\s+not|won't)\b"
    r"|\bnot\s+(?:able|permitted|allowed)\s+to\b",
    re.IGNORECASE,
)
_SELF_BLOCK_REASON = re.compile(
    r"\bplan[-\s]?mod(?:e|us)\b"
    r"|\bpermission[-\s]mode\b"
    r"|\b(?:my|meine[nr]?|eigene[nr]?)\s+(?:tools?|werkzeuge)\b.{0,40}\b(?:disabled|deaktiviert)\b"
    r"|\btools?\s+(?:are\s+)?(?:disabled|deaktiviert|abgeschaltet)\b"
    r"|\b(?:read[-\s]only|nur[-\s]lesend)\s+mod(?:e|us)\b"
    r"|\bsandbox\b.{0,60}\b(?:blockiert|blocks|prevents|verhindert)\b"
    r"|\b(?:blockiert|blocks|prevents|verhindert)\b.{0,60}\bausf(?:ü|ue)hrung\b",
    re.IGNORECASE,
)
# Eine solche Weigerung ist kurz und begruendet sich sofort. Ein langer Bericht, der das
# Wort „Plan-Modus" beilaeufig erwaehnt, ist keine Weigerung — und ihn zu verwerfen waere
# schlimmer als der Fehler, den wir suchen.
MAX_SELF_BLOCK_CHARS = 900
MAX_SELF_BLOCK_RETRIES = 1
SELF_BLOCK_NOTE = (
    "[You just declined the task citing a restriction of your OWN process (plan mode, "
    "disabled tools, a sandbox). That restriction is deliberate: you execute nothing, "
    "which is exactly why asking is safe. It does not limit what you may REQUEST. Emit "
    "the TOOL_CALL line for the tool you need — a separate security kernel judges it and "
    "asks the operator where required. If you genuinely need no tool, answer the task in "
    "prose, but do not refuse because of your own sandbox.]"
)

# A final-answer checker is deliberately allowed one correction round. It can make the
# run more conservative, never more powerful: its note is context for another model
# turn, and every resulting tool request still passes the same kernel.
MAX_FINAL_REVIEW_RETRIES = 1
MAX_REVIEW_NOTE_CHARS = 800


def looks_self_blocked(text: str) -> bool:
    """Lehnt diese Antwort die Aufgabe mit einer Schranke des eigenen Prozesses ab?"""
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_SELF_BLOCK_CHARS:
        return False
    return bool(_SELF_BLOCK_REFUSAL.search(stripped) and _SELF_BLOCK_REASON.search(stripped))


class AgentStatus(str, Enum):
    ANSWERED = "answered"
    NEEDS_HUMAN = "needs_human"
    STEP_LIMIT = "step_limit"
    # Ein angekuendigter Ablauf, der an einer Stelle haengenblieb. Bewusst NICHT
    # `STEP_LIMIT`: dort ist die Notbremse gefallen, hier hat der Lauf selbst gesagt,
    # was er vorhat, und das ist nicht eingetreten. Der Unterschied gehoert in den
    # Bericht, weil er unterschiedliche Reaktionen verlangt.
    PLAN_ABORTED = "plan_aborted"


class ProgressStage(str, Enum):
    """Kanal-neutrale Stationen eines Laufs; absichtlich ohne Telegram-Begriffe."""

    THINKING = "thinking"
    PLAN = "plan"
    TOOL = "tool"
    RESULT = "result"
    # ⚠️ Eine eigene Station, weil eine Korrektur mitten im Lauf sonst unsichtbar bliebe:
    # der Kanal soll sie anzeigen, und `talos why` soll sie im Protokoll finden. Ein Lauf,
    # der auf halber Strecke die Richtung wechselt, ohne dass irgendwo steht warum, ist
    # genau die Sorte Verlauf, die hinterher niemand mehr erklaeren kann.
    REDIRECTED = "redirected"


@dataclass(frozen=True)
class AgentProgress:
    """Fortschritt ohne Prompt, Ergebnis oder rohe Tool-Argumente.

    `step`/`max_steps` tragen den Zaehler bis in die Anzeige. Ohne sie konnte der Kanal
    nur „irgendwas laeuft" sagen — mit ihnen sieht the operator, ob ein Lauf vorankommt oder
    gleich ins Schritt-Limit rennt.
    """

    stage: ProgressStage
    tool: str = ""
    status: str = ""
    summary: str = ""
    step: int = 0
    max_steps: int = 0


@dataclass(frozen=True)
class AgentResult:
    status: AgentStatus
    text: str
    pending: ToolRequest | None = None
    steps: int = 0
    history: tuple[str, ...] = ()
    # Der angekuendigte Ablauf, falls es einen gab. Er reist mit, damit eine Freigabe
    # in der Mitte ihn nicht loescht: sonst waere „kurz etwas freigeben" der Weg,
    # Abbruchbedingung und Budget eines Plans loszuwerden.
    plan: PlanRun | None = None


def parse_tool_call(text: str) -> tuple[str, dict, tuple[str, ...]] | None:
    """Extrahiert (tool, args, targets) aus einer TOOL_CALL-Zeile — oder None."""
    match = _TOOL_RE.search(text)
    if match is None:
        return None
    try:
        obj = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    tool = str(obj.get("tool", ""))
    args = obj.get("args", {})
    targets = tuple(str(t) for t in obj.get("targets", []))
    if not tool or not isinstance(args, dict):
        return None
    return tool, args, targets


def run_agent(
    propose: Propose,
    executor: Executor,
    identity: Principal,
    run_id: str,
    *,
    max_steps: int = MAX_STEPS,
    progress: Progress | None = None,
    initial_history: tuple[str, ...] = (),
    steps_used: int = 0,
    plan: PlanRun | None = None,
    redirect: Redirect | None = None,
    final_check: FinalCheck | None = None,
) -> AgentResult:
    history = list(initial_history)
    active = plan
    foreign_retries = 0
    self_block_retries = 0
    final_review_retries = 0
    if steps_used >= max_steps:
        return AgentResult(
            AgentStatus.STEP_LIMIT,
            "Step limit reached.",
            steps=max_steps,
            history=tuple(history),
            plan=active,
        )
    for step in range(steps_used + 1, max_steps + 1):
        # Die Decke des Plans liegt UNTER dem Hausmass und wird hier zuerst geprueft:
        # ein Lauf, der mehr braucht als er angekuendigt hat, endet mit einem Bericht
        # darueber — nicht schweigend an der Notbremse hundert Schritte spaeter.
        if active is not None and not active.aborted and step > active.ceiling:
            stopped = active.abort(BUDGET_REASON)
            return AgentResult(
                AgentStatus.PLAN_ABORTED,
                stopped.report(),
                steps=step - 1,
                history=tuple(history),
                plan=stopped,
            )
        # Eine Korrektur des Betreibers wird HIER eingelegt — zwischen zwei Schritten,
        # bevor der naechste Zug aus der Historie gebildet wird. In einen laufenden
        # Modellaufruf greift niemand hinein; der ist ein blockierender Subprozess.
        # Sie ist ein Zug DESSELBEN Sprechers und traegt kein zusaetzliches Recht: jeder
        # Werkzeugaufruf danach geht durch denselben Kernel, und eine abgelehnte
        # Handlung wird durch eine nachgeschobene Nachricht nicht erlaubt.
        if redirect is not None:
            for korrektur in redirect.take():
                history.append(korrektur.as_turn())
                _emit(
                    progress,
                    AgentProgress(ProgressStage.REDIRECTED, step=step, max_steps=max_steps),
                )
        _emit(progress, AgentProgress(ProgressStage.THINKING, step=step, max_steps=max_steps))
        text = propose(history)

        # Der Plan wird genau einmal gelesen, im ersten Zug, der einen enthaelt. Danach
        # ist er fest: ein Werkzeug-Ergebnis ist fremder Text, und koennte es das Modell
        # zu einer zweiten, groesseren Ankuendigung bewegen, waere Prompt-Injection ein
        # Weg, sich mitten im Lauf Budget nachzukaufen.
        declared_now = False
        if active is None:
            declared = parse_plan(text)
            if declared is not None:
                active = PlanRun.begin(declared, at_step=step, hard_max=max_steps)
                declared_now = True
                # Die Quittung muss in den Verlauf, sonst kuendigt das Modell im
                # naechsten Zug dasselbe noch einmal an — und diese zweite Ankuendigung
                # traegt keinen Werkzeugwunsch mehr, wuerde also als fertige Antwort
                # gelten. Der Betreiber bekaeme den Plan statt seiner Ausfuehrung.
                history.append(
                    f"[plan recorded — {declared.headline()}. Now carry out the first step.]"
                )
                _emit(
                    progress,
                    AgentProgress(
                        ProgressStage.PLAN, summary=declared.headline(),
                        step=step, max_steps=max_steps,
                    ),
                )

        call = parse_tool_call(text)
        if call is None:
            # Eine Ankuendigung ohne ersten Schritt ist keine Antwort — sonst bekaeme der
            # Betreiber den Plan als Ergebnis vorgelegt, waehrend nichts davon geschah.
            if declared_now:
                continue
            # Fremdsyntax ist kein Ergebnis, sondern ein misslungener Zug. Einmal
            # nachfassen statt ausliefern: `Read(/tmp/x)` als Antwort zu praesentieren
            # hiesse, einen Ausfall als Erfolg zu verkaufen.
            if looks_foreign(text) and foreign_retries < MAX_FOREIGN_RETRIES:
                foreign_retries += 1
                history.append(FOREIGN_NOTE)
                continue
            # Eine Weigerung, die sich auf die eigene Prozess-Schranke beruft, ist
            # ebenfalls kein Ergebnis — sie sieht nur wie eines aus. Genau EIN Nachfassen:
            # bleibt es dabei, ist die Ablehnung die Antwort des Modells und wird
            # ausgeliefert. Zweimal dieselbe Belehrung schickt niemand.
            if looks_self_blocked(text) and self_block_retries < MAX_SELF_BLOCK_RETRIES:
                self_block_retries += 1
                history.append(SELF_BLOCK_NOTE)
                continue
            if final_check is not None:
                try:
                    review_ok, review_note = final_check(text.strip(), tuple(history))
                except Exception:
                    # Review is a quality guard, not a security gate. Its own failure
                    # must not make an otherwise working agent unavailable.
                    review_ok, review_note = True, ""
                if not review_ok:
                    note = " ".join(str(review_note).split())[:MAX_REVIEW_NOTE_CHARS]
                    if final_review_retries < MAX_FINAL_REVIEW_RETRIES:
                        final_review_retries += 1
                        history.append(
                            "[Answer review failed — do not claim completion yet. "
                            f"{note}. Obtain the exact missing evidence with a normal "
                            "TOOL_CALL, or state the uncertainty explicitly.]"
                        )
                        continue
                    text = f"{text.strip()}\n\n⚠ NOT VERIFIED — {note}"
            # Hier faellt das Urteil des Codes ueber eine Antwort, die das Modell selbst
            # geschrieben hat. Die Antwort bleibt vollstaendig stehen — sie zu verwerfen
            # hiesse, Brauchbares wegzuwerfen. Aber ihr „fertig" steht ab jetzt nicht
            # mehr unwidersprochen da, wenn eine angekuendigte Bedingung nie eintrat.
            return AgentResult(
                AgentStatus.ANSWERED,
                _with_verdict(text.strip(), active),
                steps=step,
                history=tuple(history),
                plan=active,
            )

        tool, args, targets = call
        summary = _safe_tool_summary(tool, args, targets)
        _emit(
            progress,
            AgentProgress(
                ProgressStage.TOOL, tool=tool, status="running", summary=summary,
                step=step, max_steps=max_steps,
            ),
        )
        req = ToolRequest(tool=tool, identity=identity, args=args, targets=targets)
        outcome = executor.run(req, run_id)
        _emit(
            progress,
            AgentProgress(
                ProgressStage.RESULT,
                tool=tool,
                status=outcome.status.value,
                summary=summary,
                step=step,
                max_steps=max_steps,
            ),
        )

        if outcome.status is Status.NEEDS_HUMAN:
            note = f"Needs your approval: {tool} ({outcome.detail})."
            return AgentResult(
                AgentStatus.NEEDS_HUMAN,
                note,
                pending=req,
                steps=step,
                history=tuple(history),
                plan=active,
            )

        history.append(tool_history_entry(tool, outcome.status.value, outcome.detail, outcome.result))

        if active is not None:
            active = active.record_call().observe(
                ok=outcome.status is Status.DONE,
                output=f"{outcome.detail} {'' if outcome.result is None else outcome.result}",
                targets=guard_targets(req),
            )
            # Die Abbruchbedingung. Ohne Plan improvisiert das Modell um einen
            # Fehlschlag herum — beim naechsten Versuch meist groesser als beim
            # ersten. Mit Plan endet der Lauf hier und sagt, woran.
            if outcome.status is not Status.DONE:
                stopped = active.abort(
                    f"{tool} — {outcome.status.value}: {outcome.detail}"
                )
                return AgentResult(
                    AgentStatus.PLAN_ABORTED,
                    stopped.report(),
                    steps=step,
                    history=tuple(history),
                    plan=stopped,
                )

    return AgentResult(
        AgentStatus.STEP_LIMIT,
        "Step limit reached.",
        steps=max_steps,
        history=tuple(history),
        plan=active,
    )


def _with_verdict(answer: str, plan: PlanRun | None) -> str:
    """Haengt das deterministische Urteil an — als Notiz, nie in die Prosa hinein.

    Dieselbe Bauart wie die Notiz einer stehenden Freigabe: die Antwort gehoert dem
    Modell, die Zeile darunter dem System. Kein Zeichen wandert in den Text selbst,
    und ohne angekuendigte Bedingungen entsteht gar keine Zeile — eine Quittung ueber
    nichts waere Laerm.
    """
    if plan is None:
        return answer
    urteil = plan.verdict()
    if not urteil:
        return answer
    mark = SYM_OK if not plan.unmet else SYM_FAIL
    return f"{answer}\n\n{mark} {urteil}"


def tool_history_entry(tool: str, status: str, detail: str, result: object | None) -> str:
    """Bound one untrusted tool result before it re-enters the reasoner prompt."""
    raw = f"[{tool} -> {status}] {detail} {'' if result is None else result}".strip()
    if len(raw) <= MAX_TOOL_RESULT_CHARS:
        return raw
    return raw[: MAX_TOOL_RESULT_CHARS - len(TOOL_RESULT_CUT)] + TOOL_RESULT_CUT


def _emit(callback: Progress | None, event: AgentProgress) -> None:
    """Die Anzeige ist Komfort: ein kaputter Status-Sink darf den echten Lauf nicht stoppen."""
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        pass


def _safe_tool_summary(tool: str, args: dict, targets: tuple[str, ...]) -> str:
    """Nur die Art der Arbeit beschreiben, nie Prompt, Inhalt oder vollen Shell-Befehl."""
    labels = {
        "read_file": "read",
        "write_file": "write",
        "run_shell": "shell",
        "undo_last": "undo",
        "vault_search": "search vault",
        "vault_get": "read vault note",
        "vault_write_note": "write vault note",
        "agent_consult": "consult second agent",
    }
    label = labels.get(tool, "run tool")
    if tool not in {"read_file", "write_file", "undo_last"}:
        return label
    raw = args.get("path")
    if raw is None and targets:
        raw = targets[0]
    if raw is None:
        return label
    name = os.path.basename(str(raw).rstrip("/"))[:80]
    lowered = name.lower()
    if not name or any(mark in lowered for mark in ("secret", "token", "credential", ".env", "key")):
        return f"{label} — protected file"
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return f"{label} — {name}" if name else label
