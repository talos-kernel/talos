"""Talos aktualisieren — mit demselben Beweis, den die Erstinstallation verlangt.

Warum es dieses Modul ueberhaupt gibt: `site/install.sh` bricht ab, sobald das Ziel-
verzeichnis existiert ("I do not touch what is not mine"). Es gibt damit bisher gar
keinen Weg von einer Version zur naechsten ausser "loeschen und neu installieren" —
und der nimmt Event-Log, Konfiguration und Arbeitsverzeichnis des Betreibers mit.

Was hier bewusst NICHT kopiert wird: Hermes' `hermes update` macht `git pull` plus
Neuinstallation der Abhaengigkeiten, Backup standardmaessig aus, kein Test-Gate. Talos'
ganzes Versprechen ist, dass es sich vorfuehren laesst. Ein Update, das den Sicherheits-
kern austauscht, ohne ihn vorher zu beweisen, waere genau die Hintertuer, die der Kernel
sonst verbietet. Deshalb der Ablauf:

  1. Der neue Baum entsteht NEBEN dem alten (`<prefix>.new-<version>`), nie darueber.
  2. Die sha256 wird gegen die veroeffentlichte Pruefsumme geprueft. Stimmt sie nicht
     (oder fehlt sie), wird nichts entpackt.
  3. Test-Suite UND `redteam.py` laufen IM NEUEN BAUM, sichtbar. Faellt eines von beiden
     aus, wird nicht umgeschaltet: der neue Baum wird verworfen, die alte Installation
     bleibt unberuehrt, der Exit-Code ist != 0.
  4. Erst danach die zwei Umbenennungen. Der alte Baum bleibt als
     `<prefix>.old-<alte-version>` liegen — der Rueckweg ist ein `mv`, und er steht als
     fertige Zeile in der Schlussmeldung.
  5. Es wird nichts gestartet. Kein Cron, kein Autostart, kein Neustart eines laufenden
     Dienstes. Wer aktualisiert, hat es getippt; das ist die Einwilligung, und eine
     andere gibt es nicht.

Wem welcher Pfad gehoert
------------------------
    <prefix>/talos/ · tests/ · redteam.py · requirements*.txt · .venv/
        gehoeren der Auslieferung. Sie kommen aus dem Tarball und werden ersetzt.
    <prefix>/talos.env      Konfiguration des Betreibers (Bot-Token, Allowlist, 0600)
    <prefix>/data/          Event-Log und Snapshots (0700) — die Belege ueber alles Getane
    <prefix>/workspace/     Arbeitsverzeichnis des Agenten
    <prefix>/SOUL.md        Name und Wesen des Agenten. Sieht aus wie Auslieferung, ist
                            es nicht: die erste Ueberschrift ist der NAME. Wer keine
                            eigene hat, behaelt die ausgelieferte.
        gehoeren dem Betreiber. Sie werden nie ersetzt, sondern in den neuen Baum
        KOPIERT (nicht verschoben) — nur so bleibt der alte Baum vollstaendig und der
        Rueckweg wirklich trivial. Nach dem Umschalten schreibt der Agent in die Kopie;
        der Stand im alten Baum ist der eingefrorene Zustand von vor dem Update.

Veroeffentlichungs-Format (dieselbe Basis wie der Installer, `TALOS_BASE`)
-------------------------------------------------------------------------
    <base>/dist/latest.txt                      eine Zeile, nur die Version.
                                                Leerzeilen und `#`-Zeilen werden
                                                uebersprungen; die erste uebrige Zeile
                                                gilt. Erlaubt sind `[A-Za-z0-9._-]`,
                                                beginnend alphanumerisch, max. 32 Zeichen.
    <base>/dist/talos-<version>.tar.gz          die Auslieferung, ein Wurzelverzeichnis
                                                (wie `tar --strip-components=1`)
    <base>/dist/talos-<version>.tar.gz.sha256   `<64 hex>  talos-<version>.tar.gz`
    <base>/dist/talos-<version>.tar.gz.sig      Ed25519 ueber die Archiv-Bytes, base64
    <base>/dist/CHANGELOG-<version>.md          optional; fehlt sie, wird das gesagt

Warum die Signatur und nicht nur die Pruefsumme
-----------------------------------------------
Archiv UND Pruefsumme kommen von derselben Basis-URL. Wer den Server, das CDN oder das
DNS beherrscht, ersetzt schlicht beides — die Pruefsumme beweist dann nur, dass die
Datei heil angekommen ist, nicht dass sie von uns stammt. Genau so stand es als offener
Befund in der Pruefung vom 05.08., und fuer einen Updater ist das die schwerste Sorte
Loch: am Ende fuehrt er den Code aus.

Die Signatur bricht das, weil ihr Schluessel NICHT auf dem Server liegt. Geprueft wird
gegen `RELEASE_PUBLIC_KEY` — eingebacken in den Code, der schon installiert IST. Ein
Angreifer, der die Veroeffentlichung beherrscht, kann Archiv, Pruefsumme und Signatur
austauschen und scheitert trotzdem: er hat den privaten Schluessel nicht.

⚠️ Das Vertrauen haengt am LAUFENDEN Baum, nicht am neuen. Ein Update ersetzt auch
diesen oeffentlichen Schluessel. Wer je ein korrekt signiertes Update unterschiebt,
besitzt damit alle folgenden. Das ist keinem Selbst-Updater auszutreiben — es gehoert
nur gesagt, statt weggelassen.

⚠️ Fehlt der Schluessel hier (leerer String), wird NICHT still auf die Pruefsumme
zurueckgefallen: dann verweigert das Update. Ein Rueckfall waere genau der Weg, den ein
Angreifer nimmt — Signatur weglassen und darauf hoffen, dass es wie frueher weiterlaeuft.

Es wird bewusst KEINE Versions-Ordnung berechnet. Was veroeffentlicht ist, gilt — auch
wenn es aelter ist als das Laufende. Sonst waere ein Rueckruf einer schlechten Version
per Veroeffentlichung nicht installierbar.

WARNUNG — die Versionsnummer steht an ZWEI Stellen: `talos/__init__.py`
(`__version__`) und `site/install.sh` (`VERSION=`). Sie sind bereits einmal
auseinandergelaufen (Paket 0.0.1 gegen Installer 0.2.0-alpha), und genau dieser Riss
ist fuer einen Updater toedlich: dieses Modul vergleicht `talos.__version__` gegen
`latest.txt`, weil das die Zahl ist, die im laufenden Prozess wirklich gilt. Sagt der
Installer etwas anderes, vergleicht das Update gegen eine Basis, die nie ausgeliefert
wurde — es meldet "aktuell", waehrend eine neue Version bereitliegt, oder umgekehrt.
Die beiden Stellen muessen zusammengefuehrt bleiben (eine Quelle, die andere liest sie,
oder ein Test haelt sie zusammen). Wer hier hochzaehlt, zaehlt im Installer mit.

Aufruf::

    run_update(["--check"])                     nur nachsehen, laedt nichts
    run_update([])                              pruefen, beweisen, umschalten
    run_update(["--prefix", "/opt/talos", "--base", "https://example.org"])

Alle Netz-, Datei- und Subprozessaufrufe laufen ueber injizierbare Abhaengigkeiten
(`http`, `runner`, `stdout`), damit dieses Modul ohne Netz und ohne echte Installation
pruefbar ist — ein Updater, den man nicht testen kann, ist selbst ein Risiko.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from . import __version__
from .ux import SYM_FAIL, SYM_OK, SYM_TOOL

# `http(url) -> bytes` und `runner(argv, cwd) -> exit-code`. Mehr Vertrag brauchen die
# beiden nicht; alles Weitere waere eine Kupplung an requests bzw. subprocess.
Fetcher = Callable[[str], bytes]
Runner = Callable[[Sequence[str], Path], int]

# ⚠️ Diese Adresse steht im ausgelieferten Code — eine Installation fragt beim Update
# genau die, die sie selbst traegt. Ein Wechsel erreicht deshalb NIE die bereits
# installierten Fassungen: die laufen weiter gegen die alte, bis jemand sie von Hand
# umstellt. Wer hier umzieht, laesst die alte Adresse so lange weiterleiten, bis
# nachweislich niemand mehr von dort laedt — sonst bekommen genau die Installationen nie
# wieder ein Update, auch nicht das, welches die Adresse korrigieren wuerde.
# `_urlopen_fetch` folgt einer Weiterleitung; gemessen, nicht angenommen. Die Signatur
# haengt am Schluessel im ausgelieferten Code, nicht am Hostnamen — ein Umzug kann sie
# nicht entwerten.
DEFAULT_BASE = "https://talos-agent.ch"
HTTP_TIMEOUT_S = 60
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024  # eine Auslieferung ist klein; alles darueber ist keine
CHANGELOG_MAX_LINES = 40
LIVE_WINDOW_S = 300  # frisch geschriebenes Event-Log -> da laeuft vermutlich einer
VENV_PYTHON = Path(".venv") / "bin" / "python"
# ⚠️ `SOUL.md` gehoert hierher, obwohl es aussieht wie eine Datei der Auslieferung.
# Seine erste Ueberschrift IST der Name des Agenten, und der Rest ist sein Wesen und
# seine Sprachregel. Kaeme es aus dem Tarball, wuerde ein Update jede benannte
# Installation still auf die neutrale Fassung zuruecksetzen — der Agent hiesse danach
# anders und spraeche anders, ohne dass jemand das getippt haette. Wer keine eigene hat,
# behaelt die ausgelieferte: `_carry_state` ueberspringt, was nicht existiert.
STATE_PATHS = ("talos.env", "data", "workspace", "SOUL.md")

# Version aus einer fremden Datei landet in einer URL UND in einem Pfad neben der
# Installation. Ohne diese Schranke waere `../../..` ein Schreibzugriff ausserhalb.
# Der oeffentliche Teil des Veroeffentlichungs-Schluessels, roh und base64. Er wird MIT
# dem Code ausgeliefert und liegt damit nie auf dem Server, gegen den er schuetzt.
# Der private Teil liegt beim Betreiber, offline, und geht nirgendwo mit.
RELEASE_PUBLIC_KEY = "Do7lfPckC7pJJtD4BECN/mLPIOqHZVWm/j/MfJOK2hk="

_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")

EXIT_OK = 0
EXIT_FAILED = 1      # allgemeiner Abbruch
EXIT_FETCH = 2       # Veroeffentlichung nicht lesbar
EXIT_CHECKSUM = 3    # sha256 fehlt oder passt nicht
EXIT_PROOF = 4       # Test-Suite oder Angriffs-Suite im neuen Baum durchgefallen
EXIT_OCCUPIED = 5    # ein Pfad ist belegt, der uns nicht gehoert
EXIT_SIGNATURE = 6   # Signatur fehlt, passt nicht, oder ist nicht pruefbar


class UpdateError(RuntimeError):
    """Abbruchgrund mit Exit-Code. Jeder Abbruch laesst die alte Installation stehen."""

    def __init__(self, message: str, code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Options:
    check: bool
    prefix: Path
    base: str


@dataclass(frozen=True)
class _Plan:
    """Alle Pfade und Versionen eines Laufs — einmal berechnet, danach nur gelesen."""

    base: str
    prefix: Path
    staging: Path
    retired: Path
    running: str
    latest: str


class _Writer:
    """Ausgabe im Ton des Installers: eingerueckte Schritte, ein Zeichen je Ergebnis.

    Der Maschinen-Konsolentext bleibt englisch (Konvention aus CLAUDE.md), die Zeichen
    kommen aus `ux.py` — `SYM_TOOL` markiert wie im Installer den Beginn eines Schritts.
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def step(self, text: str) -> None:
        self._write(f"\n  {SYM_TOOL} {text}")

    def ok(self, text: str) -> None:
        self._write(f"    {SYM_OK} {text}")

    def fail(self, text: str) -> None:
        self._write(f"\n  {SYM_FAIL} stopped: {text}\n")

    def note(self, text: str) -> None:
        self._write(f"    {text}")

    def line(self, text: str = "") -> None:
        self._write(text)

    def _write(self, text: str) -> None:
        print(text, file=self._stream, flush=True)


