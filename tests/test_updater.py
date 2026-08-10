"""Update: nur mit Beweis, nur neben der alten Installation, nie automatisch.

Alles laeuft gegen gefaelschtes HTTP, einen gefaelschten Runner und ein `tmp_path` —
kein Netz, keine echte Installation, kein Subprozess. Ein Updater, der sich nur mit
Netzzugang pruefen laesst, waere selbst ein Risiko.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import tarfile
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from talos import __version__ as RUNNING
from talos import updater
from talos.updater import run_update

BASE = "https://talos.test"
NEW = "9.9.9"

# Ein eigenes Schluesselpaar fuer die Tests. Der echte private Schluessel liegt beim
# Betreiber und hat in keinem Repository etwas zu suchen — auch nicht in einem Test.
_TEST_KEY = Ed25519PrivateKey.generate()
_TEST_PUBLIC = base64.b64encode(
    _TEST_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode()


@pytest.fixture(autouse=True)
def _pin_the_test_key(monkeypatch):
    """Jeder Test hier prueft gegen den TESTschluessel, nie gegen den ausgelieferten."""
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEY", _TEST_PUBLIC)

RELEASE_FILES = {
    "talos/__init__.py": f'__version__ = "{NEW}"\n',
    "tests/test_smoke.py": "def test_smoke() -> None:\n    assert True\n",
    "redteam.py": "raise SystemExit(0)\n",
    "requirements.txt": "requests>=2.31\n",
    "requirements-dev.txt": "pytest>=8.0\n",
}


# --- Attrappen ----------------------------------------------------------------------


class FakeHttp:
    """Kennt nur die veroeffentlichten URLs und merkt sich, was wirklich geholt wurde."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = dict(pages)
        self.requested: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.requested.append(url)
        if url not in self.pages:
            raise FileNotFoundError(url)
        return self.pages[url]


def _step_of(argv: list[str]) -> str:
    """Benennt den Schritt anhand der Argument-*Tokens*.

    Bewusst nicht ueber die zusammengesetzte Zeile: `tmp_path` von pytest enthaelt selbst
    das Wort "pytest", ein Substring-Treffer wuerde also den venv-Schritt scheitern lassen
    und der Test pruefte etwas anderes als er behauptet.
    """
    if "venv" in argv:
        return "venv"
    if "pip" in argv:
        return "pip"
    if "pytest" in argv:
        return "pytest"
    if any(part.endswith("redteam.py") for part in argv):
        return "redteam"
    return "unknown"


class FakeRunner:
    """Fuehrt nichts aus, merkt sich die Aufrufe und laesst einen Schritt scheitern."""

    def __init__(self, failing: str | None = None) -> None:
        self.failing = failing
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv, cwd) -> int:
        recorded = [str(part) for part in argv]
        self.calls.append((recorded, Path(cwd)))
        return 1 if self.failing == _step_of(recorded) else 0

    @property
    def steps(self) -> list[str]:
        return [_step_of(argv) for argv, _ in self.calls]

    @property
    def commands(self) -> list[str]:
        return [" ".join(argv) for argv, _ in self.calls]


