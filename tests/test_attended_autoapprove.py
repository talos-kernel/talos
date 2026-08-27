"""Attended-Auto-Freigabe: attended locker, unattended strikt.

Der Owner-Entscheid: ein interaktiver Lauf (eine eingehende Nachricht eines
erlaubten Principals — ein Mensch ist da und kann hinschauen) laesst die
Routineklasse ohne Freigabe-Prompt laufen. Diese Datei ist die Wache fuer die
drei Eigenschaften, an denen das haengt:

1. **Nur die Routineklasse, nur attended.** Irreversible Werkzeuge, Wirkung nach
   aussen (`requires_env`) und Floor-Ziele fragen weiter — ueberall.
2. **Unattended/delegiert bit-identisch.** Die Decken schlagen vor der
   Auto-Freigabe zu; ein zeitgesteuerter oder delegierter Lauf bekommt exakt die
   alten Urteile.
3. **Leise, aber nie unsichtbar.** Jede Auto-Freigabe steht als
   `approval.auto_attended` im Event-Log, und das Receipt nennt sie beim Namen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from talos import config, schema
from talos.autonomy import (
    AUTO_ATTENDED_REASON,
    WORKSPACE,
    AutonomyGovernor,
    GovernedKernel,
    attended_routine,
    is_auto_attended,
)
from talos.capability import CapabilityMint, GrantedRunner
from talos.channel import Principal, Trust
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.schedule import UNATTENDED_REASON, UnattendedCeiling
from talos.snapshot import Snapshotter
from talos.subagent import READ_ONLY_REASON, ReadOnlyCeiling
from talos.tools import default_manifest

OWNER = Principal("telegram", "100000001")
ALLOWED = frozenset({OWNER})
HOME = Path.home()

SHELL = ToolRequest("run_shell", OWNER, {"command": "date"})
SHELL_ETC = ToolRequest("run_shell", OWNER, {"command": "cat /etc/passwd"})
WRITE_WS = ToolRequest("write_file", OWNER, {"path": str(WORKSPACE / "a.txt"), "content": "x"})
WRITE_HOME = ToolRequest("write_file", OWNER, {"path": str(HOME / "notes.md"), "content": "x"})
BASHRC = ToolRequest("write_file", OWNER, {"path": str(HOME / ".bashrc"), "content": "x"})
SECRET_WRITE = ToolRequest("write_file", OWNER, {"path": str(HOME / ".ssh" / "config"), "content": "x"})
ETC = ToolRequest("write_file", OWNER, {"path": "/etc/passwd", "content": "x"})
READ = ToolRequest("read_file", OWNER, {"path": str(HOME / "notes.md")})
DELEGATE_CODE = ToolRequest("delegate_code", OWNER, {"prompt": "bau was"})


def kernel() -> PolicyKernel:
    # Wie im Produktiv-Default des Kernels: shell_needs_human=True — sonst waere
    # SHELL schon vor der Auto-Freigabe ALLOW und der Test pruefte nichts.
    return PolicyKernel(default_manifest(), ALLOWED)


def governed(level: int = 5, auto: bool = True, **decken) -> GovernedKernel:
    return GovernedKernel(
        kernel(), AutonomyGovernor(level), lambda _c: Trust.FULL,
        attended_autoapprove=auto, **decken,
    )


# --- attended: die Routineklasse laeuft ohne Prompt -----------------------------
def test_shell_ist_attended_auto_freigegeben():
    decision = governed().decide(SHELL)
    assert decision.verdict is Verdict.ALLOW
    assert decision.reason.startswith(AUTO_ATTENDED_REASON)
    assert is_auto_attended(decision)


def test_risky_shell_ist_attended_auto_freigegeben():
    """`risky:` des Command-Floors gehoert zur Sandbox-Arbeit, nicht zur Mauer."""
    risky = ToolRequest("run_shell", OWNER, {"command": "rm -rf /tmp/talos-test-x"})
    decision = governed().decide(risky)
    assert decision.verdict is Verdict.ALLOW
    assert is_auto_attended(decision)


def test_natuerliches_allow_traegt_kein_praefix():
    """Nur umgewandelte Urteile heissen Auto-Freigabe — der Rest bleibt der Kernel."""
    for req in (READ, WRITE_WS, WRITE_HOME):
        decision = governed().decide(req)
        assert decision.verdict is Verdict.ALLOW
        assert not is_auto_attended(decision)


# --- attended: was NICHT zur Routineklasse gehoert, fragt weiter ----------------
def test_confined_delegation_ist_attended_routine(monkeypatch):
    """delegate_code gehoert seit dem Owner-Entscheid 27.08. zur Routineklasse:
    der Job entsteht wirklich, aber hinter der Confinement-Wand (eigener OS-User,
    wegwerfbarer Workspace, Deadline, keine Talos-Secrets) — die Einsperrung
    vertritt den Prompt. Der Agent soll Claude Code bei jeder Aufgabe nutzen, ohne
    dass jede Delegation einzeln fragt."""
    monkeypatch.setenv("TALOS_CLAUDE_WORKER_SOCKET", "/tmp/talos-test.sock")
    decision = governed().decide(DELEGATE_CODE)
    assert decision.verdict is Verdict.ALLOW
    assert is_auto_attended(decision)


def test_confined_delegation_bleibt_unattended_deny(monkeypatch):
    """Dieselbe Delegation ist unbeaufsichtigt weiterhin DENY — die Decke hat
    NEEDS_HUMAN laengst verworfen, bevor die Auto-Freigabe greifen koennte."""
    monkeypatch.setenv("TALOS_CLAUDE_WORKER_SOCKET", "/tmp/talos-test.sock")
    decke = UnattendedCeiling()
    an = governed(unattended=decke)
    with decke.active():
        decision = an.decide(DELEGATE_CODE)
    assert decision.verdict is Verdict.DENY
    assert not is_auto_attended(decision)


def test_persistenz_schreiben_fragt_auch_attended():
    """Reversibel hin oder her: ~/.bashrc ist ein Floor-Ziel, keine Routine."""
    assert governed().decide(BASHRC).verdict is Verdict.NEEDS_HUMAN


def test_secret_schreiben_fragt_auch_attended():
    assert governed().decide(SECRET_WRITE).verdict is Verdict.NEEDS_HUMAN


def test_harte_denys_bleiben_attended_deny():
    """Adversarial: ein /etc-Pfad im Kommando ist auch attended DENY, nicht Routine."""
    assert governed().decide(SHELL_ETC).verdict is Verdict.DENY
    assert governed().decide(ETC).verdict is Verdict.DENY


def test_klasse_steht_an_einer_stelle_und_lebt_von_den_specs():
    """Die Klassendefinition ist ein Prädikat auf den Spec-Eigenschaften."""
    spec_shell = default_manifest().get("run_shell")
    spec_delegate = default_manifest().get("delegate_code")
    spec_write = default_manifest().get("write_file")
    assert attended_routine(SHELL, spec_shell, kernel()) is True
    assert attended_routine(DELEGATE_CODE, spec_delegate, kernel()) is True   # confined
    assert attended_routine(BASHRC, spec_write, kernel()) is False
    assert attended_routine(WRITE_HOME, spec_write, kernel()) is True
    assert attended_routine(SHELL, None, kernel()) is False


# --- die Decken behalten Vorrang ------------------------------------------------
def test_unattended_ist_bit_identisch_zum_alten_verhalten(monkeypatch):
    """Scheduled: exakt die alten Urteile — NEEDS_HUMAN wird DENY, nichts wird leise."""
    monkeypatch.setenv("TALOS_CLAUDE_WORKER_SOCKET", "/tmp/talos-test.sock")
    decke = UnattendedCeiling()
    an = governed(unattended=decke)
    aus = governed(auto=False, unattended=decke)
    with decke.active():
        for req in (SHELL, BASHRC, SECRET_WRITE, DELEGATE_CODE, READ, WRITE_WS, ETC):
            assert an.decide(req) == aus.decide(req), req.tool
        assert an.decide(SHELL).verdict is Verdict.DENY
        assert an.decide(SHELL).reason == UNATTENDED_REASON


def test_delegiert_bleibt_read_only():
    decke = ReadOnlyCeiling()
    an = governed(delegated=decke)
    with decke.active():
        assert an.decide(SHELL).verdict is Verdict.DENY
        assert an.decide(SHELL).reason == READ_ONLY_REASON
        assert an.decide(READ).verdict is Verdict.ALLOW


def test_regler_hat_vorrang_vor_der_auto_freigabe():
    """Wer die Leine kuerzer stellt, WILL gefragt werden — die Auto-Freigabe weicht nicht."""
    for stufe in (1, 2, 3, 4):
        assert governed(level=stufe).decide(SHELL).verdict is not Verdict.ALLOW, stufe


def test_kanal_ohne_volles_vertrauen_bekommt_keine_auto_freigabe():
    ask = GovernedKernel(
        kernel(), AutonomyGovernor(5), lambda _c: Trust.ASK, attended_autoapprove=True
    )
    assert ask.decide(SHELL).verdict is Verdict.DENY


# --- der Schalter -----------------------------------------------------------------
def test_schalter_aus_stellt_das_alte_verhalten_wieder_her():
    assert governed(auto=False).decide(SHELL).verdict is Verdict.NEEDS_HUMAN


def test_config_schalter_default_an_und_abstellbar(monkeypatch):
    monkeypatch.delenv("TALOS_ATTENDED_AUTOAPPROVE", raising=False)
    assert config.load_config(require_channel=False).attended_autoapprove is True
    monkeypatch.setenv("TALOS_ATTENDED_AUTOAPPROVE", "0")
    assert config.load_config(require_channel=False).attended_autoapprove is False


def test_schluessel_ist_eine_einstellung_wie_completion_push():
    eintrag = schema.BY_NAME["TALOS_ATTENDED_AUTOAPPROVE"]
    assert eintrag.kind == schema.SETTING and eintrag.writable
    assert eintrag.default == "1"


# --- Evidenz: das Event und der ehrliche Receipt ---------------------------------
def _executor(tmp_path: Path, runner, **decken) -> Executor:
    log = EventLog(tmp_path / "ev.db")
    gov = AutonomyGovernor(5)
    policy = GovernedKernel(
        kernel(), gov, lambda _c: Trust.FULL, attended_autoapprove=True, **decken
    )
    mint = CapabilityMint(policy, governor=gov)
    return Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners={"run_shell": runner}),
        mint=mint,
    )


def test_auto_freigabe_schreibt_event_und_ehrlichen_receipt(tmp_path):
    calls = []
    ex = _executor(tmp_path, lambda req: calls.append(req.args["command"]) or "ok")
    out = ex.run(SHELL, "run-auto")
    assert out.status is Status.DONE
    assert calls == ["date"]  # gelaufen ohne Prompt

    auto = ex.log.recent(5, ("approval.auto_attended",))
    assert len(auto) == 1
    payload = auto[-1]["payload"]
    assert payload["tool"] == "run_shell"
    assert payload["reason"].startswith(AUTO_ATTENDED_REASON)

    # Der Receipt luegt nicht: er nennt die Auto-Freigabe, nicht „your approval".
    receipt = ex.log.recent(1, ("exec.result",))[-1]["payload"]
    assert AUTO_ATTENDED_REASON in receipt["detail"]
    # Und das Token gibt keine menschliche Freigabe vor, die es nie gab.
    grant = ex.log.recent(1, ("grant.issued",))[-1]["payload"]
    assert grant["human_approved"] is False


def test_unattended_schreibt_kein_auto_event_und_laesst_nichts_laufen(tmp_path):
    calls = []
    decke = UnattendedCeiling()
    ex = _executor(tmp_path, lambda req: calls.append(1), unattended=decke)
    with decke.active():
        out = ex.run(SHELL, "run-unattended")
    assert out.status is Status.DENIED
    assert calls == []
    assert ex.log.recent(5, ("approval.auto_attended",)) == []
