"""Die Befehlszeile — und warum sie kurz bleibt.

Hermes hat rund siebzig Unterbefehle, OpenClaw rund fuenfzig. Beide sind darueber
gewachsen und tragen inzwischen Befehle, die sich gegenseitig erklaeren muessen. Talos
hat sieben, und jeder beantwortet eine Frage, die ein Betreiber wirklich stellt:

    setup    wie richte ich das ein
    doctor   warum geht etwas nicht
    config   was steht drin, und wie aendere ich es
    models   womit kann es denken
    status   was hat es zuletzt getan
    update   wie komme ich auf den neuen Stand
    version  welcher Stand ist das hier

⚠️ **Die Unterbefehle laufen VOR `load_config()`.** Sonst stirbt ausgerechnet `setup` an
der fehlenden Konfiguration, die es gerade anlegen soll — und `doctor` an genau dem
Zustand, den zu diagnostizieren seine Aufgabe ist. Das war einmal so und ist der Grund
fuer diese Reihenfolge.

⚠️ **Kein Befehl startet den Agenten nebenbei.** Wer ihn laufen lassen will, ruft
`python -m talos` ohne Argument. Auch `setup` und `update` hoeren auf, wenn sie fertig
sind — der Schalter bleibt beim Menschen.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import __version__
from .ux import SYM_FAIL, SYM_OK, SYM_TALOS

HELP = f"""
  {SYM_TALOS} talos {__version__}

  python -m talos                    run the agent
  python -m talos --once             a single cycle, for diagnosis

  chat                               a session in this terminal — approvals at a tty
  ask "..."                          one turn from here — no chat, no approvals
  setup [identity|model|mail]        configure it; writes a file and stops
  doctor [--online]                  what is missing — changes nothing
  config list|get|set|validate       read and change settings
  models [--refresh]                 which models a provider offers
  status                             what it did last
  events [--limit n] [--tool t]      what happened — filterable, read-only
  why <event-id>                     why that was allowed or refused
  verify                             prove the log was not edited after the fact
  report [--run <id>] [--out <f>]    a record for someone else to read
  review [--window <n>]              what this installation should change
  update [--check]                   new version beside the old one, tests first
  version                            {__version__}
  help                               this list

  Every subcommand takes --help.