def _tarball(files: dict[str, str], *, root: str = f"talos-{NEW}") -> bytes:
    """Baut eine Auslieferung wie die echte: ein Wurzelverzeichnis, darin der Baum."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}" if root else name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _publication(
    version: str,
    tarball: bytes | None = None,
    *,
    checksum: str | None = None,
    changelog: str | None = None,
    with_checksum_file: bool = True,
    with_signature: bool = True,
    signature: bytes | None = None,
) -> dict[str, bytes]:
    pages = {f"{BASE}/dist/latest.txt": f"# published\n{version}\n".encode()}
    if tarball is not None:
        pages[f"{BASE}/dist/talos-{version}.tar.gz"] = tarball
        if with_checksum_file:
            digest = checksum or hashlib.sha256(tarball).hexdigest()
            pages[f"{BASE}/dist/talos-{version}.tar.gz.sha256"] = (
                f"{digest}  talos-{version}.tar.gz\n".encode()
            )
        if with_signature:
            roh = signature if signature is not None else _TEST_KEY.sign(tarball)
            pages[f"{BASE}/dist/talos-{version}.tar.gz.sig"] = base64.b64encode(roh)
    if changelog is not None:
        pages[f"{BASE}/dist/CHANGELOG-{version}.md"] = changelog.encode("utf-8")
    return pages


def _installation(tmp_path: Path) -> Path:
    """Eine Installation wie nach `install.sh`: Auslieferung plus Eigentum des Betreibers."""
    prefix = tmp_path / "talos"
    (prefix / "talos").mkdir(parents=True)
    (prefix / "talos" / "__init__.py").write_text(
        f'__version__ = "{RUNNING}"\n', encoding="utf-8"
    )
    (prefix / "tests").mkdir()
    (prefix / "redteam.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (prefix / "talos.env").write_text("TELEGRAM_BOT_TOKEN=geheim\n", encoding="utf-8")
    (prefix / "data").mkdir()
    log = prefix / "data" / "eventlog.db"
    log.write_text("die belege", encoding="utf-8")
    # Standardfall: seit einem Tag kein Lauf. Der "da laeuft noch einer"-Hinweis haengt
    # an der Schreibzeit des Event-Logs und wird in seinem eigenen Test frisch gesetzt.
    stale = time.time() - 24 * 3600
    os.utime(log, (stale, stale))
    (prefix / "workspace").mkdir()
    (prefix / "workspace" / "notiz.txt").write_text("meine arbeit", encoding="utf-8")
    return prefix


def _update(prefix: Path, http, runner=None, *, check: bool = False) -> tuple[int, str]:
    stream = io.StringIO()
    argv = ["--prefix", str(prefix), "--base", BASE]
    if check:
        argv.append("--check")
    code = run_update(argv, stdout=stream, http=http, runner=runner or FakeRunner())
    return code, stream.getvalue()


def _untouched(prefix: Path) -> bool:
    """Die alte Installation steht noch genau so da wie vorher."""
    return (
        (prefix / "talos" / "__init__.py").read_text(encoding="utf-8")
        == f'__version__ = "{RUNNING}"\n'
        and (prefix / "talos.env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=geheim\n"
        and (prefix / "data" / "eventlog.db").read_text(encoding="utf-8") == "die belege"
    )


# --- --check ------------------------------------------------------------------------


def test_check_reports_current_version_and_downloads_nothing(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(RUNNING))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner, check=True)

    assert code == 0
    assert f"already on {RUNNING}" in output
    assert http.requested == [f"{BASE}/dist/latest.txt"]
    assert runner.calls == []


def test_check_reports_available_update_without_fetching_it(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner, check=True)

    assert code == 0
    assert f"update available: {RUNNING}  ->  {NEW}" in output
    assert "nothing was downloaded" in output
    assert http.requested == [f"{BASE}/dist/latest.txt"]
    assert runner.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


# --- Pruefsumme ---------------------------------------------------------------------


def test_wrong_checksum_aborts_before_anything_is_unpacked(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES), checksum="0" * 64))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_CHECKSUM
    assert "sha256 mismatch" in output
    assert runner.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


def test_missing_checksum_file_aborts(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES), with_checksum_file=False))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code != 0
    assert "could not read" in output
    assert runner.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


# --- Beweise im neuen Baum ----------------------------------------------------------


def test_failing_test_suite_prevents_the_switch(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))
    runner = FakeRunner(failing="pytest")

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_PROOF
    assert "the test suite failed" in output
    assert "Not switching" in output
    # Nach dem Fehlschlag laeuft die Angriffs-Suite gar nicht mehr — und nichts wird umbenannt.
    assert "redteam" not in runner.steps
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


def test_failing_adversarial_suite_prevents_the_switch(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))
    runner = FakeRunner(failing="redteam")

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_PROOF
    assert "the adversarial suite failed" in output
    assert "pytest" in runner.steps  # die Test-Suite lief vorher und war gruen
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


def test_new_version_without_adversarial_suite_is_refused(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    without_redteam = {k: v for k, v in RELEASE_FILES.items() if k != "redteam.py"}
    http = FakeHttp(_publication(NEW, _tarball(without_redteam)))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_PROOF
    assert "ships no redteam.py" in output
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


# --- Die Seele gehoert dem Betreiber, nicht der Auslieferung -------------------------


def test_a_named_agent_keeps_its_name_across_an_update(tmp_path: Path) -> None:
    """`SOUL.md` sieht aus wie eine Datei der Auslieferung und ist es nicht: seine erste
    Ueberschrift IST der Name des Agenten, der Rest sein Wesen und seine Sprachregel.
    Kaeme sie aus dem Tarball, wuerde ein Update jede benannte Installation still auf die
    neutrale Fassung zuruecksetzen — der Agent hiesse danach anders, ohne dass irgendwer
    das getippt haette."""
    prefix = _installation(tmp_path)
    (prefix / "SOUL.md").write_text("# ARGOS\n\nDu bist Argos.\n", encoding="utf-8")
    geliefert = dict(RELEASE_FILES, **{"SOUL.md": "# TALOS\n\nYou are Talos.\n"})
    http = FakeHttp(_publication(NEW, _tarball(geliefert)))

    code, _ = _update(prefix, http, FakeRunner())

    assert code == 0
    assert (prefix / "SOUL.md").read_text(encoding="utf-8").startswith("# ARGOS")


def test_an_unnamed_installation_takes_the_delivered_soul(tmp_path: Path) -> None:
    """Die Gegenprobe: wer keine eigene hat, bekommt die ausgelieferte. Sonst haette eine
    frische Installation nach dem ersten Update gar keine Identitaet mehr."""
    prefix = _installation(tmp_path)          # ohne eigene SOUL.md
    geliefert = dict(RELEASE_FILES, **{"SOUL.md": "# TALOS\n\nYou are Talos.\n"})
    http = FakeHttp(_publication(NEW, _tarball(geliefert)))

    code, _ = _update(prefix, http, FakeRunner())

    assert code == 0
    assert (prefix / "SOUL.md").read_text(encoding="utf-8").startswith("# TALOS")


# --- Erfolgsfall --------------------------------------------------------------------


def test_successful_update_switches_and_keeps_the_operators_files(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(
        _publication(NEW, _tarball(RELEASE_FILES), changelog="# 9.9.9\n- kernel change\n")
    )
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)
    retired = tmp_path / f"talos.old-{RUNNING}"

    assert code == 0
    # Der Pfad zeigt auf den neuen Baum ...
    assert (prefix / "talos" / "__init__.py").read_text(encoding="utf-8") == (
        f'__version__ = "{NEW}"\n'
    )
    # ... und das Eigentum des Betreibers ist da, unveraendert.
    assert (prefix / "talos.env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=geheim\n"
    assert (prefix / "data" / "eventlog.db").read_text(encoding="utf-8") == "die belege"
    assert (prefix / "workspace" / "notiz.txt").read_text(encoding="utf-8") == "meine arbeit"
    # Der Rueckweg liegt vollstaendig daneben, inklusive der eigenen Daten von vorher.
    assert _untouched(retired)
    assert not (tmp_path / f"talos.new-{NEW}").exists()
    assert f"rm -rf {prefix} && mv {retired} {prefix}" in output


def test_update_shows_the_change_list_before_switching(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(
        _publication(NEW, _tarball(RELEASE_FILES), changelog="- policy.py: neuer Floor\n")
    )

    code, output = _update(prefix, http)

    assert code == 0
    assert f"{RUNNING}  ->  {NEW}" in output
    assert "policy.py: neuer Floor" in output
    assert "replaces the security kernel" in output
    # Die Aenderungsliste steht vor der Schlussmeldung, nicht danach.
    assert output.index("policy.py: neuer Floor") < output.index("is now 9.9.9")


def test_missing_change_list_is_named_but_does_not_abort(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http)

    assert code == 0
    assert "no change list published" in output


def test_nothing_is_started_and_nothing_is_scheduled(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == 0
    # Der ganze Lauf besteht aus venv, pip, pytest, redteam — nichts startet den Agenten.
    assert set(runner.steps) == {"venv", "pip", "pytest", "redteam"}
    joined = " ".join(runner.commands)
    for word in ("-m talos", "crontab", "systemctl", "launchctl", "nohup", "&"):
        assert word not in joined
    assert "is not running" in output
    assert "The switch is yours." in output


def test_a_running_instance_is_named_but_not_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein gefundener Prozess wird benannt — und trotzdem ruehrt das Update ihn nicht an."""
    prefix = _installation(tmp_path)
    monkeypatch.setattr(
        updater, "_prefix_processes", lambda _: updater._Liveness(True, "pid 4242")
    )
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == 0
    assert "an instance is running" in output
    assert "pid 4242" in output
    assert "still holds the" in output and RUNNING in output
    assert "Stop it and start it yourself" in output
    assert set(runner.steps) == {"venv", "pip", "pytest", "redteam"}  # nichts gestartet


