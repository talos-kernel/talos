"""Capability-Token: die Erlaubnis ist an eine Handlung gebunden, einmalig und kurzlebig.

Die alte Allow-Liste liess sich nicht angreifen — sie war ein Name in einer Menge.
Ein Token schon, und genau darum steht hier fuer jede der vier Eigenschaften ein Fall:
Faelschung, Ablauf, Zweitgebrauch, vertauschte Handlung. Dazu die eigentliche Pointe:
die Praegestelle fragt den Kernel selbst, also kann ein DENY nirgends zu einem Token
werden — auch nicht, wenn der Aufrufer das Gegenteil behauptet.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import tools
from talos.capability import (
    CapabilityError,
    CapabilityMint,
    GrantedRunner,
    action_fingerprint,
)
from talos.policy import PolicyKernel, ToolRequest
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")
HOME = str(Path.home())


def _mint(clock=None) -> CapabilityMint:
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    return CapabilityMint(policy, clock=clock) if clock else CapabilityMint(policy)


def _write(path: Path) -> ToolRequest:
    return ToolRequest("write_file", OWNER, {"path": str(path), "content": "x"}, ())


# --- Praegen: der Mint urteilt selbst --------------------------------------------
def test_allowed_write_gets_a_token(tmp_path: Path) -> None:
    grant = _mint().issue(_write(tmp_path / "notes.md"))
    assert grant.tool == "write_file"
    assert grant.expires_at > grant.issued_at
    assert grant.human_approved is False


def test_deny_yields_no_token() -> None:
    """Der Mint glaubt keinem Aufrufer — er fragt den Kernel selbst."""
    with pytest.raises(CapabilityError) as err:
        _mint().issue(ToolRequest("read_file", OWNER, {"path": f"{HOME}/.ssh/id_ed25519"}, ()))
    assert "kein Token" in str(err.value)


def test_deny_yields_no_token_even_with_human_approved() -> None:
    """Ein „ja" hebt einen DENY nicht auf — es gibt keinen Weg, daraus ein Recht zu machen."""
    with pytest.raises(CapabilityError):
        _mint().issue(ToolRequest("run_shell", OWNER, {"command": "rm -rf /"}, ()), human_approved=True)


def test_needs_human_without_approval_yields_no_token() -> None:
    with pytest.raises(CapabilityError) as err:
        _mint().issue(_write(Path(HOME) / ".bashrc"))
    assert "ohne Freigabe" in str(err.value)


def test_needs_human_with_approval_yields_a_marked_token() -> None:
    grant = _mint().issue(_write(Path(HOME) / ".bashrc"), human_approved=True)
    assert grant.human_approved is True


# --- Einloesen: die vier Eigenschaften -------------------------------------------
def test_forged_token_is_rejected(tmp_path: Path) -> None:
    """Das Geheimnis verlaesst den Mint nie — ein handgebautes Grant hat keine Signatur."""
    mint = _mint()
    req = _write(tmp_path / "notes.md")
    grant = mint.issue(req)
    forged = replace(grant, mac="00" * 32)
    with pytest.raises(CapabilityError) as err:
        mint.redeem(forged, req)
    assert "Signatur" in str(err.value)


def test_token_from_a_foreign_mint_is_rejected(tmp_path: Path) -> None:
    """Prozesslokales Geheimnis: ein Token aus einer anderen Praegestelle gilt hier nicht."""
    req = _write(tmp_path / "notes.md")
    foreign = _mint().issue(req)
    with pytest.raises(CapabilityError):
        _mint().redeem(foreign, req)


def test_expired_token_is_rejected(tmp_path: Path) -> None:
    now = [1000.0]
    mint = _mint(clock=lambda: now[0])
    req = _write(tmp_path / "notes.md")
    grant = mint.issue(req)

    now[0] = 1000.0 + 31.0  # ueber die TTL hinaus
    with pytest.raises(CapabilityError) as err:
        mint.redeem(grant, req)
    assert "abgelaufen" in str(err.value)


def test_token_is_single_use(tmp_path: Path) -> None:
    mint = _mint()
    req = _write(tmp_path / "notes.md")
    grant = mint.issue(req)

    mint.redeem(grant, req)  # erste Einloesung geht durch
    with pytest.raises(CapabilityError) as err:
        mint.redeem(grant, req)  # Replay derselben Anfrage
    assert "verbraucht" in str(err.value)


