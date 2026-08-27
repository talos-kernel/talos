"""Talos-Konfiguration. Secrets nur aus Env/Datei, fail-fast bei fehlenden Werten.

Talos (Talos) — der bronzene Wächter-Automat. MVP-Konfiguration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .channel import Principal
from .credentials import (
    LEGACY_BASE_URL,
    LEGACY_MESSAGE,
    CredentialStore,
    from_lookup,
    parse_worker_socket,
)
from .web import parse_allowed_addresses

HOME = Path.home()
# Wo die Zugangsdaten liegen. Ueberschreibbar, weil eine zweite Instanz auf demselben
# Rechner eine eigene Datei braucht (etwa `<name>-telegram.env`) — stand der Pfad
# fest im Quelltext, ueberschrieb ihn jeder Deploy und der Agent startete nicht mehr,
# mit einer Meldung, die auf die falsche Datei zeigte.
SECRETS_ENV = Path(
    os.environ.get("TALOS_SECRETS_ENV") or (HOME / ".secrets" / "talos-telegram.env")
).expanduser()
# Der Installer legt seine Konfiguration NEBEN den Code (`~/talos/talos.env`) und sagt dem
# Nutzer, er solle genau die editieren. Gelesen wurde bisher nur `SECRETS_ENV` — der
# oeffentliche Weg endete also zuverlaessig mit „TELEGRAM_BOT_TOKEN fehlt", obwohl der
# Nutzer getan hatte, was dastand. Beide Orte werden jetzt gelesen.
#
# Rangfolge, absichtlich in dieser Reihenfolge: Prozess-Env schlaegt alles (so ueberschreibt
# ein Dienst gezielt), danach `~/.secrets` (der private, ausserhalb des Codes liegende Ort),
# zuletzt die Datei im Installationsverzeichnis. Ein liegengebliebenes `talos.env` aus einer
# alten Installation darf eine bewusst gesetzte private Konfiguration nicht verdraengen.
INSTALL_DIR = Path(__file__).resolve().parent.parent
LOCAL_ENV = INSTALL_DIR / "talos.env"

# Skills liegen dort, wo der Betreiber sie ohnehin hat — Talos liefert KEINE mit.
# Das ist eine Lizenzentscheidung, keine Bequemlichkeit: „Agent Skills" ist ein offener
# Standard, aber jeder einzelne Skill hat seinen eigenen Autor und seine eigene Lizenz
# (die Spec kennt dafuer ein Feld `license`). Fremde Skills auszuliefern waere der
# einzige Schritt, der rechtlich heikel wuerde. Gelesen wird in dieser Reihenfolge;
# bei Namensgleichheit gewinnt die erste Wurzel. Fehlende Verzeichnisse sind still.
SKILLS_DIRS: tuple[Path, ...] = (
    INSTALL_DIR / "skills",
    HOME / ".talos" / "skills",
    HOME / ".claude" / "skills",
)
# Am Installationsort verankert, nie an einem geratenen Pfad. Der feste Wert
# (`~/talos/talos/data`) funktionierte nur, solange es genau eine Installation gab und
# sie zufaellig dort lag. Eine zweite unter einem anderen Praefix legte beim Start ein
# LEERES `data/` an und lief scheinbar normal weiter — mit verlorenem Event-Log, und
# damit ohne Autonomie-Stand, ohne stehende Freigaben, ohne Modellwahl und ohne
# Zeitplaene. Nichts davon meldet sich; ein Agent ohne Gedaechtnis sieht aus wie ein
# frisch installierter.
DATA_DIR = INSTALL_DIR / "data"
EVENTLOG_DB = DATA_DIR / "eventlog.db"
# Das Langzeitgedaechtnis liegt neben dem Event-Log — beides gehoert dem Betreiber
# und beides ist gitignored. Ein Update kopiert `data/`, ersetzt es nie.
RECALL_DB = DATA_DIR / "recall.db"
# Das Gespraechsarchiv (session_search) — dieselbe Zusicherung: gehoert dem Betreiber,
# gitignored, ein Update kopiert `data/` und ersetzt es nie.
TRANSCRIPT_DB = DATA_DIR / "transcript.db"
# Zeitplaene — dieselbe Zusicherung wie Event-Log, Recall und Archiv.
SCHEDULE_DB = DATA_DIR / "schedules.db"
# Mitgelieferte Automatisierungs-Blueprints (talos/blueprints.py). Quelltext, keine
# Laufzeitdaten: installiert wird daraus erst auf Kommando; der INSTALLIERTE Stand
# liegt unter `data/`, wie alles, was ein Update mitnehmen darf.
BLUEPRINTS_DIR = INSTALL_DIR / "blueprints"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
# Die live geholten Modellnamen je Anbieter. Liegt bei den Laufzeitdaten und nicht
# bei der Konfiguration: er ist wiederbeschaffbar, und ein Update darf ihn verlieren.
MODEL_CACHE = DATA_DIR / "models-cache.json"
# Operator-owned entity graph. Like recall and transcripts it survives updates, but it
# is declarative context rather than a database: names, distinctions and fixed status
# sources are reviewable in one bounded JSON file.
ENTITIES_FILE = DATA_DIR / "entities.json"
# Stimmmodelle fuer die Sprachausgabe. Neben `data/`, weil sie dem Betreiber
# gehoeren und ein Update sie mitnimmt statt sie zu ersetzen.
VOICE_DIR = DATA_DIR / "voices"
PIPER_BIN = str(INSTALL_DIR / ".venv" / "bin" / "piper")
VAULT_DIR = HOME / ".talos" / "vault"
QMD_BIN = str(HOME / ".local" / "bin" / "qmd")
HERMES_BIN = str(HOME / ".local" / "bin" / "hermes")
DEFAULT_MODEL_PROVIDER = "openai-codex"
DEFAULT_MODEL = "gpt-5.6-sol"
# Statusanzeige: "geometric" (Vorgabe, Talos' gravierte Zeichen) oder "expressive"
# (Emoji + Verben). Nur die Anzeige, nie die Substanz. Eine Instanz waehlt ueber
# TALOS_STATUS_STYLE; ein unbekannter Wert bleibt geometrisch.
STATUS_STYLE = "geometric"


def _default_hermes_helper(filename: str) -> Path:
    """Find Hermes helper modules in wrapper checkout, uv tool, or source install."""
    wrapper_checkout = HOME / ".hermes" / "hermes-agent" / "hermes_cli" / filename
    if wrapper_checkout.is_file():
        return wrapper_checkout
    executable = Path(HERMES_BIN).expanduser().resolve(strict=False)
    tool_root = executable.parent.parent
    matches = sorted(tool_root.glob(f"lib/python*/site-packages/hermes_cli/{filename}"))
    if matches:
        return matches[0]
    return Path("/usr/local/lib/hermes-agent/hermes_cli") / filename


HERMES_PROVIDER_CATALOG = _default_hermes_helper("provider_catalog.py")
HERMES_MODELS = _default_hermes_helper("models.py")

# Absichtlich LEER. Wer den Agenten steuern darf, ist die einzige Schranke vor dem
# Kernel — ein eingebauter Vorgabewert waere hier keine Bequemlichkeit, sondern eine
# im Quelltext nachlesbare Hintertuer. Ohne `TALOS_ALLOWED_PRINCIPALS` startet nichts.
DEFAULT_ALLOWED_USER_IDS = ""

CLAUDE_BIN = "/usr/local/bin/claude"  # OAuth/Max-Wrapper, kein API-Billing
REASONER_TIMEOUT_S = 180

# Guardian Mode. Stand bis 2026-08-02: JEDES run_shell holte eine Freigabe, auch `ls` —
# der Kommentar im Kernel nannte das selbst einen Platzhalter fuer die fehlende Sandbox.
# Jetzt entscheidet der Command-Floor (nach Hermes modelliert und unveraendert scharf):
# hardline -> DENY, riskant -> Freigabe, geschuetzter Pfad im Kommando -> DENY, Rest laeuft.
# Bewusster Tausch, den Hermes' yolo genauso eingeht: ein Shell-String laesst sich
# rekonstruieren (`P=/etc; cat $P/passwd`), der Pfad-Floor sieht nur literale Tokens.
# Rueckweg ohne Deploy: TALOS_SHELL_NEEDS_HUMAN=1 in der Env, oder `/autonomy 3`.
SHELL_NEEDS_HUMAN = os.environ.get("TALOS_SHELL_NEEDS_HUMAN", "0") == "1"
TELEGRAM_POLL_TIMEOUT_S = 50


def _read_env_file(path: Path) -> dict[str, str]:
    """Liest eine simple KEY=VALUE-Env-Datei (0600). Fehlt sie -> leeres Dict."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class TalosConfig:
    bot_token: str
    bot_username: str
    allowed_principals: frozenset[Principal]
    eventlog_db: Path
    snapshot_dir: Path = SNAPSHOT_DIR
    claude_bin: str = CLAUDE_BIN
    reasoner_timeout_s: int = REASONER_TIMEOUT_S
    poll_timeout_s: int = TELEGRAM_POLL_TIMEOUT_S
    vault_dir: Path = VAULT_DIR
    qmd_bin: str = QMD_BIN
    hermes_bin: str = HERMES_BIN
    hermes_provider_catalog: Path = HERMES_PROVIDER_CATALOG
    hermes_models: Path = HERMES_MODELS
    model_provider: str = DEFAULT_MODEL_PROVIDER
    model_name: str = DEFAULT_MODEL
    # Die Laufzeit-Fallback-Kette (TALOS_MODEL_FALLBACKS), kommagetrennt als
    # `provider/model`. Leer heisst: kein Fallback, ein Fehler ist ein Fehler.
    # Sie gilt pro Lauf und ruehrt die persistierte Modellwahl nie an.
    model_fallbacks: str = ""
    # Der Socket des Modell-Workers (TALOS_MODEL_WORKER=socket://…). Leer heisst:
    # Direktweg — Vorgabe. Gesetzt heisst: der Agent haelt KEINE Provider-Schluessel
    # mehr; `api_credentials` ist dann absichtlich leer, und das ist der Soll-Zustand.
    model_worker: str = ""
    # Fester, operator-owned Beratungskanal zu einem zweiten Agenten. Das Token bleibt
    # in der Secret-Datei und wird nie Bestandteil eines Tool-Arguments.
    agent_consult_url: str = ""
    agent_consult_token: str = ""
    # Private Namen/Aliase des konsultierbaren Agenten; nur fuer explizite Handoff-Erkennung.
    agent_consult_aliases: tuple[str, ...] = ()
    # Der Claude-Worker: aus, bis der Betreiber ihn bewusst einschaltet und ihm einen
    # Socket zeigt. Ohne Socket gibt es das Werkzeug gar nicht — ein konfigurierter,
    # aber toter Dienst waere ein Pfad, der bei jeder Delegation scheitert.
    claude_worker_enabled: bool = False
    claude_worker_socket: str = ""
    claude_worker_root: str = ""
    # Eigenes HOME der Claude-Jobs — dort liegt NUR der Claude-OAuth-Stand, kein
    # Talos-Geheimnis. Es ist absichtlich ein zweites Zuhause, nicht das des Agenten.
    claude_worker_home: str = ""
    claude_worker_bin: str = "claude"
    claude_worker_max_parallel: int = 2
    claude_worker_job_timeout_s: int = 900
    # Browser-Automatisierung (chrome-devtools-mcp) INNERHALB der Worker-Sandbox,
    # nie als natives Werkzeug — Vorgabe AUS, wie der Worker selbst: ein Chrome
    # mit Netz im Job erweitert die Angriffsflaeche der Sandbox, also ist es ein
    # bewusster Betreiber-Entscheid und kein Mitlaeufer des Worker-Schalters.
    browser_mcp_enabled: bool = False
    # Der Completion-Push: eine kurze, faktische Meldung, wenn ein delegierter Job
    # endet. Voreingestellt AN — ein Job, der nebenher laeuft, hat sonst keinen Weg
    # zurueck. Er liefert nie Modellprosa; wer ihn abstellt, fragt per delegate_status.
    completion_push: bool = True
    # Die Attended-Auto-Freigabe: in einem interaktiven Lauf (eine eingehende
    # Nachricht eines erlaubten Principals — ein Mensch ist da und kann hinschauen)
    # laeuft die Routineklasse ohne Freigabe-Prompt: reversible Werkzeuge mit
    # Snapshot/undo und eingesperrte Shell-Arbeit ohne Zugangsdaten. Unbeaufsichtigte
    # und delegierte Laeufe bleiben exakt so strikt wie bisher, die Kernel-Floors
    # auch — die Auto-Freigabe greift nur auf ein kernel-eigenes NEEDS_HUMAN, nie
    # auf ein DENY. Voreingestellt AN (Owner-Entscheid); jede Auto-Freigabe steht
    # als `approval.auto_attended` im Event-Log.
    attended_autoapprove: bool = True
    status_style: str = STATUS_STYLE
    shell_needs_human: bool = SHELL_NEEDS_HUMAN
    skills_dirs: tuple[Path, ...] = SKILLS_DIRS
    # WhatsApp ist ein reiner MELDE-Weg (Trust.NOTIFY). Fehlen die Werte, wird der
    # Kanal gar nicht erst angelegt — ein Zustellweg ohne Zugangsdaten waere ein
    # Kanal, der bei jeder Nachricht scheitert, statt schlicht nicht da zu sein.
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_to: str = ""
    # Mail als zweiter EINGANG — per IMAP-Abruf, nie per empfangendem Server. Fehlt eines
    # der drei Pflichtstuecke, gibt es den Kanal nicht (dieselbe Regel wie bei WhatsApp).
    mail_host: str = ""
    mail_user: str = ""
    mail_password: str = ""
    # Getrennt, weil IMAP und SMTP bei den meisten Anbietern auf verschiedenen Namen
    # liegen. Leer heisst: derselbe Host.
    mail_smtp_host: str = ""
    # Die Kennung, mit der sich der eigene Mailserver im `Authentication-Results`-Kopf
    # ausweist. Gesetzt, prueft Talos NUR den Stempel dieses Servers — ohne sie zaehlt
    # der oberste Kopf. Siehe `mail.verify_sender`.
    mail_authserv_id: str = ""
    # Die eigenen API-Zugangsdaten — der einzige Weg, den die oeffentliche Fassung geht.
    # Leer heisst: es wird ueber eine lokal angemeldete CLI gedacht.
    # ⚠️ Ein Bestand pro ANBIETER, nie ein Schluessel fuer alle: bis 05.08. stand hier
    # `ANTHROPIC_API_KEY or OPENAI_API_KEY` in EINEM Feld, und wer den Anbieter wechselte,
    # schickte den Schluessel des einen an den anderen. Siehe `credentials.py`.
    api_credentials: CredentialStore = field(default_factory=CredentialStore)
    # Websuche braucht einen eigenen Schluessel. Ohne ihn gibt es KEINEN geratenen
    # Ersatzanbieter — das Werkzeug meldet sich sauber als nicht verfuegbar.
    brave_api_key: str = ""
    # `http://` bleibt aus. Wer es braucht (lokales Testgeraet), schaltet es bewusst frei.
    web_allow_http: bool = False
    # Einzelne Adressen, die den SSRF-Adressfilter passieren duerfen — typisch der eigene
    # Server im Tailnet. Bewusst Adressen, nie Netze: „mein VPS" ist eine Adresse, „das
    # Tailnet" waere ein Bereich, und Bereiche sind das, wogegen der Filter gebaut ist.
    web_allowed_addresses: frozenset[str] = frozenset()


