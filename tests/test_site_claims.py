"""Die Website darf nichts behaupten, was die Suite nicht deckt.

Diese Seite verkauft genau eine Eigenschaft: dass ihre Aussagen nachpruefbar sind. Der
Installer fuehrt beide Suiten vor den Augen des Nutzers aus, statt um Vertrauen zu bitten.
Eine Zahl auf der Landingpage, die nicht mehr stimmt, ist deshalb kein Schoenheitsfehler,
sondern beschaedigt das einzige Argument.

Genau das war der Zustand: die Seite nannte 578 Tests, waehrend es 619 waren, und sprach
von 296 Zeilen im Kernel, wo 353 stehen — dieselbe falsche Zahl stand auch in `CLAUDE.md`.
So etwas driftet immer, weil niemand nach einem Feature die Landingpage nachzaehlt.

Also zaehlt der Test nach. Er liest die Zahlen aus der Seite und haelt sie gegen die
Wirklichkeit: die Zahl der eingesammelten Tests, die Zahl der Redteam-Faelle, die Laenge
des Kernels. Wer eine davon aendert und die Seite vergisst, erfaehrt es hier.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site" / "index.html"
REDTEAM = ROOT / "redteam.py"
KERNEL = ROOT / "talos" / "policy.py"

# `site/` traegt `export-ignore` und ist damit nicht Teil der Auslieferung: der Nutzer
# installiert den Agenten, nicht die Landingpage. Ohne diesen Schalter scheitern die drei
# Tests im ausgepackten Tarball — ausgerechnet in dem Lauf, den der Installer vorfuehrt,
# um Vertrauen zu erzeugen. Uebersprungen wird nur, was hier nachweislich fehlt; im
# Repository existiert `site/` immer, dort laufen sie also.
pytestmark = pytest.mark.skipif(
    not SITE.exists(),
    reason="site/ gehoert nicht zur Auslieferung — diese Pruefungen laufen im Repository.",
)

# ⚠️ Nicht an `data-count` haengen. Diese Auszeichnung gehoerte zu einer frueheren
# Fassung der Startseite; als sie durch eine andere ersetzt wurde, fand der Test NULL
# Zahlen und meldete „die Seite nennt []" — er behauptete einen Fehler, wo nur seine
# eigene Annahme veraltet war. Gelesen wird deshalb jede Zahl im Dokument: die Frage
# lautet „steht die richtige Zahl auf der Seite", nicht „steht sie in diesem Attribut".
_COUNTER = re.compile(r"\b(\d{2,5})\b")


def _claims() -> list[int]:
    text = SITE.read_text(encoding="utf-8")
    return [int(value) for value in _COUNTER.findall(text)]


def _collected_tests() -> int:
    """Wie viele Tests pytest wirklich einsammelt.

    Bewusst pytest selbst fragen statt `def test_` zu zaehlen: die beiden Zahlen sind
    verschieden (Parametrisierung), und die Seite nennt die Zahl, die der Installer dem
    Nutzer vorfuehrt — das ist die von pytest.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match is not None, f"pytest nannte keine Anzahl:\n{result.stdout[-400:]}"
    return int(match.group(1))


