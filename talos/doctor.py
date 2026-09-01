"""Diagnose — was fehlt, bevor es jemandem im Betrieb auffaellt.

Der Grund fuer dieses Modul ist eine Beobachtung: Talos haengt inzwischen an vier
Stuecken, die es nicht selbst mitbringt (ffmpeg, faster-whisper, ddgs, eine
piper-Stimme). Fehlt eines, meldet sich das **beim Aufruf** — also mitten in einem
Gespraech, in dem der Betreiber gerade etwas anderes wollte. Ein einziger Befehl, der
das vorher sagt, ist der Unterschied zwischen „geht nicht" und „installier das noch".

Drei Regeln, an denen sich dieses Modul messen lassen muss:

⚠️ **Es aendert NICHTS.** Ein Doktor, der nebenbei repariert, ist keiner: dann weiss
niemand mehr, in welchem Zustand die Maschine vorher war. Es wird gelesen, gezaehlt und
nachgesehen — kein Verzeichnis angelegt, keine Datei geschrieben, kein Dienst gestartet.

⚠️ **Es geht nicht von selbst ins Netz.** Ohne `--online` faellt kein einziger Aufruf
nach draussen. Ein Diagnosebefehl, der ungefragt den Bot-Token an Telegram schickt, ist
eine Ueberraschung — und er waere ausgerechnet dort unbrauchbar, wo man ihn am
dringendsten braucht: auf einer Maschine ohne Netz.

⚠️ **Es zeigt kein Geheimnis.** Weder Wert noch Laenge noch Praefix; nur *ob* etwas
gesetzt ist und woher es kommt. Eine Diagnose, die man nicht in ein Ticket kopieren
darf, wird nicht benutzt.

Der Rueckgabewert trennt Pflicht von Kuer: **1 nur dann, wenn etwas fehlt, ohne das
der Agent nicht laeuft.** Ein fehlendes ffmpeg ist eine Notiz, keine Stoerung — sonst
faellt ein Cron-Waechter wegen einer Faehigkeit um, die niemand benutzt.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .ux import SYM_FAIL, SYM_OK

MIN_PYTHON = (3, 11)
# Rechte, die eine Datei mit Token und Rechteliste haben sollte.
CONFIG_MODE = 0o600

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: SYM_OK, WARN: "·", FAIL: SYM_FAIL}


@dataclass(frozen=True)
class Check:
    """Ein Befund. `critical` entscheidet ueber den Rueckgabewert, nicht die Farbe."""

    area: str
    label: str
    state: str
    detail: str = ""
    critical: bool = False

    @property
    def blocking(self) -> bool:
        return self.state is FAIL and self.critical


def _bin(name: str, *, which=shutil.which) -> str:
    return which(name) or ""


def _module(name: str) -> bool:
    """Ist das Paket importierbar — ohne es zu importieren.

    `find_spec` statt `import`: faster-whisper zieht beim Import ein Modell und
    mehrere hundert Megabyte Bibliothek nach. Eine Diagnose darf nicht teurer sein
    als das, was sie diagnostiziert.
    """
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_python() -> Check:
    passt = sys.version_info[:2] >= MIN_PYTHON
    return Check(
        "runtime", "python", OK if passt else FAIL,
        ".".join(str(t) for t in sys.version_info[:3]),
        critical=True,
    )


def check_config(paths: Iterable[Path], *, uid: int | None = None) -> tuple[Check, ...]:
    """Wo die Konfiguration liegt — und ob ihre Rechte zu ihrem Inhalt passen.

    Geprueft wird, weil in dieser Datei Token, Schluessel UND die Liste der erlaubten
    Kennungen stehen. `chmod 644` daran ist kein Schoenheitsfehler: jeder Nutzer der
    Maschine liest dann den Bot-Token.

    ⚠️ **Der Eigentuemer zaehlt mehr als der Modus.** Eine Datei mit `640`, die einem
    ANDEREN Benutzer gehoert und hier nur lesbar ist, ist STRENGER als `600` unter dem
    eigenen — dann naemlich kann der Agent seine eigene Rechteliste ueberhaupt nicht mehr
    schreiben, auch nicht an jedem Codefehler vorbei. Wer das nicht unterscheidet, meldet
    die haertere Einrichtung als Mangel und erzieht zum Rueckbau.
    """
    eigen = os.getuid() if uid is None else uid
    gefunden: list[Check] = []
    for pfad in paths:
        if not pfad.is_file():
            continue
        zustand = pfad.stat()
        modus = zustand.st_mode & 0o777
        fremd = zustand.st_uid != eigen
        if fremd and not os.access(pfad, os.W_OK):
            gefunden.append(Check(
                "config", str(pfad), OK,
                f"mode {modus:o}, owned by uid {zustand.st_uid} — read-only here, so the "
                f"agent cannot rewrite its own allowlist even if the floor were wrong",
            ))
            continue
        eng = modus <= CONFIG_MODE
        gefunden.append(Check(
            "config", str(pfad), OK if eng else WARN,
            f"mode {modus:o}" + ("" if eng else " — token and allowlist are world-readable"),
        ))
    if not gefunden:
        return (Check("config", "config file", FAIL,
                      "none found — run `talos setup`", critical=True),)
    return tuple(gefunden)


def check_identity(principals: tuple) -> Check:
    """Die Allowlist. Leer heisst nicht „offen", sondern „startet nicht" — trotzdem
    gehoert sie hierher: der Betreiber soll SEHEN, wie viele Kennungen befehlen duerfen,
    ohne die Datei zu oeffnen, die er gerade nicht lesen soll."""
    if not principals:
        return Check("identity", "allowlist", FAIL, "empty — nobody may command it",
                     critical=True)
    kanaele = sorted({p.channel for p in principals})
    return Check("identity", "allowlist", OK,
                 f"{len(principals)} principal(s) on {', '.join(kanaele)}")


def check_model(provider: str, model: str, *, claude_bin: str, hermes_bin: str,
                credentials, which=shutil.which) -> tuple[Check, ...]:
    """Der Denkweg. Ein CLI-Weg ohne CLI ist der haeufigste stille Ausfall: der Agent
    startet, nimmt Nachrichten an und scheitert erst am ersten Gedanken."""
    zeilen = [Check("model", "provider", OK, f"{provider} / {model}" if model else provider)]
    laufzeit = {"claude-cli": claude_bin, "hermes": hermes_bin}.get(provider, "")
    if laufzeit:
        pfad = which(laufzeit) or (laufzeit if Path(laufzeit).is_file() else "")
        zeilen.append(Check(
            "model", f"{laufzeit} CLI", OK if pfad else FAIL,
            pfad or f"not found — {provider} cannot think without it", critical=True,
        ))
    else:
        # Ein Schluesselweg. Welcher Name gilt, sagt der Katalog — nicht ein Blick auf
        # den Slug: `"openai" in provider` hielt `deepseek` und `openrouter` faelschlich
        # fuer Anthropic-Anbieter und fragte nach dem falschen Namen.
        from .credentials import key_var

        name = key_var(provider) or f"a key for {provider}"
        gesetzt = credentials.has(provider)
        zeilen.append(Check(
            "model", name, OK if gesetzt else FAIL,
            "set" if gesetzt else "missing — this provider needs a key of its own, and "
            "no other provider's key stands in for it",
            critical=True,
        ))
    return tuple(zeilen)


def check_model_overrides(overrides, *, known: frozenset[str] | None) -> tuple[Check, ...]:
    """Die Betreiber-Overrides (`TALOS_MODEL_OVERRIDES`): was gilt, was herausfiel.

    Nie kritisch — ein Override, der nicht trifft, kostet einen Preis in `/usage`,
    keinen Start. Aber sichtbar: der Befund aus dem Laden (falsches Feld) und der aus
    dem Abgleich (Modell, das kein Katalog listet) stehen hier, jeder einzeln.
    `known=None` heisst: der Katalog war nicht ladbar, die Namen bleiben ungeprueft —
    und das steht dann dabei, statt als „alles gut" durchzugehen.
    """
    from . import modelinfo

    if not overrides.entries and not overrides.dropped:
        return ()
    bereinigt = modelinfo.reconcile(overrides, known) if known is not None else overrides
    zeilen: list[Check] = []
    if bereinigt.entries:
        namen = ", ".join(sorted(bereinigt.entries))
        nachsatz = "" if known is not None else " (names not checked — catalog not loadable)"
        zeilen.append(Check(
            "model", "overrides", OK,
            f"{len(bereinigt.entries)} model(s) corrected via {modelinfo.ENV_VAR}: {namen}{nachsatz}",
        ))
    zeilen += [Check("model", "override dropped", WARN, grund) for grund in bereinigt.dropped]
    return tuple(zeilen)


def _known_models(overrides) -> frozenset[str] | None:
    """Der Massstab fuer die Override-Namen — nur geholt, wenn es etwas zu pruefen gibt,
    weil er den Anbieter-Katalog laedt. Nicht ladbar heisst `None`, nie „leer"."""
    if not overrides.entries:
        return frozenset()
    from . import models

    try:
        return models.known_model_ids()
    except Exception:
        return None