# --- Standard-Abhaengigkeiten -------------------------------------------------------


def _trust_store() -> ssl.SSLContext:
    """Ein Kontext, der auch dort Wurzeln findet, wo Python von sich aus keine hat.

    Auf macOS bringt der python.org-Build seine CA-Datei nicht mit; sie entsteht erst,
    wenn jemand `Install Certificates.command` ausfuehrt, und das tut fast niemand.
    `ssl.get_default_verify_paths().cafile` ist dann `None`, und **jeder** Aufruf endet in
    `CERTIFICATE_VERIFY_FAILED` — der Update-Weg war auf diesen Rechnern also nie
    begehbar, obwohl `curl` daneben problemlos laedt.

    `certifi` liegt ohnehin im Baum (`requests` zieht es), also wird es benutzt, wenn die
    Standardpfade leer sind. **Die Pruefung wird nie abgeschaltet**, nur die Quelle der
    Wurzeln ergaenzt: ein Updater, der `verify_mode` senkt, um durchzukommen, ersetzt einen
    laufenden Waechter durch das, was gerade auf der Leitung liegt.
    """
    default = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if paths.cafile or paths.capath:
        return default
    try:
        import certifi
    except ImportError:
        return default
    return ssl.create_default_context(cafile=certifi.where())


def _urlopen_fetch(url: str) -> bytes:
    """Holt eine URL: nur https, mit Timeout, mit Groessendeckel.

    Der Installer laesst `TALOS_BASE` frei. Beim Update ersetzt der Nutzer aber einen
    laufenden Waechter — eine Quelle, die jeder im Netz umschreiben kann, darf das nicht
    liefern. Wer wirklich lokal testen will, injiziert `http`.
    """
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a source that is not https: {url}", EXIT_FETCH)
    with urlopen(  # noqa: S310 - Schema oben geprueft
        url, timeout=HTTP_TIMEOUT_S, context=_trust_store()
    ) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise UpdateError(f"{url} is larger than {MAX_DOWNLOAD_BYTES} bytes.", EXIT_FETCH)
    return payload