def _redteam_cases() -> int:
    """Die Faelle der Angriffs-Suite: Eintraege in CASES plus die eigenstaendigen Pruefungen.

    Gezaehlt wird die Zahl, die `redteam.py` selbst am Ende ausgibt — nicht eine
    Schaetzung. Deshalb wird sie hier aus dem Lauf gelesen, nicht aus dem Quelltext.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(REDTEAM)], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    match = re.search(r"(\d+)/(\d+) cases", result.stdout)
    assert match is not None, f"redteam.py nannte keine Fallzahl:\n{result.stdout[-400:]}"
    assert match.group(1) == match.group(2), "die Angriffs-Suite ist nicht vollstaendig gruen"
    return int(match.group(2))


def test_the_site_states_the_real_number_of_tests() -> None:
    assert _collected_tests() in _claims(), (
        f"Die Seite nennt {_claims()}, tatsaechlich sind es {_collected_tests()} Tests."
    )


def test_the_site_states_the_real_number_of_adversarial_cases() -> None:
    cases = _redteam_cases()
    assert cases in _claims(), f"Die Seite nennt {_claims()}, die Angriffs-Suite hat {cases} Faelle."


def test_the_site_states_the_real_length_of_the_kernel() -> None:
    """Der Gate-Pfad soll in einer Sitzung lesbar bleiben — dann muss die Laenge stimmen."""
    lines = len(KERNEL.read_text(encoding="utf-8").splitlines())
    assert lines in _claims(), f"Die Seite nennt {_claims()}, policy.py hat {lines} Zeilen."


def test_the_site_states_the_real_number_of_tools() -> None:
    """Die Seite zaehlt die Werkzeuge auf und nennt ihre Zahl.

    Genau die Sorte Angabe, die driftet: wer ein Werkzeug ergaenzt, denkt an das Manifest
    und an die Tests — nicht an die Landingpage. Hier faellt es auf.
    """
    from talos.tools import default_manifest

    echte = default_manifest().tools
    anzahl = len(echte) if not hasattr(echte, "items") else len(list(echte))
    assert anzahl in _claims(), f"Die Seite nennt {_claims()}, es sind {anzahl} Werkzeuge."

    text = SITE.read_text(encoding="utf-8")
    namen = sorted(echte) if isinstance(echte, (dict, set, frozenset)) else sorted(
        spec.name for spec in echte
    )
    fehlend = [name for name in namen if f"<code>{name}</code>" not in text]
    assert not fehlend, f"Die Seite listet diese Werkzeuge nicht: {fehlend}"


# --- Jede Seite, nicht nur die Startseite ------------------------------------------
# ⚠️ Am 20.08. driftete genau das: index.html war aktuell (dieser Test zwang sie dazu),
# waehrend dossier.html 1590 Tests und docs/ 1548 nannte — zwei Seiten derselben Site,
# drei verschiedene Wahrheiten. Der Test las nur eine Datei, also sah er eine Wahrheit.
# Seitdem gilt die Pflicht fuer jede Seite des Auftritts; die Werkzeug-NAMEN bleiben
# der Startseite vorbehalten (sie ist die einzige, die das Inventar auflistet).
# Am 27.08. ging dossier.html in der Startseite auf (#ledger, #myth, #limits) — eine
# Struktur statt zwei, damit Zahlen nicht mehr zwischen parallelen Kopien driften
# koennen; die Vergleichsseite trat dafuer unter dieselbe Pflicht.
EXTRA_PAGES = (
    ROOT / "site" / "console.html",
    ROOT / "site" / "docs" / "index.html",
    ROOT / "site" / "vergleich" / "index.html",
    ROOT / "site" / "registry" / "index.html",
    ROOT / "site" / "redteam" / "index.html",
    ROOT / "site" / "setup" / "index.html",
    ROOT / "site" / "llms.txt",
)

# ⚠️ Am 02.09. nannte die Vergleichsseite 27 Werkzeuge (Kopfzeile) und 26 (Fliesstext),
# echt waren 28 — und der Test war gruen, weil `\b\d{2,5}\b` auch die 28 aus dem Datum
# „2026-08-28" zaehlte. Ein Zaehler, den ein Datum erfuellt, bewacht nichts. Datumsangaben
# werden deshalb vor dem Zaehlen entfernt; und die Prosa-Zahlen („26 tools", „tool
# number 26") bekommen unten ihren eigenen Test, weil sie nie als Zaehler formatiert sind.
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TOOL_PROSE = re.compile(r"\b(\d+)(?:</b>)?\s*tools\b|\btool number (\d+)\b")


def _tool_count() -> int:
    from talos.tools import default_manifest

    echte = default_manifest().tools
    return len(echte) if not hasattr(echte, "items") else len(list(echte))


def _page_claims(seite: Path) -> list[int]:
    text = _ISO_DATE.sub(" ", seite.read_text(encoding="utf-8"))
    return [int(value) for value in _COUNTER.findall(text)]


def test_every_page_states_the_real_tool_count_in_prose() -> None:
    """„26 tools" im Fliesstext driftet leiser als jeder Zaehler — und steht laenger."""
    echt = _tool_count()
    for seite in (SITE, *EXTRA_PAGES, ROOT / "README.md"):
        if not seite.exists():
            continue
        genannt = [int(a or b) for a, b in _TOOL_PROSE.findall(seite.read_text(encoding="utf-8"))]
        falsch = [zahl for zahl in genannt if zahl != echt]
        assert not falsch, f"{seite.relative_to(ROOT)} nennt {falsch} Werkzeuge, echt sind {echt}."


def test_every_page_of_the_site_states_the_real_numbers() -> None:
    erwartet = {
        "Tests": _collected_tests(),
        "Adversarial-Faelle": _redteam_cases(),
        "Kernel-Zeilen": len(KERNEL.read_text(encoding="utf-8").splitlines()),
        "Werkzeuge": _tool_count(),
    }
    for seite in (SITE, *EXTRA_PAGES):
        if not seite.exists():
            continue
        claims = _page_claims(seite)
        for label, zahl in erwartet.items():
            assert zahl in claims, (
                f"{seite.relative_to(ROOT)} nennt {label} nicht (hat {claims}, "
                f"echt ist {zahl})."
            )


