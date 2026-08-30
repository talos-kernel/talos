"""Conductor — die Orchestrierung: Ingest -> Log -> Agent-Loop -> (Freigabe) -> Reply.

Jeder Schritt wird als Event protokolliert (Observability + Audit + Wiederaufnahme).
Reasoner, Executor und Sender sind injiziert (testbar ohne echtes Telegram/Modell).

Pro Nachricht zwei Wege — und sie kreuzen sich nie:
  1. **Freigabe-Runde**: liegt für die Konversation eine geparkte NEEDS_HUMAN-Anfrage offen,
     IST die Nachricht des Betreibers Entscheid. „ja" führt genau diese eine Anfrage aus (erneut durch
     den Kernel, mit human_approved=True), „nein" verwirft, „immer" führt aus und legt für
     genau diese Handlung eine stehende Freigabe an, alles andere fragt erneut. One-shot
     mit TTL; nach dem Entscheid ist der Store leer. Der Reasoner wird hier NIE befragt —
     sein Output darf niemals als „ja" gelesen werden.

     Eine stehende Freigabe ersetzt an genau einer Stelle das getippte „ja" — sie ist keine
     zweite Erlaubnisquelle. Der Kernel läuft danach vollständig neu; DENY bleibt DENY, und
     der Autonomie-Regler steht davor. Siehe `standing.py`.
  2. **Agent-Loop**: sonst schlägt der Reasoner Tool-Calls vor, der Policy-Kernel im Executor
     gated sie. NEEDS_HUMAN parkt die exakte Anfrage und fragt den Betreiber mit den Kernel-Fakten.

**Zwei Gates, zwei Zuständigkeiten.** Der Kernel entscheidet über *Wirkung* (welches Tool
darf was anfassen). Hier entscheidet die *Vertrauensstufe des Kanals* über die Steuerung:
freigeben und den Autonomie-Regler stellen darf nur ein Kanal, dessen Identität etwas
beweist. Beides ist ausschließlich einschränkend — kein Kanal bekommt hier ein Recht,
das der Kernel nicht ohnehin gäbe.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import inspect
from pathlib import Path
import threading
import time
from typing import Callable, Iterator, Protocol

from .agent_loop import AgentStatus, run_agent, tool_history_entry
from .approval import ApprovalPicker, ApprovalStore, Pending, is_affirmative, is_always, is_negative
from .attachment import extract as extract_media
from .attachment import resolve as resolve_media
from .capability import action_fingerprint
from .channel import Activity, Inbound, Principal, StructuredMessage, Trust
from .commands import CommandCenter, is_command, parse
from .eventlog import Event, EventLog, new_run_id
from .executor import Executor, Outcome, Status
from .memory import Memory, Turn, render
from .plan import PlanRun
from .policy import ToolRequest, command_risk_paths, guard_targets
from .question import CALLBACK_PREFIX as QUESTION_PREFIX, Answer, QuestionDesk, SKIP_WORDS
from .reasoner import Reasoner
from .redirect import Redirect
from .standing import StandingStore
from .ux import SYM_GATE

Sender = Callable[[str, str], None]
TrustLookup = Callable[[str], Trust]
ActivityBegin = Callable[[str], Activity | None]
StructuredSender = Callable[[str, StructuredMessage], None]


class ReplyStream(Protocol):
    """Eine Antwort, die waehrend des Denkens mitwaechst — und am Ende die endgueltige ist.

    Steht hier statt neben `Activity` in `channel.py`, damit dieser Schritt rein additiv
    bleibt; inhaltlich gehoert er dorthin. Der Conductor kennt weiterhin keinen Anbieter,
    nur diese vier Methoden:

      `begin_turn` — neuer Reasoner-Zug, neue Entscheidung ueber Prosa vs. Maschinerie
      `push`       — ein Text-Delta (die Senke, die der Reasoner fuettert)
      `adopt`      — die gewachsene Nachricht wird die endgueltige Antwort; `False` = ging nicht
      `settle`     — einfrieren, weil die Antwort einen anderen Weg nimmt
    """

    def begin_turn(self) -> None: ...

    def push(self, delta: str) -> None: ...

    def adopt(self, text: str) -> bool: ...

    def settle(self) -> None: ...


ReplyBegin = Callable[[str], ReplyStream | None]

# Kommandos, die den Zustand von Talos selbst verstellen. Sie brauchen einen Kanal,
# dessen Identität trägt — nicht bloß eine zugelassene Kennung.
CONTROL_COMMANDS: frozenset[str] = frozenset(
    {"autonomy", "approve", "deny", "undo", "stop", "revoke", "model"}
)

# Kommandos, die einen laufenden Auftrag beenden. Eine offene Rückfrage gehört zu
# diesem Auftrag und muss mit ihm sterben — sonst wartet der Worker weiter auf eine
# Antwort, die niemand mehr geben will.
ABORT_COMMANDS: frozenset[str] = frozenset({"stop", "cancel"})


@dataclass(frozen=True)
class AskContext:
    """Wem gegenüber ein laufender Auftrag eine Rückfrage stellen darf."""

    principal: Principal
    conversation: str
    trust: Trust


class AskContexts:
    """Wer gerade in welchem Chat denkt — je ausführendem Thread ein Eintrag.

    Der `ask_operator`-Runner sitzt tief im Agent-Loop und bekommt nur eine
    `ToolRequest`. Chat und Vertrauensstufe stehen dort nicht drin — und dürfen dort
    auch nie hin: käme beides aus den Argumenten, entschiede das Modell, WEN es fragt
    und mit welcher Stufe. Deshalb hinterlegt der Conductor sie hier, am Thread, der
    den Lauf ausführt, und der Runner liest sie nur.
    """

    def __init__(self) -> None:
        self._by_thread: dict[int, AskContext] = {}
        self._lock = threading.Lock()

    @contextmanager
    def active(self, context: AskContext) -> Iterator[None]:
        key = threading.get_ident()
        with self._lock:
            previous = self._by_thread.get(key)
            self._by_thread = {**self._by_thread, key: context}
        try:
            yield
        finally:
            with self._lock:
                rest = {k: v for k, v in self._by_thread.items() if k != key}
                # Wiederherstellen statt löschen: ein Lauf kann einen zweiten anstossen
                # (stehende Freigabe -> `_park` -> `_run_task`), und der äussere behält
                # danach seinen eigenen Rückweg.
                self._by_thread = rest if previous is None else {**rest, key: previous}

    def current(self) -> AskContext | None:
        return self._by_thread.get(threading.get_ident())

    def conversations(self) -> tuple[str, ...]:
        """Alle Chats mit einem laufenden Auftrag — für den Abbruch beim Herunterfahren."""
        return tuple(dict.fromkeys(c.conversation for c in tuple(self._by_thread.values())))


# Wie weit zurueck fuer die Lehren gelesen wird. Gross genug fuer ein Muster,
# klein genug, dass es jeden Zug nicht spuerbar verlangsamt.
LESSON_WINDOW = 400

# Der Selbstreview. Taeglich, weil eine fehlende Bibliothek keine Stunde drueckt und ein
# Bericht pro Stunde nur beibringt, ihn wegzuwischen. Das Fenster ist groesser als bei den
# Lehren: dort geht es um den naechsten Zug, hier um ein Muster ueber Tage.
REVIEW_INTERVAL_S = 24 * 60 * 60
REVIEW_WINDOW = 1_500
REVIEW_TYPES = ("exec.intent", "exec.result", "grant.issued", "review.reported")


@dataclass(frozen=True)
class Conductor:
    """`trust_of` ohne Vorgabewert — aus demselben Grund wie bei `GovernedKernel`.

    Die erste Fassung fiel ohne Registry auf `Trust.FULL` zurück. Das ist genau die
    Richtung, in die ein Vorgabewert nie fallen darf: ein vergessener Parameter hätte
    still jeden Kanal zu des Betreibers Stimme gemacht. Die Registry fällt bei einem unbekannten
    Kanal auf `NOTIFY` — hier gibt es gar keinen Fall zum Fallen.
    """

    log: EventLog
    reasoner: Reasoner
    executor: Executor
    send: Sender
    allowed_principals: frozenset[Principal]
    trust_of: TrustLookup
    approvals: ApprovalStore = field(default_factory=ApprovalStore)
    approval_picker: ApprovalPicker | None = None
    commands: CommandCenter | None = None
    memory: Memory = field(default_factory=Memory)
    # Leer per Vorgabe: ein vergessener Parameter darf nur weniger erlauben, nie mehr.
    # In `__main__` kommt der Stand aus dem Event-Log (`standing.restore`).
    standing: StandingStore = field(default_factory=StandingStore)
    begin_activity: ActivityBegin | None = None
    # Ohne diese Senke verhaelt sich alles exakt wie bisher: die Antwort kommt am Stueck.
    # Kanaele ohne Live-Bearbeitung (Mail, CLI, Tests) liefern hier schlicht nichts.
    begin_reply: ReplyBegin | None = None
    send_structured: StructuredSender | None = None
    # Dateianhaenge aus MEDIA:-Tags (`attachment.py`): (conversation, gegateter Pfad)
    # -> bool. `False` heisst: der Kanal kann keine Dateien — ein ehrliches Nein, kein
    # Fehler. Injiziert wie `send_structured`; `None` = kein Weg verdrahtet.
    send_file: "Callable[[str, str], bool] | None" = None
    # Liefert die Quittungszeile unter den Verlauf (Werkzeuge, Dauer, Token, Modell).
    # Injiziert statt aus dem Meter gezogen: der Conductor soll die Messung nicht kennen,
    # und Tests brauchen dafuer keinen echten Reasoner.
    usage_footer: Callable[[], str] | None = None
    # Was der Maschine gerade fehlt, damit „ich kann das nicht" nie die ganze Antwort
    # ist. Injiziert aus demselben Grund wie die Quittung: der Conductor soll den Doktor
    # nicht kennen, und ein Test soll dafuer kein ffmpeg installieren muessen.
    # ⚠️ Diese Quelle kennt nur MAENGEL. Urteile des Kernels kommen hier nie an — der
    # Unterschied zwischen „kann nicht" und „darf nicht" ist der ganze Grund fuer das
    # eigene Feld (siehe `remedy.py`).
    capability_gaps: Callable[[], tuple[tuple[str, str], ...]] | None = None
    # Die Decke fuer Laeufe, vor denen niemand sitzt (`/background`). Ohne sie wird ein
    # Hintergrundauftrag ABGELEHNT, nicht ungedeckelt ausgefuehrt — ein vergessener
    # Parameter darf nur weniger erlauben, nie mehr. Dieselbe Instanz wie beim Zeitplan.
    unattended: object | None = None
    background: object = field(default_factory=lambda: __import__(
        "talos.background", fromlist=["BackgroundDesk"]).BackgroundDesk())
    # Langzeitgedaechtnis, nur LESEND. Geschrieben wird ausschliesslich ueber ein
    # Kommando des Betreibers — es gibt bewusst kein Werkzeug, mit dem sich das
    # Modell selbst etwas merken koennte. Sonst waere ein einziger erfolgreicher
    # Einfluesterungsversuch in jedem kuenftigen Lauf wieder dabei.
    recall: object | None = None
    # Lernschritt NACH zugestellter Antwort (`distill.py`): deterministischer
    # Trigger (nur Laeufe mit echtem Werkzeugeinsatz), Auswahl durch das Modell,
    # Bilanz aus dem Event-Log. Vorgabe AUS — ein vergessener Parameter darf nur
    # weniger koennen; `__main__` schaltet ueber TALOS_DISTILL (Vorgabe an).
    distill: bool = False
    # Entity knowledge, derived working state and deterministic final-answer review.
    # It is quality control, never an authority source: the layer can request one retry
    # or qualify an answer, but every resulting tool call still passes the same kernel.
    intelligence: object | None = None
    # Gespraechsarchiv (session_search). Der Conductor SCHREIBT nur — am selben Punkt
    # wie `memory.remember`, also ausschliesslich zugestellte, beantwortete Zuege.
    # Gelesen wird nie automatisch: der Rueckweg in einen Prompt fuehrt allein ueber
    # das gegatete `session_search`-Werkzeug, das der Betreiber im Verlauf sieht.
    transcript: object | None = None
    # Rückfragen des Agenten. EINE Instanz, geteilt zwischen Poll-Thread (löst Klick
    # oder Zahl ein) und Worker (wartet darauf). Sie ist kein zweiter Erlaubnisweg:
    # eine Antwort gibt nichts frei, sie geht als unvertraute Daten in den Lauf zurück.
    questions: QuestionDesk = field(default_factory=QuestionDesk, compare=False, repr=False)
    ask_contexts: AskContexts = field(default_factory=AskContexts, compare=False, repr=False)
    # Postfach fuer eine Korrektur am LAUFENDEN Auftrag. EINE Instanz, geteilt zwischen
    # Poll-Thread (legt ab) und Worker (nimmt). Wie `questions` ist es kein zweiter
    # Erlaubnisweg: die Korrektur ist ein Zug desselben Sprechers und geht als solcher in
    # die Historie — jeder Werkzeugaufruf danach passiert denselben Kernel.
    redirect: Redirect = field(default_factory=Redirect, compare=False, repr=False)
    execution_lock: threading.RLock = field(
        default_factory=threading.RLock, compare=False, repr=False
    )

    def is_inline(self, update: Inbound) -> bool:
        """True = sofort im Poll-Thread beantworten, statt in die Warteschlange zu legen.

        Kommandos und bedeutungslose blanke ja/nein-Antworten bleiben sofort. Echte
        Freigaben laufen dagegen im Worker: Callback-Ack geschieht dort vor der Wirkung,
        waehrend der Poll-Thread fuer `/stop` frei bleibt.
        """
        if update.callback is not None:
            # Die Antwort auf eine Rückfrage MUSS sofort bleiben. Der Worker hat genau
            # einen Platz und steht gerade in `wait()` — käme die Antwort in seine
            # Warteschlange, wartete er auf sich selbst, bis das Zeitlimit ihn erlöst.
            if update.callback.data.startswith(QUESTION_PREFIX):
                return True
            if update.callback.data.startswith(ApprovalPicker.PREFIX):
                return False
            parts = update.callback.data.split(":")
            # Provider/page navigation is instant. Only the final model selection can
            # launch a validation subprocess and therefore belongs on the worker.
            return not (len(parts) == 4 and parts[0] == "tm" and parts[2] == "m")
        if is_command(update.text):
            # `/retry`, approvals and an exact typed model switch can block or execute.
            # They run in the Worker so Telegram keeps polling and `/stop` stays live.
            name, rest = parse(update.text)
            if name in {"retry", "approve", "deny"}:
                return False
            if name == "model" and len(rest.split()) == 2:
                return False
            return True
        if self.approvals.get(update.conversation) is not None:
            return False
        if self._looks_like_answer(update):
            return True
        return is_affirmative(update.text) or is_always(update.text) or is_negative(update.text)

    def _looks_like_answer(self, update: Inbound) -> bool:
        """Nur eine WEGWAHL, kein Einlösen — `is_inline` darf nichts verbrauchen.

        Spiegelt bewusst knapp, was `QuestionDesk.resolve_text` annimmt. Ein „nein" hier
        ist harmlos: die Nachricht geht in die Warteschlange und läuft, wenn die Frage
        erledigt ist. Ein falsches „ja" wäre teuer — der Poll-Thread liefe in dasselbe
        Ausführungsschloss, das der wartende Worker hält. Deshalb im Zweifel eng.
        """
        ticket = self.questions.pending(update.conversation)
        if ticket is None:
            return False
        raw = update.text.strip().lower()
        if raw in SKIP_WORDS:
            return True
        number = raw.rstrip(").").strip()
        return number.isdigit() and 1 <= int(number) <= len(ticket.entry.options)

    def handle(self, update: Inbound) -> bool:
        """Verarbeitet eine Nachricht. False, wenn sie übersprungen wurde (Dublette/fremd)."""
        run_id = new_run_id()
        fresh = self.log.append(
            Event(
                run_id=run_id,
                actor="ingress",
                type="task.received",
                payload={"principal": str(update.principal), "conversation": update.conversation},
                idempotency_key=update.dedup_key,
            )
        )
        if not fresh:
            return False  # dieselbe Nachricht schon verarbeitet -> idempotent überspringen

        # Nur zugelassene Identitäten — und die sind kanal-qualifiziert. Dieselbe Nummer
        # auf einem anderen Kanal ist eine andere Identität, nicht dieselbe.
        if update.principal not in self.allowed_principals:
            self.log.append(
                Event(run_id, "policy", "task.rejected", {"principal": str(update.principal)})
            )
            return False

        # Ein Kanal auf NOTIFY liefert nur aus. Was von dort hereinkommt, ist kein Auftrag —
        # auch dann nicht, wenn die Kennung stimmt.
        trust = self.trust_of(update.channel)
        if trust is Trust.NOTIFY:
            self.log.append(
                Event(run_id, "policy", "task.rejected",
                      {"principal": str(update.principal), "reason": "kanal_nur_zustellung"})
            )
            return False

        # Callback-Daten sind nur ein opaker Transport. Erst nachdem Principal und Kanal
        # geprüft sind, darf serverseitiger State daraus eine Aktion ableiten.
        if update.callback is not None:
            callback = update.callback
            # Rückfrage VOR der Freigabe — erkannt am eigenen Präfix, nicht an der
            # Reihenfolge allein: beide Wege tragen opake Token, und ein Token des einen
            # ist für den anderen strukturell unlesbar. `FULL` verlangt dieser Zweig
            # bewusst nicht — antworten gibt nichts frei (siehe question.py), und alles
            # mit Wirkung liegt weiterhin hinter der Decke darunter.
            if callback.data.startswith(QUESTION_PREFIX):
                return self._answer_by_button(update, run_id, callback)
            if trust is not Trust.FULL:
                message = StructuredMessage(_no_control(update.channel, trust))
            elif callback.data.startswith(ApprovalPicker.PREFIX):
                pending = self.approvals.get(update.conversation)
                decision = (
                    self.approval_picker.consume(
                        callback.data,
                        principal=update.principal,
                        conversation=update.conversation,
                        pending=pending,
                    )
                    if self.approval_picker is not None
                    else None
                )
                if decision is not None and pending is not None:
                    self._ack_approval_callback(update, run_id, pending)
                    return self._resolve_approval(update, run_id, pending, decision)
                message = StructuredMessage(
                    "Approval invalid or expired. Nothing ran.",
                    callback_notice="Invalid or expired",
                )
            else:
                picker = self.commands.model_picker if self.commands is not None else None
                if picker is None:
                    message = StructuredMessage("Picker invalid or expired. Nothing changed.")
                elif len(callback.data.split(":")) == 4 and callback.data.split(":")[2] == "m":
                    with self.execution_lock:
                        message = picker.handle(
                            callback.data,
                            principal=update.principal,
                            conversation=update.conversation,
                        )
                else:
                    message = picker.handle(
                        callback.data,
                        principal=update.principal,
                        conversation=update.conversation,
                    )
            message = replace(
                message,
                edit_message_id=callback.message_id,
                callback_query_id=callback.query_id,
            )
            return self._reply_structured(update, run_id, message)

        # Kommandos zuerst — sie sind deterministisch und müssen auch bei offener Freigabe
        # durchkommen (`/pending` darf nicht als „weder ja noch nein" abgewiesen werden).
        text = update.text
        if self.commands is not None and is_command(text):
            name, rest = parse(text)
            self.log.append(
                Event(run_id, "human", "command", {"name": name, "principal": str(update.principal)})
            )
            if name in CONTROL_COMMANDS and trust is not Trust.FULL:
                self.log.append(Event(run_id, "policy", "control.rejected",
                                      {"name": name, "channel": update.channel}))
                return self._reply(update, run_id, _no_control(update.channel, trust))
            # Vor dem Kommando, nicht danach: `/stop` soll den wartenden Worker sofort
            # loslassen, statt ihn bis zum Zeitlimit der Rückfrage stehen zu lassen.
            if name in ABORT_COMMANDS:
                self.questions.cancel(update.conversation)
            if name == "model" and len(rest.split()) == 2:
                with self.execution_lock:
                    result = self.commands.dispatch(
                        name, rest, principal=update.principal, conversation=update.conversation
                    )
            else:
                result = self.commands.dispatch(
                    name, rest, principal=update.principal, conversation=update.conversation
                )
            if result.request is not None:
                return self._dispatch_request(update, run_id, result.request, result.reply or "")
            if result.background is not None:
                return self._start_background(update, run_id, result.background)
            if result.structured is not None:
                return self._reply_structured(update, run_id, result.structured)
            if result.forward_as is None:
                return self._reply(update, run_id, result.reply or "(keine Antwort)")
            text = result.forward_as  # /approve -> „ja": weiter wie eine normale Antwort

        # Offene Rückfrage vor der Freigabe-Runde — und ebenfalls über die Form, nicht
        # über die Position: `resolve_text` nimmt nur Zahlen und Abbruchwörter, die
        # Freigabe nur ja/immer/nein. Ein „ja" fällt hier also durch, eine „2" käme
        # unten nur als „weder ja noch nein" an. Beides zusammen offen kann es nicht
        # geben: geparkt wird erst, wenn ein Lauf endet — gefragt wird mitten darin.
        if self.questions.pending(update.conversation) is not None:
            return self._answer_by_text(update, run_id, text)

        pending = self.approvals.get(update.conversation)
        if pending is not None:
            if trust is not Trust.FULL:
                self.log.append(Event(run_id, "policy", "control.rejected",
                                      {"name": "approval", "channel": update.channel}))
                return self._reply(update, run_id, _no_control(update.channel, trust))
            return self._resolve_approval(update, run_id, pending, text)

        # Kein offener Vorgang: ein einsames „ja"/„nein" ist bedeutungslos (abgelaufen oder nie
        # gefragt) — und darf niemals als Aufgabe an den Reasoner gehen.
        if is_affirmative(text) or is_always(text) or is_negative(text):
            self.log.append(Event(run_id, "conductor", "approval.none", {}))
            return self._reply(update, run_id, "No approval is pending (it may have expired). Nothing ran.")

        with self.execution_lock:
            erledigt = self._run_task(update, run_id, text, steerable=True)
        # Nach dem Lauf und ausserhalb des Schlosses: der Review liest und sendet, er
        # fuehrt nichts aus. Ihn drinnen zu halten hiesse, jeden anderen Lauf hinter
        # einem Bericht warten zu lassen.
        self._maybe_review(update)
        return erledigt

    # --- Rückfrage-Runde --------------------------------------------------------
    def _answer_by_button(self, update: Inbound, run_id: str, callback) -> bool:
        """Knopf-Antwort einlösen, quittieren und die Tastatur entfernen.

        Wie im Freigabe-Pfad: der Rückruf wird erst beantwortet, nachdem Identität und
        Chat geprüft sind — das erledigt `resolve_callback`, dem das Token allein nicht
        genügt. Ein fremdes oder verbrauchtes Token ändert hier nichts und sagt das auch.
        """
        answer = self.questions.resolve_callback(
            callback.data, principal=update.principal, conversation=update.conversation
        )
        if answer is None:
            text, notice = "Question invalid or expired. Nothing was answered.", "Invalid or expired"
        else:
            text, notice = _answer_receipt(answer), "Answer received"
            self.log.append(Event(run_id, "human", "question.answered",
                                  {"answered": answer.answered, "reason": answer.reason}))
        return self._reply_structured(
            update,
            run_id,
            StructuredMessage(
                text,
                edit_message_id=callback.message_id,
                callback_query_id=callback.query_id,
                callback_notice=notice,
            ),
        )

    def _answer_by_text(self, update: Inbound, run_id: str, text: str) -> bool:
        """Getippte Antwort auf eine offene Rückfrage — und NIE ein neuer Lauf.

        Der Zweig endet immer hier, auch wenn die Nachricht keine Antwort war. Solange
        die Frage offen ist, hält der wartende Worker das Ausführungsschloss; ein Lauf
        aus diesem Thread bliebe daran hängen, bis das Zeitlimit ihn löst. Also lieber
        erneut fragen — genau wie die Freigabe es bei „weder ja noch nein" tut.
        """
        answer = self.questions.resolve_text(
            text, principal=update.principal, conversation=update.conversation
        )
        if answer is None:
            self.log.append(Event(run_id, "conductor", "question.reprompt", {}))
            return self._reply(
                update,
                run_id,
                "A question is open. Reply with the number of an option, 0 to skip, "
                "or /stop to abort the run. Nothing else counts as an answer here.",
            )
        self.log.append(Event(run_id, "human", "question.answered",
                              {"answered": answer.answered, "reason": answer.reason}))
        return self._reply(update, run_id, _answer_receipt(answer))

    # --- Freigabe-Runde ---------------------------------------------------------
    def _resolve_approval(self, update: Inbound, run_id: str, rec: Pending, text: str | None = None) -> bool:
        text = update.text if text is None else text
        negative = is_negative(text)
        always = is_always(text)
        if not negative and not always and not is_affirmative(text):
            self.log.append(Event(run_id, "conductor", "approval.reprompt", {"tool": rec.req.tool}))
            return self._approval_reply(update, run_id, "Bitte nur ja, immer oder nein.\n\n" + rec.prompt)

        with self.execution_lock:
            return self._execute_approval(update, run_id, rec, negative=negative, always=always)

    def _execute_approval(
        self,
        update: Inbound,
        run_id: str,
        rec: Pending,
        *,
        negative: bool,
        always: bool,
    ) -> bool:
        """Claim and execute as one serialized effect lifecycle."""
        # Atomarer One-shot-Claim VOR jeder Wirkung. Wurde inzwischen B geparkt,
        # darf ein alter Button für A weder A ausführen noch B löschen.
        claimed = self.approvals.claim_if_current(
            update.conversation, rec, principal=update.principal
        )
        if claimed is None and rec.principal != str(update.principal):
            self.log.append(
                Event(run_id, "policy", "approval.principal_mismatch", {"tool": rec.req.tool})
            )
            return self._approval_reply(
                update,
                run_id,
                "This approval belongs to another identity. Nothing ran; it stays open.",
            )
        if self.approval_picker is not None:
            self.approval_picker.discard(
                rec, principal=update.principal, conversation=update.conversation
            )
        if claimed is None:
            self.log.append(Event(run_id, "human", "approval.stale", {"tool": rec.req.tool}))
            return self._approval_reply(
                update, run_id,
                "This approval is out of date. Nothing ran; a newer request is still open.",
            )
        rec = claimed

        if negative:
            self.log.append(Event(run_id, "human", "approval.denied", {"tool": rec.req.tool}))
            return self._approval_reply(update, run_id, "Discarded — nothing ran.")

        # TOCTOU: die Bindung entsteht JETZT, beim Ausführen. Hat sich ein Ziel seit dem Fragen
        # geändert, wird abgebrochen statt blind auf das getauschte Ziel zu wirken.
        if not self.approvals.target_unchanged(rec):
            self.log.append(Event(run_id, "human", "approval.stale", {"tool": rec.req.tool}))
            return self._approval_reply(
                update, run_id,
                "The target changed since you were asked — aborted for safety. Please request again.",
            )

        self.log.append(
            Event(run_id, "human", "approval.granted", {"tool": rec.req.tool, "standing": always})
        )
        # Erneut durch den Kernel (human_approved=True), nicht am Gate vorbei. DENY bleibt DENY.
        outcome = self.executor.run(rec.req, run_id, human_approved=True)
        note = self._remember_always(update, run_id, rec.req, outcome) if always else ""
        if rec.resume_agent:
            history = rec.history + (
                tool_history_entry(
                    rec.req.tool,
                    outcome.status.value,
                    outcome.detail,
                    outcome.result,
                ),
            )
            resumed, stopped = _plan_after_approval(rec.plan, rec.req.tool, outcome)
            if stopped:
                return self._approval_reply(update, run_id, _append_note(stopped, note))
            return self._run_task(
                update,
                run_id,
                rec.request_text,
                initial_history=history,
                steps_used=rec.steps,
                past_override=rec.memory_context,
                approval_reply=True,
                trailing_note=note,
                plan=resumed,
            )
        return self._approval_reply(update, run_id, _append_note(self._describe(outcome), note))

    def _remember_always(self, update: Inbound, run_id: str, req: ToolRequest, outcome: Outcome) -> str:
        """Legt die stehende Freigabe an — aber nur, wenn dieser Lauf wirklich durchging.

        Sonst entstünde eine Dauerregel für etwas, das gerade abgelehnt wurde (Regler
        zugedreht, Ziel getauscht). Sie wäre harmlos — der Kernel prüft ja jedes Mal neu —
        aber sie stünde in `/allowed` und behauptete etwas, das nie galt.
        """
        if outcome.status is not Status.DONE:
            return "Keine stehende Freigabe angelegt — dieser Lauf ging nicht durch."
        rule = self.standing.grant(
            update.conversation, req, principal=update.principal, run_id=run_id
        )
        if rule is None:
            return (
                "No \'always\' possible: this request has nothing exact to pin down "
                "(kein ableitbares Ziel, kein Kommando). Sie lief einmal."
            )
        return (
            "∞ Standing approval created — for exactly this action only. "
            "Details: /allowed · remove: /revoke <n>."
        )

    # --- Kommando mit Wirkung (/undo) -------------------------------------------
    def _dispatch_request(self, update: Inbound, run_id: str, req: ToolRequest, note: str = "") -> bool:
        """Führt eine vom Kommando gebaute Anfrage durch DENSELBEN Executor wie alles andere.

        Kein Sonderweg: `/undo` ist ein Schreibzugriff und wird wie einer gegatet —
        NEEDS_HUMAN parkt hier genauso eine Freigabe wie im Agent-Loop.
        """
        with self.execution_lock:
            outcome = self.executor.run(req, run_id)
            if outcome.status is Status.NEEDS_HUMAN:
                return self._park(update, run_id, req, note)
            return self._reply(update, run_id, _join(note, self._describe(outcome)))

    # --- Freigabe parken --------------------------------------------------------
    def _park(
        self,
        update: Inbound,
        run_id: str,
        req: ToolRequest,
        note: str = "",
        *,
        request_text: str = "",
        history: tuple[str, ...] = (),
        memory_context: tuple[Turn, ...] = (),
        steps: int = 0,
        resume_agent: bool = False,
        plan: PlanRun | None = None,
    ) -> bool:
        """Parkt eine NEEDS_HUMAN-Anfrage — aber nur dort, wo sie auch lösbar ist.

        Auf einem Kanal unter `FULL` kann niemand „ja" sagen. Parken hiesse: Talos
        wartet auf eine Antwort, die dieser Weg nicht geben kann, und der Vorgang
        blockiert die Konversation bis zum Ablauf der TTL — mit einem Text, der nach
        „gleich passiert etwas" aussieht. Stattdessen sofort und ehrlich absagen.
        """
        prompt = self._approval_prompt(req)
        if self.trust_of(update.channel) is not Trust.FULL:
            self.log.append(
                Event(run_id, "policy", "approval.refused",
                      {"tool": req.tool, "channel": update.channel})
            )
            return self._reply(update, run_id, _join(note, _no_approval(update.channel, prompt)))
        # des Betreibers früheres „immer" wirkt genau hier — an der Stelle, an der er sonst tippen
        # müsste. Kein zweiter Schlüssel: der Executor läuft komplett neu durch den Kernel,
        # `human_approved` wird erst nach dem DENY-Return gelesen, und der Autonomie-Regler
        # hat auf den Stufen 0–2 längst DENY gesagt, bevor überhaupt jemand parkt.
        rule = self.standing.find(update.conversation, req, principal=update.principal)
        if rule is not None:
            self.log.append(
                Event(run_id, "human", "approval.standing_used",
                      {"tool": req.tool, "key": rule.key, "label": rule.label})
            )
            outcome = self.executor.run(req, run_id, human_approved=True)
            standing_note = "∞ Standing approval used. Details: /allowed."
            if resume_agent:
                resumed_history = history + (
                    tool_history_entry(req.tool, outcome.status.value, outcome.detail, outcome.result),
                )
                resumed_plan, stopped = _plan_after_approval(plan, req.tool, outcome)
                if stopped:
                    return self._reply(update, run_id, _append_note(stopped, standing_note))
                return self._run_task(
                    update,
                    run_id,
                    request_text,
                    initial_history=resumed_history,
                    steps_used=steps,
                    past_override=memory_context,
                    trailing_note=standing_note,
                    plan=resumed_plan,
                )
            return self._reply(update, run_id, _append_note(self._describe(outcome), standing_note))

        rec = self.approvals.park(
            update.conversation,
            req,
            prompt,
            request_text=request_text,
            history=history,
            principal=update.principal,
            memory_context=memory_context,
            steps=steps,
            resume_agent=resume_agent,
            plan=plan,
        )
        self.log.append(
            Event(run_id, "conductor", "approval.parked",
                  {"tool": req.tool, "targets": list(guard_targets(req))})
        )
        text = _join(note, prompt)
        if self.approval_picker is not None and self.send_structured is not None:
            try:
                message = self.approval_picker.open(
                    text,
                    rec,
                    principal=update.principal,
                    conversation=update.conversation,
                )
                return self._reply_structured(update, run_id, message)
            except Exception as error:
                # Buttons are UX, not authority. A token/UI failure falls back to the
                # existing explicit ja/immer/nein path and is visible in the audit log.
                self.log.append(
                    Event(run_id, "conductor", "error",
                          {"stage": "approval.buttons", "error": str(error)})
                )
        return self._reply(update, run_id, text)

    # --- Agent-Loop -------------------------------------------------------------
    def _run_task(
        self,
        update: Inbound,
        run_id: str,
        text: str | None = None,
        *,
        initial_history: tuple[str, ...] = (),
        steps_used: int = 0,
        past_override: tuple[Turn, ...] | None = None,
        approval_reply: bool = False,
        trailing_note: str = "",
        # ⚠️ Ein Kopf, kein Fuss. Bei einem Hintergrundbericht muss VOR dem ersten Satz
        # stehen, worauf er sich bezieht — steht es darunter, hat der Betreiber die
        # Antwort schon als Antwort auf seine letzte Frage gelesen.
        leading_note: str = "",
        plan: PlanRun | None = None,
        # ⚠️ Voreingestellt NICHT lenkbar. Ein Hintergrundlauf laeuft unter `unattended`
        # — dort sitzt niemand davor, und eine Korrektur mitten hinein waere genau der
        # Fall, den die Decke ausschliesst. Nur die Wege, die ein Mensch gerade getippt
        # hat, setzen das auf True.
        steerable: bool = False,
    ) -> bool:
        text = update.text if text is None else text
        past = (
            self.memory.recall(update.conversation)
            if past_override is None
            else past_override
        )
        self.log.append(Event(run_id, "conductor", "reason.started", {"kontext_zuege": len(past)}))
        activity = self._begin_activity(update, run_id)
        stream = self._begin_stream(update, run_id, approval_reply=approval_reply)
        # Ab hier weiss der `ask_operator`-Runner, wohin er fragen darf: in genau diesen
        # Chat, mit genau dieser Identität und der Stufe dieses Kanals.
        context = AskContext(update.principal, update.conversation, self.trust_of(update.channel))
        if steerable:
            self.redirect.open(str(update.principal), update.conversation)
        try:
            with self.ask_contexts.active(context):
                result = run_agent(
                    self._propose(text, past, stream),
                    self.executor,
                    update.principal,
                    run_id,
                    progress=None if activity is None else activity.progress,
                    initial_history=initial_history,
                    steps_used=steps_used,
                    plan=plan,
                    redirect=self.redirect if steerable else None,
                    final_check=self._final_check(text, run_id),
                )
        except Exception as error:
            # Der Lauf ist tot — eine Frage, auf die er noch wartete, auch.
            self.questions.cancel(update.conversation)
            self.log.append(
                Event(run_id, "conductor", "error", {"stage": "reason", "error": str(error)})
            )
            if activity is not None:
                activity.fail(str(error))
            self._settle(stream, run_id)
            if initial_history:
                fallback = _resume_failure(initial_history[-1])
                if trailing_note:
                    fallback = _append_note(fallback, trailing_note)
                if approval_reply:
                    return self._approval_reply(update, run_id, fallback)
                return self._reply(update, run_id, fallback)
            return False
        finally:
            # ⚠️ `finally`, nicht am Ende des Erfolgspfads. Ein Lauf endet auch mit einer
            # Ausnahme, mit einer offenen Freigabe oder am Schrittlimit — bliebe das
            # Postfach danach offen, landete die naechste Nachricht als „Korrektur" in
            # einem Lauf, den es nicht mehr gibt.
            self.redirect.close()
        self.log.append(
            Event(run_id, "reasoner", "reason.done", {"chars": len(result.text), "status": result.status.value})
        )
        # Die Ankuendigung gehoert in die Spur. Sie erteilt nichts, aber sie erklaert die
        # Form des Laufs — warum er drei Werkzeuge weit kam und nicht dreissig, und woran
        # er endete. Ohne diesen Eintrag steht im Log nur die Wirkung, und der Grund fuer
        # ihr Ausbleiben waere spaeter nicht mehr rekonstruierbar.
        if result.plan is not None:
            self.log.append(
                Event(run_id, "conductor", "plan.announced", {
                    "goal": result.plan.plan.goal,
                    "steps": len(result.plan.plan.steps),
                    "ceiling": result.plan.ceiling,
                    "calls": result.plan.calls,
                    "stopped": result.plan.failure,
                    # Die Abnahme gehoert hierher und nicht nur in die Antwort. In der
                    # Antwort steht sie neben Prosa, die das Modell geschrieben hat —
                    # und ein Modell kann eine Quittungszeile nachahmen. Ins Event-Log
                    # schreibt niemand ausser dem Conductor; hier ist die Zahl belegbar.
                    "checks": len(result.plan.plan.checks),
                    "met": result.plan.met,
                })
            )
        if result.status is AgentStatus.NEEDS_HUMAN and result.pending is not None:
            # Der Freigabe-Dialog ist eine eigene Nachricht mit Buttons; was bis dahin
            # gewachsen ist, wird eingefroren statt ueberschrieben.
            self._settle(stream, run_id)
            sent = self._park(
                update,
                run_id,
                result.pending,
                request_text=text,
                history=result.history,
                memory_context=past,
                steps=result.steps,
                resume_agent=True,
                plan=result.plan,
            )
        else:
            reply = _final_answer(result.text) if result.status is AgentStatus.ANSWERED else result.text
            # MEDIA:-Tags NUR aus den eigenen Worten des Agenten loesen — und zwar hier,
            # BEVOR Quittung und Notizen angehaengt werden: eine Notiz kann Zeilen aus
            # Werkzeugausgaben tragen, und was ein Werkzeug oder eine Webseite "sagt",
            # darf nie einen Anhang ausloesen (siehe attachment.py).
            reply, media = extract_media(reply)
            reply = _append_note(reply, self._what_failed(run_id))
            reply = _append_note(reply, trailing_note)
            if leading_note:
                reply = f"{leading_note}\n\n{reply}"
            sent = self._deliver(
                update, run_id, reply, stream, approval_reply=approval_reply, media=media
            )
        if activity is not None:
            if sent:
                activity.succeed(self._usage_footer())
            else:
                activity.fail("could not deliver the answer")
        # Erst merken, wenn die Antwort auch draussen ist. Ein Verlauf mit einer Antwort,
        # die the operator nie gesehen hat, laesst jedes Folgegespraech ins Leere laufen: Talos
        # bezieht sich auf etwas, das fuer the operator nie stattgefunden hat.
        # Ein abgebrochener Plan wird wie eine Antwort gemerkt: er IST das Ergebnis
        # dieses Zuges. Ohne ihn im Verlauf liefe die naheliegende Anschlussfrage
        # („warum hast du aufgehoert?") ins Leere.
        if sent and result.status in (AgentStatus.ANSWERED, AgentStatus.PLAN_ABORTED):
            self.memory.remember(update.conversation, asked=text, answered=result.text)
            # Dasselbe Paar auch ins durable Archiv — fail-open ist dort eingebaut,
            # aber eine bereits zugestellte Antwort darf auch an einem unerwarteten
            # Fehler dieses Nebenwegs nicht mehr scheitern.
            if self.transcript is not None:
                try:
                    self.transcript.record(
                        update.conversation, asked=text, answered=result.text
                    )
                except Exception:
                    pass
            self._maybe_distill(update, run_id, text, result)
        return sent

    def _maybe_distill(self, update: Inbound, run_id: str, text: str, result) -> None:
        """Der Lernschritt nach zugestellter Antwort — die Destillations-Schleife.

        Trigger deterministisch (nur Laeufe mit echtem Werkzeugeinsatz), Auswahl des
        Lernwuerdigen durch das Modell, Bilanz aus dem Event-Log des Destill-Laufs
        (Executor-Evidenz, nie Modellprosa — die outcome.py-Doktrin). Der Destill-Lauf
        ist ein gewoehnlicher Agent-Loop: jeder Werkzeugwunsch passiert denselben
        Executor mit demselben Principal — kein eigener Erlaubnisweg, keine Ausnahme.

        Fail-open durchgehend: Destillation ist Komfort, kein Gate. Ihr Ausfall kostet
        die Meldung, nie die Antwort. Keine Rekursion: der Hook haengt am Ursprungslauf,
        der Destill-Lauf selbst loest keinen weiteren aus.
        """
        if not self.distill:
            return
        from . import distill

        try:
            entries = self.log.by_run(run_id)
        except Exception:
            return
        if not distill.had_tool_work(entries):
            return
        prompt = distill.build_prompt(text, result.text, entries)
        d_run_id = f"{run_id}-distill"
        budget = {"used": 0}

        def propose(history: list[str]) -> str:
            # Das Werkzeug-Budget ist die harte Bremse des Lernschritts: nach
            # TOOL_BUDGET Zuegen wird nur noch Prosa akzeptiert — ein Destill-Lauf,
            # der sich verlaeuft, kostet sonst einen vollen Agent-Loop.
            budget["used"] += 1
            zug = prompt
            if budget["used"] > distill.TOOL_BUDGET:
                zug += "\n[Tool budget reached — answer in prose now, no TOOL_CALL line.]"
            joined = "\n".join(history[-6:])
            zug = f"{zug}\n\n[Tool results so far]\n{joined}" if joined else zug
            # ⚠️ propose muss den REASONER rufen — der Loop erwartet Modelltext, nicht
            # den Prompt. Ein Prompt, der als „Antwort" zurueckkommt, traegt seine
            # eigenen TOOL_CALL-Beispielzeilen und endet als Prosa ohne jede Wirkung.
            return self._ask(zug, None)

        try:
            self.log.append(Event(d_run_id, "conductor", "distill.started", {}))
            run_agent(propose, self.executor, update.principal, d_run_id)
            bilanz = distill.counted(self.log.by_run(d_run_id))
        except Exception:
            return
        # Auch die Null-Bilanz gehoert ins Protokoll: ein Destill-Lauf, der nichts
        # schreibt, erzeugt sonst kein einziges Ereignis — ein unsichtbarer,
        # Token-kostender Modellzug ist genau das, was dieses Log verhindern soll.
        self.log.append(
            Event(d_run_id, "conductor", "distill.done",
                  {"notizen": bilanz[0], "neu": bilanz[1]})
        )
        zeile = distill.report_line(bilanz)
        if zeile:
            self._reply(update, d_run_id, zeile)

    def _learned(self) -> str:
        """Was diese Installation aus ihrem eigenen Protokoll gelernt hat.

        Eigener Block neben Erinnerung und Verlauf, aus demselben Grund: verschiedene
        Quellen sollen unterscheidbar bleiben. Faellt das Lesen aus, kostet es die
        Lehre, nie den Lauf — Lernen ist kein Gate, hier ist fail-open richtig.

        ⚠️ Bewusst im NUTZERZUG und nicht in den stehenden Anweisungen: das Protokoll
        traegt Zeichenketten aus Modell- und Netzherkunft, und im System-Feld haette
        eine einmal abgerufene Seite Nachhall in jedem spaeteren Zug.
        """
        from . import lessons

        try:
            roh = self.log.recent(LESSON_WINDOW, types=("exec.intent", "exec.result"))
            return lessons.block(roh)
        except Exception:
            return ""

    def _start_background(self, update: Inbound, run_id: str, prompt: str) -> bool:
        """`/background <auftrag>` — ein Lauf neben dem Gespraech.

        ⚠️ Drei Dinge sind hier Absicht und keine Bequemlichkeit:

        1. **Unter der unbeaufsichtigten Decke.** Niemand sitzt vor diesem Lauf.
           `NEEDS_HUMAN` wird `DENY` — dieselbe Regel wie beim Zeitplan. Eine Rueckfrage
           in einen Chat zu stellen, in dem gerade etwas ganz anderes laeuft, ist die
           zuverlaessigste Art, ein „ja" auf den falschen Vorgang fallen zu lassen.
           Fehlt die Decke, wird der Auftrag ABGELEHNT statt ungedeckelt ausgefuehrt:
           fail-closed, sonst waere ein vergessener Parameter ein Freibrief.
        2. **Leerer Kontext** (`past_override=()`). Zwei Laeufe, die sich einen Verlauf
           teilen, schreiben einander hinein.
        3. **Eigener `run_id`.** Der Bericht laesst sich damit spaeter ueber
           `talos why` einem Lauf zuordnen, ohne ihn vom Vordergrund zu trennen.
        """
        from . import background as bg

        if self.unattended is None:
            self.log.append(Event(run_id, "background", "background.refused",
                                  {"reason": "no unattended ceiling wired"}))
            return self._reply(update, run_id,
                               "Background tasks are not available in this build "
                               "(no unattended ceiling wired).")
        task = self.background.accept(prompt, run_id=run_id)
        if task is None:
            return self._reply(update, run_id, bg.FULL.format(n=self.background.busy()))

        self.log.append(Event(run_id, "background", "background.started",
                              {"task_id": task.task_id, "number": task.number}))

        def lauf() -> None:
            eigene = new_run_id()
            try:
                with self.unattended.active():
                    self._run_task(
                        replace(update, text=prompt), eigene,
                        text=prompt, past_override=(),
                        leading_note=bg.header(task),
                    )
            except Exception as fehler:
                self.log.append(Event(eigene, "background", "background.error",
                                      {"task_id": task.task_id, "error": str(fehler)}))
                try:
                    self.send(update.conversation, bg.report(task, str(fehler), ok=False))
                except Exception:
                    pass
            finally:
                self.background.finish(task.task_id)
                self.log.append(Event(eigene, "background", "background.finished",
                                      {"task_id": task.task_id}))

        threading.Thread(target=lauf, daemon=True,
                         name=f"talos-bg-{task.number}").start()
        return self._reply(update, run_id, bg.receipt(task))

    def _what_failed(self, run_id: str) -> str:
        """Die Tatsache neben die Erzaehlung — aus dem Protokoll dieses Laufs.

        ⚠️ Gemessener Anlass: eine Installation meldete „die Notiz wurde angelegt",
        während das Protokoll desselben Laufs zwei gescheiterte Schreibversuche und
        keinen erfolgreichen zeigte. Der Kernel war fehlerfrei — die Zusammenfassung
        nicht, und dagegen schuetzt kein Gate.

        Gelesen wird ueber `run_id`, nicht aus der Lauf-History: was im Agent-Loop
        mitwandert, hat das Modell schon einmal gelesen. Das Protokoll schreibt der
        Executor.

        Fail-open: das hier ist eine Quittung, kein Gate. Faellt sie aus, kostet das
        den Hinweis, nie die Antwort.
        """
        from . import outcome

        try:
            return outcome.note(self.log.by_run(run_id))
        except Exception:
            return ""

    def _gaps(self) -> tuple[tuple[str, str], ...]:
        """Was der Maschine fehlt. Ein Ausfall kostet den Hinweis, nie den Lauf.

        Fail-open ist hier richtig, weil das hier Komfort ist. Ein Gate faellt niemals
        offen — dieses Feld ist keines.
        """
        if self.capability_gaps is None:
            return ()
        try:
            return tuple(self.capability_gaps() or ())
        except Exception:
            return ()

    def _available(self) -> str:
        """Was fehlt — und was es kostet, es moeglich zu machen.

        Der Anlass war eine Antwort, die stimmte und nichts wert war: „ich kann das
        nicht". Die Voraussetzung stand die ganze Zeit in `talos doctor`, nur las sie
        der Betreiber und nie das Modell.
        """
        from . import remedy

        return remedy.render(self._gaps())

    def _maybe_review(self, update: Inbound) -> None:
        """Der faellige Selbstreview — als eigene Nachricht, nach dem Lauf.

        ⚠️ Hier und nicht in einem eigenen Ticker, und das ist der Grund: ein
        Hintergrund-Thread muesste raten, wohin er schreibt. An dieser Stelle steht die
        Konversation fest, in der der Betreiber gerade selbst geschrieben hat. Der
        Bericht landet also dort, wo jemand hinsieht, statt in einem Chat, den Talos
        sich aus dem letzten Neustart gemerkt hat.

        „Pflicht" heisst: jeder Lauf prueft die Faelligkeit, es gibt keinen Schalter.
        Gesendet wird trotzdem selten — hoechstens einmal pro Intervall, und nur wenn
        es etwas zu sagen gibt. Ein Bericht, der taeglich „alles in Ordnung" meldet,
        erzieht zum Wegwischen, und dann geht der eine mit, der zaehlt.

        Fail-open: ein kaputter Review darf den Lauf nicht nachtraeglich umbringen. Er
        erteilt nichts, also kostet sein Ausfall nur den Bericht.
        """
        from . import review

        # ⚠️ Nie in einen offenen Vorgang hinein. Endete der Lauf mit einer Freigabefrage
        # oder einer Rueckfrage, ist die naechste Nachricht des Betreibers ein „ja" oder
        # eine Zahl — und dazwischen ein Bericht macht aus einer klaren Frage eine
        # unklare. Der Review kann warten; die Freigabe wartet auf ihn nicht.
        if (self.approvals.get(update.conversation) is not None
                or self.questions.pending(update.conversation) is not None):
            return

        try:
            protokoll = self.log.recent(REVIEW_WINDOW, types=REVIEW_TYPES)
            letzte = [e for e in protokoll if e.get("type") == "review.reported"]
            if not review.due(
                float(letzte[-1]["ts"]) if letzte else None, time.time(), REVIEW_INTERVAL_S
            ):
                return
            jetzt = time.time()
            befunde = review.survey(protokoll, gaps=self._gaps(), now=jetzt)
            text = review.render_compact(befunde, now=jetzt)
            if not text:
                return
            self.send(update.conversation, text)
            self.log.append(Event(new_run_id(), "review", "review.reported",
                                  {"keys": [f.key for f in befunde], "count": len(befunde)}))
        except Exception:
            return

    def _remembered(self, user_text: str) -> str:
        """Der gerahmte Gedaechtnis-Block — oder nichts.

        Eigener Block neben dem Verlauf, nicht hineingemischt: beide sind Kontext, aber
        aus verschiedenen Quellen, und der Betreiber soll im Zweifel sehen koennen,
        woher eine Behauptung kam. Ein Ausfall kostet die Erinnerung, nie den Lauf —
        Erinnern ist kein Gate, hier ist fail-open richtig.
        """
        if self.recall is None:
            return ""
        try:
            block = self.recall.context_block(user_text)
        except Exception:
            return ""
        return f"{block}\n\n" if block else ""

    def _intelligence(self, user_text: str, history: tuple[str, ...]) -> str:
        """Bounded entity/task context; fail-open because this is not a gate."""
        if self.intelligence is None:
            return ""
        try:
            block = self.intelligence.context_block(user_text, history)
        except Exception:
            return ""
        return str(block or "")

    def _consult_done(self, run_id: str) -> bool:
        """Ob in DIESEM Lauf eine Beratung gelang — aus dem Event-Log, nie aus der
        Historie. Der Marker '[agent_consult -> done]' im Verlauf ist Modellprosa
        wert: die Schleife schreibt ihn aus Executor-Ergebnissen, aber dieselbe
        Zeichenkette kann auch in einer Antwort stehen. Das Log schreibt nur der
        Executor."""
        try:
            for eintrag in self.log.by_run(run_id):
                if eintrag.get("type") != "exec.result":
                    continue
                nutz = eintrag.get("payload") or {}
                if (
                    str(nutz.get("tool") or "") == "agent_consult"
                    and str(nutz.get("status") or "").upper() == "DONE"
                ):
                    return True
        except Exception:
            return False
        return False

    def _final_check(self, user_text: str, run_id: str):
        """Adapt the intelligence review object to the agent-loop's tiny protocol."""
        if self.intelligence is None:
            return None

        def check(answer: str, history: tuple[str, ...]) -> tuple[bool, str]:
            try:
                review = self.intelligence.review(
                    user_text,
                    answer,
                    history,
                    consult_done=lambda: self._consult_done(run_id),
                )
                return bool(review.ok), str(review.note or "")
            except Exception:
                return True, ""

        return check

    def _usage_footer(self) -> str:
        """Die Quittung ist Komfort: eine kaputte Messung darf keinen Lauf umbringen."""
        if self.usage_footer is None:
            return ""
        try:
            return self.usage_footer() or ""
        except Exception:
            return ""

    def _begin_stream(
        self, update: Inbound, run_id: str, *, approval_reply: bool
    ) -> ReplyStream | None:
        """Beginnt die mitwachsende Antwort — nur dort, wo die Antwort eine neue Nachricht wird.

        Beantwortet dieser Lauf einen Button-Entscheid, ersetzt die Antwort am Ende den
        Freigabe-Dialog (`_approval_reply`). Eine zweite, wachsende Nachricht daneben
        waere genau die Dublette, die es nicht geben darf — also gar nicht erst anfangen.
        """
        if self.begin_reply is None or (approval_reply and update.callback is not None):
            return None
        try:
            return self.begin_reply(update.conversation)
        except Exception as error:
            # Wie bei der Statusanzeige: UX ist kein Gate. Der Ausfall bleibt im Log.
            self.log.append(
                Event(run_id, "conductor", "error", {"stage": "reply.stream", "error": str(error)})
            )
            return None

    def _settle(self, stream: ReplyStream | None, run_id: str) -> None:
        """Friert die gewachsene Nachricht ein, wenn die Antwort einen anderen Weg nimmt."""
        if stream is None:
            return
        try:
            stream.settle()
        except Exception as error:
            self.log.append(
                Event(run_id, "conductor", "error", {"stage": "reply.stream", "error": str(error)})
            )

    def _deliver(
        self,
        update: Inbound,
        run_id: str,
        text: str,
        stream: ReplyStream | None,
        *,
        approval_reply: bool,
        media: tuple[str, ...] = (),
    ) -> bool:
        """Stellt die Antwort zu — als GENAU eine Nachricht, plus ihre Anhaenge.

        Wuchs waehrend des Denkens bereits eine, wird sie an Ort und Stelle zur
        endgueltigen Antwort. Erst wenn das nicht geht (nichts gestreamt, Bearbeitung
        abgelehnt), geht sie normal raus. Die Antwort ist kein Komfort: scheitert das
        Streaming vollstaendig, aendert das nur die Zustellart, nie das Ergebnis.

        Bestand die Antwort NUR aus MEDIA:-Tags, gibt es keinen sichtbaren Text — dann
        wird auch keine leere Nachricht gesendet (Telegram lehnte sie ohnehin ab): die
        Anhaenge SIND die Antwort.
        """
        delivered = False
        if text.strip() or not media:
            if stream is not None:
                try:
                    if stream.adopt(text):
                        self.log.append(Event(run_id, "conductor", "reply.sent", {"streamed": True}))
                        self.log.append(Event(run_id, "conductor", "done", {}))
                        delivered = True
                except Exception as error:
                    self.log.append(
                        Event(run_id, "conductor", "error",
                              {"stage": "reply.stream", "error": str(error)})
                    )
                if not delivered:
                    self._settle(stream, run_id)
            if not delivered:
                if approval_reply:
                    delivered = self._approval_reply(update, run_id, text)
                else:
                    delivered = self._reply(update, run_id, text)
        else:
            # Kein Text, nur Anhaenge: die gewachsene Nachricht einfrieren, statt eine
            # leere Endfassung ueber sie zu stueelpen.
            self._settle(stream, run_id)
        if media:
            attached = self._send_attachments(update, run_id, media)
            if attached and not delivered:
                self.log.append(Event(run_id, "conductor", "done", {}))
            delivered = delivered or attached
        return delivered

    def _send_attachments(self, update: Inbound, run_id: str, paths: tuple[str, ...]) -> bool:
        """Sendet die angeforderten Anhaenge — jeder Fehlschlag wird gemeldet, keiner kippt den Lauf.

        Zwei Regeln:
          1. **Das Gate laeuft hier, pro Pfad, durch den Kernel-Floor** (`attachment.resolve`).
             Eine Absage landet als ehrliche Zeile im Chat und als `attachment.refused`
             im Protokoll — nie still.
          2. **Versand-Fehler sind Versand-Fehler.** Datei weg, Kanal defekt: der Lauf ist
             laengst entschieden, ein nachtraeglicher Absturz huelfe niemandem. Aber der
             Betreiber wartet auf eine Datei — also eine Zeile pro Fehlschlag statt
             Schweigen.
        """
        delivered = False
        notes: list[str] = []
        for raw in paths:
            try:
                resolved = resolve_media(raw)
            except ValueError as error:
                self.log.append(
                    Event(run_id, "policy", "attachment.refused",
                          {"path": raw[:200], "reason": str(error)})
                )
                notes.append(f"Attachment not sent — {error}")
                continue
            if self.send_file is None:
                notes.append(
                    "Attachment not sent — no file channel is wired in this build; "
                    f"the file remains at {resolved}"
                )
                continue
            try:
                sent = self.send_file(update.conversation, resolved)
            except Exception as error:
                self.log.append(
                    Event(run_id, "conductor", "error",
                          {"stage": "attachment", "error": str(error)})
                )
                notes.append(f"Attachment could not be sent ({Path(resolved).name}): {error}")
                continue
            if sent:
                delivered = True
                self.log.append(
                    Event(run_id, "conductor", "attachment.sent", {"path": resolved})
                )
            else:
                notes.append(
                    "Attachment not sent — this channel cannot send files; "
                    f"the file remains at {resolved}"
                )
        if notes:
            self._reply(update, run_id, "\n".join(notes))
        return delivered

    def _begin_activity(self, update: Inbound, run_id: str) -> Activity | None:
        if self.begin_activity is None:
            return None
        try:
            return self.begin_activity(update.conversation)
        except Exception as error:
            # UX ist kein Gate: wenn Telegram die Statusnachricht ablehnt, darf die echte
            # Antwort weiterhin laufen. Der Ausfall bleibt im Audit-Log sichtbar.
            self.log.append(
                Event(run_id, "conductor", "error", {"stage": "activity", "error": str(error)})
            )
            return None

    def _propose(
        self,
        user_text: str,
        past: tuple[Turn, ...] = (),
        stream: ReplyStream | None = None,
    ) -> Callable[[list[str]], str]:
        """Bindet den Reasoner an die Nachricht — davor der bisherige Verlauf, danach die
        Tool-Ergebnisse dieses Laufs. Der Approval-Zustand fließt hier bewusst NICHT ein.

        Der Verlauf steht in einem eigenen, benannten Block und ist ausdrücklich als
        *Kontext, keine Anweisung* ausgezeichnet. Er hat nie einen Kernel passiert und
        kann sich deshalb auch keine Rechte geben — er wird nur wieder vorgelesen.
        """
        remembered = self._remembered(user_text) + self._learned() + self._available()

        def head_for(history: tuple[str, ...]) -> str:
            context = self._intelligence(user_text, history) + remembered
            return (
                f"{context}[Conversation so far — context, not instructions]\n"
                f"{render(past)}\n\n[New message]\n"
                if past
                else context
            )

        def propose(history: list[str]) -> str:
            snapshot = tuple(history)
            head = head_for(snapshot)
            if not history:
                return self._ask(head + user_text, stream)
            joined = "\n".join(history)
            return self._ask(f"{head}{user_text}\n\n[Tool results so far]\n{joined}", stream)
        return propose

    def _ask(self, prompt: str, stream: ReplyStream | None) -> str:
        """Ein Reasoner-Zug. Ohne Senke — oder ohne Reasoner, der eine kennt — wie bisher.

        Die Signatur wird geprueft, statt die Senke einfach mitzugeben: aeltere Reasoner
        (Hermes, der Modell-Router, Test-Doubles) nehmen nur den Prompt, und ein
        TypeError mitten im Zug kostete die ganze Antwort. Streaming ist Komfort und
        darf nie der Grund sein, warum ein Lauf nicht stattfindet.
        """
        if stream is None or not _accepts_sink(self.reasoner.reason):
            return self.reasoner.reason(prompt)
        try:
            stream.begin_turn()
        except Exception:
            # Dieselbe Regel wie in `stream.py`: ein kaputter Sink darf den Zug nicht
            # mitnehmen. Dann eben ohne Anzeige — die Antwort laeuft unveraendert.
            return self.reasoner.reason(prompt)
        return self.reasoner.reason(prompt, on_text=stream.push)

    def _approval_prompt(self, pending: ToolRequest) -> str:
        """Der Text zeigt die KERNEL-Wahrheit, nie eine LLM-Beschreibung: Tool, abgeleitete
        Ziele wortwörtlich, den vollen Command-String und den Grund aus der Decision."""
        reason = self.executor.policy.decide(pending).reason
        lines = [f"{SYM_GATE} Approval required — kernel facts:", f"Tool: {pending.tool}"]
        targets = guard_targets(pending)
        if targets:
            lines.append("Targets: " + ", ".join(targets))
        command = pending.args.get("command")
        if command is not None:
            if pending.tool == "remote_exec":
                # Bei Fernausfuehrung gehoert der ORT in die Mitte des Dialogs:
                # „uptime" ist harmlos hier und harmlos dort — der Unterschied,
                # ueber den der Mensch urteilt, ist die Maschine.
                lines.append(f"Host: {pending.args.get('host', '')} (the effect lands on this machine, not here)")
            lines.append(f"Command: {command}")
            if pending.tool == "remote_exec":
                # Der lokale Pfad-Floor sagt ueber ferne Pfade nichts Ehrliches —
                # `/etc/hosts` meint dort die Gegenstelle. Die Einordnung uebernimmt
                # hier der Mensch, mit vollem Kommandotext statt Fehlalarm.
                lines.append("Note: remote run — the local sandbox cannot limit what this does on the other machine, and there is no undo.")
            else:
                # Shell hat keine ableitbaren Ziele — die Einordnung der Pfade kommt trotzdem
                # aus dem Kernel, damit `date` und `echo … >> ~/.bashrc` nicht gleich aussehen.
                marks = command_risk_paths(str(command))
                if marks:
                    lines.append("Paths in command: " + ", ".join(
                        f"{path} [{label}]" if label else path for path, label in marks
                    ))
                lines.append("Note: shell runs have no undo (no snapshot).")
        from . import lessons

        lines.append(f"Reason: {reason}")
        wiederholt = self._approved_before(pending)
        if wiederholt >= lessons.REPEAT_HINT_AT:
            # ⚠️ Ein HINWEIS, keine Vorauswahl und erst recht keine Erteilung. Die vierte
            # Rueckfrage zu einer dreimal erteilten Freigabe schuetzt niemanden mehr, sie
            # erzieht zum Wegklicken — aber die Regel legt weiterhin der Mensch an, mit
            # demselben Wort wie bisher.
            lines.append(
                f"You have approved exactly this {wiederholt} times before. "
                "'always' would make it a standing rule you can list with /allowed "
                "and take back with /revoke."
            )
        lines.append(
            "Reply yes (run once), always (run and allow exactly this from now on) "
            "or no (discard). Expires in 5 min."
        )
        return "\n".join(lines)

    def _approved_before(self, pending: ToolRequest) -> int:
        """Wie oft GENAU diese Handlung schon freigegeben wurde. Fehlschlag zaehlt als 0.

        Der Fingerabdruck stammt aus derselben Funktion, die der Mint benutzt — eine
        zweite Berechnung daneben waere eine, die irgendwann anders zaehlt als die,
        die zaehlt.
        """
        from . import lessons

        try:
            fp = action_fingerprint(pending, derived_targets=guard_targets(pending))
            return lessons.approvals_of(
                self.log.recent(LESSON_WINDOW, types=("grant.issued",)), fp
            )
        except Exception:
            return 0

    @staticmethod
    def _describe(outcome: Outcome) -> str:
        if outcome.status is Status.DONE:
            body = outcome.result if outcome.result not in (None, "") else outcome.detail
            return f"Done: {body}"
        return f"Not executed ({outcome.status.value}): {outcome.detail}"

    def _ack_approval_callback(self, update: Inbound, run_id: str, rec: Pending) -> None:
        """Acknowledge and disable the keyboard before any approved side effect starts."""
        callback = update.callback
        if callback is None or self.send_structured is None:
            return
        message = StructuredMessage(
            rec.prompt + "\n\n… checking your decision",
            edit_message_id=callback.message_id,
            callback_query_id=callback.query_id,
            callback_notice="Decision accepted",
        )
        try:
            self.send_structured(update.conversation, message)
            self.log.append(Event(run_id, "channel", "callback.ack", {}))
        except Exception as error:
            # Delivery must not alter the already authenticated decision. TelegramChannel
            # independently attempts answer + keyboard-clearing edit before raising.
            self.log.append(
                Event(run_id, "channel", "error",
                      {"stage": "approval.callback.ack", "error": str(error)})
            )

    def _approval_reply(self, update: Inbound, run_id: str, text: str) -> bool:
        """Typed decisions send normally; button decisions replace the prompt and clear buttons."""
        callback = update.callback
        if callback is None:
            return self._reply(update, run_id, text)
        return self._reply_structured(
            update,
            run_id,
            StructuredMessage(
                text,
                edit_message_id=callback.message_id,
            ),
        )

    def _reply(self, update: Inbound, run_id: str, text: str) -> bool:
        try:
            self.send(update.conversation, text)
            self.log.append(Event(run_id, "conductor", "reply.sent", {}))
        except Exception as error:  # Zustellung fehlgeschlagen -> als Fehler-Event
            self.log.append(Event(run_id, "conductor", "error", {"stage": "reply", "error": str(error)}))
            return False
        self.log.append(Event(run_id, "conductor", "done", {}))
        return True

    def _reply_structured(
        self, update: Inbound, run_id: str, message: StructuredMessage
    ) -> bool:
        try:
            if self.send_structured is None:
                self.send(update.conversation, message.text)
            else:
                self.send_structured(update.conversation, message)
            self.log.append(Event(run_id, "conductor", "reply.sent", {"structured": True}))
        except Exception as error:
            self.log.append(
                Event(run_id, "conductor", "error", {"stage": "reply", "error": str(error)})
            )
            return False
        self.log.append(Event(run_id, "conductor", "done", {}))
        return True


