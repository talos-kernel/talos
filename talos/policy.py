"""Policy-Kernel — deterministisches Gating. KEIN LLM, keine Heuristik.

Kernel-Spec v0.2 §3/§8 und die Kernel-Kritik zweier unabhaengiger Pruefer: Risiko/Reversibilität
werden objektiv aus Zielressource + Argumenten + Identität + Tool-Manifest berechnet.
Grundhaltung: **default-deny**. Ein unbypassbarer Hardline-Floor (Hermes-Muster) verbietet
geschützte Pfade VOR jeder Erlaubnis. Reversibel → ALLOW; irreversibel → NEEDS_HUMAN.

Der Kernel *urteilt* — er *erlaubt* nicht. Aus einem ALLOW wird erst dann eine Wirkung,
wenn der Mint (capability.py) daraus ein Token auf genau diese Anfrage praegt. Die frühere
`write_exec_allowlist` ist damit ersatzlos weg: eine zweite Namensliste neben dem Manifest,
die ein Dauerrecht an einem Tool-Namen festmachte, statt an einer Handlung.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from . import command_floor
from .channel import Principal
from .manifest import Effect, ToolManifest
from .vault import DEFAULT_VAULT_DIR, VaultPathError, canonical_target_from_args

HOME = Path.home()

# Der eigene Bauplan — ABGELEITET aus dem Modulpfad, nie als Zeichenkette geraten.
# Ein fester Pfad hier ueberlebt weder eine Umbenennung des Verzeichnisses noch ein
# `TALOS_PREFIX=/opt/talos`: die Liste schuetzt danach ein Verzeichnis, das es nicht
# gibt, waehrend der Agent seinen eigenen Kernel und seine SOUL.md ungefragt
# ueberschreiben darf. Der Fall ist nicht theoretisch — er ist beim Umbenennen des
# Installationsverzeichnisses bereits eingetreten und musste von Hand nachgezogen werden.
PACKAGE_DIR = Path(__file__).resolve().parent
INSTALL_DIR = PACKAGE_DIR.parent
SOUL_FILE = INSTALL_DIR / "SOUL.md"
AGENTS_FILE = INSTALL_DIR / "AGENTS.md"
USER_FILE = INSTALL_DIR / "USER.md"
# Der freie Schreibbereich der Stufe 4 haengt am selben Anker (siehe `autonomy.WORKSPACE`).
# Er liegt bewusst NEBEN dem Code, nicht darin: laege er darunter, fienge der
# Persistenz-Floor jede gewoehnliche Schreibarbeit ab und Stufe 4 waere wertlos.
WORKSPACE_DIR = INSTALL_DIR / "workspace"


def _both_forms(*prefixes: str) -> tuple[str, ...]:
    """Jeden Praefix in beiden Schreibweisen: wie geschrieben UND aufgeloest.

    `_hits` vergleicht gegen `os.path.realpath(ziel)`. Ist der PRAEFIX selbst ein
    Symlink, trifft er danach nie: auf macOS zeigt `/etc` auf `/private/etc`, also
    wurde aus `write_file /etc/passwd` ein ALLOW — der komplette Tier-B-Floor war
    auf dieser Plattform offen, obwohl die Liste vollstaendig aussah.

    Es ist kein macOS-Sonderfall. Dieselbe Falle greift ueberall dort, wo HOME ein
    Symlink ist (verschluesselte oder verschobene Home-Verzeichnisse), und dann ist
    es `~/.ssh`, das aufhoert geschuetzt zu sein.

    Beide Formen stehen drin, nie nur die aufgeloeste: sonst faellt der Schutz aus,
    sobald der Link spaeter verschwindet oder woanders hin zeigt.
    """
    forms: list[str] = []
    for prefix in prefixes:
        forms.append(prefix)
        real = os.path.realpath(prefix)
        if real != prefix:
            forms.append(real)
    return tuple(dict.fromkeys(forms))


# Tier B — Systemzerstörend, kein Recovery: IMMER tabu, auch mit Freigabe (Bricking-Schutz).
SYSTEM_PREFIXES: tuple[str, ...] = _both_forms(
    "/etc",
    "/boot",
    "/root",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
)

def _config_files() -> tuple[str, ...]:
    """Die eigene Konfigurationsdatei — an beiden Orten, an denen sie liegen kann.

    ⚠️ Der Kernel liest den Pfad SELBST aus der Umgebung, statt ihn sich von `config.py`
    geben zu lassen. Ein Floor, der von dem Modul abhaengt, welches die geschuetzte Datei
    laedt, schuetzt sie erst, nachdem sie gelesen wurde — und haengt dann an genau den
    Werten, die er schuetzen soll.
    """
    orte = [str(INSTALL_DIR / "talos.env"), os.environ.get("TALOS_SECRETS_ENV", "")]
    return tuple(str(Path(o).expanduser()) for o in orte if o.strip())


# Tier A — Secrets/Credentials: Schreiben fragt den Betreiber (NEEDS_HUMAN), Lesen bleibt gesperrt (Leak-Schutz).
SECRET_PREFIXES: tuple[str, ...] = _both_forms(
    # Die eigene Konfiguration, und zwar aus ZWEI Gruenden zugleich:
    #   * Sie traegt Bot-Token und API-Schluessel — der Installer legt sie mit `chmod 600`
    #     genau dorthin und sagt dem Nutzer, er solle diese Datei ausfuellen. Lesen waere
    #     ein Leak, und zwar bei jeder oeffentlichen Installation.
    #   * Sie traegt `TALOS_ALLOWED_PRINCIPALS` — die Liste derer, die dem Agenten
    #     ueberhaupt etwas sagen duerfen. Wer sie schreibt, traegt sich selbst ein.
    # Ohne diesen Eintrag war beides ein gewoehnliches `write_file`/`read_file` mit
    # ALLOW: eine Rechteerweiterung ueber den Umweg „Datei", also genau die Klasse, die
    # Tier C fuer `~/.bashrc` und den eigenen Quelltext laengst schliesst. Der Kernel
    # blieb dabei intakt — nur seine Identitaetsliste kam aus einer Datei, die der
    # Agent selbst beschreiben durfte.
    *_config_files(),
    str(HOME / ".secrets"),
    str(HOME / ".claude" / "oauth-token"),
    str(HOME / ".claude" / ".credentials.json"),
    str(HOME / ".claude" / "channels"),
    str(HOME / ".ssh"),
    # ⚠️ Zugangsdaten IM Notizspeicher stehen nicht hier, sondern in `_vault_secret()`:
    # sie werden am ORDNERNAMEN erkannt, auf jeder Ebene. Hier stand vorher genau ein
    # ausgeschriebener Pfad — der schuetzte eine einzige Ablage und keine zweite.
    #
    # Die Zugangsdaten der ueblichen Werkzeugketten. Sie fehlten — und das war keine
    # theoretische Luecke: die Sandbox haengt die Wurzel LESBAR ein und maskiert genau
    # diese Liste, `read_file` gatet gegen genau diese Liste. Alles hier Fehlende war
    # also im Sandkasten lesbar und landete ueber das Werkzeugergebnis im Modellkontext.
    # Besonders schief: `~/.netrc` ist dem Projekt als Credential-Datei bekannt (deshalb
    # `trust_env=False` in `web.py`), stand aber nicht drin.
    str(HOME / ".netrc"),
    str(HOME / ".aws"),
    str(HOME / ".gnupg"),
    str(HOME / ".docker" / "config.json"),
    str(HOME / ".kube"),
    str(HOME / ".git-credentials"),
    str(HOME / ".config" / "gcloud"),
    str(HOME / ".config" / "gh"),
    str(HOME / ".npmrc"),
    str(HOME / ".pypirc"),
)
# Tier C — Persistenz/Autoritaet (Kritik M3): Inhalte, die SPAETER ausgefuehrt oder
# von privilegierten Jobs konsumiert werden. Lesen ist harmlos, Schreiben holt des Betreibers
# Freigabe — sonst schreibt sich der Agent ueber den Umweg "Datei" genau die Rechte,
# die der Floor ihm direkt verwehrt. Talos' eigener Code steht bewusst mit drin.
PERSISTENCE_PREFIXES: tuple[str, ...] = _both_forms(
    str(HOME / ".bashrc"),
    str(HOME / ".bash_profile"),
    str(HOME / ".bash_logout"),
    str(HOME / ".profile"),
    str(HOME / ".zshrc"),
    str(HOME / ".config" / "systemd"),
    str(HOME / ".config" / "autostart"),
    str(HOME / ".local" / "bin"),
    str(HOME / ".claude"),
    str(PACKAGE_DIR),
    str(SOUL_FILE),
    str(AGENTS_FILE),
    str(USER_FILE),
    "/var/spool/cron",
)


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_HUMAN = "needs_human"


# Ein Standbild landet IMMER hier, nie an einer vom Modell gewaehlten Stelle — deshalb
# steht die Ableitung im Kernel und nicht im Werkzeug. Waere der Ausgabepfad ein
# Argument, gaebe es einen zweiten Weg, Bytes AUS EINER FREMDEN DATEI an eine gewaehlte
# Stelle zu schreiben; ein Videocontainer laesst sich fuellen, und das Ergebnis stuende
# dann dort, wo der Angreifer es haben will. So gibt es diesen Weg nicht: `frames.grab`
# ruft genau diese Funktion, der Kernel urteilt ueber genau diesen Pfad.
FRAME_INBOX = WORKSPACE_DIR / "inbox"
_FRAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def frame_seconds(at: object) -> float | None:
    """Der gewuenschte Zeitpunkt in Sekunden — oder None fuer „nimm die Mitte".

    Steht hier und nicht im Werkzeug, weil der DATEINAME davon abhaengt: Kernel und
    Runner muessen denselben Wert lesen, sonst urteilt der Kernel ueber eine Datei,
    die so nie entsteht.
    """
    text = str(at if at is not None else "").strip()
    if not text:
        return None
    try:
        wert = float(text)
    except ValueError:
        return None
    if wert < 0 or wert in (float("inf"), float("-inf")) or wert != wert:
        return None
    return wert


def frame_output_path(video: object, at: object = "") -> str:
    """Wohin das Standbild geschrieben wird — allein aus den Argumenten abgeleitet.

    Der Name traegt den ANGEFORDERTEN Zeitpunkt, nie den berechneten: die Mitte haengt
    an der Dauer des Videos, und die kennt der Kernel nicht, ohne die Datei anzufassen.
    """
    stamm = _FRAME_SAFE.sub("-", Path(str(video)).stem)[:60].strip("-") or "video"
    sekunden = frame_seconds(at)
    marke = f"-{sekunden:g}s" if sekunden is not None else ""
    return str(FRAME_INBOX / f"frame-{stamm}{marke}.jpg")


_CLAUDE_JOB_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def claude_work_root() -> str:
    """Wurzel, UNTERHALB derer jeder Claude-Job-Workspace liegt.

    Wird hier aus der Umgebung gelesen, nicht aus config.py — ein Floor, der
    config fragte, schuetzte das Verzeichnis erst, NACHDEM es gelesen wurde
    (die _config_files-Regel).
    """
    configured = os.environ.get("TALOS_CLAUDE_WORKER_ROOT", "")
    return configured or str(WORKSPACE_DIR / "claude-jobs")


def claude_job_workspace(job_id: str) -> str:
    """Das Verzeichnis, in dem ein delegierter Claude-Job schreiben darf.

    Vom Kernel abgeleitet, nie aus den Argumenten — das Modell kann nicht
    waehlen, wo die Bytes eines fremden Agenten landen (das
    frame_output_path-Muster).
    """
    safe = _CLAUDE_JOB_SAFE.sub("-", job_id)[:64]
    return str(Path(claude_work_root()) / f"job-{safe}")


_SKILL_WRITE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SKILL_WRITE_NAME_MAX = 40


def skill_write_root() -> str:
    """Wurzel, UNTERHALB derer ein neu geschriebener Skill landet.

    Wird hier aus der Umgebung gelesen, nicht aus config.py — ein Floor, der
    config fragte, schuetzte das Verzeichnis erst, NACHDEM es gelesen wurde
    (die _config_files-Regel). Die Aufloesung spiegelt `load_config`:
    `TALOS_SKILLS_DIRS` schlaegt die ausgelieferte Liste. Geschrieben wird in
    die Wurzel, die `~/.talos/skills` IST — nur wenn der Betreiber die Liste
    ganz ersetzt hat, in deren erste. So landet ein geschriebener Skill
    garantiert dort, wo `discover_skills` ihn beim naechsten Zug auch sucht.
    """
    raw = os.environ.get("TALOS_SKILLS_DIRS", "").strip()
    if raw:
        roots = [part for part in raw.split(os.pathsep) if part.strip()]
    else:
        roots = [
            str(INSTALL_DIR / "skills"),
            str(HOME / ".talos" / "skills"),
            str(HOME / ".claude" / "skills"),
        ]
    talos_root = os.path.realpath(_expand(str(HOME / ".talos" / "skills")))
    for root in roots:
        if os.path.realpath(_expand(root)) == talos_root:
            return _expand(root)
    return _expand(roots[0]) if roots else str(HOME / ".talos" / "skills")


REMOTE_HOSTS_ENV = "TALOS_REMOTE_HOSTS"


def remote_hosts(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Die ssh-Aliase, die `remote_exec` erreichen darf — Betreiberkonfiguration.

    Gelesen wird aus der Umgebung (Kernel und Runner rufen dieselbe Ableitung,
    das skill_write_root-Muster), nie aus Modellargumenten: die Allowlist ist der
    einzige Ort, der sagt, welche fernen Maschinen existieren. Ein leerer Wert
    heisst „das Werkzeug hat keine Gegenstelle" — die `requires_env`-Regel des
    Kernels macht daraus DENY, bevor hier ueberhaupt geurteilt wird.
    """
    raw = (environ if environ is not None else os.environ).get(REMOTE_HOSTS_ENV, "")
    hosts = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(dict.fromkeys(hosts))