def check_channels(*, bot_token: str, mail_host: str, mail_user: str,
                   mail_password: str, mail_authserv_id: str = "") -> tuple[Check, ...]:
    """Nur *ob* gesetzt, nie was. Der Token wird hier absichtlich nicht geprueft —
    das kostet einen Netzaufruf und passiert erst mit `--online`."""
    zeilen = [Check(
        "channels", "telegram token", OK if bot_token else FAIL,
        "set" if bot_token else "missing — there is no way in without it", critical=True,
    )]
    teile = (mail_host, mail_user, mail_password)
    if any(teile):
        vollstaendig = all(teile)
        zeilen.append(Check(
            "channels", "mail (imap)", OK if vollstaendig else WARN,
            "configured" if vollstaendig else "incomplete — host, user and password are all required",
        ))
        # ⚠️ Ohne eigene Kennung beweist kein `Authentication-Results` etwas, also
        # verwirft `mail.verify_sender` seit dem Fail-closed-Fix JEDE Mail. Das ist
        # richtig — aber ohne diese Zeile sieht es aus wie ein toter Kanal: eingerichtet,
        # erreichbar, und trotzdem kommt nie etwas an. Genau die Sorte Stille, die
        # jemanden dazu bringt, die Pruefung wieder auszubauen.
        if not str(mail_authserv_id).strip():
            zeilen.append(Check(
                "channels", "TALOS_MAIL_AUTHSERV_ID", FAIL,
                "missing — every mail is discarded: without the name your own receiving "
                "server stamps, no Authentication-Results header proves a sender",
            ))
    else:
        # ⚠️ Der Weg gehoert in den Befund, nicht in den Kopf des Lesers. Seit `remedy.py`
        # geht dieser Text auch an das MODELL: ein Befund ohne Abhilfe wird dort zu „ist
        # nicht eingerichtet" — richtig und unbrauchbar. Mit Abhilfe wird er zu einem Satz,
        # nach dem jemand etwas tun kann.
        zeilen.append(Check("channels", "mail (imap)", WARN,
                            "not configured — optional; `talos setup mail`"))
    return tuple(zeilen)