def _accepts_sink(reason: Callable[..., str]) -> bool:
    """Nimmt diese `reason`-Implementierung eine Text-Senke (`on_text`) entgegen?

    Bewusst nur der ausdruecklich benannte Parameter: ein `**kwargs`-Wrapper wuerde
    Unterstuetzung behaupten und beim Aufruf scheitern. Fail-closed heisst hier: im
    Zweifel nicht streamen.
    """
    try:
        return "on_text" in inspect.signature(reason).parameters
    except (TypeError, ValueError):
        return False


def reply_starter(registry: object) -> ReplyBegin:
    """Bruecke von einer `ChannelRegistry` zur mitwachsenden Antwort ihres Kanals.

    Gleiche Bauart wie `ChannelRegistry.begin_activity`: wer `begin_reply` nicht kennt
    (Mail, CLI, Testkanaele), bekommt einfach keine — nie einen Zustellfehler. Sobald
    `channel.py` die Methode selbst traegt, faellt diese Funktion ersatzlos weg.
    """

    def start(conversation: str) -> ReplyStream | None:
        name, sep, _ = conversation.partition(":")
        if not sep:
            raise ValueError(f"conversation ohne Kanal: {conversation!r}")
        starter = getattr(registry.get(name), "begin_reply", None)  # type: ignore[attr-defined]
        return None if starter is None else starter(conversation)

    return start