def skill_write_path(name: object) -> str:
    """Wohin ein neuer Skill geschrieben wird — allein aus dem Namen abgeleitet.

    Das frame_output_path-Muster: der Runner ruft genau diese Funktion, der
    Kernel urteilt ueber genau diesen Pfad — das Modell waehlt ihn nie. Ein
    ungueltiger Name liefert die Wurzel selbst: der Runner lehnt ihn ohnehin
    ab, und das Urteil faellt ueber ein Verzeichnis statt ueber einen
    erfundenen Pfad.
    """
    text = str(name or "")
    if len(text) > SKILL_WRITE_NAME_MAX or not _SKILL_WRITE_NAME.fullmatch(text):
        return skill_write_root()
    return str(Path(skill_write_root()) / text / "SKILL.md")


TARGET_EXTRACTORS = {
    "read_file": lambda args: (str(args.get("path", "")),) if "path" in args else (),
    # Sehen hat ein echtes Ziel — den Bildpfad. Das ist der Grund, warum Talos die Datei
    # selbst laedt statt sie den Reasoner holen zu lassen: nur so gibt es ueberhaupt
    # etwas, das der Kernel pruefen kann.
    "see_image": lambda args: (str(args.get("path", "")),) if "path" in args else (),
    "write_file": lambda args: (str(args.get("path", "")),) if "path" in args else (),
    # Sprechen schreibt eine Datei. Das Ziel ist der Ausgabepfad — damit gilt derselbe
    # Persistenz-Floor: eine Stimme nach `~/.config/systemd/` holt die Freigabe.
    "speak": lambda args: (str(args.get("path", "")),) if "path" in args else (),
    # Hoeren liest eine Datei. Dasselbe Ziel-Muster wie `see_image` — es entsteht nichts.
    "hear": lambda args: (str(args.get("path", "")),) if "path" in args else (),
    # Standbild aus einem Video: ZWEI Ziele, und beide muessen es sein.
    #   * Die QUELLE, weil ein Video eine Datei ist wie jede andere. Ohne sie waere
    #     Frame Capture der bequemste Weg am Secret-Floor vorbei: ein Video unter
    #     `~/.secrets/` liesse sich in ein Bild verwandeln, das `see_image` danach
    #     ungehindert liest — die Sperre haette gehalten und trotzdem nichts genuetzt.
    #   * Das ERGEBNIS, weil dort eine Datei entsteht. Der Pfad steht nicht in den
    #     Argumenten, sondern wird abgeleitet (`frame_output_path`), damit das Modell
    #     ihn nicht waehlen kann; gegatet und gesichert wird er trotzdem wie jedes
    #     andere Schreibziel.
    "grab_frame": lambda args: (
        (str(args.get("path", "")), frame_output_path(args.get("path", ""), args.get("at", "")))
        if str(args.get("path", ""))
        else ()
    ),
    "run_shell": lambda args: (),
    # Fernausfuehrung: kein Dateisystem-Ziel — die Wirkung entsteht auf einer
    # anderen Maschine, und der Host ist kein Pfad, sondern ein ssh-Alias aus der
    # Betreiber-Allowlist (remote_hosts). Die eigentliche Einordnung faellt in
    # `_decide_remote`: Hardline-Floor auch fern, danach ausnahmslos NEEDS_HUMAN,
    # weil keine lokale Sandbox ueber Maschinengrenzen reicht.
    "remote_exec": lambda args: (),
    "send_mail": lambda args: (),
    # Rückfrage an den Betreiber: kein Ziel, weil sie nichts anfasst — nur Text und
    # eine Auswahl. Der Eintrag muss trotzdem hier stehen: ein Werkzeug ohne Extractor
    # ist per Bauart DENY (siehe `decide`, Schritt 0.5), und ein DENY wäre hier absurd —
    # der Agent dürfte nicht einmal fragen, was er tun soll.
    "ask_operator": lambda args: (),
    "vault_search": lambda args: (),
    # The model supplies only an entity name. The runner resolves that name against
    # the operator-owned registry and chooses the fixed URL/unit; there is therefore
    # no model-controlled filesystem target to derive here.
    "entity_status": lambda args: (),
    # Gespraechsarchiv: kein Ziel, wie bei `vault_search`. Die eigentliche Grenze — nur
    # die EIGENE Konversation ist durchsuchbar — liegt im Runner, der die Konversation
    # aus dem Thread-Kontext nimmt (`ask_operator`-Bauart), nie aus den Argumenten.
    "session_search": lambda args: (),
    # Delegieren: kein Ziel, wie `ask_operator`. Der Aufruf fasst nichts an — er startet
    # einen zweiten Lauf, dessen JEDER Werkzeugwunsch erneut einzeln hier vorbeikommt,
    # dann zusaetzlich unter der Nur-Lesen-Decke. Der Eintrag muss trotzdem stehen: ein
    # Werkzeug ohne Extractor ist per Bauart DENY.
    "delegate": lambda args: (),
    # Operator-konfigurierte Agentenberatung. Endpoint und Credential liegen im Runner,
    # das Modell liefert nur begrenzten Fragetext; die Antwort erteilt keine Capability.
    "agent_consult": lambda args: (),
    # Delegieren an den Claude-Worker: das Ziel ist die kernel-abgeleitete
    # Wurzel, unter der jeder Job-Workspace liegt — nie ein Modellpfad. Der
    # Floor greift also, bevor ein einziger Byte des fremden Agenten faellt.
    "delegate_code": lambda args: (claude_work_root(),),
    # DAG-Delegation: dasselbe Ziel wie `delegate_code` — jeder Knoten wird ein
    # eigener Job in einem kernel-abgeleiteten Workspace unter dieser Wurzel.
    # Der Floor greift also, bevor ein einziger Frame den Prozess verlaesst.
    "delegate_dag": lambda args: (claude_work_root(),),
    # Status lesen fasst nichts an — aber der Eintrag muss stehen: ein
    # Werkzeug ohne Extractor ist per Bauart DENY (siehe `decide`, Schritt 0.5).
    "delegate_status": lambda args: (),
    # Zurückrollen wirkt auf die ORIGINALPFADE — genau die sind das Ziel und werden
    # gegatet. Ein Undo auf ~/.bashrc fragt den Betreiber also wie ein Schreiben dorthin.
    "undo_last": lambda args: tuple(
        str(entry[0]) for entry in (args.get("entries") or ()) if entry
    ),
    # Ein neuer Skill: das Ziel ist der kernel-abgeleitete Pfad der SKILL.md —
    # das Modell nennt einen Namen, nie einen Pfad (skill_write_path). Es ist die
    # haerteste Persistenz im Haus: der Text steht ab dem naechsten Zug in JEDEM
    # Prompt. Das Manifest deklariert das Werkzeug darum irreversible — der
    # Kernel antwortet ausnahmslos NEEDS_HUMAN, konfigurierbar ist das nicht.
    "skill_write": lambda args: (skill_write_path(args.get("name", "")),),
    # Web: bewusst KEIN Ziel, wie bei `run_shell` und `vault_search`. Der Kernel schickt
    # jedes Ziel durch `os.path.realpath`; aus `https://example.com/x` wuerde dabei ein
    # Pfad `<cwd>/https:/example.com/x`, und der Snapshotter legte einen Undo-Eintrag auf
    # dieses Phantom. Ein Scheinziel in einem Dateisystem-Floor ist schlechter als keins.
    # Die echte Pruefung ist `web.guard_url` und laeuft im Runner — sie sperrt Loopback,
    # RFC 1918, Link-Local samt Metadaten-Adresse und CGNAT (also Tailscale), prueft die
    # AUFGELOESTEN Adressen statt nur den Namen und schickt jede Weiterleitung erneut
    # durch dieselbe Pruefung, statt ihr zu folgen.
    "web_fetch": lambda args: (),
    # Der rendernde Browser: kein Ziel, aus demselben Grund wie `web_fetch` — eine URL
    # ist kein Pfad, und ein Scheinziel in einem Dateisystem-Floor ist schlechter als
    # keins. Die echte Grenze ist `web.guard_url` PLUS der Aufloesungs-Kaefig in
    # `browser.resolver_rules`: der Browser erreicht genau den geprueften Host.
    "browse": lambda args: (),
    "web_search": lambda args: (),
}