def check_capabilities(*, voice_dir: Path, piper_bin: str, which=shutil.which) -> tuple[Check, ...]:
    """Die vier Stuecke, die Talos nicht mitbringt. Keines ist kritisch: fehlt eines,
    fehlt genau ein Werkzeug — nicht der Agent."""
    ffmpeg, ffprobe = _bin("ffmpeg", which=which), _bin("ffprobe", which=which)
    stimme = ""
    if voice_dir.is_dir():
        modelle = sorted(voice_dir.glob("*.onnx"))
        stimme = str(modelle[0].name) if modelle else ""
    piper = _bin(piper_bin, which=which) or (piper_bin if Path(piper_bin).is_file() else "")
    return (
        Check("capabilities", "grab_frame (ffmpeg)", OK if ffmpeg and ffprobe else WARN,
              f"{ffmpeg or 'ffmpeg missing — install it via the system package manager'}"
              + ("" if ffprobe else " — ffprobe missing, duration falls back to 1s")),
        Check("capabilities", "hear (faster-whisper)", OK if _module("faster_whisper") else WARN,
              "installed" if _module("faster_whisper")
              else "missing — `pip install faster-whisper`, runs locally"),
        Check("capabilities", "web_search (ddgs)", OK if _module("ddgs") else WARN,
              "installed" if _module("ddgs") else "missing — `pip install ddgs`, no key needed"),
        Check("capabilities", "speak (piper)", OK if piper and stimme else WARN,
              f"{piper or 'piper missing — `pip install piper-tts`'}"
              + (f", voice {stimme}" if stimme
                 else f" — no .onnx voice in {voice_dir}; download one from the piper voices")),
    )


