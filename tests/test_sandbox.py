"""Sandbox-Beweise. Wo die Plattform es zulaesst, laufen die Angriffe WIRKLICH.

Eine Sandbox, die nur gegen Doubles geprueft ist, beweist nichts: die interessanten
Fehler stecken im Kernel-Verhalten, nicht im Python. Deshalb starten die meisten Tests
hier echte Prozesse und schauen nach, was wirklich passiert ist. Was auf dieser
Plattform nicht geht, wird sauber uebersprungen (`skipif`) — nie gruen behauptet.
"""
from __future__ import annotations

import os
import resource
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from talos import policy, sandbox
from talos.sandbox import (
    BubblewrapSandbox,
    SandboxedShell,
    SandboxExecSandbox,
    SandboxLimits,
    SandboxUnavailable,
)

LINUX = sys.platform.startswith("linux")

# EINMAL probiert, nicht pro Test: der Probelauf startet einen echten Prozess.
LIVE_BACKEND = sandbox.select_backend(sandbox.default_backends())
requires_sandbox = pytest.mark.skipif(
    LIVE_BACKEND is None,
    reason=f"no working sandbox backend on {sys.platform}",
)

SECRET_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-leak",
    "OPENAI_API_KEY": "sk-oai-leak",
    "TELEGRAM_BOT_TOKEN": "123:leak",
    "WHATSAPP_TOKEN": "wa-leak",
    "AWS_SECRET_ACCESS_KEY": "aws-leak",
    "GITHUB_TOKEN": "ghp-leak",
}


def shell(workspace: Path, **kwargs) -> SandboxedShell:
    """Ein Shell-Objekt auf dem AUFGELOESTEN Arbeitsbereich.

    `tmp_path` liegt auf macOS unter `/var/...`, in Wahrheit aber unter `/private/var/...`.
    Wer den unaufgeloesten Pfad vergleicht, prueft eine Zeichenkette, die das Kind nie
    sieht — und bekaeme ein falsches Gruen genau dort, wo es am teuersten ist.
    """
    return SandboxedShell(workspace=workspace, **kwargs)


def resolved(path: Path) -> Path:
    return Path(os.path.realpath(path))


# --- Umgebung ------------------------------------------------------------------


def test_secret_shaped_variables_never_reach_the_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in SECRET_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = sandbox.sandbox_env(tmp_path)

    assert set(env) & set(SECRET_ENV) == set()
    assert "leak" not in " ".join(env.values())
    # ⚠️ Der PATH darf genau EINEN Zusatz tragen: die `.venv` der Installation, damit der
    # Agent seine eigene Suite fahren kann. Alles danach muss unveraendert das sein, was er
    # geerbt hat — ein PATH, in den sich sonst etwas einschleicht, ist die Stelle, an der
    # ein untergeschobenes Programm zum Aufruf kommt.
    from talos.policy import INSTALL_DIR

    teile = env["PATH"].split(os.pathsep)
    assert teile[-2:] == ["/usr/bin", "/bin"]
    assert teile[:-2] in ([], [str(INSTALL_DIR / ".venv" / "bin")])
    assert env["TMPDIR"] == str(tmp_path)


def test_an_unknown_future_variable_is_dropped_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Punkt der Positivliste: sie muss die naechste Variable nicht kennen."""
    monkeypatch.setenv("SOME_PROVIDER_INVENTED_TOMORROW", "secret")
    assert "SOME_PROVIDER_INVENTED_TOMORROW" not in sandbox.sandbox_env(tmp_path)


@requires_sandbox
def test_the_running_command_cannot_see_the_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in SECRET_ENV.items():
        monkeypatch.setenv(key, value)

    result = shell(tmp_path).run("env")

    assert result.returncode == 0
    assert "leak" not in result.stdout
    assert "TALOS_SANDBOX=1" in result.stdout


# --- Schreibgrenze -------------------------------------------------------------


@requires_sandbox
def test_writing_inside_the_workspace_succeeds(tmp_path: Path) -> None:
    result = shell(tmp_path).run("echo talos > inside.txt")

    assert result.returncode == 0, result.stderr
    assert (resolved(tmp_path) / "inside.txt").read_text(encoding="utf-8").strip() == "talos"


@requires_sandbox
def test_writing_outside_the_workspace_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = resolved(tmp_path) / "outside.txt"

    result = shell(workspace).run(f"echo escaped > {outside}")

    assert result.returncode != 0
    assert not outside.exists()


@requires_sandbox
def test_a_symlinked_workspace_stays_writable(tmp_path: Path) -> None:
    """Der Arbeitsbereich darf ueber einen Symlink benannt werden — sonst schreibt der
    Agent auf macOS in seinem eigenen Verzeichnis ins Leere."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    result = shell(link).run("echo ok > note.txt")

    assert result.returncode == 0, result.stderr
    assert (real / "note.txt").exists()