def test_an_idle_service_is_not_reported_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Fehler vom 06.08.: „Nothing was started" bei laufendem Dienst.

    Gemessen wurde nur die Schreibzeit von `data/eventlog.db` in einem Fuenf-Minuten-
    Fenster. Der Dienst lief, hatte aber seit Stunden keine Nachricht bekommen — ein
    Waechter im Leerlauf sah damit aus wie ein toter, und die Schlussmeldung sagte einem
    Betreiber das Gegenteil dessen, was der Fall war.
    """
    prefix = _installation(tmp_path)
    stale = time.time() - 24 * 3600
    os.utime(prefix / "data" / "eventlog.db", (stale, stale))       # seit Stunden still
    monkeypatch.setattr(
        updater, "_prefix_processes", lambda _: updater._Liveness(True, "pid 99")
    )
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http, FakeRunner())

    assert code == 0
    assert "Nothing was started" not in output
    assert "is not running" not in output
    assert "an instance is running" in output


def test_not_knowing_is_said_instead_of_claiming_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die dritte Antwort ist der Kern: „ich konnte es nicht feststellen".

    Sie fiel vorher mit „es laeuft nichts" zusammen. Ein Betreiber, dem das Update sagt,
    es laufe nichts, startet nicht neu — und der alte Kern bleibt im Speicher.
    """
    prefix = _installation(tmp_path)
    monkeypatch.setattr(
        updater, "_prefix_processes", lambda _: updater._Liveness(None, "no /proc here")
    )
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http, FakeRunner())

    assert code == 0
    assert "could not tell whether an instance is running" in output
    assert "no /proc here" in output
    assert "Nothing was started, nothing was scheduled" not in output
    assert "started and scheduled nothing" in output          # was WIRKLICH gilt