"""


def _zeit(roh: object) -> str:
    """Der Zeitstempel des Logs ist eine Unix-Zahl. Abgeschnitten sah er aus wie ein
    Datum und war keins — hier wird er wirklich umgerechnet."""
    import datetime

    try:
        return datetime.datetime.fromtimestamp(float(roh)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(roh or "")[:19]


def cmd_version(out=None) -> int:
    (out or sys.stdout).write(f"{__version__}\n")
    return 0


def cmd_status(out=None, *, log=None) -> int:
    """Was zuletzt geschah — aus dem Event-Log, ohne Netz und ohne Konfiguration.

    Bewusst nicht „laeuft der Dienst": das weiss der Dienstverwalter besser (`systemctl
    status`), und eine zweite Antwort darauf waere eine, die manchmal luegt. Hier steht,
    was der Agent selbst protokolliert hat — die einzige Quelle, die ein Reasoner nicht
    faelschen kann.
    """
    schreiben = (out or sys.stdout).write
    if log is None:
        from .config import EVENTLOG_DB as EVENT_DB
        from .eventlog import EventLog

        if not Path(EVENT_DB).is_file():
            schreiben(f"\n  no event log yet at {EVENT_DB} — it has not run.\n\n")
            return 0
        log = EventLog(Path(EVENT_DB))

    modell = log.recent(1, ("model.selected",))
    letzte = log.recent(8)
    schreiben("\n")
    if modell:
        nutzlast = modell[-1].get("payload", {})
        schreiben(f"  model    {nutzlast.get('provider', '?')} / {nutzlast.get('model', '?')}\n")
    else:
        schreiben("  model    never chosen — the shipped default applies\n")
    if not letzte:
        schreiben("  events   none\n\n")
        return 0
    schreiben(f"  events   last {len(letzte)}, newest first\n\n")
    for eintrag in reversed(letzte):
        schreiben(f"    {_zeit(eintrag.get('ts'))}  "
                  f"{str(eintrag.get('type', '')):22} {eintrag.get('actor', '')}\n")
    schreiben("\n")
    return 0


def cmd_verify(out=None, *, log=None) -> int:
    """Ob das Event-Log seit dem Schreiben unveraendert ist — aus dem Log selbst, ohne
    Netz und ohne Konfiguration.

    Die Hash-Kette macht aus „vertrau dem Protokoll" ein „das Protokoll beweist sich".
    Bricht sie, nennt der Befehl die erste manipulierte Stelle und endet mit 1 — damit ein
    Aufruf in Skript, Cron oder Installer daran scheitert, statt still weiterzulaufen.

    ⚠️ Der Alt-Praefix wird ehrlich genannt: eine frisch aktualisierte Installation hat
    lauter ungekettete Zeilen, und „intakt" ueber null geschuetzten Eintraegen waere eine
    Halbwahrheit — genau die Sorte, die diese Software zu vermeiden verspricht.
    """
    schreiben = (out or sys.stdout).write
    if log is None:
        from .config import EVENTLOG_DB as EVENT_DB
        from .eventlog import EventLog

        if not Path(EVENT_DB).is_file():
            schreiben(f"\n  no event log yet at {EVENT_DB} — nothing to verify.\n\n")
            return 0
        log = EventLog(Path(EVENT_DB))

    broken = log.verify()
    total = log.count()
    protected = log.protected_count()
    legacy = total - protected
    schreiben("\n")
    if broken is None:
        schreiben(f"  {SYM_OK} the event log is intact — "
                  f"{protected} of {total} entries chained, none altered.\n")
        if legacy:
            wort = "entry predates" if legacy == 1 else "entries predate"
            schreiben(f"    {legacy} {wort} the chain and cannot be proven either way.\n")
        schreiben("\n")
        return 0
    schreiben(f"  {SYM_FAIL} the event log was edited after the fact — "
              f"first broken entry: id {broken}.\n")
    schreiben(f"    {protected} of {total} entries are chained; "
              "everything before the break still verified.\n\n")
    return 1


def _lazy(modul: str, name: str):
    """Erst beim Aufruf importieren. `doctor` soll nicht an einem Import scheitern,
    den nur `setup` braucht — und der Start des Agenten nicht an beiden."""

    def aufrufen(rest: list[str]) -> int:
        from importlib import import_module

        return getattr(import_module(f".{modul}", __package__), name)(rest)

    return aufrufen


def cmd_ask(rest: list[str], out=None) -> int:
    """`talos ask "…"` — ein Zug, Antwort auf stdout.

    Die beiden Riegel stehen VOR dem Aufbau: aus der Sandbox heraus gar nicht, und ohne
    Eintrag in der Allowlist auch nicht. Beides zu pruefen, nachdem der Agent schon
    steht, hiesse, den Denkweg fuer einen Aufruf zu oeffnen, der nichts darf.
    """
    import os

    from .askcli import check_identity, refuse_in_sandbox

    schreiben = (out or sys.stdout).write
    frage = " ".join(r for r in rest if not r.startswith("-")).strip()
    if not frage:
        schreiben('  usage: talos ask "your question"\n')
        return 2
    grund = refuse_in_sandbox()
    if grund:
        schreiben(f"  {grund}\n")
        return 3
    from .config import load_config

    config = load_config(require_channel=False)
    grund = check_identity(config.allowed_principals, os.getuid())
    if grund:
        schreiben(f"  {grund}\n")
        return 3

    from .__main__ import run

    run(ask=frage)
    return 0


def cmd_chat(rest: list[str], out=None) -> int:
    """`talos chat` — eine Sitzung im Terminal statt eines einzelnen Zuges.

    Dieselben zwei Riegel wie bei `ask`, in derselben Reihenfolge und aus demselben
    Grund: aus der Sandbox heraus gar nicht, und ohne Eintrag in der Allowlist auch
    nicht. Beides zu pruefen, nachdem der Agent schon steht, hiesse den Denkweg fuer
    einen Aufruf zu oeffnen, der nichts darf.
    """
    import os

    from .askcli import check_identity, refuse_in_sandbox

    schreiben = (out or sys.stdout).write
    if "--help" in rest or "-h" in rest:
        schreiben("  usage: talos chat        (a session in this terminal; `exit` to leave)\n")
        return 0
    grund = refuse_in_sandbox()
    if grund:
        schreiben(f"  {grund}\n")
        return 3
    from .config import load_config

    config = load_config(require_channel=False)
    grund = check_identity(config.allowed_principals, os.getuid())
    if grund:
        schreiben(f"  {grund}\n")
        return 3

    from .__main__ import run

    run(chat=True)
    return 0


def _help(_rest: list[str]) -> int:
    sys.stdout.write(HELP)
    return 0


# Die eine Wahrheit darueber, was es gibt. `HELP` wird in `test_cli.py` dagegen
# gehalten, damit die Hilfe keinen Befehl nennt, den es nicht gibt — und umgekehrt.
TABLE: dict[str, object] = {
    "setup": _lazy("setup_wizard", "run_setup"),
    "doctor": _lazy("doctor", "run_doctor"),
    "config": _lazy("configcli", "run_config"),
    "models": _lazy("models", "run_models"),
    "ask": cmd_ask,
    "chat": cmd_chat,
    "status": lambda _rest: cmd_status(),
    "events": _lazy("eventscli", "run_events"),
    "why": _lazy("eventscli", "run_why"),
    "verify": lambda _rest: cmd_verify(),
    "report": _lazy("report", "run_report"),
    "review": _lazy("review", "run_review"),
    "update": _lazy("updater", "run_update"),
    "version": lambda _rest: cmd_version(),
    "help": _help,
}


def dispatch(args: list[str]) -> int | None:
    """Fuehrt einen Unterbefehl aus. `None` heisst: kein Unterbefehl, also starten."""
    befehl = args[0] if args and not args[0].startswith("-") else ""
    if not befehl:
        if args and args[0] in ("-h", "--help"):
            return _help(args)
        if args and args[0] == "--version":
            return cmd_version()
        return None
    handler = TABLE.get(befehl)
    if handler is None:
        sys.stdout.write(f"\n  unknown command: {befehl}\n{HELP}")
        return 2
    return handler(args[1:])


COMMANDS = tuple(TABLE)

__all__ = ["COMMANDS", "HELP", "TABLE", "cmd_status", "cmd_verify", "cmd_version", "dispatch"]
