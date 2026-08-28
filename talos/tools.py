"""Konkrete Talos-Tools + Manifest-Builder.

Drei Start-Tools nach dem „wie Hermes"-Vorbild: Datei lesen/schreiben und Shell,
dazu `undo_last` als Rückwärtsgang. Die Runner werden NUR aufgerufen, wenn der
Policy-Kernel + Command-Floor freigegeben haben (siehe executor.py). Sie selbst
enthalten keine Sicherheitslogik — Trennung von Gate und Vollzug.
"""
from __future__ import annotations

from . import browser, claudejobs, frames, hearing, sandbox, speech, transcript, vision, web

import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable

from .manifest import Effect, ToolManifest, ToolSpec
from .policy import ToolRequest, claude_job_workspace
from .question import Answer, AnswerReason, QuestionDesk
from .snapshot import Entries, restore_entries
from .vault import (
    make_vault_get_runner,
    make_vault_search_runner,
    make_vault_write_runner,
    vault_get,
    vault_search,
    vault_write_note,
)

SHELL_TIMEOUT_S = 60
# Wie weit `/undo` zurückschaut, um den Snapshot zum angefragten Lauf zu finden.
UNDO_LOOKBACK = 200


def _need(req: ToolRequest, key: str) -> str:
    """Ein Pflichtargument als Text — oder ein klarer Fehler statt eines nackten KeyError.

    Ein fehlendes `path` schlug bisher als `req.args["path"]` mit `KeyError` durch und
    erschien dem Modell als kryptisches „error · 'path'", das einen Plan abbrach. Das
    Urteil faellt weiter der Kernel; hier geht es nur um eine brauchbare Meldung, aus der
    das Modell den korrekten Aufruf ableiten kann.
    """
    if key not in req.args:
        raise ValueError(f"das Werkzeug braucht das Argument '{key}'")
    return str(req.args[key])


def read_file(req: ToolRequest) -> str:
    return Path(_need(req, "path")).read_text(encoding="utf-8")