def check_sandbox(*, backends=None, allow_unconfined: bool = False) -> Check:
    """Ohne Isolation verweigert die Shell — und das ist die richtige Vorgabe. Hier
    steht es trotzdem, weil ein `run_shell`, das immer „refused" sagt, sonst wie ein
    Fehler aussieht statt wie eine Entscheidung."""
    from . import sandbox as sb

    kandidaten = sb.default_backends() if backends is None else backends
    gewaehlt = sb.select_backend(kandidaten)
    if gewaehlt is not None:
        return Check("sandbox", "shell isolation", OK, gewaehlt.name)
    if allow_unconfined:
        return Check("sandbox", "shell isolation", WARN,
                     "none — but TALOS_SANDBOX_ALLOW_UNCONFINED=1 lets the shell run anyway")
    return Check("sandbox", "shell isolation", WARN,
                 "none available — run_shell refuses instead of running unprotected")


def check_state(*, data_dir: Path, workspace: Path) -> tuple[Check, ...]:
    """Schreibbarkeit wird GEPRUEFT, nicht hergestellt: ein Doktor, der das fehlende
    Verzeichnis anlegt, verwandelt einen Befund in eine stille Aenderung."""
    zeilen = []
    for name, pfad, kritisch in (("event log", data_dir, True), ("workspace", workspace, False)):
        if pfad.is_dir():
            beschreibbar = os.access(pfad, os.W_OK)
            zeilen.append(Check("state", name, OK if beschreibbar else FAIL, str(pfad),
                                critical=kritisch))
        else:
            eltern = pfad.parent
            machbar = eltern.is_dir() and os.access(eltern, os.W_OK)
            zeilen.append(Check(
                "state", name, OK if machbar else FAIL,
                f"{pfad} — will be created on first run" if machbar
                else f"{pfad} — parent is not writable", critical=kritisch,
            ))
    return tuple(zeilen)


def check_bot_online(token: str, *, get: Callable[..., object] | None = None) -> Check:
    """Der einzige Netzaufruf, und nur mit `--online`. Er beweist, was sonst niemand
    beweisen kann: dass der Token GILT — ein widerrufener sieht formal gueltig aus."""
    if not token:
        return Check("online", "telegram getMe", FAIL, "no token to check", critical=True)
    if get is None:
        import requests

        get = requests.get
    try:
        antwort = get(f"https://api.telegram.org/bot{token}/getMe", timeout=20)
        daten = antwort.json() if callable(getattr(antwort, "json", None)) else {}
    except Exception as fehler:                       # Netz, DNS, TLS — alles dasselbe hier
        return Check("online", "telegram getMe", FAIL, type(fehler).__name__, critical=True)
    if not isinstance(daten, dict) or not daten.get("ok"):
        beschreibung = str((daten or {}).get("description", "rejected"))[:80]
        return Check("online", "telegram getMe", FAIL, beschreibung, critical=True)
    name = str((daten.get("result") or {}).get("username", "?"))
    return Check("online", "telegram getMe", OK, f"@{name}")


