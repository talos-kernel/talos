"""Das Schema der Konfiguration — und die Trennlinie, an der `config set` haltmacht.

Bis hierher war die Konfiguration eine Menge Zeichenketten, die `config.py` einsammelt.
Das reicht, solange nur ein Mensch sie tippt. Sobald ein Befehl sie schreiben darf,
braucht es eine Antwort auf die Frage, welche Schluessel das ueberhaupt sein duerfen —
und die darf **keine Namensheuristik** sein („alles mit KEY ist geheim"), weil der erste
Schluessel, der anders heisst, dann durchrutscht.

Das Sicherheitskriterium: **Kann eine Aenderung Befehlsgeber zulassen, einen
Kernel-Filter lockern, geschuetzte Daten umleiten oder Zugangsdaten ersetzen?** Dann ist
sie Politik oder Geheimnis — und nicht per CLI schreibbar. Alles andere ist Einstellung.

Daraus drei Klassen:

* ``SETTING`` — les- und schreibbar. Modellname, Anbieter, Zeitlimits.
* ``SECRET`` — nie anzeigbar, nie per ``config set``. Token, Passwoerter, Schluessel.
* ``POLICY`` — nie per ``config set``, auch nicht mit Bestaetigung. Die Rechteliste, der
  Pfad auf die Geheimnisdatei, die benannten Netz-Ausnahmen. Wer diese drei schreiben
  kann, braucht keinen Kernel mehr zu ueberreden: er stellt ihn um.

⚠️ **Bei ``SECRET`` antworten „gesetzt" und „nicht gesetzt" GLEICH.** Sonst ist `config
get` ein Orakel dafuer, welche Zugaenge eine Maschine hat — und das ist die Auskunft, die
ein Angreifer zuerst braucht. Aus demselben Grund gibt es keine Sternchen nach Laenge,
kein Praefix, kein `last4` und keinen Hash: jede dieser Maskierungen verraet etwas.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

SETTING, SECRET, POLICY = "setting", "secret", "policy"

# Was `config get` fuer ein Geheimnis ausgibt — immer dasselbe, ob gesetzt oder nicht.
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class Key:
    name: str
    kind: str
    help: str
    default: str = ""
    validate: Callable[[str], str] | None = None

    @property
    def writable(self) -> bool:
        """Nur Einstellungen. Geheimnisse gehen ueber den Assistenten, Politik ueber
        den Menschen an der Datei — beides bewusst umstaendlicher als ein Einzeiler."""
        return self.kind == SETTING

    @property
    def readable(self) -> bool:
        return self.kind != SECRET


def _one_line(value: str) -> str:
    """Kein Zeilenumbruch in eine KEY=VALUE-Datei.

    ⚠️ Das ist keine Kosmetik: ein `\\n` im Wert haengt eine ZWEITE Zeile an, und die
    kann jeden anderen Schluessel setzen — auch `TALOS_ALLOWED_PRINCIPALS`. Ein
    schreibbarer Schluessel waere damit ein Schreibrecht auf alle.
    """
    text = str(value)
    if any(zeichen in text for zeichen in "\n\r\x00"):
        raise ValueError("a value must not contain line breaks or null bytes")
    return text.strip()


def _bool01(value: str) -> str:
    text = _one_line(value)
    if text not in ("0", "1"):
        raise ValueError("expected 0 or 1")
    return text


def _positive_int(value: str) -> str:
    text = _one_line(value)
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("expected a positive whole number")
    return text


KEYS: tuple[Key, ...] = (
    # --- Politik: die drei, mit denen man den Kernel umstellt statt ihn zu ueberreden.
    Key("TALOS_ALLOWED_PRINCIPALS", POLICY,
        "who may command the agent (channel:id, comma separated) — this IS the "
        "permission list; `talos setup` writes it after proving the identity"),
    Key("TALOS_SECRETS_ENV", POLICY,
        "path to the credentials file — bending it makes the agent load someone "
        "else's secrets"),
    Key("TALOS_WEB_ALLOWED_ADDRESSES", POLICY,
        "named exceptions to the network address filter — every entry is a hole "
        "in guard_url that somebody decided to make"),
    Key("TALOS_AGENT_CONSULT_URL", POLICY,
        "fixed endpoint for inter-agent consultation — changing it redirects private reports"),
    Key("TALOS_AGENT_CONSULT_ALIASES", POLICY,
        "private names that make explicit consultation requests mandatory"),
    Key("TALOS_CLAUDE_WORKER_ENABLED", POLICY,
        "master switch for the Claude worker delegation tool", default="0",
        validate=_bool01),
    Key("TALOS_CLAUDE_WORKER_SOCKET", POLICY,
        "unix socket of the Claude worker — changing it redirects delegated jobs"),
    Key("TALOS_CLAUDE_WORKER_ROOT", POLICY,
        "root directory for Claude job workspaces — jobs may write only below it"),
    Key("TALOS_CLAUDE_WORKER_HOME", POLICY,
        "dedicated HOME for Claude jobs holding only the Claude OAuth state"),
    Key("TALOS_CLAUDE_WORKER_BIN", POLICY,
        "pinned claude binary path for worker jobs", default="claude"),
    # ⚠️ Die Basis-Adressen stehen nicht hier, sondern werden unten aus dem Katalog
    # erzeugt — eine pro Anbieter. Eine einzige `TALOS_API_BASE_URL` fuer alle war der
    # Weg, auf dem OpenAI-Anfragen an Anthropics Basis gingen (Befund 05.08.).
    Key("TALOS_MAIL_AUTHSERV_ID", POLICY,
        "the name your own receiving server stamps into Authentication-Results — it "
        "names WHOSE verdict counts as proof of a sender, so a wrong name here makes "
        "a forged header authentic"),
    Key("TALOS_SHELL_NEEDS_HUMAN", POLICY,
        "make every shell command ask first — only ever tightens, but it is a "
        "kernel setting and not a preference", default="0", validate=_bool01),
    # --- Geheimnisse.
    Key("TELEGRAM_BOT_TOKEN", SECRET, "the bot token — there is no way in without it"),
    Key("TALOS_MAIL_PASSWORD", SECRET, "IMAP password for the mail channel"),
    Key("ANTHROPIC_API_KEY", SECRET, "your own Anthropic key, if the provider needs one"),
    Key("OPENAI_API_KEY", SECRET, "your own OpenAI-compatible key, if the provider needs one"),
    Key("TALOS_BRAVE_API_KEY", SECRET,
        "optional search key — without it the keyless provider answers"),
    Key("TALOS_AGENT_CONSULT_TOKEN", SECRET,
        "credential for the fixed inter-agent consultation endpoint"),
    # --- Einstellungen.
    Key("TALOS_MODEL_PROVIDER", SETTING, "which provider thinks", validate=_one_line),
    Key("TALOS_MODEL", SETTING, "which model of that provider", validate=_one_line),
    Key("TALOS_MODEL_FALLBACKS", SETTING,
        "comma-separated provider/model chain tried in order when a run fails with a "
        "classified error (e.g. ollama/qwen3:27b,nvidia-nim/meta/llama-3.3-70b-instruct) "
        "— the persisted model choice is never touched", validate=_one_line),
    Key("TALOS_OWNER_LABEL", SETTING, "how the agent addresses its operator",
        validate=_one_line),
    Key("TELEGRAM_BOT_USERNAME", SETTING, "shown in the greeting; purely cosmetic",
        validate=_one_line),
    Key("TALOS_MAIL_HOST", SETTING, "IMAP host of the mail channel", validate=_one_line),
    Key("TALOS_MAIL_USER", SETTING, "IMAP user of the mail channel", validate=_one_line),
    Key("TALOS_MAIL_SMTP_HOST", SETTING, "SMTP host for replies", validate=_one_line),
    Key("TALOS_VAULT_DIR", SETTING, "where the notes live", validate=_one_line),
    Key("TALOS_QMD_BIN", SETTING, "the qmd binary", validate=_one_line),
    Key("TALOS_HERMES_BIN", SETTING, "the hermes binary", validate=_one_line),
    Key("TALOS_SKILLS_DIRS", SETTING, "where skills are read from", validate=_one_line),
    Key("TALOS_POLL_TIMEOUT_S", SETTING, "long-poll seconds", default="30",
        validate=_positive_int),
    Key("TALOS_WEB_ALLOW_HTTP", SETTING, "allow plain http as well as https",
        default="0", validate=_bool01),
    Key("TALOS_CLAUDE_WORKER_MAX_PARALLEL", SETTING,
        "max concurrent Claude worker jobs", default="2", validate=_positive_int),
    Key("TALOS_CLAUDE_WORKER_JOB_TIMEOUT", SETTING,
        "overall deadline per Claude job in seconds", default="900",
        validate=_positive_int),
)

def _base_url_keys() -> tuple[Key, ...]:
    """Eine Basis-Adresse pro Anbieter mit eigenem Ziel, aus dem Katalog erzeugt.

    ⚠️ POLICY, nicht SETTING: wer die Adresse biegt, schickt den Schluessel — und bei
    schluessellosen lokalen Anbietern wenigstens die PROMPTS — an eine Maschine, die er
    nicht gewaehlt hat; die Antwort kommt als Gedanke dieses Agenten zurueck. Erzeugt
    statt getippt, weil eine handgepflegte Liste beim naechsten Anbieter luecken haette,
    und eine Luecke hiesse hier: nicht schreibbar, aber auch nicht als Geheimnis erkannt.
    """
    from . import catalog
    from .credentials import base_url_var

    return tuple(
        Key(base_url_var(info.slug), POLICY,
            f"where {info.label} is reached — bending it sends this installation's "
            "prompts and keys to a machine you did not choose")
        for info in catalog.PROVIDERS
        if (info.needs_key and info.env_key) or info.auth == "local"
    )


def _provider_key_keys() -> tuple[Key, ...]:
    """Der Schluessel jedes Anbieters, ebenfalls aus dem Katalog. Immer SECRET."""
    from . import catalog

    gesehen: set[str] = set()
    erzeugt = []
    for info in catalog.PROVIDERS:
        if not info.needs_key or not info.env_key or info.env_key in gesehen:
            continue
        gesehen.add(info.env_key)
        erzeugt.append(Key(info.env_key, SECRET, f"your own key for {info.label}"))
    return tuple(erzeugt)


# Handgeschriebenes zuerst: steht ein Name in beiden Quellen (`ANTHROPIC_API_KEY`),
# gewinnt der handgeschriebene Text.
KEYS = KEYS + tuple(
    key for key in _provider_key_keys() + _base_url_keys()
    if key.name not in {k.name for k in KEYS}
)

BY_NAME: dict[str, Key] = {key.name: key for key in KEYS}


def get(name: str) -> Key | None:
    return BY_NAME.get(str(name).strip())


def unknown(names: object) -> tuple[str, ...]:
    """Namen, die das Schema nicht kennt. Kein Fehler — nur ein Befund.

    Absichtlich nicht abweisend: eine Konfiguration darf Zeilen tragen, die Talos
    nicht liest (ein Kommentar des Betreibers an sich selbst, ein Wert fuer ein
    Hilfsskript). Wer sie verboten kaeme, zwaenge dazu, sie woanders zu verstecken.
    """
    return tuple(sorted(str(n) for n in (names or ()) if str(n) not in BY_NAME))


def secrets() -> tuple[str, ...]:
    return tuple(key.name for key in KEYS if key.kind == SECRET)


def policy_keys() -> tuple[str, ...]:
    return tuple(key.name for key in KEYS if key.kind == POLICY)


def worker_scope(name: str) -> bool:
    """Gehoert dieser Schluessel im Worker-Modus in die Worker-Env statt ins Agent-Env?

    Rein eine ORTS-Auskunft, keine vierte Klasse: ein Provider-Schluessel bleibt
    SECRET — `config get` antwortet `[REDACTED]`, ob der Modell-Worker laeuft oder
    nicht, und `config set` lehnt ihn weiter ab. Was sich im Worker-Modus aendert,
    ist der erwartete Zustand der Agent-Umgebung: der Schluessel FEHLT dort
    absichtlich (`/etc/talos/model.env`, UID `talos-model`), und dieses Fehlen ist
    der Soll-Zustand, kein Mangel.
    """
    from .credentials import worker_scope_names

    return str(name).strip() in worker_scope_names()


__all__ = [
    "BY_NAME",
    "KEYS",
    "POLICY",
    "REDACTED",
    "SECRET",
    "SETTING",
    "Key",
    "get",
    "policy_keys",
    "secrets",
    "unknown",
    "worker_scope",
]
