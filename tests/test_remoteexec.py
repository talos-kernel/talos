"""remote_exec — Fernausfuehrung durch dasselbe Gate.

`run_shell` darf seit der Sandbox ohne Rueckfrage laufen, weil die Wirkung eingesperrt
ist. `remote_exec` dreht genau diesen Punkt um: die Wirkung entsteht auf einer ANDEREN
Maschine, die lokale Sandbox sperrt nur den ssh-Clienten ein. Diese Tests halten die
Vertrauensform fest: Host aus der Betreiber-Allowlist oder DENY, Hardline auch fern
DENY, und danach ausnahmslos NEEDS_HUMAN — der `SHELL_NEEDS_HUMAN=0`-Komfort gilt hier
nie, und eine stehende Regel bindet an exakt (host, command), nicht an den Host allein.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from talos import remoteexec, tools
from talos.autonomy import AutonomyGovernor, GovernedKernel
from talos.channel import Principal, Trust
from talos.manifest import Effect
from talos.policy import (
    SHELL_FORBIDDEN_PREFIXES,
    PolicyKernel,
    ToolRequest,
    Verdict,
    remote_hosts,
)
from talos.remoteexec import (
    MAX_COMMAND_CHARS,
    RemoteExecError,
    build_command,
)
from talos.schedule import UnattendedCeiling
from talos.standing import action_key, action_label

OWNER = Principal("telegram", "42")


def _req(**args: object) -> ToolRequest:
    return ToolRequest(tool="remote_exec", identity=OWNER, args=dict(args))


def _kernel() -> PolicyKernel:
    # shell_needs_human=False ist der produktive Komfort-Stand der lokalen Sandbox —
    # gerade GEGEN ihn muessen die remote_exec-Regeln halten.
    return PolicyKernel(
        tools.default_manifest(), frozenset({OWNER}), shell_needs_human=False
    )


@pytest.fixture(autouse=True)
def _hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TALOS_REMOTE_HOSTS", "mac,nas")


# --- Manifest und Verdrahtung -------------------------------------------------------

def test_manifest_declares_the_trust_form() -> None:
    spec = tools.default_manifest().get("remote_exec")
    assert spec is not None
    assert spec.effect is Effect.EXEC
    assert spec.reversible is False
    assert spec.requires_env == frozenset({"TALOS_REMOTE_HOSTS"})
    # Bewusst KEIN sandbox_required: das Flag deklariert „Wirkung hinter einer
    # Confinement-Wand" — hier sperrt die Sandbox nur den lokalen Clienten ein.
    # Mit dem Flag fiele das Werkzeug in die Routineklasse der Attended-
    # Auto-Freigabe (gemessen am ersten Live-E2E: auto-approved statt gefragt).
    assert spec.sandbox_required is False
    assert tools.RUNNERS["remote_exec"] is remoteexec.remote_exec


def test_no_attended_autoapproval() -> None:
    """EXEC mit requires_env ohne Confinement ist per Bauart keine Routine —
    die Attended-Auto-Freigabe darf hier nie greifen (Doktrin autonomy.py)."""
    from talos.autonomy import attended_routine

    kernel = _kernel()
    spec = tools.default_manifest().get("remote_exec")
    assert attended_routine(_req(host="mac", command="uptime"), spec, kernel) is False


def test_allowlist_parsing() -> None:
    assert remote_hosts({"TALOS_REMOTE_HOSTS": " mac , nas,,mac "}) == ("mac", "nas")
    assert remote_hosts({"TALOS_REMOTE_HOSTS": ""}) == ()
    assert remote_hosts({}) == ()


# --- Der Kernel: Allowlist, Hardline, ausnahmslos Mensch ----------------------------

def test_without_configured_hosts_the_tool_is_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TALOS_REMOTE_HOSTS", raising=False)
    decision = _kernel().decide(_req(host="mac", command="uptime"))
    assert decision.verdict is Verdict.DENY
    assert "TALOS_REMOTE_HOSTS" in decision.reason


def test_a_host_outside_the_allowlist_is_deny_not_a_question() -> None:
    decision = _kernel().decide(_req(host="eve", command="uptime"))
    assert decision.verdict is Verdict.DENY
    assert "allowlist" in decision.reason


def test_every_allowed_call_still_needs_a_human() -> None:
    """Der Kern der Vertrauensform: die Sandbox reicht nicht ueber Maschinengrenzen,
    darum gilt der lokale SHELL_NEEDS_HUMAN=0-Komfort hier ausdruecklich nicht."""
    decision = _kernel().decide(_req(host="mac", command="uptime"))
    assert decision.verdict is Verdict.NEEDS_HUMAN
    assert "mac" in decision.reason


def test_hardline_is_deny_even_remote() -> None:
    decision = _kernel().decide(_req(host="mac", command="rm -rf /"))
    assert decision.verdict is Verdict.DENY
    assert "hardline" in decision.reason


def test_remote_paths_do_not_trip_the_local_path_floor() -> None:
    """`/etc/hosts` fern zu lesen ist Fernwartung, kein lokaler Secret-Zugriff: der
    Pfad-Floor wuerde hier falsch alarmieren — das Urteil bleibt die Freigabefrage."""
    decision = _kernel().decide(_req(host="mac", command="cat /etc/hosts"))
    assert decision.verdict is Verdict.NEEDS_HUMAN


def test_missing_host_or_command_is_deny() -> None:
    assert _kernel().decide(_req(command="uptime")).verdict is Verdict.DENY
    assert _kernel().decide(_req(host="mac")).verdict is Verdict.DENY


def test_under_the_unattended_ceiling_it_becomes_a_deny() -> None:
    ceiling = UnattendedCeiling()
    kernel = GovernedKernel(
        _kernel(), AutonomyGovernor(5), lambda _c: Trust.FULL, unattended=ceiling
    )
    req = _req(host="mac", command="uptime")
    assert kernel.decide(req).verdict is Verdict.NEEDS_HUMAN
    with ceiling.active():
        assert kernel.decide(req).verdict is Verdict.DENY


# --- Stehende Freigaben: der Abdruck bindet host UND command -------------------------

def test_standing_key_binds_host_and_command_exactly() -> None:
    base = action_key(_req(host="mac", command="uptime"))
    assert base is not None
    assert action_key(_req(host="nas", command="uptime")) != base
    assert action_key(_req(host="mac", command="uptime -s")) != base
    assert action_key(_req(host="mac", command="uptime")) == base


def test_standing_key_refuses_the_unbindable() -> None:
    assert action_key(_req(host="mac")) is None
    assert action_key(_req(command="uptime")) is None


def test_standing_label_names_host_and_command() -> None:
    assert action_label(_req(host="mac", command="uptime")) == "remote_exec mac: uptime"


# --- Kommandobau: das Fernkommando bleibt GENAU ein argv-Element ---------------------

def test_build_command_keeps_the_remote_command_as_one_argument() -> None:
    line = build_command("mac", "echo 'a; b' && cat /tmp/x")
    parts = shlex.split(line)
    assert parts[0] == "ssh"
    assert "BatchMode=yes" in parts
    # Nach dem Host genau EIN Element — die lokale Shell kann daraus kein zweites
    # Kommando gewinnen, egal wie viele `;`/`&&` das Fernkommando traegt.
    assert parts[-2] == "mac"
    assert parts[-1] == "echo 'a; b' && cat /tmp/x"


def test_build_command_does_not_turn_injection_into_local_shell() -> None:
    line = build_command("mac", "x'; touch /tmp/pwned; #'")
    parts = shlex.split(line)
    assert parts[-1] == "x'; touch /tmp/pwned; #'"


# --- Masken: genau eine Ausnahme, und es ist ~/.ssh ----------------------------------

def test_masked_list_drops_exactly_the_ssh_dir() -> None:
    ssh_dir = os.path.realpath(Path.home() / ".ssh")
    masked = remoteexec._masked_without_ssh()
    assert all(os.path.realpath(p) != ssh_dir for p in masked)
    dropped = [
        p for p in SHELL_FORBIDDEN_PREFIXES if os.path.realpath(p) == ssh_dir
    ]
    assert dropped, "Annahme: der Floor maskiert ~/.ssh — sonst ist der Test leer"
    assert len(masked) == len(SHELL_FORBIDDEN_PREFIXES) - len(dropped)


def test_backends_are_confined_only() -> None:
    names = {b.name for b in remoteexec.remote_backends()}
    assert "unconfined" not in names and "none" not in names


# --- Der Runner: Weigerungen vor dem ersten Byte, Ausgabe wie run_shell --------------

def test_runner_refuses_an_unknown_host_before_anything_starts() -> None:
    with pytest.raises(RemoteExecError, match="allowlist"):
        remoteexec.remote_exec(_req(host="eve", command="uptime"))


def test_runner_refuses_an_empty_command() -> None:
    with pytest.raises(RemoteExecError, match="non-empty"):
        remoteexec.remote_exec(_req(host="mac", command="  "))


def test_runner_refuses_an_overlong_command() -> None:
    with pytest.raises(RemoteExecError, match=str(MAX_COMMAND_CHARS)):
        remoteexec.remote_exec(_req(host="mac", command="x" * (MAX_COMMAND_CHARS + 1)))


def test_runner_refuses_without_any_configured_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TALOS_REMOTE_HOSTS", raising=False)
    with pytest.raises(RemoteExecError, match="no remote hosts"):
        remoteexec.remote_exec(_req(host="mac", command="uptime"))


def test_runner_refuses_a_host_that_is_not_a_plain_alias() -> None:
    with pytest.raises(RemoteExecError, match="plain ssh alias"):
        remoteexec.remote_exec(_req(host="mac;id", command="uptime"))


def test_runner_without_a_confined_backend_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remoteexec.sandbox, "select_backend", lambda _c: None)
    out = remoteexec.remote_exec(_req(host="mac", command="uptime"))
    assert out.startswith("rc=refused")


def test_runner_runs_networked_and_formats_like_run_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _Result:
        backend = "fake"
        returncode = 0
        stdout = "up 3 days\n"
        stderr = ""
        timed_out = False
        truncated = False

    def _fake_run(command: str, **kwargs: object) -> _Result:
        seen["command"] = command
        seen.update(kwargs)
        return _Result()

    monkeypatch.setattr(remoteexec.sandbox, "select_backend", lambda _c: object())
    monkeypatch.setattr(remoteexec.sandbox, "run_sandboxed", _fake_run)
    out = remoteexec.remote_exec(_req(host="mac", command="uptime"))
    assert seen["allow_network"] is True
    assert shlex.split(str(seen["command"]))[-1] == "uptime"
    assert out == "rc=0 [fake] mac\nup 3 days"