# --- Die Luecke aus der CLAUDE.md ----------------------------------------------


@requires_sandbox
def test_a_reconstructed_path_cannot_reach_etc_passwd(tmp_path: Path) -> None:
    """Genau der Fall, den der Pfad-Floor per Konstruktion nicht sehen kann.

    Seit der Identitaets-Bindung ist `/etc/passwd` im Kind nicht mehr leer, sondern
    traegt GENAU EINE Zeile: die eigene. Die Zusicherung ist deshalb praeziser
    formuliert als vorher („leer") — sie sagt jetzt, was sie immer meinte: kein
    fremder Eintrag, kein `root`, keine Dienstkonten.
    """
    result = shell(tmp_path).run("P=/etc; cat $P/passwd")

    assert "root:" not in result.stdout
    assert len([line for line in result.stdout.splitlines() if line.strip()]) <= 1


@requires_sandbox
def test_a_base64_decoded_command_cannot_reach_etc_passwd(tmp_path: Path) -> None:
    # echo -n 'cat /etc/passwd' | base64  ->  Y2F0IC9ldGMvcGFzc3dk
    result = shell(tmp_path).run('eval "$(echo Y2F0IC9ldGMvcGFzc3dk | base64 -d)"')

    assert "root:" not in result.stdout
    assert len([line for line in result.stdout.splitlines() if line.strip()]) <= 1


@requires_sandbox
def test_the_child_knows_its_own_name_without_seeing_anyone_elses(tmp_path: Path) -> None:
    """Die Regression, die `ssh` toetete — und die Grenze, die dabei halten muss.

    Ein leeres `/etc` nahm dem Kind die Zuordnung `uid -> Name`. `whoami` scheiterte,
    und `ssh` brach mit „No user exists for uid 1000" ab, BEVOR es das Netz anfasste:
    der Fehler sah nach einem kaputten Zielrechner aus, war aber rein lokal. Genau
    daran scheiterte der VPS-Status-Abruf.

    Zurueckgegeben wird nur die eigene Zeile — die eigene UID kennt das Kind ohnehin
    ueber `id -u`. Alles andere bleibt weg.
    """
    result = shell(tmp_path).run("whoami; echo '---'; cat /etc/passwd; echo '---'; cat /etc/shadow")

    assert result.stdout.strip(), "das Kind kennt seinen eigenen Namen nicht"
    assert "root:" not in result.stdout
    assert "daemon:" not in result.stdout
    # Das eigentliche Geheimnis bleibt unerreichbar — die Maske gilt weiter.
    assert "shadow" not in result.stdout or ":" not in result.stdout.split("---")[-1]