def write_file(req: ToolRequest) -> str:
    path = Path(_need(req, "path"))
    content = _need(req, "content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"{len(content)} Zeichen geschrieben nach {path}"


def run_shell(req: ToolRequest) -> str:
    """Shell — ab hier eingesperrt statt geraten.

    Bis hierher lief das Kommando einfach als Kindprozess, und der Schutz bestand aus
    dem Pfad-Floor des Kernels, der nur **literale** Tokens sieht. `P=/etc; cat $P/passwd`
    kam damit durch, ebenso `eval` und alles base64-Rekonstruierte — die `CLAUDE.md`
    nannte das selbst die bewusst offene Luecke.

    Der Unterschied ist nicht ein besserer Regex: die Sandbox raet nicht, was ein
    Kommando tun WIRD, sie begrenzt, was es KANN. Wurzel nur lesbar, schreibbar nur der
    Arbeitsbereich, Netz aus, Umgebung auf eine Positivliste reduziert (eine Verbotsliste
    vergisst immer die naechste Variable). Der Floor bleibt trotzdem: er faengt
    Offensichtliches frueh und liefert dem Freigabe-Text seine Pfad-Einordnung.

    Ist keine Isolation verfuegbar, wird **verweigert statt ungeschuetzt ausgefuehrt** —
    ausser der Betreiber hat das ausdruecklich abgeschaltet.
    """
    command = _need(req, "command")
    try:
        result = sandbox.run_sandboxed(command)
    except sandbox.SandboxUnavailable as error:
        return f"rc=refused\n{error}"
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    tail = out if not err else f"{out}\n[stderr] {err}".strip()
    note = ""
    if result.timed_out:
        note = "\n[timed out]"
    elif result.truncated:
        note = "\n[output truncated]"
    return f"rc={result.returncode} [{result.backend}]\n{tail}{note}".strip()


def normalise_entries(raw: object) -> Entries:
    """JSON kennt keine Tupel — aus dem Log kommen Listen zurück. Hier vereinheitlicht,
    damit der Vergleich „angefragt == protokolliert" nicht an der Form scheitert."""
    out: list[tuple[str, str | None]] = []
    for item in raw or ():  # type: ignore[union-attr]
        original, backup = (list(item) + [None])[:2]
        out.append((str(original), None if backup in (None, "") else str(backup)))
    return tuple(out)


def make_undo_runner(log) -> Callable[[ToolRequest], str]:
    """Baut den `undo_last`-Runner mit Zugriff auf den Event-Log.

    Wichtig: die Backup-Pfade kommen aus dem LOG, nicht aus den Argumenten. Sonst wäre
    `undo_last` ein Universal-Kopierer — wer die Argumente setzt, könnte eine beliebige
    Quelle über eine beliebige Zieldatei legen, und der Freigabe-Text würde nur das Ziel
    zeigen, nie die Quelle. Weichen Argumente und Log ab, bricht der Lauf ab (fail-closed).
    """

    def undo_last(req: ToolRequest) -> str:
        snapshot_id = str(req.args.get("snapshot_id", ""))
        if not snapshot_id:
            raise ValueError("undo_last ohne snapshot_id")
        recorded: Entries | None = None
        for row in log.recent(UNDO_LOOKBACK, ("snapshot.taken",)):
            if str(row["payload"].get("snapshot_id", "")) == snapshot_id:
                recorded = normalise_entries(row["payload"].get("entries"))
        if recorded is None:
            raise ValueError(f"Kein Snapshot {snapshot_id[:8]} im Event-Log")
        if normalise_entries(req.args.get("entries")) != recorded:
            raise ValueError("Snapshot-Einträge weichen vom Event-Log ab — abgebrochen")
        restore_entries(recorded)
        names = ", ".join(original for original, _ in recorded)
        return f"Snapshot {snapshot_id[:8]} rolled back: {names}"

    return undo_last


def make_ask_operator_runner(
    desk: QuestionDesk,
    *,
    context: Callable[[], object],
    send_structured: Callable[[str, object], None],
) -> Callable[[ToolRequest], str]:
    """Baut den `ask_operator`-Runner. Läuft im Worker-Thread — `wait()` blockiert.

    Chat, Identität und Vertrauensstufe kommen aus `context()`, NIE aus `req.args`:
    stünden sie in den Argumenten, könnte das Modell wählen, wen es fragt und mit
    welcher Stufe. Der Conductor hinterlegt sie am ausführenden Thread.

    Weniger als zwei verwertbare Möglichkeiten wirft `ValueError` — bewusst
    durchgereicht: das ist ein gewöhnlicher Werkzeugfehler und geht als solcher an das
    Modell zurück, statt hier zu einer stillen „keine Antwort" zu werden.
    """

    def ask_operator(req: ToolRequest) -> str:
        ctx = context()
        if ctx is None:
            # Kein bekannter Rückweg (z.B. ein Lauf ausserhalb des Conductors). Ehrlich
            # als „keine Antwort" melden statt irgendwohin zu fragen.
            return _no_answer("this run has no channel to ask on")
        ticket = desk.open(
            req.args.get("question", ""),
            req.args.get("options", ()),
            principal=ctx.principal,
            conversation=ctx.conversation,
            trust=ctx.trust,
        )
        if ticket is None:
            return _no_answer("this channel only delivers and cannot answer")
        try:
            send_structured(ctx.conversation, ticket.message)
        except Exception:
            # Ungestellte Frage: sonst wartet der Worker bis zum Zeitlimit auf eine
            # Antwort auf eine Nachricht, die nie ankam.
            desk.cancel(ctx.conversation)
            raise
        return desk.wait(ticket).as_tool_result()

    return ask_operator


def _no_answer(why: str) -> str:
    return Answer("", -1, "", AnswerReason.DECLINED).as_tool_result() + f" [{why}]"


def make_session_search_runner(
    store: transcript.TranscriptStore,
    *,
    context: Callable[[], object],
) -> Callable[[ToolRequest], str]:
    """Baut den `session_search`-Runner — Suche NUR in der eigenen Konversation.

    Die Konversation kommt aus `context()`, NIE aus `req.args` — dieselbe Grenze wie bei
    `ask_operator`, aus demselben Grund: stuende sie in den Argumenten, koennte das Modell
    waehlen, WESSEN Verlauf es liest. Ein `conversation`-Feld in den Argumenten wird
    deshalb nicht einmal angesehen.

    `context() is None` wirft, statt leer zu antworten: anders als bei `ask_operator`
    („keine Antwort" ist dort ein legitimer Gespraechsausgang) ist „kein Kontext" hier nie
    ein legitimes SUCHERGEBNIS, sondern immer ein Verdrahtungsfehler. Ein leeres Ergebnis
    liesse das Modell glauben, es sei wirklich nichts gefunden worden. Der Executor faengt
    die Ausnahme als gewoehnlichen Werkzeugfehler.
    """

    def session_search(req: ToolRequest) -> str:
        ctx = context()
        if ctx is None:
            raise ValueError(
                "session_search has no conversation context — this run is outside the conductor"
            )
        args = req.args
        query = args.get("query")
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > transcript.QUERY_MAX_CHARS
        ):
            raise ValueError(
                f"session_search query muss 1..{transcript.QUERY_MAX_CHARS} Zeichen haben"
            )
        limit = args.get("limit", transcript.DEFAULT_SEARCH_LIMIT)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not transcript.SEARCH_LIMIT_MIN <= limit <= transcript.SEARCH_LIMIT_MAX
        ):
            raise ValueError(
                "session_search limit muss eine Ganzzahl "
                f"{transcript.SEARCH_LIMIT_MIN}..{transcript.SEARCH_LIMIT_MAX} sein"
            )
        found = store.search(ctx.conversation, query, limit=limit)
        return transcript.render_results(found)

    return session_search


