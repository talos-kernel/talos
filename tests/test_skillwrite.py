"""skill_write — der gegatete Schreibweg in die Skills-Ablage.

Skills auf der Platte sind fuer den Agenten read-only („von der Platte lesen, sonst
nichts", skills.py). Diese Tests halten die eine bewusste Ausnahme fest: ein Skill
darf NEU angelegt werden — nie ueberschrieben, nie mit `allowed-tools`, nie mit
einem Geheimnis, nie ausserhalb der Wurzel — und der Kernel fragt dafuer AUSNAHMSLOS
den Menschen. Unter der UnattendedCeiling wird daraus DENY: ein Agent, der sich
unbeaufsichtigt Skills schreibt, haette die Umkehrung des Kernsatzes zur
Dauereinrichtung gemacht.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from talos import tools
from talos.autonomy import AutonomyGovernor, GovernedKernel, attended_routine
from talos.blueprints import load as load_blueprints
from talos.channel import Principal, Trust
from talos.config import BLUEPRINTS_DIR
from talos.manifest import Effect
from talos.policy import (
    PolicyKernel,
    ToolRequest,
    Verdict,
    skill_write_path,
    skill_write_root,
)
from talos.schedule import UnattendedCeiling
from talos.skills import discover_skills
from talos.skillwrite import (
    MAX_BODY_CHARS,
    MAX_DESCRIPTION_CHARS,
    SkillWriteError,
    make_skill_write_runner,
)

OWNER = Principal("telegram", "100000001")
BODY = "## Steps\n\n1. Read the input.\n2. Do the thing.\n3. Report briefly."


def _req(name: object = "my-skill", description: object = "Does one useful thing.",
         body: object = BODY) -> ToolRequest:
    return ToolRequest(
        "skill_write", OWNER, {"name": name, "description": description, "body": body}
    )


# --- Das Urteil: ausnahmslos NEEDS_HUMAN, unbeaufsichtigt DENY ---------------------

def test_the_manifest_declares_an_irreversible_write() -> None:
    spec = tools.default_manifest().get("skill_write")
    assert spec is not None
    assert spec.effect is Effect.WRITE
    # Irreversible deklariert — das ist der einzige Grund, warum der Kernel hier
    # ausnahmslos NEEDS_HUMAN antwortet, und es ist an keinen Schalter koppelbar.
    assert spec.reversible is False


def test_the_kernel_always_asks_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALOS_SKILLS_DIRS", raising=False)
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    decision = kernel.decide(_req())
    assert decision.verdict is Verdict.NEEDS_HUMAN
    assert "irreversible" in decision.reason


def test_under_the_unattended_ceiling_it_becomes_a_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dieselbe Konstruktion wie der Blueprint-Deckel-Test: ein installierter
    Zeitplan erteilt kein Recht — was einen Menschen braucht, wird DENY."""
    monkeypatch.delenv("TALOS_SKILLS_DIRS", raising=False)
    ceiling = UnattendedCeiling()
    kernel = GovernedKernel(
        PolicyKernel(tools.default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5),
        lambda _c: Trust.FULL,
        unattended=ceiling,
    )
    req = _req()
    assert kernel.decide(req).verdict is Verdict.NEEDS_HUMAN
    with ceiling.active():
        assert kernel.decide(req).verdict is Verdict.DENY