def _subprocess_runner(argv: Sequence[str], cwd: Path) -> int:
    """Fuehrt einen Schritt sichtbar aus — stdout/stderr bleiben am Terminal.

    Genau wie im Installer: die Suiten laufen vor den Augen des Nutzers, nicht in einem
    verschluckten Puffer, dessen Ergebnis er glauben muesste.
    """
    completed = subprocess.run(list(argv), cwd=str(cwd), check=False)
    return completed.returncode


# --- Argumente ----------------------------------------------------------------------


def _value_of(items: Sequence[str], flag: str) -> str | None:
    """Liest `--flag wert` und `--flag=wert`, ohne die uebergebene Liste zu veraendern."""
    for index, item in enumerate(items):
        if item == flag and index + 1 < len(items):
            return items[index + 1]
        if item.startswith(f"{flag}="):
            return item.split("=", 1)[1]
    return None


def _parse_args(argv: Sequence[str]) -> _Options:
    """`--check`, `--prefix`, `--base`. Unbekannte Argumente werden ignoriert.

    Ignorieren statt Abbrechen, weil der Einstiegspunkt sein eigenes Schaltwort
    (z.B. `--update`) mitschickt und der Updater darueber nichts wissen muss.
    """
    prefix = (
        _value_of(argv, "--prefix")
        or os.environ.get("TALOS_PREFIX")
        or str(Path(__file__).resolve().parent.parent)
    )
    base = _value_of(argv, "--base") or os.environ.get("TALOS_BASE") or DEFAULT_BASE
    return _Options(
        check="--check" in argv,
        prefix=Path(prefix).expanduser(),
        base=base.rstrip("/"),
    )


# --- Veroeffentlichung lesen --------------------------------------------------------


def _fetch(fetch: Fetcher, url: str) -> bytes:
    """Jede Transportstoerung ist derselbe Abbruch: wir wissen nicht, was dort steht."""
    try:
        return fetch(url)
    except UpdateError:
        raise
    except Exception as error:  # noqa: BLE001 - Transportfehler sind nicht unterscheidbar
        raise UpdateError(f"could not read {url}: {error}", EXIT_FETCH) from error