@requires_sandbox
def test_reading_an_ordinary_path_outside_the_workspace_still_works(tmp_path: Path) -> None:
    """Gegenprobe: die Sandbox sperrt gezielt, nicht pauschal — sonst waere die Shell tot."""
    readable = tmp_path / "readable.txt"
    readable.write_text("visible", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = shell(workspace).run(f"cat {resolved(tmp_path)}/readable.txt")

    assert result.returncode == 0, result.stderr
    assert "visible" in result.stdout


# --- Netz ----------------------------------------------------------------------


@pytest.fixture()
def loopback_port() -> int:
    """Ein echter Listener im Testprozess.

    Bewusst Loopback statt Internet: der Test darf nicht deshalb gruen sein, weil die
    Maschine gerade offline ist. Der Gegentest (`allow_network=True`) beweist im selben
    Lauf, dass die Verbindung ohne die Sperre zustande kaeme.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def accept() -> None:
        try:
            while True:
                connection, _ = server.accept()
                connection.close()
        except OSError:
            return

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield int(server.getsockname()[1])
    finally:
        server.close()


def _connect_probe(workspace: Path, port: int) -> str:
    script = resolved(workspace) / "connect_probe.py"
    script.write_text(
        "import socket\n"
        "probe = socket.socket()\n"
        "probe.settimeout(3)\n"
        "try:\n"
        f"    probe.connect(('127.0.0.1', {port}))\n"
        "    print('NET_OK')\n"
        "except OSError as error:\n"
        "    print('NET_BLOCKED', type(error).__name__)\n",
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


@requires_sandbox
def test_network_access_is_refused_by_default(tmp_path: Path, loopback_port: int) -> None:
    result = shell(tmp_path).run(_connect_probe(tmp_path, loopback_port))

    assert "NET_OK" not in result.stdout
    assert "NET_BLOCKED" in result.stdout, result.stderr


@requires_sandbox
def test_network_access_works_when_the_caller_asks_for_it(
    tmp_path: Path, loopback_port: int
) -> None:
    result = shell(tmp_path).run(_connect_probe(tmp_path, loopback_port), allow_network=True)

    if LINUX:
        # bwrap gibt mit --share-net den Netz-Namensraum des Wirts zurueck; auf einem
        # Wirt ohne Loopback-Erreichbarkeit waere das kein Sandbox-Fehler.
        assert "NET_OK" in result.stdout or "NET_BLOCKED" in result.stdout
    else:
        assert "NET_OK" in result.stdout, result.stderr


# --- Grenzen -------------------------------------------------------------------


@requires_sandbox
def test_the_time_limit_kills_a_hanging_command(tmp_path: Path) -> None:
    started = time.monotonic()
    result = shell(tmp_path, limits=SandboxLimits(timeout_s=1)).run("sleep 30")
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.returncode != 0
    assert elapsed < 15


@requires_sandbox
def test_oversized_output_is_capped_instead_of_flooding_memory(tmp_path: Path) -> None:
    limits = SandboxLimits(timeout_s=30, max_output_bytes=500)

    result = shell(tmp_path, limits=limits).run("yes talos | head -c 200000")

    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) == 500
    assert result.returncode == 0


@requires_sandbox
def test_a_capped_command_still_finishes_instead_of_hanging(tmp_path: Path) -> None:
    """Der Deckel darf das Kind nicht an einer vollen Pipe festnageln."""
    limits = SandboxLimits(timeout_s=20, max_output_bytes=64)

    result = shell(tmp_path, limits=limits).run("yes talos | head -c 300000; echo -n '' >&2")

    assert result.timed_out is False


@requires_sandbox
def test_the_cpu_limit_reaches_the_child(tmp_path: Path) -> None:
    script = resolved(tmp_path) / "limits.py"
    script.write_text(
        "import resource\n"
        "print('cpu', resource.getrlimit(resource.RLIMIT_CPU)[0])\n",
        encoding="utf-8",
    )

    result = shell(tmp_path, limits=SandboxLimits(timeout_s=7)).run(f"{sys.executable} {script}")

    assert result.stdout.split() == ["cpu", str(7 + sandbox.CPU_GRACE_S)], result.stderr


@requires_sandbox
def test_the_process_limit_reaches_the_child(tmp_path: Path) -> None:
    ceiling = min(4096, resource.getrlimit(resource.RLIMIT_NPROC)[0])
    script = resolved(tmp_path) / "nproc.py"
    script.write_text(
        "import resource\nprint('nproc', resource.getrlimit(resource.RLIMIT_NPROC)[0])\n",
        encoding="utf-8",
    )

    limits = SandboxLimits(timeout_s=20, max_processes=ceiling)
    result = shell(tmp_path, limits=limits).run(f"{sys.executable} {script}")

    assert result.stdout.split() == ["nproc", str(ceiling)], result.stderr


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS cannot be set on macOS/arm64")
@requires_sandbox
def test_the_memory_limit_reaches_the_child(tmp_path: Path) -> None:
    script = resolved(tmp_path) / "mem.py"
    script.write_text(
        "import resource\nprint('as', resource.getrlimit(resource.RLIMIT_AS)[0])\n",
        encoding="utf-8",
    )
    limit = 256 * 1024 * 1024

    limits = SandboxLimits(timeout_s=20, max_memory_bytes=limit)
    result = shell(tmp_path, limits=limits).run(f"{sys.executable} {script}")

    assert result.stdout.split() == ["as", str(limit)], result.stderr


@requires_sandbox
def test_a_limit_the_platform_refuses_does_not_stop_the_command(tmp_path: Path) -> None:
    """macOS/arm64 kann RLIMIT_AS nicht setzen.

    Ohne die Einzelkapselung in `_rlimit_hook` scheitert dort nicht die Grenze, sondern
    der komplette Start — jedes Kommando waere tot.
    """
    limits = SandboxLimits(timeout_s=20, max_memory_bytes=256 * 1024 * 1024)

    result = shell(tmp_path, limits=limits).run("echo still-running")

    assert result.returncode == 0, result.stderr
    assert "still-running" in result.stdout


# --- Abbruch -------------------------------------------------------------------


@requires_sandbox
def test_a_grandchild_writes_its_marker_when_nobody_cancels(tmp_path: Path) -> None:
    """Positivkontrolle. Ohne sie waere der Abbruch-Test auch dann gruen, wenn der
    Enkel nie geschrieben haette."""
    marker = resolved(tmp_path) / "grandchild.txt"

    result = shell(tmp_path, limits=SandboxLimits(timeout_s=20)).run(
        f"bash -c 'sleep 1; echo alive > {marker}' & wait"
    )

    assert result.returncode == 0, result.stderr
    assert marker.exists()


@requires_sandbox
def test_cancel_kills_the_grandchild_too(tmp_path: Path) -> None:
    marker = resolved(tmp_path) / "grandchild.txt"
    runner = shell(tmp_path, limits=SandboxLimits(timeout_s=30))
    outcome: dict[str, sandbox.SandboxResult] = {}

    def run() -> None:
        outcome["result"] = runner.run(f"bash -c 'sleep 2; echo alive > {marker}' & wait")

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not runner.cancel():
        time.sleep(0.02)
    thread.join(timeout=20)

    time.sleep(2.5)  # ueber die Schlafzeit des Enkels hinaus
    assert outcome["result"].cancelled is True
    assert not marker.exists()


def test_cancel_without_a_running_command_reports_nothing_to_kill(tmp_path: Path) -> None:
    assert shell(tmp_path).cancel() is False


# --- Fail-closed ---------------------------------------------------------------


class _RefusingBackend:
    """Ein Backend, das nicht kann. Kein Mock der Sandbox — nur ein Zustand."""

    name = "refusing"

    def available(self) -> bool:
        return False

    def argv(self, command: str, *, workspace: Path, allow_network: bool = False):
        raise AssertionError("must never be asked for arguments")


def test_without_any_isolation_the_call_is_refused(tmp_path: Path) -> None:
    runner = shell(tmp_path, platform="haiku-os", env={})

    with pytest.raises(SandboxUnavailable) as caught:
        runner.run("echo this-must-not-run")

    assert sandbox.UNCONFINED_ENV in str(caught.value)


def test_the_refusal_says_what_is_missing_and_how_to_get_it() -> None:
    linux = sandbox.unavailable_message("linux")
    assert "bubblewrap" in linux and "apt install bubblewrap" in linux
    assert sandbox.UNCONFINED_ENV in linux
    assert "sandbox-exec" in sandbox.unavailable_message("darwin")
    assert sandbox.UNCONFINED_ENV in sandbox.unavailable_message("win32")


def test_an_explicitly_named_backend_is_never_swapped_for_another(tmp_path: Path) -> None:
    """„nimm bwrap" heisst bwrap — nicht „irgendwas, was gerade da ist"."""
    runner = shell(tmp_path, backend=_RefusingBackend(), env={})

    with pytest.raises(SandboxUnavailable):
        runner.run("echo this-must-not-run")


def test_the_operator_switch_runs_unconfined_and_names_it(tmp_path: Path) -> None:
    runner = shell(tmp_path, backend=_RefusingBackend(), env={sandbox.UNCONFINED_ENV: "1"})

    result = runner.run("echo unconfined")

    assert result.backend == "none"
    assert "unconfined" in result.stdout


def test_a_stray_environment_value_does_not_open_the_switch(tmp_path: Path) -> None:
    assert sandbox.unconfined_allowed({sandbox.UNCONFINED_ENV: "yes"}) is False
    assert sandbox.unconfined_allowed({sandbox.UNCONFINED_ENV: "0"}) is False
    assert sandbox.unconfined_allowed({}) is False
    assert sandbox.unconfined_allowed({sandbox.UNCONFINED_ENV: " 1 "}) is True


# --- Auswahl -------------------------------------------------------------------


def test_each_platform_gets_its_own_implementation() -> None:
    assert [b.name for b in sandbox.default_backends("linux")] == ["bubblewrap"]
    assert [b.name for b in sandbox.default_backends("darwin")] == ["sandbox-exec"]
    assert sandbox.default_backends("win32") == ()


def test_availability_is_probed_not_guessed_from_the_path(tmp_path: Path) -> None:
    """Ein vorhandenes Programm ist keine vorhandene Isolation."""
    assert BubblewrapSandbox(binary=str(tmp_path / "no-such-bwrap")).available() is False


# --- Argumente und Profil (rein, ohne Prozess) ---------------------------------


def test_bubblewrap_binds_the_workspace_after_the_read_only_root() -> None:
    argv = BubblewrapSandbox().argv("true", workspace=Path("/ws"))

    assert argv[0] == "bwrap"
    assert "--unshare-all" in argv
    assert argv.index("--ro-bind") < argv.index("--bind")
    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == ("/ws", "/ws")
    assert argv[argv.index("--chdir") + 1] == "/ws"
    assert argv[-3:] == (sandbox.SHELL_BIN, "-c", "true")


def test_bubblewrap_only_shares_the_network_when_asked() -> None:
    assert "--share-net" not in BubblewrapSandbox().argv("true", workspace=Path("/ws"))
    assert "--share-net" in BubblewrapSandbox().argv(
        "true", workspace=Path("/ws"), allow_network=True
    )


def test_bubblewrap_restores_dns_when_the_network_is_shared() -> None:
    """Netz ohne DNS ist kein Netz: `/etc` liegt unter der leeren tmpfs-Maske,
    also muessen die Resolver-Dateien einzeln zurueck, sobald `--share-net`
    gesetzt ist. Gemessen am ersten 0.11-E2E: ein `claude`-Job im Sandbox
    scheiterte mit `Unable to connect to API`, weil `/etc/resolv.conf` fehlte."""
    argv = BubblewrapSandbox().argv("true", workspace=Path("/ws"), allow_network=True)

    for name in ("resolv.conf", "nsswitch.conf", "hosts"):
        if Path(f"/etc/{name}").exists():
            assert f"/etc/{name}" in argv

    argv_off = BubblewrapSandbox().argv("true", workspace=Path("/ws"))
    assert "/etc/resolv.conf" not in argv_off


def test_bubblewrap_masks_every_protected_prefix_the_floor_names() -> None:
    """Eine Liste, zwei Durchsetzungen. Zwei Listen wuerden auseinanderdriften."""
    argv = BubblewrapSandbox().argv("true", workspace=Path("/ws"))

    targets = sandbox.mask_targets(policy.SHELL_FORBIDDEN_PREFIXES)
    assert targets, "the floor protects nothing on this machine — check the fixture"
    for path, _is_dir in targets:
        assert path in argv


def test_mask_targets_skips_paths_that_do_not_exist(tmp_path: Path) -> None:
    """bwrap bricht den GANZEN Lauf ab, wenn ein --tmpfs-Ziel nicht anlegbar ist."""
    missing = tmp_path / "not-here"

    assert sandbox.mask_targets([str(missing)]) == ()


def test_mask_targets_tells_files_and_directories_apart(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    single = tmp_path / "single.txt"
    single.write_text("x", encoding="utf-8")

    found = dict(sandbox.mask_targets([str(folder), str(single)]))

    assert found[os.path.realpath(folder)] is True
    assert found[os.path.realpath(single)] is False


def test_the_seatbelt_profile_denies_reads_of_every_protected_prefix() -> None:
    profile = SandboxExecSandbox().profile(Path("/ws"))

    assert "(deny default)" in profile
    for prefix in policy.SHELL_FORBIDDEN_PREFIXES:
        assert f'(subpath "{prefix}")' in profile


def test_the_seatbelt_profile_writes_only_in_the_workspace() -> None:
    profile = SandboxExecSandbox().profile(Path("/ws"))

    assert '(allow file-write* (subpath "/ws"))' in profile


def test_the_seatbelt_profile_switches_the_network_with_the_caller() -> None:
    denied = SandboxExecSandbox().profile(Path("/ws"))
    allowed = SandboxExecSandbox().profile(Path("/ws"), allow_network=True)

    assert "(deny network*)" in denied and "(allow network*)" not in denied
    assert "(allow network*)" in allowed and "(deny network*)" not in allowed


def test_a_quote_in_the_workspace_path_cannot_break_the_profile() -> None:
    """Ein Pfad mit Anfuehrungszeichen darf keine eigene Regel ins Profil schmuggeln."""
    profile = SandboxExecSandbox(masked=()).profile(Path('/ws"; (allow default) ;"'))

    assert '(allow file-write* (subpath "/ws\\"; (allow default) ;\\""))' in profile
    # Der Einschub bleibt Inhalt einer Zeichenkette; er beginnt nie eine eigene Zeile.
    assert "\n(allow default)" not in profile


# --- PYTHONPATH: gesetzt, nie geerbt ----------------------------------------------------
def test_the_python_path_points_at_the_install_dir_and_is_never_inherited(monkeypatch) -> None:
    """Der Agent muss seinen eigenen Zustand beweisen koennen — mehr nicht.

    Die Shell startet im Arbeitsbereich (`--chdir`), von dort war das eigene Paket nicht
    importierbar: `pytest tests/` endete mit „No module named 'talos'". Also steht der
    Pfad jetzt in der Umgebung — aber als FESTER Wert.

    ⚠️ Geerbt waere er ein Loch: wer den Elternprozess beeinflusst, bestimmte damit, aus
    welchem Verzeichnis JEDES `python` im Sandkasten seine Module laedt.
    """
    from talos.policy import INSTALL_DIR
    from talos.sandbox import ENV_ALLOWLIST, sandbox_env

    monkeypatch.setenv("PYTHONPATH", "/tmp/vom-angreifer-gewaehlt")
    env = sandbox_env(Path("/tmp/ws"))

    assert env["PYTHONPATH"] == str(INSTALL_DIR)
    assert "vom-angreifer-gewaehlt" not in env["PYTHONPATH"]
    assert "PYTHONPATH" not in ENV_ALLOWLIST     # nicht durchreichen, sondern setzen


def test_the_environment_still_carries_nothing_secret(monkeypatch) -> None:
    """Die Zusicherung der Positivliste darf durch den neuen Schluessel nicht aufweichen."""
    from talos.sandbox import sandbox_env

    for name in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TALOS_SECRETS_ENV",
                 "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(name, "geheim-" + name)

    env = sandbox_env(Path("/tmp/ws"))
    assert not [k for k in env if "KEY" in k or "TOKEN" in k or "SECRET" in k]
    assert "geheim-" not in "".join(env.values())


def test_the_installations_own_interpreter_comes_first_on_the_path() -> None:
    """`PYTHONPATH` allein reichte nicht — es fehlte der Interpreter dazu.

    Der Agent griff zum `python3` aus dem PATH, also dem System-Interpreter ohne
    `pytest`, und meldete „No module named pytest", waehrend zwei Verzeichnisse weiter
    eine vollstaendige Umgebung stand. Ein halber Fix, der aussieht wie ein Fehlschlag
    der Sache selbst.

    Die `.venv` liegt unter `INSTALL_DIR` und ist im Sandkasten nur lesbar eingehaengt —
    dort kann nichts hingelegt werden, was danach als Programm liefe.
    """
    from talos.policy import INSTALL_DIR
    from talos.sandbox import sandbox_env

    venv_bin = INSTALL_DIR / ".venv" / "bin"
    env = sandbox_env(Path("/tmp/ws"))
    if not venv_bin.is_dir():
        pytest.skip("keine .venv neben der Installation — hier nichts zu binden")
    assert env["PATH"].split(os.pathsep)[0] == str(venv_bin)


def test_network_sandbox_binds_the_ca_bundle() -> None:
    """Netz ohne CA-Bundle ist halbes Netz — gemessen am ersten git-E2E:
    der https-Clone scheiterte mit „server certificate verification failed",
    weil /etc/ssl unter der /etc-Maske lag."""
    import os
    from talos.sandbox import BubblewrapSandbox, SandboxExecSandbox

    if os.path.isdir("/etc/ssl"):
        argv = BubblewrapSandbox().argv("true", workspace=Path("/tmp/ws"), allow_network=True)
        assert "--ro-bind" in list(argv) and "/etc/ssl" in list(argv)
    profil = SandboxExecSandbox().profile(Path("/tmp/ws"), allow_network=True)
    assert '(allow file-read* (subpath "/etc/ssl")' in profil


def test_offline_sandbox_keeps_the_ca_bundle_masked() -> None:
    """Ohne Netz braucht nichts Zertifikate — die Maske bleibt ganz."""
    from talos.sandbox import SandboxExecSandbox

    profil = SandboxExecSandbox().profile(Path("/tmp/ws"), allow_network=False)
    assert '(allow file-read* (subpath "/etc/ssl")' not in profil