def test_a_process_that_cannot_be_inspected_does_not_become_a_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ohne Leserecht auf fremde Prozesse ist „keiner gefunden" kein „keiner da"."""
    prefix = _installation(tmp_path)

    class BlindesProc:
        def is_dir(self) -> bool:
            return True

        def iterdir(self):
            class Eintrag:
                name = "4242"

                def __truediv__(self, _was):
                    raise PermissionError("fremder Nutzer")

            return [Eintrag()]

    monkeypatch.setattr(updater, "Path", lambda p="": BlindesProc() if p == "/proc" else Path(p))
    lage = updater._prefix_processes(prefix)

    assert lage.running is None, "unlesbare Prozesse wurden zu einem Nein"
    assert "could not be inspected" in lage.detail


# --- Feindliche Veroeffentlichung ---------------------------------------------------


def test_published_version_that_is_a_path_is_refused(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    http = FakeHttp({f"{BASE}/dist/latest.txt": b"../../etc\n"})
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_FETCH
    assert "not a plain version string" in output
    assert runner.calls == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]


def test_archive_that_writes_outside_itself_is_refused(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    evil = _tarball({**RELEASE_FILES, "../../evil.txt": "pwned\n"})
    http = FakeHttp(_publication(NEW, evil))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code != 0
    assert "outside its own directory" in output
    assert not (tmp_path / "evil.txt").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["talos"]
    assert _untouched(prefix)


def test_default_fetcher_refuses_a_source_that_is_not_https(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    stream = io.StringIO()

    code = run_update(
        ["--check", "--prefix", str(prefix), "--base", "http://talos.test"], stdout=stream
    )

    assert code == updater.EXIT_FETCH
    assert "not https" in stream.getvalue()


def test_trust_store_uses_certifi_only_when_the_system_has_no_roots(monkeypatch) -> None:
    """The macOS trap this guards against: a python.org build with no CA file at all.

    `ssl.get_default_verify_paths()` returns `cafile=None, capath=None` on such a
    machine, and every HTTPS call fails with CERTIFICATE_VERIFY_FAILED — `curl` works
    fine next to it because it does not share Python's trust store. `certifi` ships
    with `requests` regardless, so it is there to fall back on; this pins that the
    fallback is used, and only then.
    """
    import ssl
    from types import SimpleNamespace

    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile=None, capath=None)
    )
    context = updater._trust_store()
    assert context.verify_mode == ssl.CERT_REQUIRED

    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: SimpleNamespace(cafile="/etc/ssl/cert.pem", capath=None)
    )
    context_with_system_roots = updater._trust_store()
    assert context_with_system_roots.verify_mode == ssl.CERT_REQUIRED


def test_occupied_neighbour_directory_stops_the_update(tmp_path: Path) -> None:
    prefix = _installation(tmp_path)
    (tmp_path / f"talos.new-{NEW}").mkdir()
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_OCCUPIED
    assert "I do not touch what is not mine" in output
    assert runner.calls == []
    assert _untouched(prefix)


def test_state_that_belonged_to_root_is_reported_after_the_copy(tmp_path: Path, monkeypatch) -> None:
    """⚠️ `shutil.copy2` nimmt den Modus mit, den Eigentuemer nicht — das kann nur root.

    Die haerteste Einrichtung ist die, in der die Konfiguration root gehoert und der
    Agent sie nur lesen darf (die entscheidende Grenze). Genau diese Grenze waere
    nach einem Update still verschwunden, weil die Kopie dem Agenten gehoert. Sie wird
    nicht heimlich repariert — sie wird benannt.
    """
    import os

    from talos import updater

    prefix, staging = tmp_path / "alt", tmp_path / "neu"
    (prefix / "talos").mkdir(parents=True)
    staging.mkdir()
    (prefix / "talos.env").write_text("TELEGRAM_BOT_TOKEN=1\n", encoding="utf-8")

    class _Plan:
        pass

    plan = _Plan()
    plan.prefix, plan.staging = prefix, staging

    gesagt: list[str] = []

    class _Out:
        def step(self, text): gesagt.append(text)
        def ok(self, text): gesagt.append(text)
        def note(self, text): gesagt.append(text)
        def fail(self, text): gesagt.append(text)

    # Aus Sicht eines anderen Benutzers gehoert die Datei nicht uns.
    monkeypatch.setattr(os, "getuid", lambda: os.stat(prefix / "talos.env").st_uid + 1)
    updater._carry_state(plan, _Out())

    assert (staging / "talos.env").is_file()
    assert any("restore it: chown" in zeile for zeile in gesagt), gesagt


# --- Signatur: beweist HERKUNFT, nicht nur Unversehrtheit -------------------------------
def test_a_swapped_archive_with_a_matching_checksum_is_still_refused(tmp_path: Path) -> None:
    """Der Angriff, gegen den die Pruefsumme nichts ausrichtet.

    Archiv UND Pruefsumme kommen von derselben Basis-URL. Wer den Server, das CDN oder das
    DNS beherrscht, tauscht beides aus — die Pruefsumme passt danach perfekt und beweist
    nur, dass die Datei heil ankam. Hier liegt ein fremdes Archiv mit korrekter Summe und
    einer Signatur von einem fremden Schluessel; genau so saehe der echte Angriff aus.
    """
    fremd = Ed25519PrivateKey.generate()
    boese = _tarball({**RELEASE_FILES, "talos/__init__.py": '__version__ = "9.9.9"\nimport os\n'})
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, boese, signature=fremd.sign(boese)))
    runner = FakeRunner()

    code, output = _update(prefix, http, runner)

    assert code == updater.EXIT_SIGNATURE
    assert "not published by the holder of the release key" in output
    assert runner.calls == []                       # nichts ausgepackt, nichts gestartet
    assert not list(tmp_path.glob("*.new-*"))


def test_a_missing_signature_is_not_a_reason_to_fall_back(tmp_path: Path) -> None:
    """Weglassen darf nicht helfen — sonst ist die Signatur eine Bitte, keine Bedingung."""
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES), with_signature=False))

    code, output = _update(prefix, http, FakeRunner())

    assert code == updater.EXIT_SIGNATURE
    assert not list(tmp_path.glob("*.new-*"))


def test_an_installation_without_a_pinned_key_refuses_instead_of_trusting(
    tmp_path: Path, monkeypatch
) -> None:
    """Kein Schluessel heisst „nichts kann die Herkunft beweisen", nicht „dann eben ohne".

    Der stille Rueckfall waere der bequemste Angriff ueberhaupt: Schluessel leeren,
    Signatur weglassen, alles laeuft wie frueher weiter.
    """
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEY", "")
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http, FakeRunner())

    assert code == updater.EXIT_SIGNATURE
    assert "nothing can prove" in output


def test_a_signature_that_is_not_base64_is_refused_cleanly(tmp_path: Path) -> None:
    """Muell darf eine Absage geben, keinen Absturz — der Updater laeuft unbeaufsichtigt."""
    prefix = _installation(tmp_path)
    tarball = _tarball(RELEASE_FILES)
    pages = _publication(NEW, tarball)
    pages[f"{BASE}/dist/talos-{NEW}.tar.gz.sig"] = b"kein base64 !!!"

    code, output = _update(prefix, FakeHttp(pages), FakeRunner())

    assert code == updater.EXIT_SIGNATURE
    assert "base64" in output


def test_the_good_case_says_which_key_proved_it(tmp_path: Path) -> None:
    """Der Betreiber soll sehen, dass geprueft wurde — sonst ist es Dekoration."""
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http, FakeRunner())

    assert code == updater.EXIT_OK
    assert "signature verified" in output


def test_the_shipped_key_is_a_real_ed25519_key() -> None:
    """⚠️ Ein Tippfehler hier faellt sonst erst beim ersten echten Update auf.

    Geprueft wird der AUSGELIEFERTE Schluessel, nicht der der Tests — deshalb ohne die
    Fixture-Ersetzung, ueber einen frischen Blick auf das Modul.
    """
    import importlib

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    echt = importlib.import_module("talos.updater").__dict__["RELEASE_PUBLIC_KEY"]
    roh = base64.b64decode(echt)
    assert len(roh) == 32
    Ed25519PublicKey.from_public_bytes(roh)         # wirft, wenn die Form nicht stimmt


# --- Was am 06.08. auf dem Pi wirklich passiert ist ---------------------------------
#
# Ein Update, das Herunterladen, Signatur, 1485 Tests und 149 Angriffsfaelle im neuen
# Baum bestanden hatte, brach im vorletzten Schritt ab: `copytree` folgte zwei toten
# Symlinks im `workspace/`, die ein frueherer Testlauf hinterlassen hatte. Beide Suiten
# gruen, und trotzdem kein Update — der Weg war nie gelaufen, nur seine Teile.


def test_a_dangling_symlink_in_the_workspace_does_not_stop_the_update(
    tmp_path: Path,
) -> None:
    """Ein Link ins Leere ist Muell, kein Grund, ein bewiesenes Update wegzuwerfen."""
    prefix = _installation(tmp_path)
    (prefix / "workspace" / "tot.txt").symlink_to(prefix / "workspace" / "gibt-es-nicht")
    assert not (prefix / "workspace" / "tot.txt").exists()   # genau das ist die Falle
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http, FakeRunner())

    assert code == updater.EXIT_OK
    # Der Link kam als LINK mit, nicht als sein (fehlendes) Ziel.
    assert (prefix / "workspace" / "tot.txt").is_symlink()
    assert (prefix / "workspace" / "notiz.txt").read_text(encoding="utf-8") == "meine arbeit"
    assert f"is now {NEW}" in output


def test_a_symlink_pointing_out_of_the_tree_is_copied_as_a_link(tmp_path: Path) -> None:
    """Sonst holt ein Update fremden Inhalt IN den Baum, den niemand dorthin gelegt hat."""
    draussen = tmp_path / "fremd.txt"
    draussen.write_text("gehoert nicht hierher", encoding="utf-8")
    prefix = _installation(tmp_path)
    (prefix / "workspace" / "zeigt-raus.txt").symlink_to(draussen)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, _ = _update(prefix, http, FakeRunner())

    assert code == updater.EXIT_OK
    kopie = prefix / "workspace" / "zeigt-raus.txt"
    assert kopie.is_symlink(), "der Inhalt wurde hereingezogen statt der Link kopiert"


def test_a_failure_while_carrying_state_leaves_no_tree_that_blocks_the_next_try(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Staging-Baum blieb liegen und `_require_absent` lehnte danach jeden Lauf ab.

    Der zweite Versuch scheiterte damit an einem belegten Pfad — mit einer Meldung, die
    den urspruenglichen Grund nicht mehr nannte. Auf dem Pi war er erst nach einem
    `rm -rf` von Hand wieder moeglich.
    """
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    def platzt(*args, **kwargs):
        raise OSError("die Platte sagt nein")

    monkeypatch.setattr(updater.shutil, "copytree", platzt)
    code, _ = _update(prefix, http, FakeRunner())

    assert code != updater.EXIT_OK
    assert _untouched(prefix), "die laufende Installation wurde angefasst"
    assert not (tmp_path / f"talos.new-{NEW}").exists(), "Sackgasse fuer den naechsten Lauf"


