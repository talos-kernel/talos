"""Der Freigabe-Text ist des Betreibers einzige Entscheidungsgrundlage.

`guard_targets("run_shell")` ist leer, also traegt der Text die Pfad-Einordnung aus dem
Kernel — sonst sehen `date` und `echo … >> ~/.bashrc` gleich aus. Zusaetzlich muss der
fehlende Undo-Pfad fuer Shell offen dastehen.
"""
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos import tools
from talos.capability import CapabilityMint, GrantedRunner
from talos.conductor import Conductor
from talos.eventlog import EventLog
from talos.executor import Executor
from talos.policy import PACKAGE_DIR, PolicyKernel, ToolRequest, command_risk_paths
from talos.snapshot import Snapshotter
from talos.channel import Principal, Trust

OWNER = Principal("telegram", "100000001")
CHAT_OWNER = "telegram:100000001"
HOME = str(Path.home())


def _conductor(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    policy = PolicyKernel(tools.default_manifest(), frozenset({OWNER}))
    mint = CapabilityMint(policy)
    executor = Executor(
        policy=policy,
        log=log,
        snapshotter=Snapshotter(tmp_path / ".snap"),
        runner=GrantedRunner(mint=mint, runners=dict(tools.RUNNERS)),
        mint=mint,
    )
    return Conductor(
        log=log,
        reasoner=None,
        executor=executor,
        send=lambda conversation, text: None,
        allowed_principals=frozenset({OWNER}),
        trust_of=lambda _channel: Trust.FULL,
    )


def _prompt(tmp_path, command: str) -> str:
    req = ToolRequest("run_shell", OWNER, {"command": command}, ())
    return _conductor(tmp_path)._approval_prompt(req)


# --- Kernel-Einordnung der Pfade -------------------------------------------------
def test_persistence_path_is_labelled(tmp_path):
    prompt = _prompt(tmp_path, "echo evil >> ~/.bashrc")
    assert f"{HOME}/.bashrc" in prompt
    assert "Persistenz" in prompt  # nicht nur der generische Shell-Grund


def test_own_source_tree_is_labelled(tmp_path):
    """Der eigene Quellbaum wird als Persistenz ausgewiesen — wo immer er liegt.

    Zitiert, weil ein Installationspfad Leerzeichen enthalten darf. Unzitiert bricht
    die Token-Erkennung am ersten Leerzeichen ab, und der Floor sah den eigenen Code
    dann gar nicht mehr.
    """
    prompt = _prompt(tmp_path, f"rm {shlex.quote(str(PACKAGE_DIR / 'policy.py'))}")
    assert "Persistenz" in prompt


def test_harmless_command_carries_no_label(tmp_path):
    prompt = _prompt(tmp_path, "date")
    assert "Persistenz" not in prompt and "Secret" not in prompt


def test_missing_undo_is_disclosed(tmp_path):
    assert "no undo" in _prompt(tmp_path, "date")


def test_file_tool_prompt_has_no_shell_note(tmp_path):
    req = ToolRequest("write_file", OWNER, {"path": "/tmp/x", "content": "y"}, ())
    assert "no undo" not in _conductor(tmp_path)._approval_prompt(req)


# --- Einordnung selbst -----------------------------------------------------------
def test_command_risk_paths_labels_by_tier():
    assert command_risk_paths("cat /etc/passwd")[0][1] == "System"
    secret = command_risk_paths("cat ~/.ssh/id_ed25519")[0][1]
    assert secret == "Secret"
    assert command_risk_paths("date") == ()