def test_no_attended_autoapproval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Die Attended-Auto-Freigabe greift nur bei der Routineklasse — ein
    irreversibles Werkzeug gehoert per Bauart nie dazu."""
    monkeypatch.delenv("TALOS_SKILLS_DIRS", raising=False)
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    req = _req()
    assert attended_routine(req, tools.default_manifest().get("skill_write"), kernel) is False


def test_the_target_is_derived_never_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TALOS_SKILLS_DIRS", raising=False)
    expected = str(Path.home() / ".talos" / "skills" / "my-skill" / "SKILL.md")
    assert skill_write_path("my-skill") == expected
    kernel = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    assert kernel.guard_targets(_req()) == (expected,)


# --- Die Weigerungen: fail-closed, ehrlich, und es faellt kein Byte -----------------

@pytest.mark.parametrize("name", [
    "Bad",            # Grossbuchstaben
    "a b",            # Leerzeichen
    "..",             # Traversal
    "../evil",        # Traversal mit Segment
    "a/b",            # ein zweites Segment
    "-ab", "ab-",     # Rand-Bindestriche — der Loader wuerde sie ablehnen
    "a--b",           # Doppel-Bindestrich — derselbe Grund
    "a" * 41,         # ueber dem Maximum
    "",               # leer
])
def test_bad_names_are_refused_and_write_nothing(tmp_path: Path, name: str) -> None:
    runner = make_skill_write_runner(tmp_path)
    with pytest.raises(SkillWriteError, match="name"):
        runner(_req(name=name))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("description", ["", "   ", "x" * (MAX_DESCRIPTION_CHARS + 1)])
def test_bad_descriptions_are_refused(tmp_path: Path, description: str) -> None:
    runner = make_skill_write_runner(tmp_path)
    with pytest.raises(SkillWriteError, match="description"):
        runner(_req(description=description))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("body", ["", "   \n  ", "x" * (MAX_BODY_CHARS + 1)])
def test_bad_bodies_are_refused(tmp_path: Path, body: str) -> None:
    runner = make_skill_write_runner(tmp_path)
    with pytest.raises(SkillWriteError, match="body"):
        runner(_req(body=body))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,text", [
    ("body", "Do the thing.\n\nallowed-tools: run_shell, write_file\n"),
    ("body", "See the Allowed-Tools section below."),
    ("description", "Does a thing allowed-tools run_shell"),
])
def test_allowed_tools_is_never_smuggled(tmp_path: Path, field: str, text: str) -> None:
    """Ein Skill ist fremder Anweisungstext im Prompt — er darf die zweite
    Erlaubnisquelle nicht einmal beantragen (tests/test_skills.py Doktrin)."""
    runner = make_skill_write_runner(tmp_path)
    with pytest.raises(SkillWriteError, match="allowed-tools"):
        runner(_req(**{field: text}))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,text", [
    ("body", "The key material:\n-----BEGIN PRIVATE KEY-----\nrest omitted"),
    ("body", "Use sk-Ab3dEfGhIjKlMnOp12 for the request."),
    ("description", "Uses token ghp_Ab3dEfGhIjKlMnOp12Qr"),
])
def test_secret_shaped_content_is_refused_not_redacted(
    tmp_path: Path, field: str, text: str,
) -> None:
    """Wie recall.py (`SecretRefused`): abgewiesen statt unkenntlich gemacht —
    ein Skill auf der Platte wird in jeden kuenftigen Prompt gelesen."""
    runner = make_skill_write_runner(tmp_path)
    with pytest.raises(SkillWriteError, match="credential"):
        runner(_req(**{field: text}))
    assert list(tmp_path.iterdir()) == []


# --- Der erfolgreiche Weg: byte-exakt, einmalig, ehrlich -----------------------------

def test_a_successful_write_is_byte_exact_and_private(tmp_path: Path) -> None:
    runner = make_skill_write_runner(tmp_path)
    result = runner(_req())
    target = tmp_path / "my-skill" / "SKILL.md"
    expected = (
        "---\nname: my-skill\ndescription: Does one useful thing.\n---\n\n" + BODY + "\n"
    )
    assert target.read_text(encoding="utf-8") == expected
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # Das Ergebnis sagt ehrlich, wo die Datei liegt und wann sie wirkt.
    assert str(target) in result
    assert "next run" in result
    assert "cannot be overwritten" in result


def test_overwriting_is_refused_and_the_file_stays(tmp_path: Path) -> None:
    runner = make_skill_write_runner(tmp_path)
    runner(_req())
    before = (tmp_path / "my-skill" / "SKILL.md").read_text(encoding="utf-8")
    with pytest.raises(SkillWriteError, match="already exists"):
        runner(_req(body="A different, improved body."))
    assert (tmp_path / "my-skill" / "SKILL.md").read_text(encoding="utf-8") == before


def test_a_written_skill_round_trips_through_the_loader(tmp_path: Path) -> None:
    """Geschrieben ist erst die halbe Wahrheit — der Loader aus skills.py muss den
    neuen Skill in derselben Wurzel auch entdecken und laden."""
    runner = make_skill_write_runner(tmp_path)
    runner(_req())
    catalog = discover_skills(tmp_path)
    skill = catalog.get("my-skill")
    assert skill is not None, f"nicht entdeckt: {catalog.rejected}"
    assert skill.description == "Does one useful thing."
    assert skill.requested_tools == ""   # keine zweite Erlaubnisquelle, auch nicht leer
    assert skill.load_body() == BODY


# --- Die Verdrahtung: Default-Runner und Kernel lesen dieselbe Konfiguration ---------

def test_the_default_runner_follows_talos_skills_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ersetzt der Betreiber die Liste ganz, landet der Skill in deren erster
    Wurzel — genau dort sucht `discover_skills` ihn dann auch."""
    ziel = tmp_path / "custom-skills"
    monkeypatch.setenv("TALOS_SKILLS_DIRS", str(ziel))
    assert skill_write_root() == str(ziel)
    result = tools.RUNNERS["skill_write"](_req())
    target = ziel / "my-skill" / "SKILL.md"
    assert target.is_file()
    assert str(target) in result
    assert discover_skills(ziel).get("my-skill") is not None


def test_the_default_root_prefers_the_talos_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Steht `~/.talos/skills` in der Liste, gewinnt es — auch wenn eine andere
    Wurzel zuerst steht."""
    andere = tmp_path / "andere"
    monkeypatch.setenv(
        "TALOS_SKILLS_DIRS",
        os.pathsep.join([str(andere), str(Path.home() / ".talos" / "skills")]),
    )
    assert skill_write_root() == str(Path.home() / ".talos" / "skills")


# --- Der Blueprint: woechentlich, durch denselben Parser wie jeder andere ------------

def test_the_shipped_blueprint_loads_and_parses() -> None:
    catalog = load_blueprints(BLUEPRINTS_DIR)
    assert catalog.rejected == (), f"verworfen: {catalog.rejected}"
    blueprint = catalog.get("skill-distillation")
    assert blueprint.schedule_fields() == {"cron": "15 20 * * SUN"}
    # Der Auftrag nennt beide Wege: den gegateten und den Vault-Fallback.
    assert "skill_write" in blueprint.prompt
    assert "vault_write_note" in blueprint.prompt