# Name -> Runner. Der Executor schlägt das Tool hier nach.
# `undo_last` fehlt bewusst: es braucht den Event-Log und wird in __main__ ergänzt.
# `ask_operator` fehlt aus demselben Grund: es braucht die geteilte `QuestionDesk`
# und den Rückweg in den Chat. `session_search` fehlt ebenfalls: es braucht den
# geteilten TranscriptStore und den Thread-Kontext des Conductors.
RUNNERS = {
    "read_file": read_file,
    "write_file": write_file,
    "run_shell": run_shell,
    "vault_search": vault_search,
    "vault_get": vault_get,
    "vault_write_note": vault_write_note,
}


def make_vault_runners(vault_dir: Path, qmd_bin: str) -> dict[str, Callable[[ToolRequest], str]]:
    """Build vault runners from the same production configuration as the kernel."""
    return {
        "vault_search": make_vault_search_runner(vault_dir, qmd_bin),
        "vault_get": make_vault_get_runner(vault_dir),
        "vault_write_note": make_vault_write_runner(vault_dir, qmd_bin),
    }


def default_manifest() -> ToolManifest:
    """Manifest der Start-Tools. Effekt/Reversibilität steuern das Gating.

    `undo_last` ist ein WRITE wie jedes andere — ein Rückrollen auf `~/.bashrc` fragt
    the operator also genauso wie ein Schreiben dorthin. Der Rückwärtsgang ist keine Hintertür.
    """
    manifest = (
        ToolManifest()
        .with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
        .with_tool(ToolSpec("write_file", Effect.WRITE, reversible=True))
        .with_tool(ToolSpec("run_shell", Effect.EXEC, reversible=False))
        .with_tool(ToolSpec("entity_status", Effect.READ, reversible=True))
        .with_tool(ToolSpec("vault_search", Effect.READ, reversible=True))
        .with_tool(ToolSpec("vault_get", Effect.READ, reversible=True))
        .with_tool(ToolSpec("vault_write_note", Effect.WRITE, reversible=True))
        .with_tool(ToolSpec("undo_last", Effect.WRITE, reversible=True))
        # Fragen wirkt nicht: kein Byte bewegt sich, kein Kommando läuft. READ hält es
        # deshalb ohne eigene Freigabe-Runde bei ALLOW — eine Rückfrage, die selbst
        # freigabepflichtig wäre, müsste den Betreiber um Erlaubnis bitten, ihn fragen
        # zu dürfen. Ausdrücklich kein WRITE und kein EXEC.
        .with_tool(ToolSpec("ask_operator", Effect.READ, reversible=True))
        # Suche im eigenen Gespraechsarchiv. READ wie `vault_search`; die Grenze
        # (nur DIESE Konversation) haelt der Runner ueber den Thread-Kontext.
        .with_tool(ToolSpec("session_search", Effect.READ, reversible=True))
        # Delegieren ist READ, und zwar nicht aus Bequemlichkeit: der Untergebene laeuft
        # unter einer Decke, die ihm alles ausser Lesen verbietet, also KANN der Aufruf
        # nichts bewirken, was ein Lesen nicht auch bewirkt. Waere er WRITE, muesste der
        # Betreiber jedes Nachsehen freigeben — und eine Freigabe, die man reflexhaft
        # erteilt, ist genau die, die spaeter durchgewunken wird.
        .with_tool(ToolSpec("delegate", Effect.READ, reversible=True))
        # Eine Beratung durch einen zweiten, operator-konfigurierten Agenten ist READ:
        # sie liefert nur begrenzten Text zurueck und erteilt weder Capability noch
        # Freigabe. Ziel und Credential kommen nie aus Modellargumenten.
        .with_tool(ToolSpec("agent_consult", Effect.READ, reversible=True))
        # Eine begrenzte Coding-Aufgabe an den eingesperrten Claude-Worker. EXEC
        # mit sandbox_required, die run_shell-Vertrauensform: die Wirkung entsteht
        # im kernel-abgeleiteten Job-Workspace, eingesperrt (Netz an — die eine
        # dokumentierte Abweichung, die Anthropic-API wird gebraucht). requires_env:
        # ohne konfigurierten Socket gibt es keinen Worker und keinen Grant.
        .with_tool(ToolSpec("delegate_code", Effect.EXEC, reversible=False,
                            requires_env=frozenset({"TALOS_CLAUDE_WORKER_SOCKET"}),
                            sandbox_required=True))
        # Den Stand eines delegierten Jobs lesen: READ wie `session_search` — der
        # Aufruf fasst nichts an, er fragt den Worker.
        .with_tool(ToolSpec("delegate_status", Effect.READ, reversible=True,
                            requires_env=frozenset({"TALOS_CLAUDE_WORKER_SOCKET"})))
        # Der rendernde Browser. READ wie `web_fetch`: er liest eine Seite, er bedient
        # sie nicht — Klicken und Formulare gibt es bewusst nicht, weil ein Klick kein
        # ableitbares Ziel hat und ein Werkzeug ohne Ziel per Bauart DENY ist.
        .with_tool(browser.browse_spec())
        # Sehen ist Lesen: das Ziel ist der Bildpfad, also greifen dieselben
        # Floors wie bei `read_file` — ein Bild unter `~/.secrets/` wird nicht
        # angesehen, und der Kernel sieht das Ziel, weil TALOS die Datei laedt
        # und nicht der Reasoner.
        .with_tool(vision.vision_spec())
        # Sprechen erzeugt eine DATEI — also WRITE mit echtem Ziel. Der Kernel
        # urteilt ueber den Ausgabepfad wie ueber jedes Schreiben, der
        # Snapshotter sichert das Vorherige, `/undo` nimmt es zurueck.
        .with_tool(speech.speak_spec())
        .with_tool(hearing.hear_spec())
        # Standbild aus einem Video. Zwei Ziele: das Video (sonst waere das hier
        # der Weg am Secret-Floor vorbei) und das Bild, dessen Pfad der Kernel
        # selbst ableitet statt ihn dem Modell zu ueberlassen. READ, obwohl eine
        # Datei entsteht — die Begruendung steht in `frames.grab_frame_spec` und
        # ist keine Bequemlichkeit, sondern die haertere Einordnung. Danach ist es
        # ein gewoehnliches Foto fuer `see_image`; eine Videoanalyse entsteht
        # daraus ausdruecklich nicht.
        .with_tool(frames.grab_frame_spec())
    )
    for spec in web.web_manifest_specs():
        manifest = manifest.with_tool(spec)
    return manifest