def collect(config, *, online: bool = False, get=None) -> tuple[Check, ...]:
    """Alle Befunde. `config` darf None sein.

    ⚠️ Eine nicht ladbare Konfiguration ist der HAeUFIGSTE Grund, diesen Befehl
    ueberhaupt aufzurufen — und ausgerechnet dann duerfen nicht alle Befunde
    ausfallen. Was ohne Konfiguration messbar ist (Python, Dateien und ihre Rechte,
    die vier optionalen Stuecke, die Isolation, die Verzeichnisse), wird gemessen;
    der Rest sagt ehrlich, dass er nichts sagen kann.
    """
    from .config import LOCAL_ENV, PIPER_BIN, SECRETS_ENV, SNAPSHOT_DIR, VOICE_DIR
    from .policy import WORKSPACE_DIR

    befunde: list[Check] = [check_python()]
    befunde += check_config((SECRETS_ENV, LOCAL_ENV))
    if config is not None:
        befunde.append(check_identity(tuple(config.allowed_principals)))
        befunde += check_model(
            config.model_provider, config.model_name,
            claude_bin=config.claude_bin, hermes_bin=config.hermes_bin,
            credentials=config.api_credentials,
        )
        befunde += check_model_overrides(
            config.model_overrides, known=_known_models(config.model_overrides),
        )
        befunde += check_channels(
            bot_token=config.bot_token, mail_host=config.mail_host,
            mail_user=config.mail_user, mail_password=config.mail_password,
            mail_authserv_id=config.mail_authserv_id,
        )
    befunde += check_capabilities(voice_dir=Path(VOICE_DIR), piper_bin=PIPER_BIN)
    befunde.append(check_sandbox(
        allow_unconfined=os.environ.get("TALOS_SANDBOX_ALLOW_UNCONFINED") == "1"
    ))
    schnappschuss = Path(config.snapshot_dir if config is not None else SNAPSHOT_DIR)
    befunde += check_state(data_dir=schnappschuss.parent, workspace=WORKSPACE_DIR)
    if online and config is not None:
        befunde.append(check_bot_online(config.bot_token, get=get))
    return tuple(befunde)


def render(checks: Iterable[Check]) -> str:
    """Nach Bereichen gruppiert — echt gruppiert, nicht bei jedem Wechsel neu.

    Ein Befund, der spaeter vorne angehaengt wird (etwa „Konfiguration nicht ladbar"),
    liesse eine Ueberschrift sonst zweimal erscheinen und den Bereich wie zwei
    verschiedene aussehen.
    """
    gruppen: dict[str, list[Check]] = {}
    for pruefung in checks:
        gruppen.setdefault(pruefung.area, []).append(pruefung)
    zeilen: list[str] = []
    for bereich, eintraege in gruppen.items():
        zeilen.append(f"\n  {bereich}")
        for pruefung in eintraege:
            detail = f"  {pruefung.detail}" if pruefung.detail else ""
            zeilen.append(f"    {_MARK[pruefung.state]} {pruefung.label}{detail}")
    return "\n".join(zeilen)


def summary(checks: Iterable[Check]) -> tuple[str, int]:
    """Die Schlusszeile und der Rueckgabewert. Nur Kritisches faellt durch."""
    liste = tuple(checks)
    blockierend = [c for c in liste if c.blocking]
    notizen = [c for c in liste if c.state is WARN or (c.state is FAIL and not c.critical)]
    if blockierend:
        was = ", ".join(c.label for c in blockierend)
        return f"\n  {SYM_FAIL} not ready: {was}\n", 1
    if notizen:
        return f"\n  {SYM_OK} ready — {len(notizen)} optional thing(s) missing\n", 0
    return f"\n  {SYM_OK} ready\n", 0


def run_doctor(argv: list[str] | None = None, *, out=None, get=None) -> int:
    """`talos doctor [--online]`. Aendert nichts, gibt 1 nur bei echten Hindernissen."""
    argumente = list(argv or [])
    schreiben = (out or sys.stdout).write
    online = "--online" in argumente

    config, fehlgrund = None, ""
    try:
        from .config import load_config

        config = load_config()
    except Exception as fehler:
        # Genau der Fall, fuer den der Befehl da ist. Er wird zu EINEM Befund unter
        # anderen — nicht zum Abbruch, sonst schweigt die Diagnose ausgerechnet an
        # dem Tag, an dem man sie braucht.
        fehlgrund = str(fehler)

    befunde = collect(config, online=online, get=get)
    if fehlgrund:
        befunde = (Check("config", "load", FAIL, fehlgrund, critical=True),) + befunde
    schreiben(render(befunde) + "\n")
    text, code = summary(befunde)
    schreiben(text)
    return code


__all__ = [
    "FAIL",
    "OK",
    "WARN",
    "Check",
    "check_bot_online",
    "check_capabilities",
    "check_channels",
    "check_config",
    "check_identity",
    "check_model",
    "check_model_overrides",
    "check_python",
    "check_sandbox",
    "check_state",
    "collect",
    "render",
    "run_doctor",
    "summary",
]
