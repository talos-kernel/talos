"""delegate_dag — ein gerichteter azyklischer Graph von Coding-Teilaufgaben.

`delegate_code` schickt EINE begrenzte Aufgabe an den eingesperrten Claude-Worker;
dieser Baustein nimmt einen ganzen Graphen entgegen: das Modell deklariert Knoten
und Kanten, der Desk haelt die Reihenfolge. Bereite Knoten (alle Eltern `done`)
werden submitted, sobald der Worker einen Slot hat; ein gescheiterter Elternteil
(`failed`/`timeout`/`gone`) laesst seine Kinder nie starten — sie werden `skipped`,
mit Grund, im Bericht. Pro gelaufenem Knoten geht ein kurzer Push in den
Ursprungschat, am Ende ein Gesamtbericht, der jeden Knoten mit Stand und
Kurzsummary nennt.

⚠️ Vier Entscheidungen, dieselben wie beim Completion-Push (notify.py):

- **Beweis aus dem Worker-Protokoll, nie aus Modellprosa.** Summary, files,
  returncode, error kommen aus dem Worker-Frame (Stream-Beleg). Kein Satz davon
  schreibt ein Modell.
- **Der Rueckweg kommt aus dem Thread-Kontext, nie aus Argumenten.** Die
  Konversation hinterlegt der Conductor am ausfuehrenden Thread; der Runner
  uebernimmt sie von dort in den Desk. Ohne Kontext wird der DAG gar nicht erst
  angenommen — ein Bericht ohne bekannten Empfaenger waere ein geratener
  Zustellweg.
- **Validierung VOR jedem Submit, benannt.** Form, Eindeutigkeit, Referenzen,
  Azyklizitaet und die MCP-Schnittmenge werden geprueft, bevor ein Frame den
  Prozess verlaesst; eine Ablehnung legt nichts im Desk ab und schickt nichts.
- **Fail-open wie jede Zustellung.** Ein belegter Worker (`busy` — der Worker
  queued nicht, siehe claudeworker) laesst den Knoten `ready`; der naechste Tick
  versucht es erneut. Ein unzustellbarer Bericht bleibt angemeldet und kommt beim
  naechsten Tick erneut: lieber zweimal gemeldet als gar nicht.

Der Desk ist in-memory wie der BackgroundDesk: ein Neustart des Agenten verliert
die laufenden DAGs — die Jobs selbst weiss der Worker ebenfalls nicht mehr
(in-memory), der naechste Stand waere `unknown_job`. Was nach einem Neustart
niemand mehr kennt, wird ehrlich als `gone` behandelt (Vorbild `gone_text`).
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .eventlog import Event, EventLog, new_run_id
from .notify import TERMINAL as WORKER_TERMINAL
from .policy import claude_job_workspace

__all__ = [
    "DEADLINE_S",
    "Dag",
    "DagDesk",
    "DagError",
    "MAX_NODES",
    "MAX_PROMPT_CHARS",
    "Node",
    "poll_dags",
    "report_text",
    "start_ready",
    "validate_nodes",
]

# Deckel wie beim Rest des Werkzeugkastens: ein DAG ist Orchestrierung, kein
# Programm. Acht Knoten bei zwei Worker-Slots sind ohnehin vier Wellen.
MAX_NODES = 8
MAX_PROMPT_CHARS = 8000
# Wanduhr ueber dem ganzen Graphen: ein haengender DAG wird nach der Frist
# abgeschlossen (Rest `skipped`) und trotzdem berichtet — nie still verhallt.
DEADLINE_S = 3600.0
# Ein Push ist kurz; der volle Beleg bleibt `delegate_status` und das Event-Log.
MAX_SUMMARY_CHARS = 300
MAX_PROMPT_SHORT = 56

_NODE_ID = re.compile(r"[a-z0-9-]{1,24}")
# Desk-seitige Endzustaende: alles, worauf nicht mehr gewartet wird.
TERMINAL = frozenset({"done", "failed", "timeout", "gone", "skipped"})
# Eltern-Zustaende, die ein Kind `skipped` statt starten lassen.
GESCHEITERT = frozenset({"failed", "timeout", "gone", "skipped"})


class DagError(ValueError):
    """Eine benannte Ablehnung der DAG-Deklaration — der Runner meldet sie als
    Text zurueck, statt einen halb verstandenen Graphen zu starten."""


def _zeile(text: object, limit: int) -> str:
    """Eine Zeile, gedeckelt — dasselbe Muster wie `notify._zeile`: Frame-Inhalt
    darf weder den Push sprengen noch eine zweite, wie eine eigene Meldung
    aussehende Zeile faelschen."""
    einzeilig = " ".join(str(text or "").split())
    return einzeilig if len(einzeilig) <= limit else einzeilig[: limit - 1] + "…"


@dataclass
class Node:
    """Ein Knoten. `state`: ready → submitted → done|failed|timeout|gone, oder
    skipped (Elternteil gescheitert / DAG-Frist). `note` traegt die Kurzform fuer
    den Bericht: Summary, Fehler oder den Skip-Grund."""

    id: str
    prompt: str
    depends_on: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()
    job_id: str = ""
    state: str = "ready"
    note: str = ""


@dataclass
class Dag:
    """Ein laufender Graph. `conversation` ist der Rueckweg — er steht hier, weil
    er bei der Anmeldung aus dem Thread-Kontext kam, nicht weil ihn jemand
    uebergab (das `notify.Watch`-Muster)."""

    dag_id: str
    conversation: str
    nodes: dict[str, Node]
    started_at: float
    deadline_s: float = DEADLINE_S


@dataclass
class DagDesk:
    """Welche Graphen gerade laufen. Eine Instanz, geteilt zwischen dem
    Werkzeug-Thread (Anmeldung) und dem Ticker (Abfrage, Freigabe, Bericht)."""

    _dags: dict[str, Dag] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def register(self, lauf: Dag) -> None:
        with self._lock:
            self._dags[lauf.dag_id] = lauf

    def drop(self, dag_id: str) -> None:
        with self._lock:
            self._dags.pop(dag_id, None)

    def pending(self) -> tuple[Dag, ...]:
        with self._lock:
            return tuple(self._dags.values())

    def busy(self) -> int:
        with self._lock:
            return len(self._dags)


def validate_nodes(roh: object, *, mcp_allowed: Iterable[str]) -> tuple[Node, ...]:
    """Prueft die Deklaration und liefert die Knoten in topologischer Ordnung.

    Wirft `DagError` mit einer benannten Begruendung — NIE wird ein halb
    verstandener Graph gestartet, und nie wird ein Prompt still gekuerzt: ein
    gekuerzter Prompt waere ein anderer Job, und der Unterschied stuende in
    keinem Protokoll. Die MCP-Namen werden gegen dieselbe fertig gerechnete
    Schnittmenge geprueft wie bei `delegate_code` — der Runner kennt nur ihr
    Ergebnis, nie Registry oder Schalter selbst.
    """
    erlaubt = frozenset(mcp_allowed)
    if not isinstance(roh, list) or not roh:
        raise DagError('nodes muss eine nicht-leere Liste von Knoten sein '
                       '({"id", "prompt", "depends_on"?, "mcp"?})')
    if len(roh) > MAX_NODES:
        raise DagError(f"hoechstens {MAX_NODES} Knoten pro DAG ({len(roh)} verlangt)")
    knoten: list[Node] = []
    gesehen: set[str] = set()
    for eintrag in roh:
        if not isinstance(eintrag, dict):
            raise DagError("jeder Knoten ist ein Objekt {id, prompt, depends_on?, mcp?}")
        nid = eintrag.get("id")
        if not isinstance(nid, str) or not _NODE_ID.fullmatch(nid):
            raise DagError(f"ungueltige Knoten-id {nid!r} (erlaubt: [a-z0-9-], 1-24 Zeichen)")
        if nid in gesehen:
            raise DagError(f"Knoten-id {nid!r} kommt doppelt vor")
        gesehen.add(nid)
        prompt = eintrag.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise DagError(f"Knoten {nid!r}: prompt fehlt oder ist leer")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise DagError(
                f"Knoten {nid!r}: prompt zu lang ({len(prompt)} Zeichen, "
                f"max {MAX_PROMPT_CHARS})")
        deps = eintrag.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise DagError(f"Knoten {nid!r}: depends_on muss eine Liste von ids sein")
        mcp = eintrag.get("mcp", [])
        if not isinstance(mcp, list) or not all(isinstance(n, str) for n in mcp):
            raise DagError(
                f"Knoten {nid!r}: mcp muss eine Liste von Servernamen sein "
                '(z.B. "mcp": ["chrome-devtools"])')
        namen = tuple(dict.fromkeys(mcp))
        for name in namen:
            if name not in erlaubt:
                raise DagError(
                    f"Knoten {nid!r}: mcp-Server {name!r} ist nicht freigeschaltet "
                    "(Registry data/mcp-servers.json geschnitten mit TALOS_MCP_SERVERS)")
        knoten.append(Node(id=nid, prompt=prompt,
                           depends_on=tuple(dict.fromkeys(deps)), mcp=namen))
    ids = {n.id for n in knoten}
    for n in knoten:
        for elternteil in n.depends_on:
            if elternteil not in ids:
                raise DagError(
                    f"Knoten {n.id!r}: depends_on kennt {elternteil!r} nicht")
    # Azyklizitaet per topologischem Sortieren (Kahn): was nicht sortiert werden
    # kann, enthaelt einen Zyklus — und ein Zyklus wuerde sonst fuer immer auf
    # einen Elternteil warten, der nie fertig werden kann.
    fertig: list[Node] = []
    offen = list(knoten)
    while offen:
        naechste = [n for n in offen if all(d in {f.id for f in fertig}
                                            for d in n.depends_on)]
        if not naechste:
            rest = ", ".join(n.id for n in offen)
            raise DagError(f"Zyklus im DAG: {rest} warten aufeinander")
        fertig.extend(naechste)
        offen = [n for n in offen if n not in naechste]
    return tuple(fertig)


def _workspace(work_root: str, job_id: str) -> str:
    """Dieselbe Ableitung wie beim delegate_code-Runner: Blattname aus der
    Kernelfunktion (`claude_job_workspace`), Wurzel aus der Verdrahtung."""
    return str(Path(work_root) / Path(claude_job_workspace(job_id)).name)


def start_ready(lauf: Dag, *, submit_one: Callable, work_root: str,
                log: EventLog | None = None) -> int:
    """Submitted jeden `ready`-Knoten, dessen Eltern alle `done` sind.

    Die job_id erzeugt diese Seite selbst — stuende sie in den Argumenten,
    koennte das Modell bestehende Jobs adressieren. `busy` (der Worker queued
    nicht) und jede andere Nicht-Annahme lassen den Knoten `ready`: der naechste
    Tick versucht es erneut, die DAG-Frist deckelt das Warten. Gibt die Zahl der
    neu gestarteten Knoten zurueck.
    """
    gestartet = 0
    for node in lauf.nodes.values():
        if node.state != "ready":
            continue
        if any(lauf.nodes[elternteil].state != "done" for elternteil in node.depends_on):
            continue
        job_id = uuid.uuid4().hex[:12]
        try:
            antwort = submit_one(job_id, node.prompt,
                                 _workspace(work_root, job_id), node.mcp)
        except Exception as fehler:
            _log(log, "dag.error", {"dag_id": lauf.dag_id, "node": node.id,
                                    "stage": "submit", "error": str(fehler)})
            continue
        if antwort.get("ok"):
            node.job_id, node.state = job_id, "submitted"
            gestartet += 1
        # Nicht ok (busy, unavailable, …): bereit BLEIBEN — kein Verlust, der
        # naechste Tick fragt erneut, statt einen nie gestarteten Knoten fuer
        # gescheitert zu erklaeren.
    return gestartet


def node_text(lauf: Dag, node: Node, frame: dict) -> str:
    """Der kurze Push eines Endzustands — alles Sichtbare aus dem Worker-Frame
    und der Anmeldung, kein Wort Modelltext (Muster `notify.completion_text`)."""
    state = str(frame.get("state", "?"))
    zeilen = [f"delegate_dag {lauf.dag_id} node {node.id} finished — {state}",
              f"  {_zeile(node.prompt, MAX_PROMPT_SHORT)}"]
    if state == "done":
        zeilen.append(f"summary: {_zeile(frame.get('summary'), MAX_SUMMARY_CHARS) or '(none)'}")
        dateien = [str(f) for f in (frame.get("files") or [])][:5]
        zeilen.append(f"files: {', '.join(dateien) if dateien else '(none)'}")
    zeilen.append(f"returncode: {frame.get('returncode')}")
    fehler = _zeile(frame.get("error"), MAX_SUMMARY_CHARS)
    if fehler:
        zeilen.append(f"error: {fehler}")
    return "\n".join(zeilen)


def node_gone_text(lauf: Dag, node: Node) -> str:
    """Der Worker kennt den Job nicht mehr (Neustart — in-memory, siehe
    claudeworker). Das ist ein Endzustand, kein Wackeln: benannt, statt ein
    Ergebnis zu erfinden (Muster `notify.gone_text`)."""
    return (f"delegate_dag {lauf.dag_id} node {node.id} — the worker no longer "
            f"knows this job\n  {_zeile(node.prompt, MAX_PROMPT_SHORT)}\n"
            "(worker restarted? node counted as gone, dependents skipped)")


def report_text(lauf: Dag) -> str:
    """Der Gesamtbericht: eine Zeile Zaehlung, eine Zeile pro Knoten. Ehrlich —
    jeder Knoten erscheint mit seinem tatsaechlichen Endstand, und was skipped
    wurde, traegt den Grund."""
    zaehlung: dict[str, int] = {}
    for node in lauf.nodes.values():
        zaehlung[node.state] = zaehlung.get(node.state, 0) + 1
    teile = [f"{n} {stand}" for stand in ("done", "failed", "timeout", "gone", "skipped")
             if (n := zaehlung.get(stand))]
    zeilen = [f"delegate_dag {lauf.dag_id} finished — {', '.join(teile)}"]
    for node in lauf.nodes.values():
        zeile = f"- {node.id}: {node.state}"
        if node.note:
            zeile += f" — {node.note}"
        zeilen.append(zeile)
    return "\n".join(zeilen)


def _log(log: EventLog | None, typ: str, payload: dict) -> None:
    if log is not None:
        log.append(Event(new_run_id(), "dag", typ, payload))


def _tick_dag(lauf: Dag, *, desk: DagDesk, status: Callable, submit_one: Callable,
              send: Callable[[str, str], None], work_root: str,
              log: EventLog | None, clock: Callable[[], float]) -> int:
    """Ein Tick ueber einen Graphen. Wirft NIE nach aussen weiter als bis zu
    `poll_dags`; ein gescheiteter Sendeversuch laesst den Eintrag angemeldet
    (lieber zweimal als gar nicht)."""
    zugestellt = 0
    # Die Wanduhr-Frist: was dann noch nicht terminal ist, wird skipped — der
    # Bericht unten laeuft trotzdem und sagt es ehrlich.
    if clock() - lauf.started_at > lauf.deadline_s:
        for node in lauf.nodes.values():
            if node.state not in TERMINAL:
                node.state = "skipped"
                node.note = f"DAG-Frist {int(lauf.deadline_s)}s erreicht"
    # Laufende Knoten fragen. Terminal heisst: zuerst zustellen, dann den Stand
    # setzen — ein Push, dessen Zustellung scheitert, bleibt submitted und kommt
    # beim naechsten Tick erneut.
    for node in lauf.nodes.values():
        if node.state != "submitted":
            continue
        try:
            frame = status(node.job_id)
        except Exception as fehler:
            _log(log, "dag.error", {"dag_id": lauf.dag_id, "node": node.id,
                                    "stage": "status", "error": str(fehler)})
            continue
        text = None
        if not frame.get("ok"):
            # `unknown_job` ist ein Endzustand (Worker neu gestartet); alles
            # andere (`unavailable`) ist voruebergehend — naechster Tick.
            if frame.get("kind") == "unknown_job":
                node.note = "the worker no longer knows this job"
                text, stand = node_gone_text(lauf, node), "gone"
            else:
                continue
        elif str(frame.get("state", "")) in WORKER_TERMINAL:
            stand = str(frame.get("state"))
            if stand == "done":
                node.note = _zeile(frame.get("summary"), MAX_SUMMARY_CHARS)
            else:
                node.note = _zeile(frame.get("error"), MAX_SUMMARY_CHARS) or stand
            text = node_text(lauf, node, frame)
        if text is None:
            continue
        try:
            send(lauf.conversation, text)
        except Exception as fehler:
            _log(log, "dag.error", {"dag_id": lauf.dag_id, "node": node.id,
                                    "stage": "send", "error": str(fehler)})
            continue
        node.state = stand
        _log(log, "dag.pushed", {"dag_id": lauf.dag_id, "node": node.id,
                                 "conversation": lauf.conversation, "state": stand})
        zugestellt += 1
    # Freigabe und Skip-Ausbreitung: die Knoten liegen in topologischer Ordnung
    # (validate_nodes), also genuegt EIN Durchlauf — ein Elternteil steht immer
    # vor seinem Kind, und ein frisch geskipptes Elternteil wird im selben
    # Durchlauf gesehen.
    for node in lauf.nodes.values():
        if node.state != "ready":
            continue
        for elternteil in node.depends_on:
            stand = lauf.nodes[elternteil].state
            if stand in GESCHEITERT:
                node.state = "skipped"
                node.note = f"Elternteil {elternteil} {stand}"
                break
    # Ein Skip bekommt keinen eigenen Push (kein Worker-Beleg, kein Lauf) — der
    # Bericht unten traegt ihn samt Grund.
    start_ready(lauf, submit_one=submit_one, work_root=work_root, log=log)
    # Alles terminal → Gesamtbericht, dann aufloesen. Ein gescheiterter Versand
    # laesst den Eintrag stehen: der Bericht kommt beim naechsten Tick erneut.
    if lauf.nodes and all(node.state in TERMINAL for node in lauf.nodes.values()):
        try:
            send(lauf.conversation, report_text(lauf))
        except Exception as fehler:
            _log(log, "dag.error", {"dag_id": lauf.dag_id, "stage": "report",
                                    "error": str(fehler)})
            return zugestellt
        desk.drop(lauf.dag_id)
        _log(log, "dag.pushed", {"dag_id": lauf.dag_id,
                                 "conversation": lauf.conversation, "state": "report"})
        zugestellt += 1
    return zugestellt


def poll_dags(desk: DagDesk, *, status: Callable[[str], dict],
              submit: Callable, send: Callable[[str, str], None],
              work_root: str, log: EventLog | None = None,
              clock: Callable[[], float] = time.monotonic) -> int:
    """Ein Tick: jeden laufenden Graphen einmal weiterdrehen.

    Gibt die Zahl der Zustellungen (Knoten-Pushes plus Berichte) zurueck. Wirft
    NIE — ein kaputter Worker oder Kanal kostet den Tick, nicht den Waechter.
    """
    zugestellt = 0
    for lauf in desk.pending():
        try:
            zugestellt += _tick_dag(
                lauf, desk=desk, status=status,
                submit_one=lambda job_id, prompt, workspace, mcp: submit(
                    job_id, prompt, workspace, mcp),
                send=send, work_root=work_root, log=log, clock=clock)
        except Exception as fehler:
            _log(log, "dag.error", {"dag_id": lauf.dag_id, "error": str(fehler)})
    return zugestellt