def _optional_text(fetch: Fetcher, url: str) -> str | None:
    """Fuer Beiwerk (Changelog): fehlt es, wird das gesagt — es bricht nichts ab."""
    try:
        return fetch(url).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - eine fehlende Aenderungsliste ist kein Abbruchgrund
        return None


def _published_version(fetch: Fetcher, base: str) -> str:
    """Erste nicht-leere, nicht-`#`-Zeile aus `<base>/dist/latest.txt` ist die Version."""
    text = _fetch(fetch, f"{base}/dist/latest.txt").decode("utf-8", "replace")
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if not _SAFE_VERSION.match(candidate):
            raise UpdateError(
                f"the published version {candidate!r} is not a plain version string — "
                "it would end up in a URL and in a path next to your installation.",
                EXIT_FETCH,
            )
        return candidate
    raise UpdateError(f"{base}/dist/latest.txt names no version.", EXIT_FETCH)


def _expected_sha256(fetch: Fetcher, plan: _Plan) -> str:
    """Liest `<tarball>.sha256`. Fehlt sie, wird nicht entpackt — anders als im Installer.

    Der Installer warnt hier nur. Der Unterschied ist bewusst: beim ersten Mal entscheidet
    der Nutzer, ob er Talos ueberhaupt will; beim Update tauscht er einen Waechter aus,
    der bereits laeuft. Ein unverifiziertes Archiv hat an dieser Stelle nichts zu suchen.
    """
    url = f"{plan.base}/dist/talos-{plan.latest}.tar.gz.sha256"
    fields = _fetch(fetch, url).decode("utf-8", "replace").split()
    if not fields or not _SHA256_HEX.match(fields[0]):
        raise UpdateError(
            f"{url} carries no sha256 sum. Refusing to unpack an unverified archive.",
            EXIT_CHECKSUM,
        )
    return fields[0].lower()


def _verify_signature(payload: bytes, fetch: Fetcher, plan: _Plan, out: _Writer) -> None:
    """Beweist, dass dieses Archiv von UNS stammt — nicht nur, dass es heil ankam.

    Die Pruefsumme liegt neben dem Archiv; wer das eine ersetzen kann, ersetzt das andere.
    Der Schluessel dagegen liegt hier, im Baum, der schon laeuft.
    """
    if not RELEASE_PUBLIC_KEY.strip():
        # ⚠️ Kein stiller Rueckfall auf „dann eben nur die Pruefsumme". Genau diesen Weg
        # nimmt ein Angreifer: Signatur weglassen und hoffen, dass es weiterlaeuft.
        raise UpdateError(
            "no release key is pinned in this installation, so nothing can prove the "
            "archive came from us. Refusing to unpack.",
            EXIT_SIGNATURE,
        )
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise UpdateError(
            "the `cryptography` package is missing, so the release signature cannot be "
            "checked. Install it (`pip install -r requirements.txt`) — an update is the "
            "one place that must not be taken on trust.",
            EXIT_SIGNATURE,
        ) from None

    url = f"{plan.base}/dist/talos-{plan.latest}.tar.gz.sig"
    try:
        roh = _fetch(fetch, url)
    except UpdateError as fehler:
        # ⚠️ Als EXIT_SIGNATURE, nicht als EXIT_FETCH. Eine fehlende Signatur ist kein
        # Netzproblem, sondern eine unbewiesene Herkunft — und wer sie unter „Abruf
        # fehlgeschlagen" ablegt, laedt zum Nochmalversuchen ein, statt zum Nachsehen.
        raise UpdateError(
            f"no signature at {url} ({fehler}). This release cannot prove it came from "
            "us, so it will not be unpacked.",
            EXIT_SIGNATURE,
        ) from None
    try:
        signature = base64.b64decode(roh.strip(), validate=True)
    except (ValueError, binascii.Error):
        raise UpdateError(f"{url} is not valid base64.", EXIT_SIGNATURE) from None
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(RELEASE_PUBLIC_KEY))
        key.verify(signature, payload)
    except InvalidSignature:
        raise UpdateError(
            "the release signature does not match this archive. Either the download was "
            "tampered with or it was not published by the holder of the release key. "
            "Nothing was unpacked, nothing was changed.",
            EXIT_SIGNATURE,
        ) from None
    except Exception as fehler:                       # kaputte Schluesselform, falsche Laenge
        raise UpdateError(f"the release signature could not be checked: {fehler}",
                          EXIT_SIGNATURE) from None
    out.ok("signature verified against the key shipped with this installation")


def _download(fetch: Fetcher, plan: _Plan, out: _Writer) -> bytes:
    """Laedt das Tarball, prueft Pruefsumme UND Signatur.

    Beides, nicht eines: die Pruefsumme faengt den kaputten Download mit einer Meldung,
    die einem Menschen etwas sagt, die Signatur faengt den ausgetauschten. Die zweite
    ersetzt die erste nicht — sie beantwortet eine andere Frage.
    """
    out.step("Fetching the new version")
    url = f"{plan.base}/dist/talos-{plan.latest}.tar.gz"
    out.note(url)
    payload = _fetch(fetch, url)
    expected = _expected_sha256(fetch, plan)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise UpdateError(
            f"sha256 mismatch — published {expected[:16]}…, downloaded {actual[:16]}…. "
            "Nothing was unpacked, nothing was changed.",
            EXIT_CHECKSUM,
        )
    out.ok(f"sha256 {actual[:16]}…{actual[-8:]} matches the published sum")
    _verify_signature(payload, fetch, plan, out)
    return payload