# --- Das README zaehlt genauso mit wie die Seite ---------------------------------------
README = ROOT / "README.md"
# ⚠️ Die Abzeichen sind URL-kodiert: `red%20team-130%2F130`. Ein Suchen-und-Ersetzen nach
# „130/130" traf sie deshalb NIE, und das Abzeichen stand auf 125, waehrend der Fliesstext
# zwei Absaetze weiter 130 sagte — auf der Startseite des oeffentlichen Repos.
# ⚠️ Erst dekodieren, dann Zahlen suchen. Der erste Anlauf las `%20` in „red%20team" als
# die Zahl 20 und `%2F` als Trenner, der nie kam — er meldete Ziffern, die niemand
# geschrieben hat, und uebersah die, die dastanden.
_BADGE = re.compile(r"img\.shields\.io/badge/([^)\s]+)")


def _readme_numbers() -> list[int]:
    from urllib.parse import unquote

    zahlen: list[int] = []
    for treffer in _BADGE.finditer(README.read_text(encoding="utf-8")):
        zahlen += [int(z) for z in re.findall(r"\d+", unquote(treffer.group(1)))]
    return zahlen


def test_the_readme_badges_state_the_real_numbers() -> None:
    zahlen = _readme_numbers()
    assert _collected_tests() in zahlen, (
        f"Das Test-Abzeichen nennt {zahlen}, tatsaechlich sind es {_collected_tests()}."
    )
    assert _redteam_cases() in zahlen, (
        f"Das Red-Team-Abzeichen nennt {zahlen}, tatsaechlich sind es {_redteam_cases()}."
    )
    assert _tool_count() in zahlen, (
        f"Das Werkzeug-Abzeichen nennt {zahlen}, tatsaechlich sind es {_tool_count()}."
    )


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


# --- Was mitveroeffentlicht wird, darf das Private nicht benennen -------------------


def test_the_published_guidance_does_not_name_the_private_repository() -> None:
    """`CLAUDE.md` liegt im Baum und wird damit mitveroeffentlicht.

    Am 06.08. stand dort ausgeschrieben, wie das private Repository heisst und was die
    private Installation ist — in einer Datei, die jeder Besucher des oeffentlichen
    Repos oeffnen kann. Kein Geheimnis, aber auch nichts, was dorthin gehoert: die
    Adressen stehen in `git remote -v`, die Betriebsdetails im Vault.

    Geprueft wird gegen die Datei, nicht gegen die Absicht — ein Kommentar, der es
    verspricht, ist beim naechsten Umbau vergessen.

    ⚠️ Die verbotenen Marken werden aus `git remote -v` GELESEN, nicht hier
    hingeschrieben. Ein Wächter, der ausbuchstabiert, wovor er schützt, verrät dieselbe
    Sache an einer anderen Stelle — und dieser Test wird mitveröffentlicht. Nebenbei
    folgt er damit einer Umbenennung von selbst.
    """
    import subprocess

    wurzel = Path(__file__).resolve().parent.parent
    try:
        roh = subprocess.run(
            ["git", "-C", str(wurzel), "remote", "-v"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):       # kein git -> nichts zu pruefen
        pytest.skip("git ist hier nicht erreichbar")
    # ⚠️ BEIDE Segmente, Konto und Repo. Der erste Entwurf nahm nur das letzte Stueck der
    # URL — und war damit blind fuer den Kontonamen, also fuer genau den Fund, der ihn
    # ausgeloest hat. Ein Waechter, der nur die Haelfte ableitet, meldet Ruhe.
    verboten: set[str] = set()
    for zeile in roh.splitlines():
        felder = zeile.split()
        if not zeile.startswith("private\t") or len(felder) < 2:
            continue
        teile = felder[1].rstrip("/").removesuffix(".git").split("/")
        verboten.update(stueck for stueck in teile[-2:] if stueck and "." not in stueck)
    if not verboten:
        pytest.skip("kein `private`-Remote — im Tarball und im oeffentlichen Klon normal")

    # ⚠️ Geprueft wird die AUSLIEFERUNG, nicht eine Handvoll Dateien. Der erste Entwurf
    # sah nur CLAUDE.md, README.md und AGENTS.md an — und uebersah damit, dass
    # `scripts/sync-public.sh` die GitHub-Kennung des Betreuers ausgeschrieben in jedes
    # Tarball trug. Aufgefallen ist das beim Scan des fertigen Archivs von Hand; genau
    # den macht dieser Test jetzt ueberfluessig.
    liste = subprocess.run(
        ["git", "-C", str(wurzel), "ls-files"],
        capture_output=True, text=True, timeout=30, check=False,
    ).stdout.split()
    for pfad in liste:
        datei = wurzel / pfad
        if not datei.is_file():
            continue
        try:
            text = datei.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):           # Bilder, Schriften
            continue
        for marke in verboten:
            assert marke not in text, (
                f"{pfad} nennt {marke!r}. Der Baum wird veroeffentlicht — die Adressen "
                "stehen in `git remote -v`."
            )


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