@dataclass(frozen=True)
class ToolRequest:
    tool: str
    identity: Principal
    args: dict
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str


# Rangfolge der Urteile. Alles, was ueber dem Kernel liegt (Autonomie-Regler,
# Kanal-Decke), darf ausschliesslich nach oben verschieben — nie nach unten.
SEVERITY: dict[Verdict, int] = {Verdict.ALLOW: 0, Verdict.NEEDS_HUMAN: 1, Verdict.DENY: 2}


def stricter(decision: Decision, ceiling: Decision) -> Decision:
    """Von zwei Urteilen gilt das strengere; bei Gleichstand bleibt der Grund des ersten.

    Die eine Stelle, an der Verschaerfung stattfindet. Jede Decke im System geht
    hierdurch — damit ist strukturell ausgeschlossen, dass eine davon versehentlich
    abschwaecht: `stricter` kann per Konstruktion nichts zurueckgeben, das milder ist
    als `decision`.
    """
    return ceiling if SEVERITY[ceiling.verdict] > SEVERITY[decision.verdict] else decision


def _hits(target: str, prefixes: tuple[str, ...]) -> bool:
    """Realpath-basiert: wehrt Symlink-/`..`-Umgehungen ab.

    Expandiert VOR dem Vergleich (`_expand`) — sonst prüft der Kernel eine andere
    Zeichenkette als der Executor später anfasst: `~/.secrets/x` traf keinen einzigen
    Präfix und kam als „allow" durch, während `guard_targets` längst expandiert hat.
    Ein Floor, der nur die ausgeschriebene Schreibweise kennt, ist kein Floor.
    """
    lexical = os.path.abspath(os.path.normpath(_expand(target)))
    real = os.path.realpath(lexical)
    for prefix in prefixes:
        prefix_lexical = os.path.abspath(os.path.normpath(prefix))
        # Prefixe werden auch hier erneut aufgeloest. Ein beim Prozessstart noch
        # fehlender Operatorpfad kann spaeter ein Symlink werden oder umgebogen
        # werden; sein Schutz darf dadurch nicht aus dem Kernel verschwinden.
        prefix_real = os.path.realpath(prefix_lexical)
        for candidate in (lexical, real):
            if candidate == prefix_lexical or candidate.startswith(prefix_lexical + os.sep):
                return True
            if candidate == prefix_real or candidate.startswith(prefix_real + os.sep):
                return True
    return False