def test_token_cannot_be_bent_to_another_target(tmp_path: Path) -> None:
    """Der Kern des Ganzen: harmloses Ziel praegen, gefaehrliches einsetzen."""
    mint = _mint()
    harmless = _write(tmp_path / "notes.md")
    grant = mint.issue(harmless)

    dangerous = _write(Path(HOME) / ".bashrc")
    with pytest.raises(CapabilityError) as err:
        mint.redeem(grant, dangerous)
    assert "andere Handlung" in str(err.value)


def test_token_cannot_be_bent_to_another_tool(tmp_path: Path) -> None:
    mint = _mint()
    grant = mint.issue(_write(tmp_path / "notes.md"))
    other = ToolRequest("read_file", OWNER, {"path": str(tmp_path / "notes.md")}, ())
    with pytest.raises(CapabilityError) as err:
        mint.redeem(grant, other)
    assert "gilt fuer write_file" in str(err.value)


def test_token_cannot_be_bent_to_another_identity(tmp_path: Path) -> None:
    mint = _mint()
    req = _write(tmp_path / "notes.md")
    grant = mint.issue(req)
    stranger = ToolRequest("write_file", 111111, dict(req.args), ())
    with pytest.raises(CapabilityError):
        mint.redeem(grant, stranger)


def test_nothing_is_not_a_token(tmp_path: Path) -> None:
    with pytest.raises(CapabilityError):
        _mint().redeem(None, _write(tmp_path / "notes.md"))  # type: ignore[arg-type]


# --- Fingerabdruck ----------------------------------------------------------------
def test_fingerprint_is_stable_and_discriminating(tmp_path: Path) -> None:
    a = _write(tmp_path / "notes.md")
    again = _write(tmp_path / "notes.md")
    other = _write(tmp_path / "notes2.md")
    assert action_fingerprint(a) == action_fingerprint(again)
    assert action_fingerprint(a) != action_fingerprint(other)


def test_fingerprint_covers_kernel_derived_targets(tmp_path: Path) -> None:
    """Was das Modell deklariert, ist Beiwerk — die abgeleiteten Ziele stehen mit drin."""
    path = tmp_path / "notes.md"
    honest = ToolRequest("write_file", OWNER, {"path": str(path), "content": "x"}, ())
    lying = ToolRequest("write_file", OWNER, {"path": str(path), "content": "x"}, ("/etc/passwd",))
    # Unterschiedliche Deklaration -> unterschiedlicher Abdruck, also kein geteiltes Token.
    assert action_fingerprint(honest) != action_fingerprint(lying)


# --- GrantedRunner: ohne Token kein Effekt ----------------------------------------
def test_granted_runner_needs_a_valid_grant(tmp_path: Path) -> None:
    ran: list[str] = []
    mint = _mint()
    runner = GrantedRunner(mint=mint, runners={"write_file": lambda req: ran.append(req.tool)})
    req = _write(tmp_path / "notes.md")

    grant = mint.issue(req)
    runner(req, grant)
    assert ran == ["write_file"]

    with pytest.raises(CapabilityError):
        runner(req, grant)  # verbraucht — kein zweiter Lauf
    assert ran == ["write_file"]


def test_granted_runner_refuses_unregistered_tool(tmp_path: Path) -> None:
    """Erst das Token, dann der Runner: ein unbekanntes Tool laeuft nicht ins Leere."""
    mint = _mint()
    runner = GrantedRunner(mint=mint, runners={})
    req = _write(tmp_path / "notes.md")
    with pytest.raises(CapabilityError) as err:
        runner(req, mint.issue(req))
    assert "kein Runner registriert" in str(err.value)


# --- Buchhaltung -------------------------------------------------------------------
def test_stats_count_issued_and_redeemed(tmp_path: Path) -> None:
    mint = _mint()
    req = _write(tmp_path / "notes.md")
    mint.redeem(mint.issue(req), req)
    stats = mint.stats()
    assert stats["issued"] == 1 and stats["redeemed"] == 1