def test_carrying_state_does_not_say_it_found_nothing_after_listing_what_it_found(
    tmp_path: Path,
) -> None:
    """Das `else` hing am `for` statt am `if` — und lief damit immer.

    Der Lauf auf dem Pi meldete "data · workspace · SOUL.md — copied" und direkt
    darunter "nothing of yours found next to the installation". In einem Schritt, der
    genau belegen soll, dass das Eigentum des Betreibers mitgekommen ist.
    """
    prefix = _installation(tmp_path)
    http = FakeHttp(_publication(NEW, _tarball(RELEASE_FILES)))

    code, output = _update(prefix, http, FakeRunner())

    assert code == updater.EXIT_OK
    assert "copied, not moved" in output
    assert "nothing of yours found" not in output


def test_a_process_that_merely_mentions_the_path_is_not_an_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Beim Nachmessen auf dem Pi meldete die Suche einen NACHWEISLICH toten Baum als
    laufend — die PIDs waren die des eigenen Pruefbefehls, dessen Kommandozeile den Pfad
    als Argument trug. Ein `grep`, ein Editor, ein Backup-Lauf haetten dasselbe getan.
    Wer ein Programm AUS der Installation startet, hat sie in `argv[0]`.
    """
    prefix = tmp_path / "talos"
    prefix.mkdir()
    proc = tmp_path / "proc"
    for pid, argv in (("100", [f"{prefix}/.venv/bin/python", "-m", "talos"]),
                      ("200", ["/usr/bin/grep", "-r", "x", str(prefix)])):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    echt = Path
    monkeypatch.setattr(
        updater, "Path", lambda p="": proc if str(p) == "/proc" else echt(p)
    )
    lage = updater._prefix_processes(prefix)

    assert lage.running is True
    assert "100" in lage.detail, "die echte Instanz wurde nicht gefunden"
    assert "200" not in lage.detail, "ein grep auf den Pfad zaehlte als laufende Instanz"