def load_config(*, require_channel: bool = True) -> TalosConfig:
    """Baut die Config aus Env-Datei + Prozess-Env.

    `require_channel=False` heisst: dieser Aufruf braucht den Messenger gar nicht. Er ist
    fuer `ask` und `chat` gedacht, die ueber den CLI-Kanal laufen und ihre Antwort dorthin
    schreiben, wo sie gestartet wurden.

    ⚠️ Das ist eine Lockerung und wird als solche benannt. Bis 0.9.0 verlangte JEDER
    Einstieg einen Telegram-Token — auch der, der Telegram nicht anfasst. Eine frische
    Installation endete deshalb bei `talos ask "hallo"` in einem **unbehandelten
    Traceback** ueber einen fehlenden Bot-Token. Fuer jeden, der das Projekt zum ersten
    Mal ausprobiert, war das die erste und letzte Begegnung damit.

    Was die Lockerung NICHT anfasst: sie gibt niemandem ein Recht. Die Allowlist gilt
    unveraendert, der Kernel urteilt unveraendert, und im Dienstbetrieb bleibt der Token
    Pflicht — dort holt Talos wirklich Nachrichten ab, und ein Dienst ohne Kanal waere ein
    Prozess, der auf nichts hoert.
    """
    secrets = {**_read_env_file(LOCAL_ENV), **_read_env_file(SECRETS_ENV)}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token and require_channel:
        # Die Meldung nennt beide Orte und den Ausweg. Eine Fehlermeldung, die nur einen
        # Pfad nennt, schickt den Nutzer an die falsche Datei — genau das ist passiert.
        raise ValueError(
            "TELEGRAM_BOT_TOKEN fehlt — weder in der Prozess-Umgebung noch in "
            f"{LOCAL_ENV} noch in {SECRETS_ENV}. "
            "Richte es mit `python -m talos setup` ein."
        )
    username = os.environ.get("TELEGRAM_BOT_USERNAME") or secrets.get(
        "TELEGRAM_BOT_USERNAME", ""
    )
    # Neue Form `telegram:100000001`; eine nackte Zahl bleibt lesbar und landet auf
    # LEGACY_CHANNEL. Die alte Variable gilt weiter, damit ein Deploy nicht daran haengt.
    # Auch aus der Env-DATEI, nicht nur aus der Prozess-Umgebung: jeder andere Wert wird
    # so gelesen, dieser eine war die Ausnahme. Wer seine Kennung ordentlich in
    # `talos.env` eintrug, bekam trotzdem „ist leer — das waere offen fuer alle" und
    # hatte keinen Hinweis, dass genau diese Variable anders behandelt wird.
    raw_ids = (
        os.environ.get("TALOS_ALLOWED_PRINCIPALS")
        or os.environ.get("TALOS_ALLOWED_USER_IDS")
        or secrets.get("TALOS_ALLOWED_PRINCIPALS", "")
        or secrets.get("TALOS_ALLOWED_USER_IDS", "")
        or DEFAULT_ALLOWED_USER_IDS
    )
    allowed = frozenset(
        Principal.parse(part) for part in raw_ids.replace(",", " ").split() if part.strip()
    )
    if not allowed:
        if require_channel:
            # Der Dienst holt Nachrichten von aussen ab. Eine leere Liste hiesse dort
            # wirklich „offen fuer alle" — das bleibt ein Abbruch.
            raise ValueError("TALOS_ALLOWED_PRINCIPALS ist leer — das wäre offen für alle.")
        # ⚠️ Die DRITTE Schicht der Erstlauf-Wand, und die einzige Stelle, an der sie
        # faellt. Ohne Messenger gibt es genau einen Kanal: die Kommandozeile. Eine leere
        # Liste heisst dort nicht „offen fuer alle", sondern „nur wer an dieser Maschine
        # eine Shell hat" — und wer die hat, kann seine Kennung ohnehin selbst
        # eintragen. Die Wand forderte also eine Zeremonie, deren Ergebnis feststand.
        #
        # Eingesetzt wird der Aufrufer WIRKLICH, statt ihn nur durchzulassen: sonst
        # bekaeme der Kernel eine leere Erlaubnisliste und wiese danach jede Handlung ab
        # — hereingelassen, aber handlungsunfaehig, und das waere schlimmer als die Wand.
        # So steht auch in `doctor`, wer befehlen darf, statt „0 principals" zu melden.
        #
        # Es bleibt bei GENAU dieser einen Kennung. Steht auch nur ein Eintrag in der
        # Liste, ist sie erschoepfend und wird nicht ergaenzt.
        allowed = frozenset({Principal("cli", str(os.getuid()))})
    raw_skill_dirs = (
        os.environ.get("TALOS_SKILLS_DIRS") or secrets.get("TALOS_SKILLS_DIRS", "")
    ).strip()
    skills_dirs = (
        tuple(Path(part).expanduser() for part in raw_skill_dirs.split(os.pathsep) if part.strip())
        if raw_skill_dirs
        else SKILLS_DIRS
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    def _value(name: str) -> str:
        return (os.environ.get(name) or secrets.get(name, "")).strip()

    # ⚠️ Laut abbrechen statt still ignorieren. Wer die alte anbieterlose Adresse gesetzt
    # hat, wollte zu EINER bestimmten Maschine sprechen — sie kommentarlos gegen die
    # Vorgabe des Anbieters zu tauschen hiesse, seine Anfragen woanders hinzuschicken,
    # ohne dass es jemand merkt. Ein Startabbruch mit dem neuen Namen kostet eine Minute.
    if _value(LEGACY_BASE_URL):
        raise ValueError(LEGACY_MESSAGE)

    konfig = TalosConfig(
        skills_dirs=skills_dirs,
        whatsapp_token=_value("WHATSAPP_TOKEN"),
        whatsapp_phone_id=_value("WHATSAPP_PHONE_ID"),
        whatsapp_to=_value("WHATSAPP_TO"),
        mail_host=_value("TALOS_MAIL_HOST"),
        mail_user=_value("TALOS_MAIL_USER"),
        mail_password=_value("TALOS_MAIL_PASSWORD"),
        mail_smtp_host=_value("TALOS_MAIL_SMTP_HOST"),
        mail_authserv_id=_value("TALOS_MAIL_AUTHSERV_ID"),
        api_credentials=from_lookup(_value),
        brave_api_key=_value("TALOS_BRAVE_API_KEY"),
        web_allow_http=_value("TALOS_WEB_ALLOW_HTTP") == "1",
        bot_token=token,
        shell_needs_human=SHELL_NEEDS_HUMAN,
        bot_username=username,
        allowed_principals=allowed,
        eventlog_db=EVENTLOG_DB,
        snapshot_dir=SNAPSHOT_DIR,
        vault_dir=Path(
            os.environ.get("TALOS_VAULT_DIR")
            or secrets.get("TALOS_VAULT_DIR", str(VAULT_DIR))
        ).expanduser(),
        qmd_bin=(
            os.environ.get("TALOS_QMD_BIN")
            or secrets.get("TALOS_QMD_BIN", QMD_BIN)
        ),
        hermes_bin=(
            os.environ.get("TALOS_HERMES_BIN")
            or secrets.get("TALOS_HERMES_BIN", HERMES_BIN)
        ),
        hermes_provider_catalog=Path(
            os.environ.get("TALOS_HERMES_PROVIDER_CATALOG")
            or secrets.get("TALOS_HERMES_PROVIDER_CATALOG", str(HERMES_PROVIDER_CATALOG))
        ).expanduser(),
        hermes_models=Path(
            os.environ.get("TALOS_HERMES_MODELS")
            or secrets.get("TALOS_HERMES_MODELS", str(HERMES_MODELS))
        ).expanduser(),
        model_provider=(
            os.environ.get("TALOS_MODEL_PROVIDER")
            or secrets.get("TALOS_MODEL_PROVIDER", DEFAULT_MODEL_PROVIDER)
        ),
        model_name=(
            os.environ.get("TALOS_MODEL")
            or secrets.get("TALOS_MODEL", DEFAULT_MODEL)
        ),
        model_fallbacks=_value("TALOS_MODEL_FALLBACKS"),
        # ⚠️ Bewusst NUR das Prozess-Env, nicht die Env-Dateien: `ApiReasoner` liest
        # dieselbe Variable beim Bauen aus `os.environ` (die Naht gehoert dem
        # Reasoner, nicht diesem Modul). Zwei Quellen hiesse: in `talos.env`
        # gesetzt, im Reasoner ignoriert — ein stiller Rueckfall auf den Direktweg,
        # also Schluessel im Agenten, obwohl der Betreiber den Worker glaubt.
        # Der installierte Weg ist die Agent-Unit (`Environment=`), siehe
        # `docs/model-worker.md`.
        model_worker=parse_worker_socket(os.environ.get("TALOS_MODEL_WORKER", "")),
        agent_consult_url=_value("TALOS_AGENT_CONSULT_URL"),
        agent_consult_token=_value("TALOS_AGENT_CONSULT_TOKEN"),
        agent_consult_aliases=tuple(
            dict.fromkeys(
                part.strip()[:64]
                for part in _value("TALOS_AGENT_CONSULT_ALIASES").split(",")
                if part.strip()
            )
        )[:8],
        claude_worker_enabled=_value("TALOS_CLAUDE_WORKER_ENABLED") == "1",
        claude_worker_socket=_value("TALOS_CLAUDE_WORKER_SOCKET"),
        claude_worker_root=_value("TALOS_CLAUDE_WORKER_ROOT"),
        claude_worker_home=_value("TALOS_CLAUDE_WORKER_HOME"),
        claude_worker_bin=_value("TALOS_CLAUDE_WORKER_BIN") or "claude",
        claude_worker_max_parallel=int(
            _value("TALOS_CLAUDE_WORKER_MAX_PARALLEL") or "2"
        ),
        claude_worker_job_timeout_s=int(
            _value("TALOS_CLAUDE_WORKER_JOB_TIMEOUT") or "900"
        ),
        browser_mcp_enabled=_value("TALOS_BROWSER_MCP_ENABLED") == "1",
        completion_push=_value("TALOS_COMPLETION_PUSH") != "0",
        attended_autoapprove=_value("TALOS_ATTENDED_AUTOAPPROVE") != "0",
        status_style=(
            os.environ.get("TALOS_STATUS_STYLE")
            or secrets.get("TALOS_STATUS_STYLE", STATUS_STYLE)
        ),
        web_allowed_addresses=parse_allowed_addresses(
            os.environ.get("TALOS_WEB_ALLOWED_ADDRESSES")
            or secrets.get("TALOS_WEB_ALLOWED_ADDRESSES", "")
        ),
    )
    # Das Bridge-Token gehoert in KEINEN Kinderprozess: alles, was der Agent spaeter
    # startet (CLI-Reasoner, Browser, STT/TTS), erbt os.environ mit, und die Sandbox
    # deckt nur run_shell ab. Die Secret-Datei bleibt die Quelle — von dort kommt der
    # Wert bei jedem Laden erneut, das Prozess-Env darf ihn also verlieren. Traegt
    # NUR das Env ihn (keine Datei), bleibt er stehen: dann ist die Weitergabe an
    # Kinder die ausdrueckliche Konfiguration des Betreibers, und der zweite
    # Ladevorgang in run() braeche sonst den Kanal.
    if secrets.get("TALOS_AGENT_CONSULT_TOKEN"):
        os.environ.pop("TALOS_AGENT_CONSULT_TOKEN", None)
    return konfig