def make_delegate_runner(
    *,
    executor: Callable[[], object],
    ceiling: object,
    propose: Callable[[str], Callable[[list[str]], str]],
    run_id: Callable[[], str],
    max_steps: int = 0,
) -> Callable[[ToolRequest], str]:
    """Baut den `delegate`-Runner: ein zweiter Lauf, der ausschliesslich lesen darf.

    Laeuft SYNCHRON im selben Thread — deshalb wirkt die thread-gebundene Decke
    (`ReadOnlyCeiling`) genau auf ihn und auf nichts sonst. Er benutzt denselben
    Executor, denselben Kernel und dieselbe Identitaet wie der Hauptlauf: es entsteht
    kein zweiter Weg zu einer Wirkung, nur ein engerer zu einer Auskunft.

    `executor` wird spaet gebunden (Callable), weil er im Hauptprogramm erst nach den
    Runnern entsteht — dieselbe Aufloesung des Zyklus wie bei `ask_operator`.

    Die Frage kommt aus `req.args` und ist damit Modelltext — das ist unbedenklich,
    weil sie nichts erlaubt, sondern nur beschreibt, was nachgesehen werden soll. Die
    ANTWORT kehrt begrenzt zurueck und wird vom Aufrufer wie jedes Werkzeug-Ergebnis
    behandelt: als Daten, nie als Anweisung.
    """
    from .agent_loop import run_agent
    from .subagent import (
        DELEGATE_MAX_STEPS, MAX_PARALLEL, MAX_QUESTION_CHARS, bound_answer,
    )

    grenze = max_steps or DELEGATE_MAX_STEPS

    def _fragen(args: dict) -> tuple[str, ...]:
        """Eine Frage oder mehrere — mehrere laufen NEBENEINANDER."""
        roh = args.get("questions")
        if not isinstance(roh, list):
            roh = [args.get("question", "")]
        sauber = tuple(
            " ".join(str(f).split())[:MAX_QUESTION_CHARS] for f in roh
        )
        return tuple(f for f in sauber if f)[:MAX_PARALLEL]

    def _einer(frage: str) -> str:
        """Ein Untergebener. Betritt die Decke in SEINEM Thread — deshalb ist sie
        thread-gebunden: nebeneinander laufende Untergebene teilen sie sich nicht,
        jeder traegt seine eigene, und der Hauptlauf bleibt unberuehrt."""
        with ceiling.active():
            return bound_answer(
                run_agent(propose(frage), executor(), identity[0], run_id(), max_steps=grenze).text
            )

    identity: list = [None]

    def delegate(req: ToolRequest) -> str:
        fragen = _fragen(req.args)
        if not fragen:
            raise ValueError("delegate braucht eine Frage")
        identity[0] = req.identity
        if len(fragen) == 1:
            return _einer(fragen[0])

        # Mehrere: je ein Thread. Ein gescheiterter Untergebener nimmt die anderen NICHT
        # mit — sein Fehler wird an seiner Stelle berichtet, damit die Luecke sichtbar
        # ist statt still zu fehlen.
        antworten: list[str] = [""] * len(fragen)

        def lauf(index: int, frage: str) -> None:
            try:
                antworten[index] = _einer(frage)
            except Exception as fehler:
                antworten[index] = f"(failed: {fehler})"

        threads = [
            threading.Thread(target=lauf, args=(i, f), name=f"talos-delegate-{i}")
            for i, f in enumerate(fragen)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return "\n".join(
            f"[{i + 1}. {frage}]\n{antwort}"
            for i, (frage, antwort) in enumerate(zip(fragen, antworten))
        )

    return delegate


def make_delegate_code_runner(
    *,
    socket_path: str,
    work_root: str,
    exchange: claudejobs.Exchange | None = None,
    browser_enabled: bool = False,
    mcp_allowed: frozenset[str] = frozenset(),
) -> Callable[[ToolRequest], str]:
    """Baut den `delegate_code`-Runner. Dumm: ableiten, abschicken, formatieren.

    job_id erzeugt der Runner selbst — stuende sie in den Argumenten, koennte das
    Modell bestehende Jobs adressieren. Den Workspace leitet er ueber DIESELBE
    Kernelfunktion ab, ueber deren Wurzel der Kernel eben geurteilt hat (das
    grab_frame-Muster: der Runner baut die Sanitisierungsregel nicht nach, er
    ruft sie). Entschieden ist laengst, bevor hier etwas laeuft; ein Fehler des
    Workers wird benannt zurueckgegeben, nie still ersetzt. `browser: true`
    fordert chrome-devtools-mcp IM Job an — nur wirksam, wenn der Betreiber den
    Schalter gesetzt hat; eine Anforderung gegen einen abgeschalteten Schalter
    wird benannt abgelehnt, nie still ohne Browser gefahren (die Evidenz des
    Kernels wuerde sonst luegen). `"mcp": ["name", …]` ist die generische Form:
    `mcp_allowed` ist die fertig gerechnete Schnittmenge aus Registry-Datei
    (`data/mcp-servers.json`) und Agent-Schalter (`TALOS_MCP_SERVERS`), und ein
    Name ausserhalb davon wird benannt abgelehnt, BEVOR ein Frame den Prozess
    verlaesst. Das Worker-Gate dahinter bleibt eigenstaendig scharf.
    """

    def delegate_code(req: ToolRequest) -> str:
        prompt = _need(req, "prompt")
        browser = bool(req.args.get("browser"))
        if browser and not browser_enabled:
            return ("delegate_code: browser angefordert, aber der Browser-MCP "
                    "ist abgeschaltet (TALOS_BROWSER_MCP_ENABLED=0)")
        gewuenscht = req.args.get("mcp", [])
        if (not isinstance(gewuenscht, list)
                or not all(isinstance(n, str) for n in gewuenscht)):
            return ("delegate_code: mcp muss eine Liste von Servernamen sein "
                    '(z.B. "mcp": ["chrome-devtools"])')
        namen = list(dict.fromkeys(gewuenscht))
        for name in namen:
            if name not in mcp_allowed:
                return (f"delegate_code: mcp-Server {name!r} ist nicht "
                        "freigeschaltet (Registry data/mcp-servers.json "
                        "geschnitten mit TALOS_MCP_SERVERS)")
        job_id = uuid.uuid4().hex[:12]
        # Blattname aus der Kernelfunktion, Wurzel aus der Verdrahtung — im
        # Dienst ist das policy.claude_work_root(), und beide stimmen ueberein.
        workspace = str(Path(work_root) / Path(claude_job_workspace(job_id)).name)
        antwort = claudejobs.submit_job(
            socket_path, job_id, prompt, workspace, exchange=exchange,
            browser_mcp=browser, mcp_servers=namen)
        if not antwort.get("ok"):
            return (f"delegate_code: worker {antwort.get('kind', 'unavailable')}"
                    f" — {antwort.get('message', '')}")
        return (f"delegate_code job_id={job_id} state={antwort.get('state')}"
                f" (workspace {workspace})")

    return delegate_code


def make_delegate_status_runner(
    *,
    socket_path: str,
    exchange: claudejobs.Exchange | None = None,
) -> Callable[[ToolRequest], str]:
    """Baut den `delegate_status`-Runner. Liest den Worker-Stand und formatiert
    ihn — Beweis (summary/files) kommt aus dem Stream des Workers, nie aus Prosa."""

    def delegate_status(req: ToolRequest) -> str:
        job_id = _need(req, "job_id")
        antwort = claudejobs.job_status(socket_path, job_id, exchange=exchange)
        if not antwort.get("ok"):
            return (f"delegate_status: worker {antwort.get('kind', 'unavailable')}"
                    f" — {antwort.get('message', '')}")
        zeilen = [f"delegate_status job_id={job_id} state={antwort.get('state', '?')}"]
        if antwort.get("state") == "done":
            zeilen.append(f"summary: {antwort.get('summary', '')}")
            dateien = antwort.get("files") or []
            zeilen.append(f"files: {', '.join(dateien) if dateien else '(none)'}")
            zeilen.append(f"returncode: {antwort.get('returncode')}")
        if antwort.get("state") in ("failed", "timeout"):
            zeilen.append(f"returncode: {antwort.get('returncode')}")
            if antwort.get("error"):
                zeilen.append(f"error: {antwort.get('error')}")
        return "\n".join(zeilen)

    return delegate_status
