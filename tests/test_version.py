"""Eine Version, zwei Orte — und ein Test, der sie zusammenhaelt.

Der Installer laeuft, bevor es ein Paket gibt, das er nach seiner Version fragen koennte.
Die Zahl muss deshalb doppelt stehen. Doppelt stehende Zahlen driften: `talos.__version__`
sagte 0.0.1, waehrend `site/install.sh` 0.2.0-alpha auslieferte und das Tarball unter
diesem Namen ablegte. Wer daraus einen Update-Weg baut, vergleicht ab da Aepfel mit Birnen
— und meldet „aktuell", waehrend eine neue Fassung bereitliegt.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import talos

INSTALLER = Path(__file__).resolve().parent.parent / "site" / "install.sh"
_VERSION_LINE = re.compile(r'^VERSION="([^"]+)"', re.MULTILINE)

# Der Installer liegt unter `site/` und traegt damit `export-ignore` — wer aus dem Tarball
# installiert, hat ihn bereits ausgefuehrt und besitzt ihn nicht. Die Kopplung, die dieser
# Test bewacht, entsteht ohnehin beim Veroeffentlichen, also im Repository.
pytestmark = pytest.mark.skipif(
    not INSTALLER.exists(),
    reason="site/install.sh gehoert nicht zur Auslieferung — geprueft wird im Repository.",
)


def test_installer_and_package_agree_on_the_version() -> None:
    match = _VERSION_LINE.search(INSTALLER.read_text(encoding="utf-8"))
    assert match is not None, "install.sh hat keine VERSION-Zeile mehr"
    assert match.group(1) == talos.__version__


def test_the_installer_downloads_the_version_it_announces() -> None:
    """Der Tarball-Pfad muss dieselbe Zahl tragen, sonst laedt der Installer ins Leere."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'talos-${VERSION}.tar.gz' in text


# --- Pfade haengen am Installationsort, nicht an einer Vermutung -------------------
def test_the_durable_directory_is_derived_from_where_the_code_lives() -> None:
    """Ein fester Heimatpfad hier kostet einer zweiten Instanz ihr Gedaechtnis.

    Der Wert stand als `~/talos/talos/data` im Quelltext und funktionierte, solange es
    genau eine Installation gab. Ein Deploy unter anderem Praefix legte die Falle offen:
    anderen Praefix entsteht beim Start ein LEERES `data/`, und der Agent laeuft
    weiter — ohne Event-Log, also ohne Autonomie-Stand, stehende Freigaben, Modellwahl
    und Zeitplaene. Der Fix war einmal von Hand auf dem Pi gesetzt und fehlte im Repo;
    der naechste Deploy haette ihn zurueckgedreht. Deshalb steht er jetzt hier fest.
    """
    from talos import config

    assert config.DATA_DIR == config.INSTALL_DIR / "data"
    assert config.EVENTLOG_DB.parent == config.DATA_DIR
    for db in (config.RECALL_DB, config.TRANSCRIPT_DB, config.SCHEDULE_DB):
        assert db.parent == config.DATA_DIR


# --- Der Installer muss erzwingen, nicht anzeigen -----------------------------------
#
# Bis 0.9.0 rechnete er die Pruefsumme aus, DRUCKTE sie und bat den Leser, sie „mit
# <url> zu vergleichen" — er holte die veroeffentlichte Summe nie und verglich nie. Die
# Signatur pruefte er gar nicht. Der eine Weg, der ein Skript aus dem Netz in eine Shell
# leitet, hatte damit keine Durchsetzung, waehrend `talos update` beides erzwang.


def test_the_installer_fetches_the_published_checksum_and_compares_it() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert ".tar.gz.sha256" in text, "der Installer holt die veroeffentlichte Summe nicht"
    assert '[ "$SUM" = "$PUBLISHED" ]' in text, "er vergleicht sie nicht"
    assert "sha256 mismatch" in text, "eine Abweichung bricht nicht ab"


def test_a_missing_checksum_tool_is_a_refusal_not_a_note() -> None:
    """Frueher stand hier `note "checksum not verified"` — und es lief weiter."""
    text = INSTALLER.read_text(encoding="utf-8")
    assert 'die "no sha256 implementation' in text
    assert "checksum not verified" not in text


def test_the_installer_verifies_the_release_signature() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert ".tar.gz.sig" in text, "der Installer holt die Signatur nicht"
    assert "Ed25519PublicKey" in text, "er prueft sie nicht"
    assert "RELEASE_PUBLIC_KEY" in text


def test_the_installer_pins_the_same_key_as_the_updater() -> None:
    """Zwei Wege, ein Schluessel. Driften sie, prueft einer gegen etwas anderes."""
    import re

    from talos import updater

    text = INSTALLER.read_text(encoding="utf-8")
    treffer = re.search(r'RELEASE_PUBLIC_KEY = "([^"]+)"', text)
    assert treffer is not None, "install.sh nennt keinen Schluessel"
    assert treffer.group(1) == updater.RELEASE_PUBLIC_KEY


def test_nothing_is_unpacked_before_both_proofs() -> None:
    """Die Reihenfolge IST die Sicherheit: entpacken darf erst nach beiden Belegen kommen.

    Der Installer fuehrt danach die Suiten aus dem Archiv aus — ungeprueften Code
    auszupacken ist unschoen, ihn auszufuehren waere der eigentliche Schaden.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    entpacken = text.index("tar -xzf")
    assert text.index('[ "$SUM" = "$PUBLISHED" ]') < entpacken, "Pruefsumme erst nach dem Entpacken"
    assert text.index("Ed25519PublicKey") < entpacken, "Signatur erst nach dem Entpacken"