# Ordnernamen, die im Notizspeicher Zugangsdaten bedeuten. Ein Notizspeicher ist als
# Ganzes lesbar — er ist ja der Zweck des Werkzeugs — aber genau diese Faecher nicht.
_VAULT_SECRET_DIRS = frozenset({"credentials", "secrets"})


def _vault_dir() -> str:
    """Der Notizspeicher, aus der Umgebung gelesen — nicht aus `config.py`.

    Dieselbe Regel wie bei `_config_files()`: ein Floor, der `config.py` fragte,
    schuetzte erst, nachdem die Konfiguration gelesen war.
    """
    roh = os.environ.get("TALOS_VAULT_DIR") or str(DEFAULT_VAULT_DIR)
    return os.path.realpath(_expand(roh))


def _vault_secret(target: str) -> bool:
    """Liegt das Ziel in einem Zugangsdaten-Fach INNERHALB des Notizspeichers?

    Der NAME entscheidet, nicht die Stelle. Vorher stand im Floor ein einziger
    ausgeschriebener Pfad: wer seine Zugangsdaten einen Ordner daneben legte, hatte
    keinen Schutz — und der Pfad verriet nebenbei die Ablagestruktur seines Autors.
    """
    wurzel = _vault_dir()
    real = os.path.realpath(_expand(target))
    if real != wurzel and not real.startswith(wurzel + os.sep):
        return False
    rest = real[len(wurzel):].strip(os.sep)
    return any(teil.casefold() in _VAULT_SECRET_DIRS for teil in rest.split(os.sep) if teil)