# --- Auspacken ----------------------------------------------------------------------


def _is_safe_member(member: tarfile.TarInfo) -> bool:
    """Nur Dateien und Verzeichnisse innerhalb des Archivs.

    Ein Tarball, der `../` oder einen Link mitbringt, schreibt sonst dorthin, wo ihn
    niemand erwartet — dieselbe Klasse Fehler, die der Pfad-Floor im Kernel abwehrt.
    """
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    if member.issym() or member.islnk():
        return False
    return member.isfile() or member.isdir()


def _extract(archive: tarfile.TarFile, members: list[tarfile.TarInfo], dest: Path) -> None:
    """`filter="data"` wo vorhanden; die Mitglieder sind ohnehin schon geprueft."""
    try:
        archive.extractall(dest, members=members, filter="data")
    except TypeError:  # Python ohne `filter`-Parameter
        archive.extractall(dest, members=members)


def _archive_root(unpacked: Path) -> Path:
    """Ein einzelnes Wurzelverzeichnis wird uebersprungen — wie `--strip-components=1`."""
    entries = sorted(unpacked.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return unpacked


def _unpack(payload: bytes, plan: _Plan, workdir: Path) -> None:
    """Entpackt in ein Nebenverzeichnis und verschiebt den Baum an seinen Platz."""
    unpacked = workdir / "unpacked"
    unpacked.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            safe = [member for member in members if _is_safe_member(member)]
            if len(safe) != len(members):
                raise UpdateError(
                    "the archive contains paths outside its own directory. Refusing to unpack.",
                    EXIT_FAILED,
                )
            _extract(archive, safe, unpacked)
    except tarfile.TarError as error:
        # Pruefsumme stimmte, Inhalt trotzdem unbrauchbar -> die Veroeffentlichung ist kaputt.
        raise UpdateError(f"the archive could not be read: {error}", EXIT_FAILED) from error
    shutil.move(str(_archive_root(unpacked)), str(plan.staging))


# --- Neuer Baum: bauen und beweisen -------------------------------------------------


def _prepare(run: Runner, plan: _Plan, out: _Writer) -> None:
    """Eigenes venv im NEUEN Baum. Die laufende Installation wird dabei nicht angefasst."""
    out.step("Creating an isolated Python environment for the new tree")
    if run([sys.executable, "-m", "venv", str(plan.staging / ".venv")], plan.staging) != 0:
        raise UpdateError("venv failed — is python3-venv installed?")
    venv_python = plan.staging / VENV_PYTHON
    for name in ("requirements.txt", "requirements-dev.txt"):
        if not (plan.staging / name).is_file():
            continue
        argv = [str(venv_python), "-m", "pip", "install", "--quiet", "-r", name]
        if run(argv, plan.staging) != 0:
            raise UpdateError(f"{name} could not be installed. Nothing was switched.")
    out.ok(f".venv in {plan.staging.name} — {plan.prefix} untouched so far")


def _prove(run: Runner, plan: _Plan, out: _Writer) -> None:
    """Beide Suiten im NEUEN Baum. Faellt eine durch, wird nicht umgeschaltet.

    Fehlt eine der beiden, ist das selbst der Abbruchgrund: eine Version, die ihre
    Angriffs-Suite nicht mitliefert, kann nichts beweisen — und der Installer haette
    sie ebenfalls nie ohne Beweis fertig installiert.
    """
    venv_python = str(plan.staging / VENV_PYTHON)
    proofs = (
        ("test suite", "tests", [venv_python, "-m", "pytest", "-q"]),
        ("adversarial suite", "redteam.py", [venv_python, "redteam.py"]),
    )
    for label, required, argv in proofs:
        out.step(f"Running the {label} in {plan.staging.name}")
        if not (plan.staging / required).exists():
            raise UpdateError(
                f"{plan.latest} ships no {required} — that alone is a reason to stop.",
                EXIT_PROOF,
            )
        if run(argv, plan.staging) != 0:
            raise UpdateError(
                f"the {label} failed in {plan.latest}. Not switching.", EXIT_PROOF
            )
        out.ok(f"{label} green")


# --- Zustand, Umschalten, Meldung ---------------------------------------------------


def _kopierfehler(name: str, fehler: shutil.Error) -> str:
    """Aus `shutil.Error` einen Satz machen, der den Pfad nennt statt ihn zu vergraben.

    `shutil.Error` traegt eine Liste von `(quelle, ziel, grund)`-Tripeln. Ungefiltert
    landet die als `repr` in der Meldung: auf dem Pi waren das sechs Tripel mit vollen
    Pfaden in einer einzigen Zeile, und der eigentliche Grund — ein Link ins Leere —
    stand darin dreimal, ohne dass ihn jemand gesucht haette.
    """
    tripel = fehler.args[0] if fehler.args and isinstance(fehler.args[0], list) else []
    pfade = [str(eintrag[0]) for eintrag in tripel if isinstance(eintrag, (list, tuple))]
    zeilen = [f"{name} could not be copied into the new tree."]
    for pfad in pfade[:3]:
        zeilen.append(f"  {pfad}")
    if len(pfade) > 3:
        zeilen.append(f"  … and {len(pfade) - 3} more")
    zeilen.append(
        "Nothing was switched. Remove or repair those entries and run the update again."
    )
    return " ".join(zeilen) if len(zeilen) == 2 else "\n    ".join(zeilen)


def _carry_state(plan: _Plan, out: _Writer) -> None:
    """Kopiert die Pfade des Betreibers in den neuen Baum — kopiert, nicht verschoben.

    Erst nach den Beweisen: ein verworfener Baum soll das Event-Log nie gesehen haben.
    """
    out.step("Carrying your own files over")
    carried: list[str] = []
    foreign: list[tuple[str, int]] = []
    for name in STATE_PATHS:
        source = plan.prefix / name
        if not source.exists():
            continue
        destination = plan.staging / name
        if source.is_dir():
            # ⚠️ `symlinks=True` ist hier kein Detail. Ohne das FOLGT `copytree` jedem
            # Link und kopiert dessen Ziel — ein Link, dessen Ziel nicht mehr existiert,
            # ist damit ein `FileNotFoundError`, und das Update bricht NACH den Beweisen
            # ab. Genau so am 06.08. auf dem Pi: zwei tote Links aus einem frueheren
            # Testlauf im `workspace/` liessen ein sonst gruenes Update scheitern.
            # Den Link zu kopieren ist ausserdem das treuere Abbild: ein Link, der aus
            # dem Baum herauszeigt, wuerde sonst fremden Inhalt HEREINholen.
            try:
                shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
            except shutil.Error as fehler:
                raise UpdateError(_kopierfehler(name, fehler)) from fehler
        else:
            shutil.copy2(source, destination)
        carried.append(name)
        # ⚠️ `copy2` nimmt den Modus mit, den EIGENTUEMER nicht — das kann nur root.
        # Gehoerte die Datei bewusst jemand anderem (die haerteste Einrichtung: die
        # Konfiguration gehoert root, der Agent darf sie nur lesen), dann gehoert die
        # KOPIE danach dem Agenten, und die Grenze waere nach einem Update still weg.
        if source.stat().st_uid != os.getuid():
            foreign.append((name, source.stat().st_uid))
    # ⚠️ Das `else` hing frueher am `for` statt am `if` — und ein `for/else` ohne `break`
    # laeuft IMMER. Der Lauf auf dem Pi meldete deshalb "data · workspace · SOUL.md —
    # copied" und direkt darunter "nothing of yours found next to the installation".
    # Zwei Zeilen, die einander widersprechen, in genau dem Schritt, der belegen soll,
    # dass das Eigentum des Betreibers mitgekommen ist.
    if carried:
        out.ok(f"{' · '.join(carried)} — copied, not moved; the old tree stays complete")
    else:
        out.note("nothing of yours found next to the installation")
    for name, owner in foreign:
        out.note(
            f"{name} belonged to uid {owner}; the copy belongs to this user. If that "
            f"ownership WAS the boundary, restore it: chown {owner} <new tree>/{name}"
        )


def _switch(plan: _Plan) -> None:
    """Zwei Umbenennungen. Scheitert die zweite, wird die erste sofort zurueckgedreht."""
    plan.prefix.rename(plan.retired)
    try:
        plan.staging.rename(plan.prefix)
    except OSError:
        plan.retired.rename(plan.prefix)
        raise


@dataclass(frozen=True)
class _Liveness:
    """Laeuft aus dieser Installation ein Prozess? `True`, `False` — oder `None`.

    ⚠️ Die dritte Antwort ist der ganze Punkt. Vorher gab es nur zwei, und „ich konnte es
    nicht feststellen" fiel mit „es laeuft nichts" zusammen. Am 06.08. meldete ein Update
    auf einem Pi „Nothing was started, nothing was scheduled", waehrend der systemd-Dienst
    lief und den alten Code im Speicher hielt — der Agent hatte nur seit Stunden keine
    Nachricht bekommen, und gemessen wurde die Schreibzeit des Event-Logs in einem
    Fuenf-Minuten-Fenster. Ein Waechter im Leerlauf sah damit aus wie ein toter.
    """

    running: bool | None
    detail: str


def _prefix_processes(prefix: Path) -> _Liveness:
    """Sucht Prozesse, deren Arbeitsverzeichnis oder Programm in dieser Installation liegt.

    Warum das und keine Herzschlag-Datei: ein Herzschlag muesste von der LAUFENDEN Fassung
    geschrieben werden — und die ist beim Update per Definition die alte, die ihn noch
    nicht kennt. Ein Verfahren, das erst ab dem uebernaechsten Update trUEge, beantwortet
    die Frage nicht, die hier gestellt wird. Ein Prozess dagegen ist da oder nicht.

    Warum nicht `systemctl`: ein Dienst ist ein Prozess, ein Prozess nicht immer ein
    Dienst. Wer Talos von Hand im Terminal gestartet hat, faende sich in keiner Unit.
    """
    proc = Path("/proc")
    if proc.is_dir():
        treffer: list[str] = []
        blind = 0
        marke = str(prefix)
        for eintrag in proc.iterdir():
            if not eintrag.name.isdigit() or eintrag.name == str(os.getpid()):
                continue
            # ⚠️ `cmdline` ZUERST, und es traegt die Antwort fast immer. Der erste Entwurf
            # sah nur `cwd` und `exe` an — die gehoeren dem jeweiligen Nutzer, und auf
            # jedem echten Linux laufen Dutzende Prozesse als root. „Nicht lesbar" waere
            # damit der Normalfall gewesen und die Antwort IMMER „weiss ich nicht":
            # ehrlich, aber wertlos. `cmdline` ist fuer jeden lesbar, und der Dienst
            # startet ueber `<prefix>/.venv/bin/python` — der Pfad steht darin.
            gesehen = False
            try:
                roh = (eintrag / "cmdline").read_bytes()
                gesehen = True
                # ⚠️ NUR `argv[0]`, nicht die ganze Zeile. Der erste Entwurf suchte den
                # Pfad irgendwo in der Kommandozeile — und zaehlte damit jedes `grep`,
                # jeden Editor und, beim Nachmessen auf dem Pi, den eigenen Pruefbefehl
                # mit: gegen einen NACHWEISLICH toten Baum meldete er „laeuft", weil der
                # Pfad als Argument darin vorkam. Wer ein Programm AUS dieser
                # Installation startet, hat sie in `argv[0]`; wer sie nur erwaehnt, nicht.
                argv0 = roh.split(b"\0", 1)[0].decode("utf-8", "replace")
                if argv0.startswith(f"{marke}/"):
                    treffer.append(eintrag.name)
                    continue
            except OSError:
                pass
            # `cwd`/`exe` fangen den Fall, den `cmdline` nicht zeigt: von INNERHALB der
            # Installation mit relativem Pfad gestartet (`.venv/bin/python -m talos`).
            for was in ("cwd", "exe"):
                try:
                    ziel = (eintrag / was).resolve()
                except (OSError, RuntimeError):
                    continue
                gesehen = True
                if ziel == prefix or prefix in ziel.parents:
                    treffer.append(eintrag.name)
                    break
            if not gesehen:
                blind += 1                       # weder Kommandozeile noch Pfade lesbar
        if treffer:
            return _Liveness(True, f"pid {' · '.join(sorted(set(treffer))[:5])}")
        if blind:
            # ⚠️ Nichts gefunden ist NICHT dasselbe wie nichts da, solange Prozesse
            # voellig ungelesen blieben. Genau hier entstuende sonst wieder ein
            # falsches „nein".
            return _Liveness(None, f"{blind} process(es) could not be inspected at all")
        return _Liveness(False, "no process runs from this directory")

    # Kein /proc (macOS, BSD). `ps` sieht die Kommandozeile, und der Dienst startet
    # ueber `<prefix>/.venv/bin/python` — der Pfad steht darin.
    try:
        fertig = subprocess.run(
            ["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as fehler:
        return _Liveness(None, f"no way to inspect processes here ({fehler})")
    if fertig.returncode != 0:
        return _Liveness(None, "`ps` did not answer")
    # Auch hier nur das Programm, nicht die Argumente — sonst zaehlt jedes `grep`, das
    # den Pfad erwaehnt, als laufende Instanz. `ps` liefert `pid programm argumente…`.
    marke = f"{prefix}/"
    pids = []
    for zeile in fertig.stdout.splitlines():
        felder = zeile.split()
        if len(felder) < 2 or felder[0] == str(os.getpid()):
            continue
        if felder[1].startswith(marke):
            pids.append(felder[0])
    if pids:
        return _Liveness(True, f"pid {' · '.join(pids[:5])}")
    return _Liveness(False, "no process names this directory in its command line")


def _looks_live(prefix: Path) -> _Liveness:
    """Prozesse zuerst; das Event-Log nur noch, wenn die Prozessfrage offen blieb."""
    lage = _prefix_processes(prefix)
    if lage.running is not None:
        return lage
    log = prefix / "data" / "eventlog.db"
    if log.is_file() and (time.time() - log.stat().st_mtime) < LIVE_WINDOW_S:
        return _Liveness(True, "data/eventlog.db was written moments ago")
    # ⚠️ Bleibt `None`. Ein stilles Event-Log beweist nichts — der Agent kann seit
    # Stunden im Leerlauf sein. Frueher stand hier `False`, und das war die Luege.
    return lage


def _report_changes(fetch: Fetcher, plan: _Plan, out: _Writer) -> None:
    """Zeigt vor dem Umschalten, was sich aendert — inklusive der Aenderungsliste."""
    out.step("What changes")
    out.note(f"{plan.running}  ->  {plan.latest}")
    out.note("this replaces the security kernel itself — the suites below are")
    out.note("the only reason to believe the new one still holds.")
    body = _optional_text(fetch, f"{plan.base}/dist/CHANGELOG-{plan.latest}.md")
    if body is None:
        out.note(f"no change list published at {plan.base}/dist/CHANGELOG-{plan.latest}.md")
        return
    lines = [line for line in body.splitlines() if line.strip()]
    for line in lines[:CHANGELOG_MAX_LINES]:
        out.note(line)
    if len(lines) > CHANGELOG_MAX_LINES:
        out.note(f"… {len(lines) - CHANGELOG_MAX_LINES} more lines in CHANGELOG-{plan.latest}.md")


def _report_done(plan: _Plan, lage: _Liveness, out: _Writer) -> None:
    """Schlussmeldung: was jetzt gilt, was mit einem laufenden Prozess ist, der Rueckweg.

    ⚠️ Der erste Satz hiess frueher ausnahmslos „and it is not running" — auch dann, wenn
    ein Dienst lief. Gemeint war „das Update hat nichts gestartet", gelesen wurde „hier
    laeuft nichts". Was der Satz sagt, haengt jetzt daran, was gemessen wurde.
    """
    out.line("")
    if lage.running is True:
        out.line(f"  Talos in {plan.prefix} is now {plan.latest} — and an instance is running.")
    elif lage.running is False:
        out.line(f"  Talos in {plan.prefix} is now {plan.latest} — and it is not running.")
    else:
        out.line(f"  Talos in {plan.prefix} is now {plan.latest}.")
    out.line("")
    if lage.running is True:
        out.note(
            f"An instance is running from this installation ({lage.detail}). It still "
            f"holds the OLD code ({plan.running}) in memory — the switch on disk does not "
            "reach it. Stop it and start it yourself."
        )
    elif lage.running is False:
        out.note(f"Nothing was started, nothing was scheduled ({lage.detail}).")
        out.note("The switch is yours.")
    else:
        # ⚠️ Kein „nichts laeuft". Diese Zeile ist der Grund fuer die ganze Aenderung.
        out.note(
            f"I could not tell whether an instance is running here — {lage.detail}. "
            f"If one is, it still holds the OLD code ({plan.running}) in memory and will "
            "keep doing so until you restart it."
        )
        out.note("The update itself started and scheduled nothing.")
    out.line("")
    out.note(f"Roll back:  rm -rf {plan.prefix} && mv {plan.retired} {plan.prefix}")
    out.line("")


# --- Ablauf -------------------------------------------------------------------------


def _require_absent(path: Path) -> None:
    if path.exists():
        raise UpdateError(
            f"{path} already exists. Move or remove it — I do not touch what is not mine.",
            EXIT_OCCUPIED,
        )


def _make_plan(opts: _Options, running: str, latest: str) -> _Plan:
    prefix = opts.prefix
    return _Plan(
        base=opts.base,
        prefix=prefix,
        staging=prefix.parent / f"{prefix.name}.new-{latest}",
        retired=prefix.parent / f"{prefix.name}.old-{running}",
        running=running,
        latest=latest,
    )


def _perform_update(plan: _Plan, fetch: Fetcher, run: Runner, out: _Writer) -> int:
    """Laden, beweisen, umschalten — in dieser Reihenfolge und nur ganz oder gar nicht."""
    if not plan.prefix.is_dir():
        raise UpdateError(f"no installation at {plan.prefix}.", EXIT_FAILED)
    _require_absent(plan.staging)
    _require_absent(plan.retired)
    payload = _download(fetch, plan, out)
    _report_changes(fetch, plan, out)
    workdir = Path(tempfile.mkdtemp(dir=str(plan.prefix.parent), prefix=".talos-update-"))
    try:
        _unpack(payload, plan, workdir)
        _prepare(run, plan, out)
        _prove(run, plan, out)
        # ⚠️ `_carry_state` gehoert INNERHALB des Verwerfen-Blocks. Stand es davor,
        # blieb nach einem Fehlschlag hier der fertige Staging-Baum liegen — und
        # `_require_absent` lehnte damit JEDEN weiteren Versuch ab, mit einer Meldung
        # ueber einen belegten Pfad, die den urspruenglichen Grund nicht mehr nannte.
        # Auf dem Pi war der zweite Anlauf deshalb erst nach einem `rm -rf` von Hand
        # moeglich. Lieber zehn Minuten venv neu bauen als eine Sackgasse hinterlassen.
        _carry_state(plan, out)
    except Exception:
        # Der neue Baum wird verworfen; die alte Installation hat bis hierher niemand angefasst.
        discarded = plan.staging.exists()
        shutil.rmtree(plan.staging, ignore_errors=True)
        prefix_note = f"{plan.prefix} is untouched"
        out.note(f"discarded {plan.staging.name} — {prefix_note}" if discarded else prefix_note)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    looks_live = _looks_live(plan.prefix)
    _switch(plan)
    _report_done(plan, looks_live, out)
    return EXIT_OK


def run_update(
    argv: list[str] | None = None,
    *,
    stdout=None,
    http: Fetcher | None = None,
    runner: Runner | None = None,
) -> int:
    """Prueft und (ohne `--check`) vollzieht ein Update. Rueckgabe = Exit-Code.

    `--check` fragt ausschliesslich `<base>/dist/latest.txt` ab und laedt nichts.
    Ohne `--check` laeuft der volle Weg aus dem Modul-Docstring. Nichts an diesem Ablauf
    ist automatisch: es gibt keinen Zeitplan, keinen stillen Modus und keinen Neustart.
    """
    opts = _parse_args(list(sys.argv[1:] if argv is None else argv))
    out = _Writer(stdout if stdout is not None else sys.stdout)
    fetch = http or _urlopen_fetch
    run = runner or _subprocess_runner
    running = __version__
    try:
        latest = _published_version(fetch, opts.base)
    except UpdateError as error:
        out.fail(str(error))
        return error.code
    out.step("Checking the published version")
    out.note(f"running {running} · published {latest}")
    if latest == running:
        out.ok(f"already on {running} — nothing to do.")
        return EXIT_OK
    if opts.check:
        out.ok(f"update available: {running}  ->  {latest}")
        out.note("nothing was downloaded. Run the same command without --check to fetch it.")
        return EXIT_OK
    try:
        return _perform_update(_make_plan(opts, running, latest), fetch, run, out)
    except UpdateError as error:
        out.fail(str(error))
        return error.code
    except OSError as error:
        # Datei-/Rechtefehler enden wie jeder andere Abbruch: mit einer Meldung und
        # ohne halbfertigen Zustand — `_switch` dreht seine eigene Umbenennung zurueck.
        out.fail(f"the filesystem refused a step: {error}")
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - Verdrahtung im Einstiegspunkt folgt separat
    raise SystemExit(run_update())
