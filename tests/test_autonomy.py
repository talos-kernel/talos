"""Der Regler darf nur zumachen. Diese Datei ist die Wache davor.

Die interessanten Tests sind nicht die Tabellen-Tests (welche Stufe was sagt),
sondern die drei Eigenschaften: **nie freizuegiger als der Kernel**, **monoton
ueber die Stufen**, und **kein Tool, mit dem sich das Modell selbst hochdreht**.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from talos import autonomy
from talos.autonomy import (
    DEFAULT_LEVEL,
    MAX_LEVEL,
    MIN_LEVEL,
    WORKSPACE,
    AutonomyError,
    AutonomyGovernor,
    GovernedKernel,
    clamp,
    restore_level,
)
from talos.capability import CapabilityError, CapabilityMint
from talos.eventlog import Event, EventLog
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest
from talos.channel import Principal, Trust

OWNER = Principal("telegram", "100000001")
STRANGER = Principal("telegram", "749908869")
ALLOWED = frozenset({OWNER})
HOME = Path.home()

SEVERITY = {Verdict.ALLOW: 0, Verdict.NEEDS_HUMAN: 1, Verdict.DENY: 2}


def kernel() -> PolicyKernel:
    return PolicyKernel(default_manifest(), ALLOWED)


def full_trust(_channel: str) -> Trust:
    """Kanal-Decke offen: hier steht der Autonomie-Regler vor Gericht, nicht der Kanal."""
    return Trust.FULL


def governed(level: int) -> GovernedKernel:
    return GovernedKernel(kernel(), AutonomyGovernor(level), full_trust)


READ = ToolRequest("read_file", OWNER, {"path": str(HOME / "notes.md")})
WRITE_WS = ToolRequest("write_file", OWNER, {"path": str(WORKSPACE / "a.txt"), "content": "x"})
WRITE_HOME = ToolRequest("write_file", OWNER, {"path": str(HOME / "notes.md"), "content": "x"})
SHELL = ToolRequest("run_shell", OWNER, {"command": "date"})
BASHRC = ToolRequest("write_file", OWNER, {"path": str(HOME / ".bashrc"), "content": "x"})
ETC = ToolRequest("write_file", OWNER, {"path": "/etc/passwd", "content": "x"})
SECRET_READ = ToolRequest("read_file", OWNER, {"path": str(HOME / ".secrets" / "x.env")})
ALL_REQUESTS = (READ, WRITE_WS, WRITE_HOME, SHELL, BASHRC, ETC, SECRET_READ)


# --- die Leiter ------------------------------------------------------------------
@pytest.mark.parametrize(
    "level, req, expected",
    [
        (0, READ, Verdict.DENY),
        (0, WRITE_WS, Verdict.DENY),
        (0, SHELL, Verdict.DENY),
        (1, READ, Verdict.NEEDS_HUMAN),
        (1, WRITE_WS, Verdict.DENY),
        (1, SHELL, Verdict.DENY),
        (2, READ, Verdict.ALLOW),
        (2, WRITE_WS, Verdict.DENY),
        (2, SHELL, Verdict.DENY),
        (3, READ, Verdict.ALLOW),
        (3, WRITE_WS, Verdict.NEEDS_HUMAN),
        (3, WRITE_HOME, Verdict.NEEDS_HUMAN),
        (3, SHELL, Verdict.NEEDS_HUMAN),
        (4, READ, Verdict.ALLOW),
        (4, WRITE_WS, Verdict.ALLOW),
        (4, WRITE_HOME, Verdict.NEEDS_HUMAN),
        (4, SHELL, Verdict.NEEDS_HUMAN),
        (5, READ, Verdict.ALLOW),
        (5, WRITE_WS, Verdict.ALLOW),
        (5, WRITE_HOME, Verdict.ALLOW),
        (5, SHELL, Verdict.NEEDS_HUMAN),
    ],
    ids=lambda v: str(v),
)
def test_ladder(level, req, expected):
    assert governed(level).decide(req).verdict is expected


# --- die drei Eigenschaften ------------------------------------------------------
@pytest.mark.parametrize("level", range(MIN_LEVEL, MAX_LEVEL + 1))
def test_regler_ist_nie_freizuegiger_als_der_kernel(level):
    """Die eine Eigenschaft, die zaehlt: keine Stufe vergibt Rechte."""
    base = kernel()
    dial = governed(level)
    for req in ALL_REQUESTS:
        assert SEVERITY[dial.decide(req).verdict] >= SEVERITY[base.decide(req).verdict], req.tool


def test_monoton_ueber_die_stufen():
    """Hoehere Stufe darf nie strenger sein als eine niedrigere — sonst ist es kein Regler."""
    for req in ALL_REQUESTS:
        severities = [SEVERITY[governed(n).decide(req).verdict] for n in range(MIN_LEVEL, MAX_LEVEL + 1)]
        assert severities == sorted(severities, reverse=True), (req.tool, severities)


def test_stufe_5_ist_exakt_der_kernel():
    base, dial = kernel(), governed(5)
    for req in ALL_REQUESTS:
        assert dial.decide(req) == base.decide(req)


# --- Floors bleiben auf jeder Stufe ----------------------------------------------
@pytest.mark.parametrize("level", range(MIN_LEVEL, MAX_LEVEL + 1))
def test_hardline_bleibt_deny(level):
    assert governed(level).decide(ETC).verdict is Verdict.DENY


@pytest.mark.parametrize("level", (3, 4, 5))
def test_persistenz_fragt_auch_bei_hoher_stufe(level):
    assert governed(level).decide(BASHRC).verdict is Verdict.NEEDS_HUMAN


def test_stufe_4_traegt_nicht_ueber_dotdot_aus_dem_arbeitsbereich():
    escape = ToolRequest(
        "write_file", OWNER, {"path": str(WORKSPACE / ".." / "notes.md"), "content": "x"}
    )
    assert governed(4).decide(escape).verdict is Verdict.NEEDS_HUMAN


def test_stufe_4_ohne_ableitbares_ziel_fragt():
    blind = ToolRequest("write_file", OWNER, {"content": "x"})  # kein path -> keine Ziele
    assert governed(4).decide(blind).verdict is Verdict.NEEDS_HUMAN


# --- der Regler ist kein Tool ----------------------------------------------------
def test_kein_tool_stellt_den_regler():
    """Waere `set_autonomy` im Manifest, koennte das Modell seine Leine verlaengern."""
    assert default_manifest().get("set_autonomy") is None
    req = ToolRequest("set_autonomy", OWNER, {"level": 5})
    assert governed(5).decide(req).verdict is Verdict.DENY


# --- stellen ---------------------------------------------------------------------
def test_set_level_gibt_vorher_nachher():
    gov = AutonomyGovernor(2)
    assert gov.set_level(4, principal=OWNER, allowed_identities=ALLOWED) == (2, 4)
    assert gov.level == 4


@pytest.mark.parametrize("bad", (-1, 6, 99, "drei", None, ""))
def test_ungueltige_stufe_laesst_den_stand_unberuehrt(bad):
    gov = AutonomyGovernor(3)
    with pytest.raises(AutonomyError):
        gov.set_level(bad, principal=OWNER, allowed_identities=ALLOWED)
    assert gov.level == 3


def test_fremde_identitaet_stellt_nichts():
    gov = AutonomyGovernor(1)
    with pytest.raises(AutonomyError):
        gov.set_level(5, principal=STRANGER, allowed_identities=ALLOWED)
    assert gov.level == 1


@pytest.mark.parametrize("raw, expected", [(-5, 0), (0, 0), (5, 5), (9, 5), ("x", 0), (None, 0)])
def test_clamp_faellt_nach_unten_nie_nach_oben(raw, expected):
    assert clamp(raw) == expected


# --- ueberlebt den Neustart ------------------------------------------------------
def test_restore_level_ohne_eintrag_ist_default(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    assert restore_level(log) == DEFAULT_LEVEL


def test_restore_level_liest_den_letzten_stand(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    log.append(Event("r1", "human", "autonomy.set", {"level": 4}))
    log.append(Event("r2", "human", "autonomy.set", {"level": 1}))
    assert restore_level(log) == 1


def test_restore_level_faellt_bei_muell_nach_unten(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    log.append(Event("r1", "human", "autonomy.set", {"level": "hoch"}))
    assert restore_level(log) == MIN_LEVEL


def test_restore_level_faehrt_bei_kaputtem_log_nicht_hoch():
    """Unlesbares Log heisst: der letzte Stand ist unbekannt UND es gibt kein Audit.

    Das darf nicht auf die gewohnte Stufe zurueckfallen — sonst verlaengert ein
    Defekt still die Leine, die the operator kurz gestellt hatte.
    """

    class KaputtesLog:
        def recent(self, limit, types=()):
            raise RuntimeError("database disk image is malformed")

    assert restore_level(KaputtesLog()) == MIN_LEVEL
    assert restore_level(KaputtesLog(), default=MAX_LEVEL) == MIN_LEVEL


# --- Huelle verhaelt sich wie der Kernel -----------------------------------------
def test_governed_kernel_reicht_die_kernel_felder_durch():
    dial = governed(3)
    assert dial.manifest is dial.kernel.manifest
    assert dial.allowed_identities == ALLOWED
    assert dial.shell_needs_human is True


# --- Zusammenspiel mit dem Token -------------------------------------------------
def test_token_traegt_die_stufe_seiner_praegung():
    gov = AutonomyGovernor(5)
    mint = CapabilityMint(GovernedKernel(kernel(), gov, full_trust), governor=gov)
    grant = mint.issue(WRITE_HOME)
    assert grant.level == 5


def test_regler_zudrehen_entwertet_token_im_flug():
    """`/autonomy 0` ist eine Notbremse — ein bereits gepraegtes Recht darf sie nicht ueberholen."""
    gov = AutonomyGovernor(5)
    mint = CapabilityMint(GovernedKernel(kernel(), gov, full_trust), governor=gov)
    grant = mint.issue(WRITE_HOME)
    gov.set_level(0, principal=OWNER, allowed_identities=ALLOWED)
    with pytest.raises(CapabilityError):
        mint.redeem(grant, WRITE_HOME)


def test_niedrige_stufe_praegt_kein_token_auch_nicht_mit_freigabe():
    """Der Regler ist des Betreibers eigene Entscheidung — ein „ja" hebt sie nicht auf, /autonomy schon."""
    gov = AutonomyGovernor(2)
    mint = CapabilityMint(GovernedKernel(kernel(), gov, full_trust), governor=gov)
    with pytest.raises(CapabilityError):
        mint.issue(WRITE_WS, human_approved=True)


def test_ohne_regler_bleibt_der_mint_wie_er_war():
    """Bestandscode (Tests, redteam) baut den Mint ohne Regler — das muss weiter tragen."""
    mint = CapabilityMint(kernel())
    grant = mint.issue(WRITE_HOME)
    mint.redeem(grant, WRITE_HOME)  # wirft nicht


def test_tabelle_nennt_jede_stufe():
    text = autonomy.table()
    for level in range(MIN_LEVEL, MAX_LEVEL + 1):
        assert f"{level} " in text
