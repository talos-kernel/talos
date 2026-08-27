"""MEDIA:-Tags — eine erzeugte Datei wird ein echter Chat-Anhang.

Der Agent hat eine Datei erzeugt (PDF, CSV, Bild). Bisher konnte er dem Betreiber nur
ihren PFAD schreiben — der Betreiber musste selbst auf die Maschine, um sie zu sehen.
Mit einer Zeile `MEDIA:/pfad/zur/datei.pdf` in seiner Antwort schickt er sie stattdessen
als echten Anhang in denselben Chat (Telegram `sendDocument`/`sendPhoto`, WhatsApp
Dokument). Die Tag-Zeile verschwindet aus dem sichtbaren Text.

**Warum das kein Werkzeug ist — und trotzdem gegatet.** Ein `send_file`-Werkzeug haette
zwei Argumente: Pfad und Empfaenger. Den Empfaenger duerfte das Modell nie waehlen (das
`ask_operator`-Muster: der Rueckweg kommt aus dem Thread-Kontext, nie aus den
Argumenten) — es bliebe genau der Chat, in dem die Antwort ohnehin hingeht. Ein Werkzeug,
dessen einzig moeglicher Empfaenger der ist, den die Antwort sowieso hat, ist ein
zweiter Sendeweg ohne neue Faehigkeit. Deshalb loest der Conductor den Tag auf dem
Antwortpfad auf — und das GATE ist dasselbe wie ueberall: ableitbares Ziel, Kernel-Floors.

Das Ziel ist **Pfad + Empfaenger**:
  * Der Empfaenger ist die Konversation, die gerade beantwortet wird — nie Modelltext.
    Wer den DateiINHALT ohnehin als Text in dieselbe Antwort schreiben koennte
    (`read_file` ist fuer diese Pfade ALLOW), gewinnt durch den Anhang keinen neuen
    Adressaten.
  * Der Pfad wird durch dieselben Floors gejagt wie ein Leseziel: System- und
    Secret-Praefixe des Kernels, dazu erlaubte Wurzeln (Arbeitsbereich und die
    kernel-abgeleitete Claude-Job-Wurzel), ein Namensfilter fuer Zugangsdaten, ein
    Groessendeckel. `resolve` wirft `ValueError` mit Grund — der Betreiber bekommt eine
    ehrliche Zeile statt eines Anhangs.

**Nur die eigenen Worte des Agenten loesen aus.** `extract` laeuft in `Conductor._run_task`
auf `result.text` — bevor Quittungen und Notizen angehaengt werden. Werkzeugergebnisse,
Nachrichten des Betreibers und Freigabe-Texte durchlaufen diese Stelle nie: ein
`MEDIA:` in einer gelesenen Datei oder einer Webseite ist Text, kein Auftrag. Der
Prompt-Injection-Fall (Werkzeugausgabe bringt das Modell dazu, den Tag selbst zu
schreiben) bleibt als Moeglichkeit bestehen — und genau dafuer ist das Gate da: es
entscheidet ueber den Pfad, egal wer die Zeile inspiriert hat.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import policy
from .policy import SECRET_PREFIXES, SYSTEM_PREFIXES, _expand, _hits, _is_secret

TAG = "MEDIA:"
# Telegram laesst Bots bis 50 MB hochladen; darunter bleiben, weil die Datei beim
# Versand zweimal durch den Speicher geht (Lesen + HTTP) und der Dienst auf einem Pi
# laeuft. Eine groessere erzeugte Datei ist ohnehin die Ausnahme.
MAX_MEDIA_BYTES = 20 * 1024 * 1024
# Mehr Anhaenge in einer Antwort sind kein legitimer Fall, sondern ein Unfall oder ein
# Versuch. Der Ueberschuss wird nicht still verschluckt — er steht als Zeile im Text.
MAX_ATTACHMENTS = 4
MAX_PATH_CHARS = 400

# Zugangsdaten am NAMEN erkannt, auf jeder Ebene — dasselbe Prinzip wie
# `policy._VAULT_SECRET_DIRS`: eine Pfadliste schuetzt eine einzige Ablage, ein
# Namensmuster schuetzt die Sorte Datei, egal wo sie im Arbeitsbereich liegt.
_SECRET_NAMES = frozenset({
    ".env", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials.json",
})
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")

_TAG_LINE = re.compile(r"^[ \t]*MEDIA:[ \t]*(\S.*?)[ \t]*$")
_BLANK_RUN = re.compile(r"\n{3,}")


def extract(text: object) -> tuple[str, tuple[str, ...]]:
    """Tag-Zeilen aus der Antwort loesen. Gibt `(sichtbarer_text, rohe_pfade)` zurueck.

    Eine Zeile zaehlt nur, wenn sie — abgesehen von Leerraum — NUR aus `MEDIA:<pfad>`
    besteht. `MEDIA:` mitten im Satz ist Prosa und bleibt stehen: der Tag ist ein
    Protokoll der letzten Zeile, keine Auszeichnungssprache.

    Ueberzaehlige Tags werden nicht gesendet und nicht still gestrichen — sie tauchen
    als ehrliche Zeile im sichtbaren Text auf.
    """
    kept: list[str] = []
    paths: list[str] = []
    overflow = 0
    for line in str(text or "").split("\n"):
        match = _TAG_LINE.match(line)
        if match is None:
            kept.append(line)
        elif len(paths) < MAX_ATTACHMENTS:
            paths.append(match.group(1))
        else:
            overflow += 1
    clean = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip("\n")
    if overflow:
        hinweis = f"(only the first {MAX_ATTACHMENTS} attachments are sent — {overflow} more were left out)"
        clean = f"{clean}\n\n{hinweis}" if clean.strip() else hinweis
    return clean, tuple(paths)


def allowed_roots() -> tuple[str, ...]:
    """Wurzeln, unter denen ein Anhang liegen darf — kernel-abgeleitet, zur Laufzeit gelesen.

    Der Arbeitsbereich (da schreibt der Agent frei) und die Claude-Job-Wurzel (da
    schreibt der eingesperrte Worker; `delegate_code`-Ergebnisse sollen versendbar
    sein). Beide kommen aus `policy`, nie aus dem Tag — und zur Laufzeit, damit ein
    Test oder eine Umgebungsvariable sie verlegen kann.
    """
    return tuple(sorted({
        os.path.realpath(str(policy.WORKSPACE_DIR)),
        os.path.realpath(policy.claude_work_root()),
    }))


def _looks_like_credentials(name: str) -> bool:
    lowered = name.lower()
    return (
        name in _SECRET_NAMES
        or lowered.endswith(_SECRET_SUFFIXES)
        or lowered.startswith(".env")
    )


def resolve(raw: object) -> str:
    """Roher Tag-Pfad -> gepruefter, aufgeloester Pfad. `ValueError` mit Grund sonst.

    Reihenfolge ist Bedeutung: erst die Floors (die sagen auch bei einer fehlenden
    Datei etwas aus), dann die Wurzeln, dann die Existenz, zuletzt die Groesse.
    """
    text = str(raw or "").strip().strip("'\"")
    if not text or len(text) > MAX_PATH_CHARS:
        raise ValueError(f"unusable attachment path: {text[:80]!r}")
    # Relativ heisst relativ zum Arbeitsbereich, nie relativ zum Zufalls-CWD des
    # Dienstes: `MEDIA:bericht.pdf` und `MEDIA:../../vault/key.pem` werden an
    # derselben, festen Stelle aufgeschlagen.
    expanded = _expand(text)
    if not os.path.isabs(expanded):
        expanded = str(policy.WORKSPACE_DIR / expanded)
    real = os.path.realpath(os.path.abspath(os.path.normpath(expanded)))
    if _hits(real, SYSTEM_PREFIXES):
        raise ValueError(f"system path refused as attachment: {text[:120]}")
    if _is_secret(real):
        raise ValueError(f"secret path refused as attachment: {text[:120]}")
    roots = allowed_roots()
    if not any(real == root or real.startswith(root + os.sep) for root in roots):
        raise ValueError(f"attachment outside the workspace refused: {text[:120]}")
    if _looks_like_credentials(Path(real).name):
        raise ValueError(f"credential-shaped file refused as attachment: {Path(real).name}")
    if not os.path.isfile(real):
        raise ValueError(f"attachment is not a file: {text[:120]}")
    if os.path.getsize(real) > MAX_MEDIA_BYTES:
        raise ValueError(
            f"attachment larger than {MAX_MEDIA_BYTES // (1024 * 1024)} MB refused: {Path(real).name}"
        )
    return real


__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_MEDIA_BYTES",
    "MAX_PATH_CHARS",
    "TAG",
    "allowed_roots",
    "extract",
    "resolve",
]