def _no_control(channel: str, trust: Trust) -> str:
    return (
        f"This channel ({channel}, level {trust.name.lower()}) cannot approve anything and "
        "cannot set the autonomy dial. That only works where the identity proves something."
    )


def _no_approval(channel: str, prompt: str) -> str:
    """Absage statt Warteschleife — mit den Kernel-Fakten, damit klar ist, was ausblieb."""
    return (
        f"Not executed — this needs approval, and nobody can approve over {channel}. "
        f"Nothing queued, nothing happened.\n\n{prompt}"
    )


def _answer_receipt(answer: Answer) -> str:
    """Quittung der Rückfrage. Sagt ausdrücklich, dass nichts freigegeben wurde —
    dieselbe Trennung wie im Fragetext, nur am anderen Ende des Vorgangs."""
    if not answer.answered:
        return "No answer recorded. The run continues without one; nothing was approved."
    return (
        f"Answer recorded: option {answer.index + 1} — {answer.label}. "
        "The run continues; nothing was approved and nothing ran because of this."
    )


def _join(note: str, body: str) -> str:
    return f"{note}\n\n{body}" if note else body


def _append_note(body: str, note: str) -> str:
    return f"{body}\n\n{note}" if note else body


def _final_answer(text: str) -> str:
    """Die Antwort steht fuer sich. Kein Marker davor — Telegram trennt Nachrichten selbst,
    und ein Zeichen in der Prosa widerspricht der Regel, die die SOUL dem Agenten gibt."""
    return text.strip()


