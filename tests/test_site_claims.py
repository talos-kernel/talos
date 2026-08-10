"""Public claims stay verifiable and do not expose environment-specific details."""
from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site" / "index.html"

# `site/` traegt `export-ignore` und ist damit nicht Teil der Auslieferung: der Nutzer
# installiert den Agenten, nicht die Landingpage. Ohne diesen Schalter scheitern die drei
# Tests im ausgepackten Tarball — ausgerechnet in dem Lauf, den der Installer vorfuehrt,
# um Vertrauen zu erzeugen. Uebersprungen wird nur, was hier nachweislich fehlt; im
# Repository existiert `site/` immer, dort laufen sie also.
pytestmark = pytest.mark.skipif(
    not SITE.exists(),
    reason="site/ gehoert nicht zur Auslieferung — diese Pruefungen laufen im Repository.",
)


COUNT_PATTERNS = (
    r"\b\d[\d,]*\s+(?:(?:passing|passed|green|collected)\s+)?(?:unit\s+)?tests?\b",
    r"\b\d+(?:\s+\d+)?\s+(?:(?:passing|passed|green)\s+)?(?:adversarial|red\s+team)\b",
    r"(?:adversarial|red\s+team)[^\n]{0,24}\b\d+(?:\s+\d+)?\b",
    r"\btests?\s+\d[\d,]*\b",
)


def _normalise_claim_text(raw: str) -> str:
    text = html.unescape(unquote(raw))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[/_-]+", " ", text)


def test_public_surfaces_do_not_claim_exact_green_counts() -> None:
    surfaces = (
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        SITE,
        ROOT / "site" / "docs" / "index.html",
    )
    text = _normalise_claim_text(
        "\n".join(path.read_text(encoding="utf-8") for path in surfaces)
    )
    for pattern in COUNT_PATTERNS:
        assert re.search(pattern, text, re.IGNORECASE) is None, pattern


@pytest.mark.parametrize("claim", (
    "<b>1582</b>unit tests",
    "<b>164/164</b>adversarial",
    "https://img.shields.io/badge/tests-1582-green.svg",
    "1582 passing tests",
    "164 passing adversarial cases",
))
def test_exact_count_normalisation_covers_markup_and_badges(claim: str) -> None:
    text = _normalise_claim_text(claim)
    assert any(re.search(pattern, text, re.IGNORECASE) for pattern in COUNT_PATTERNS)


def test_the_site_lists_every_tool() -> None:
    """Wer ein Werkzeug ergaenzt, denkt an das Manifest — nie an die Website."""
    from talos.tools import default_manifest

    echte = default_manifest().tools
    text = SITE.read_text(encoding="utf-8")
    namen = sorted(echte) if isinstance(echte, (dict, set, frozenset)) else sorted(
        spec.name for spec in echte
    )
    fehlend = [name for name in namen if f"<code>{name}</code>" not in text]
    assert not fehlend, f"Die Seite listet diese Werkzeuge nicht: {fehlend}"


README = ROOT / "README.md"


def test_the_readme_lists_every_tool() -> None:
    """Wer ein Werkzeug ergaenzt, denkt an das Manifest — nie an das README."""
    from talos.tools import default_manifest

    echte = default_manifest().tools
    namen = sorted(echte) if isinstance(echte, (dict, set, frozenset)) else sorted(
        spec.name for spec in echte
    )
    text = README.read_text(encoding="utf-8")
    fehlend = [name for name in namen if f"`{name}`" not in text]
    assert not fehlend, f"Das README nennt diese Werkzeuge nicht: {fehlend}"


def test_repository_public_hygiene_check_passes() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/check-public-hygiene.py"],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- SECURITY.md muss stimmen, sonst ist sie schlimmer als keine --------------------


def _security() -> str:
    return (Path(__file__).resolve().parent.parent / "SECURITY.md").read_text(encoding="utf-8")


def test_the_security_policy_pins_the_same_key_as_the_updater() -> None:
    """Drei Orte, ein Schluessel: Updater, Installer, Sicherheitsrichtlinie.

    Driftet einer, prueft ein Leser gegen etwas anderes als die Software — und glaubt
    danach, verifiziert zu haben.
    """
    from talos import updater

    assert updater.RELEASE_PUBLIC_KEY in _security(), "SECURITY.md nennt einen anderen Schluessel"


def test_the_security_policy_names_a_reporting_channel_and_no_address() -> None:
    """Der Meldeweg ist GitHubs privates Formular — bewusst KEINE Mailadresse.

    Eine veroeffentlichte Adresse ist ein dauerhaftes Ziel und ein dauerhaftes Stueck
    persoenlicher Daten. Das Formular gibt denselben privaten Kanal ohne beides.
    """
    text = _security()
    assert "security/advisories/new" in text, "kein Meldeweg genannt"
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "SECURITY.md nennt eine Mailadresse"


def test_the_security_policy_states_the_limits_it_cannot_fix() -> None:
    """Die ehrliche Haelfte ist der Grund, warum die Datei etwas wert ist.

    Eine Sicherheitsrichtlinie, die nur Versprechen enthaelt, laesst den Leser genau die
    Risiken uebersehen, die kein Patch nimmt.
    """
    text = _security()
    for pflicht in ("trust chain", "bubblewrap", "release key", "Approval fatigue"):
        assert pflicht in text, f"SECURITY.md verschweigt: {pflicht}"
