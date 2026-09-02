"""Abhaengigkeiten kommen festgenagelt und gehasht — oder gar nicht.

`requirements.txt` nennt Bereiche (`requests>=2.31`): die Absicht des Betreuers, nicht
das, was auf einem Rechner landet. Was landet, steht in `requirements.lock` — jede
Version festgenagelt, jede Datei mit SHA-256, aufgeloest fuer alle Plattformen
(`uv pip compile --universal`). Installer, Updater und CI installieren daraus mit
`--require-hashes`. Erst damit deckt die Signatur ueber das Archiv auch die Pakete,
die pip DANACH laedt; ohne Lock endete der Beweis beim Archiv, und `pip install
-r requirements.txt` holte, was PyPI in dem Moment anbot (CWE-494, gefunden im
Review der llmman-Integration).

Neu erzeugen — im Wurzelverzeichnis, mit RELATIVEN Pfaden, denn der Kopf des Locks
uebernimmt die Kommandozeile wortwoertlich und darf keinen Maschinenpfad tragen:

    uv pip compile --universal --generate-hashes --python-version 3.11 \\
        requirements.txt -o requirements.lock
    uv pip compile --universal --generate-hashes --python-version 3.11 \\
        -c requirements.lock requirements-dev.txt -o requirements-dev.lock

Der zweite Aufruf nimmt den ersten als Constraint: was beide Locks nennen, nennen sie
in derselben Version — sonst baute der zweite `pip install` den ersten wieder um.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from talos import updater

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = (ROOT / "requirements.txt", ROOT / "requirements.lock")
DEV = (ROOT / "requirements-dev.txt", ROOT / "requirements-dev.lock")
INSTALLER = ROOT / "site" / "install.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}\b")
_MACHINE_PATH = re.compile(r"/Users/|/home/|[A-Za-z]:\\")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(lock: Path) -> dict[str, list[tuple[Version, int]]]:
    """Name → [(Version, Anzahl Hashes)] je Eintrag; Fortsetzungszeilen zusammengezogen."""
    text = lock.read_text(encoding="utf-8").replace("\\\n", " ")
    pins: dict[str, list[tuple[Version, int]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match is not None, f"{lock.name}: unpinned line: {line[:80]}"
        pins.setdefault(_canonical(match.group(1)), []).append(
            (Version(match.group(2)), len(_HASH.findall(line)))
        )
    return pins


def _declared(plain: Path) -> list[Requirement]:
    lines = (
        line.split("#", 1)[0].strip()
        for line in plain.read_text(encoding="utf-8").splitlines()
    )
    return [Requirement(line) for line in lines if line]


# --- Der Lock selbst -----------------------------------------------------------------


@pytest.mark.parametrize("plain, lock", [RUNTIME, DEV], ids=["runtime", "dev"])
def test_every_lock_entry_is_pinned_with_at_least_one_hash(plain: Path, lock: Path) -> None:
    pins = _pins(lock)
    assert pins, f"{lock.name} is empty"
    unhashed = [name for name, entries in pins.items() for _, hashes in entries if hashes == 0]
    assert unhashed == []


@pytest.mark.parametrize("plain, lock", [RUNTIME, DEV], ids=["runtime", "dev"])
def test_the_lock_satisfies_every_declared_range(plain: Path, lock: Path) -> None:
    """Der Lock ist die Aufloesung der Absicht — nicht eine zweite, abweichende Absicht."""
    pins = _pins(lock)
    for requirement in _declared(plain):
        entries = pins.get(_canonical(requirement.name))
        assert entries, f"{lock.name} does not pin {requirement.name}"
        for version, _ in entries:
            assert requirement.specifier.contains(version, prereleases=False), (
                f"{requirement.name}=={version} is outside {requirement.specifier}"
            )


def test_the_dev_lock_agrees_with_the_runtime_lock_on_shared_pins() -> None:
    runtime, dev = _pins(RUNTIME[1]), _pins(DEV[1])
    for name in runtime.keys() & dev.keys():
        assert {v for v, _ in runtime[name]} == {v for v, _ in dev[name]}, name


@pytest.mark.parametrize("lock", [RUNTIME[1], DEV[1]], ids=["runtime", "dev"])
def test_the_lock_carries_no_machine_path(lock: Path) -> None:
    """Der Kopf zitiert die Kommandozeile — erzeugt mit relativen Pfaden, oder gar nicht."""
    assert _MACHINE_PATH.search(lock.read_text(encoding="utf-8")) is None


# --- Wer den Lock benutzt -------------------------------------------------------------


def test_the_updater_installs_from_the_locks_under_require_hashes(tmp_path: Path) -> None:
    for name in ("requirements.lock", "requirements-dev.lock", "requirements.txt"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    assert updater.dependency_installs(tmp_path) == (
        ("requirements.lock", ["--require-hashes", "-r", "requirements.lock"]),
        ("requirements-dev.lock", ["--require-hashes", "-r", "requirements-dev.lock"]),
    )


def test_the_updater_refuses_a_tree_that_only_names_ranges(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests>=2.31\n", encoding="utf-8")
    with pytest.raises(updater.UpdateError, match="requirements.lock"):
        updater.dependency_installs(tmp_path)


def test_the_updater_needs_nothing_for_a_tree_without_dependencies(tmp_path: Path) -> None:
    assert updater.dependency_installs(tmp_path) == ()


@pytest.mark.skipif(not INSTALLER.exists(), reason="site/ gehoert nicht zur Auslieferung")
def test_the_installer_installs_from_the_locks_and_upgrades_nothing_unpinned() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert '--require-hashes -r "$PREFIX/requirements.lock"' in text
    assert '--require-hashes -r "$PREFIX/requirements-dev.lock"' in text
    assert "-r \"$PREFIX/requirements.txt\"" not in text
    assert "--upgrade pip" not in text


@pytest.mark.skipif(not WORKFLOW.exists(), reason="kein Workflow in diesem Baum")
def test_ci_installs_from_the_locks() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock -r requirements-dev.lock" in text
    assert "-r requirements.txt" not in text