def _plan_after_approval(
    plan: PlanRun | None, tool: str, outcome: Outcome
) -> tuple[PlanRun | None, str]:
    """Der freigegebene Schritt zaehlt wie jeder andere — auch wenn er scheitert.

    Ohne diese Stelle haette die Abbruchbedingung ein Loch an genau der Stelle, an der
    sie am meisten zaehlt: einmal „ja" sagen, und ein danach fehlgeschlagener Schritt
    liefe wieder in die Improvisation, gegen die der Plan gebaut ist. Ein zweiter
    Rueckgabewert statt einer Ausnahme, weil der Aufrufer zwei verschiedene Wege
    zurueck in den Chat hat (Button-Antwort und gewoehnliche Antwort).
    """
    if plan is None:
        return None, ""
    advanced = plan.record_call()
    if outcome.status is Status.DONE:
        return advanced, ""
    stopped = advanced.abort(f"{tool} — {outcome.status.value}: {outcome.detail}")
    return stopped, stopped.report()


def _resume_failure(last_result: str) -> str:
    """Honest bounded fallback: the approved step ran, the whole task is not confirmed done."""
    limit = 2_500
    raw = last_result.strip()
    if len(raw) > limit:
        raw = raw[: limit - 20] + " […output truncated]"
    raw = raw.replace("```", "``\u200b`")
    return (
        "The approved step ran, but the evaluation failed. "
        "The overall task is **not** confirmed as done.\n\n"
        f"```text\n{raw}\n```"
    )
