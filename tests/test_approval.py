"""ApprovalStore (TTL, Fingerprint/TOCTOU, ja/nein) + der Beweis, dass „ja" einen DENY
nicht aufhebt: katastrophale Kommandos und Secret-Lesen bleiben auch mit human_approved hart."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import tools
from talos.approval import ApprovalStore, is_affirmative, is_negative
from talos.capability import CapabilityMint, GrantedRunner
from talos.eventlog import EventLog
from talos.executor import Executor, Status
from talos.policy import PolicyKernel, ToolRequest
from talos.snapshot import Snapshotter
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")
CHAT = "telegram:4242"


def _executor(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    return Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )


# --- DENY ist absolut, auch mit Freigabe ----------------------------------------
def test_secret_read_stays_denied_even_with_approval(tmp_path):
    ex = _executor(tmp_path)
    req = ToolRequest("read_file", OWNER, {"path": str(Path.home() / ".ssh" / "authorized_keys")}, ())
    out = ex.run(req, "r", human_approved=True)
    assert out.status is Status.DENIED  # Secret-Lesen bleibt gesperrt (kein Datei-Read passiert)


def test_catastrophic_hardline_stays_denied_even_with_approval(tmp_path):
    ex = _executor(tmp_path)
    out = ex.run(ToolRequest("run_shell", OWNER, {"command": "rm -rf /"}, ()), "r", human_approved=True)
    assert out.status is Status.DENIED


# --- Store: park/get/clear, TTL, TOCTOU -----------------------------------------
def test_park_get_clear(tmp_path):
    store = ApprovalStore(ttl_s=300, clock=lambda: 100.0)
    req = ToolRequest("run_shell", OWNER, {"command": "echo hi"}, ())
    rec = store.park(CHAT, req, "prompt")
    assert store.get(CHAT) is rec
    store.clear(CHAT)
    assert store.get(CHAT) is None


def test_ttl_expiry(tmp_path):
    now = [100.0]
    store = ApprovalStore(ttl_s=60, clock=lambda: now[0])
    store.park(CHAT, ToolRequest("run_shell", OWNER, {"command": "echo hi"}, ()), "p")
    assert store.get(CHAT) is not None
    now[0] = 161.0
    assert store.get(CHAT) is None  # abgelaufen -> weg


def test_fingerprint_detects_target_swap(tmp_path):
    target = tmp_path / "cfg"
    target.write_text("orig", encoding="utf-8")
    store = ApprovalStore(clock=lambda: 0.0)
    rec = store.park(CHAT, ToolRequest("write_file", OWNER, {"path": str(target), "content": "x"}, ()), "p")
    assert store.target_unchanged(rec) is True
    target.write_text("tampered", encoding="utf-8")
    assert store.target_unchanged(rec) is False


def test_affirmative_and_negative_words():
    assert is_affirmative("yes")
    assert is_affirmative("YES!")
    assert is_affirmative("yes")
    assert is_negative("no")
    assert is_negative("stop")
    assert not is_affirmative("yes please do that")  # nur das blanke Wort zaehlt
    assert not is_negative("no thanks but anyway")


def test_casual_words_do_not_arm_an_approval():
    # Freigabe braucht ein bewusstes "yes" — ein beilaeufiges ok/go/sure zaehlt NICHT.
    for casual in ("ok", "okay", "go", "sure", "yep", "yup", "y"):
        assert not is_affirmative(casual), casual