def _is_secret(target: str) -> bool:
    """Der vollstaendige Secret-Test: ausgeschriebene Praefixe ODER ein Fach im Speicher."""
    return _hits(target, SECRET_PREFIXES) or _vault_secret(target)

# --- Shell-Pfad-Floor (Kritik M3) ------------------------------------------------
# run_shell nimmt einen Command-String, kein Pfad-Argument: kein Extractor kann
# hier zuverlaessig Ziele liefern, und Lesen laesst sich im String nicht vom
# Schreiben trennen. Deshalb sind Referenzen auf diese Praefixe im Kommando hart
# tabu. /usr,/bin,/sbin,/lib bleiben bewusst draussen — die stehen in harmlosen
# Kommandos staendig. Wer wirklich in ein Secret schreiben soll, nimmt write_file:
# dort greift des Betreibers Regel (NEEDS_HUMAN) mit sauberem Ziel und Snapshot.
SHELL_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    _both_forms("/etc", "/boot", "/root") + SECRET_PREFIXES
)

# Pfad-artige Tokens im Kommando. Grob nach oben abgesichert, nie nach unten:
# was hier durchrutscht, faengt weiterhin der command_floor.
_PATH_TOKEN = re.compile(r"""(?<![A-Za-z0-9_.\-/])((?:~|\$\{?HOME\}?|\.{0,2}/)[^\s'";|&<>()]*)""")

# Ein Pfad in Anfuehrungszeichen darf Leerzeichen enthalten — `_PATH_TOKEN` bricht dort ab
# und liefert nur den Anfang. Meist reicht das, weil die geschuetzten Praefixe selbst keine
# Leerzeichen haben und der Torso sie trotzdem trifft. Es reicht genau dann NICHT, wenn der
# Praefix selbst einen enthaelt: liegt die Installation in einem Verzeichnis mit Leerzeichen,
# war der eigene Quellbaum ueber die Shell nicht geschuetzt. Additiv gelesen — das hier kann
# nur zusaetzliche Pfade finden, nie welche verstecken.
_QUOTED_TOKEN = re.compile(r"""'([^']*)'|"([^"]*)\"""")
_PATH_START = ("~", "/", "./", "../", "$HOME", "${HOME}")


def _expand(raw: str) -> str:
    return os.path.expanduser(os.path.expandvars(str(raw).strip().strip("'\"")))


def command_paths(command: str) -> tuple[str, ...]:
    """Pfad-artige Tokens aus einem Shell-Command, dedupliziert und expandiert."""
    text = str(command)
    found = list(_PATH_TOKEN.findall(text))
    for single, double in _QUOTED_TOKEN.findall(text):
        token = single or double
        if token.startswith(_PATH_START):
            found.append(token)
    return tuple(dict.fromkeys(_expand(t) for t in found))


def _derived_targets(req: "ToolRequest", vault_dir: Path) -> tuple[str, ...]:
    """Derive raw targets, using the shared vault canonicalizer where required."""
    if req.tool in {"vault_search", "vault_get", "vault_write_note"}:
        return canonical_target_from_args(req.tool, req.args, vault_dir)
    extractor = TARGET_EXTRACTORS.get(req.tool)
    if extractor is None:
        return ()
    return tuple(extractor(req.args))


def guard_targets(
    req: "ToolRequest", vault_dir: str | os.PathLike[str] = DEFAULT_VAULT_DIR
) -> tuple[str, ...]:
    """Ziele fuer Bindung und Snapshot — ausschliesslich kernel-abgeleitet.

    Bewusst NICHT req.targets: was der Executor sichert, darf nicht davon abhaengen,
    was das LLM deklariert. Verzeichnisse fliegen raus, die kann der Snapshotter nicht.
    Vault-Ziele werden mit exakt demselben Canonicalizer wie im Runner aufgeloest.
    """
    derived = tuple(
        dict.fromkeys(_expand(t) for t in _derived_targets(req, Path(vault_dir)) if t)
    )
    return tuple(t for t in derived if not os.path.isdir(t))


def command_risk_paths(command: str) -> tuple[tuple[str, str], ...]:
    """Pfade im Kommando mit der Einordnung des Kernels — Grundlage fuer den Freigabe-Text.

    `run_shell` hat keine ableitbaren Ziele (guard_targets == ()), deshalb kennt der
    Freigabe-Text sonst nur den generischen Grund. Hier bekommt der Mensch dieselbe
    Einordnung, die der Kernel bei einem Datei-Tool anlegen wuerde.
    """
    marks: list[tuple[str, str]] = []
    for path in command_paths(command):
        if _hits(path, SYSTEM_PREFIXES):
            label = "System"
        elif _is_secret(path):
            label = "Secret"
        elif _hits(path, PERSISTENCE_PREFIXES):
            label = "Persistenz — wirkt nach dem Lauf weiter"
        else:
            label = ""
        marks.append((path, label))
    return tuple(marks)


@dataclass(frozen=True)
class PolicyKernel:
    """Deterministischer Türsteher. Alle Felder unveränderlich."""

    manifest: ToolManifest
    allowed_identities: frozenset[Principal]
    # Konservativer Kernel-Default: ohne die Config-Schicht (config.py setzt den
    # ausgelieferten Default SHELL_NEEDS_HUMAN=0, seit run_shell sandboxed laeuft)
    # bleibt jedes Kommando freigabepflichtig — fail-closed, und genau die
    # Konfiguration, gegen die die Angriffs-Suite laeuft.
    shell_needs_human: bool = True
    # Must match the root supplied to make_vault_runners in production.
    vault_dir: Path = DEFAULT_VAULT_DIR

    def guard_targets(self, req: ToolRequest) -> tuple[str, ...]:
        """Canonical targets for policy, grants, snapshots, approval and audit."""
        return guard_targets(req, self.vault_dir)

    def decide(self, req: ToolRequest) -> Decision:
        # 0. Tool muss deklariert sein (fail-closed bei Unbekanntem).
        spec = self.manifest.get(req.tool)
        if spec is None:
            return Decision(Verdict.DENY, f"unknown tool: {req.tool}")

        # 0.25 Deklarierte Laufzeit-Voraussetzung: ein Werkzeug, dessen
        # requires_env nicht gesetzt ist, hat das, worauf es wirkt, gar nicht
        # (kein Worker-Socket, kein Dienst). Ein Grant darauf verspraeche eine
        # Wirkung auf einen Konfigurationsstand, den der Betreiber nie gesetzt
        # hat — fail-closed, und genannt wird die fehlende Variable, nie ein Wert.
        fehlend = sorted(name for name in spec.requires_env if not os.environ.get(name))
        if fehlend:
            return Decision(Verdict.DENY, f"required env not set: {', '.join(fehlend)}")

        # 0.5 Target-Extraktion: Wir glauben nicht dem LLM (req.targets), wir leiten ab.
        if req.tool not in TARGET_EXTRACTORS and req.tool not in {"vault_get", "vault_write_note"}:
            return Decision(Verdict.DENY, f"unknown tool without target extractor: {req.tool}")

        try:
            derived_targets = _derived_targets(req, self.vault_dir)
        except VaultPathError as error:
            return Decision(Verdict.DENY, f"vault path refused: {error}")
        all_targets = tuple(set(req.targets + derived_targets))

        # 1. System-Floor zuerst — systemzerstörend, kein Recovery, unbypassbar (auch mit Freigabe).
        for target in all_targets:
            if _hits(target, SYSTEM_PREFIXES):
                return Decision(Verdict.DENY, f"system path (hardline): {target}")

        # 3. Identität.
        if req.identity not in self.allowed_identities:
            return Decision(Verdict.DENY, f"identity not allowed: {req.identity}")

        # 4. Secret/Credential-Pfade: Lesen gesperrt (Leak), Schreiben/exec fragt den Betreiber.
        for target in all_targets:
            if _is_secret(target):
                if spec.effect is Effect.READ:
                    return Decision(Verdict.DENY, f"reading secrets refused: {target}")
                return Decision(Verdict.NEEDS_HUMAN, f"writing a secret — needs your approval: {target}")

        # 4b. Tier C — wird spaeter ausgefuehrt. Lesen egal, Schreiben fragt den Betreiber.
        if spec.effect is not Effect.READ:
            for target in all_targets:
                if _hits(target, PERSISTENCE_PREFIXES):
                    return Decision(
                        Verdict.NEEDS_HUMAN, f"will be executed later — needs your approval: {target}"
                    )

        # 5. read ist frei.
        if spec.effect is Effect.READ:
            return Decision(Verdict.ALLOW, "read")

        # 6. exec: Command-Floor nach Hermes-Muster (hardline vor allem).
        if spec.effect is Effect.EXEC:
            return self._decide_exec(req)

        # 7. write: Irreversibles → Mensch. Die Erlaubnis selbst ist kein Listeneintrag
        # mehr, sondern ein Capability-Token auf genau diese Anfrage (capability.py).
        if not spec.reversible:
            return Decision(Verdict.NEEDS_HUMAN, f"irreversible: {req.tool}")
        return Decision(Verdict.ALLOW, "write, reversible")

    def _decide_exec(self, req: ToolRequest) -> Decision:
        # Fernausfuehrung zuerst: fuer remote_exec gelten andere Grenzen als fuer
        # die lokale Shell — kein Pfad-Floor (die Pfade meinen die ferne Maschine),
        # kein SHELL_NEEDS_HUMAN=0-Komfort (die Sandbox reicht nicht ueber
        # Maschinengrenzen), Hardline trotzdem (Systemzerstoerung ist ortlos).
        if req.tool == "remote_exec":
            return self._decide_remote(req)
        command = req.args.get("command")

        # Shell-artiges Tool: der Command-Floor entscheidet (hardline vor Allowlist).
        if command is not None:
            # Pfad-Floor zuerst: der Command-Floor kennt nur destruktive Muster,
            # keine Pfade — `cat ~/.secrets/x` waere sonst ein stiller Leak.
            for target in command_paths(command):
                if _hits(target, SHELL_FORBIDDEN_PREFIXES) or _vault_secret(target):
                    return Decision(Verdict.DENY, f"protected path in command: {target}")
            is_hard, hard_desc = command_floor.detect_hardline(str(command))
            if is_hard:
                return Decision(Verdict.DENY, f"hardline: {hard_desc}")
            is_danger, danger_desc = command_floor.detect_dangerous(str(command))
            if is_danger:
                return Decision(Verdict.NEEDS_HUMAN, f"risky: {danger_desc}")
            # Pfad-Floor und command_floor sind Backstops, keine Grenze: ein Shell-String
            # laesst sich beliebig rekonstruieren (P=/etc; cat $P/passwd, eval, base64).
            # Bis run_shell isoliert laeuft, entscheidet the operator — nicht der Regex.
            if self.shell_needs_human:
                return Decision(Verdict.NEEDS_HUMAN, "shell without sandbox — needs your approval")
            return Decision(Verdict.ALLOW, "exec allowed")

        # Nicht-Shell-exec (z.B. Mail senden): Irreversibles braucht menschliche Freigabe.
        spec = self.manifest.get(req.tool)
        if spec is not None and not spec.reversible:
            return Decision(Verdict.NEEDS_HUMAN, f"irreversible: {req.tool}")
        return Decision(Verdict.ALLOW, "exec allowed")

    def _decide_remote(self, req: ToolRequest) -> Decision:
        """remote_exec: die Wirkung entsteht auf einer ANDEREN Maschine.

        Drei Unterschiede zur lokalen Shell, alle bewusst:

        * **Host gegen die Betreiber-Allowlist** (`remote_hosts`), nicht gegen
          Muster: ein nicht gelisteter Host ist DENY, keine Freigabefrage — der
          Mensch soll nie ueber eine Gegenstelle abstimmen, die er nie
          konfiguriert hat.
        * **Hardline bleibt Hardline**: `rm -rf /` ist auch fern systemzerstörend
          und unbypassbar. Der lokale PFAD-Floor gilt dagegen nicht — `/etc/hosts`
          im Fernkommando meint die ferne Maschine; lokale Secret-Pfade dort
          hineinzulesen machte echte Fernwartung unmoeglich (Fehlalarm in der
          sicheren Richtung waere hier trotzdem der falsche Trade-off, weil die
          ehrliche Grenze die Freigabe mit vollem Kommandotext ist).
        * **Ausnahmslos NEEDS_HUMAN.** Die Sandbox sperrt nur den lokalen
          ssh-Clienten ein; was das Kommando fern anrichtet, begrenzt sie nicht.
          Der `SHELL_NEEDS_HUMAN=0`-Komfort der lokalen Sandbox gilt darum hier
          nicht. Erleichterung gibt es nur als stehende Regel auf exakt
          (host, command) — `standing.action_key` bindet beides.
        """
        host = req.args.get("host")
        command = req.args.get("command")
        if not isinstance(host, str) or not host.strip():
            return Decision(Verdict.DENY, "remote_exec without a host")
        hosts = remote_hosts()
        if host.strip() not in hosts:
            return Decision(
                Verdict.DENY,
                f"remote host not in the operator's allowlist: {host.strip()}",
            )
        if not isinstance(command, str) or not command.strip():
            return Decision(Verdict.DENY, "remote_exec without a command")
        is_hard, hard_desc = command_floor.detect_hardline(command)
        if is_hard:
            return Decision(Verdict.DENY, f"hardline: {hard_desc}")
        return Decision(
            Verdict.NEEDS_HUMAN,
            f"remote effect on '{host.strip()}' — beyond the local sandbox, "
            "needs your approval",
        )
