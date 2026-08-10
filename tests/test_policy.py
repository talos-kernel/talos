"""Policy-Kernel: deterministisches, fail-closed Gating."""
from __future__ import annotations

from pathlib import Path

from talos.manifest import Effect, ToolManifest, ToolSpec
from talos.policy import PolicyKernel, ToolRequest, Verdict
from talos.channel import Principal

OWNER = Principal("telegram", "100000001")


def _manifest() -> ToolManifest:
    return (
        ToolManifest()
        .with_tool(ToolSpec("read_file", Effect.READ, reversible=True))
        .with_tool(ToolSpec("write_file", Effect.WRITE, reversible=True))
        .with_tool(ToolSpec("send_mail", Effect.EXEC, reversible=False))
    )


def _kernel(**over) -> PolicyKernel:
    base = dict(
        manifest=_manifest(),
        allowed_identities=frozenset({OWNER}),
    )
    base.update(over)
    return PolicyKernel(**base)


def test_unknown_tool_is_denied_fail_closed() -> None:
    d = _kernel().decide(ToolRequest("does_not_exist", OWNER, {}))
    assert d.verdict is Verdict.DENY


def test_read_is_allowed_freely() -> None:
    d = _kernel().decide(ToolRequest("read_file", OWNER, {}, ("/tmp/x",)))
    assert d.verdict is Verdict.ALLOW


def test_reversible_write_is_allowed() -> None:
    d = _kernel().decide(ToolRequest("write_file", OWNER, {}, ("/tmp/x",)))
    assert d.verdict is Verdict.ALLOW


def test_irreversible_exec_needs_human() -> None:
    d = _kernel().decide(ToolRequest("send_mail", OWNER, {}))
    assert d.verdict is Verdict.NEEDS_HUMAN


def test_foreign_identity_is_denied() -> None:
    d = _kernel().decide(ToolRequest("read_file", 111, {}, ("/tmp/x",)))
    assert d.verdict is Verdict.DENY


def test_secret_write_needs_human() -> None:
    # the operator: Schreiben auf Secret soll ihn fragen, nicht hart blockieren.
    secret = str(Path.home() / ".secrets" / "talos-telegram.env")
    d = _kernel().decide(ToolRequest("write_file", OWNER, {}, (secret,)))
    assert d.verdict is Verdict.NEEDS_HUMAN
    assert "approval" in d.reason


def test_secret_read_stays_blocked() -> None:
    # Lesen bleibt gesperrt (Leak-Schutz), auch wenn Schreiben fragt.
    secret = str(Path.home() / ".secrets" / "talos-telegram.env")
    d = _kernel().decide(ToolRequest("read_file", OWNER, {}, (secret,)))
    assert d.verdict is Verdict.DENY


def test_system_path_stays_hard_denied() -> None:
    d = _kernel().decide(ToolRequest("write_file", OWNER, {}, ("/etc/passwd",)))
    assert d.verdict is Verdict.DENY
    assert "system path" in d.reason


def test_the_agent_cannot_write_itself_into_its_own_allowlist(tmp_path: Path) -> None:
    """Der Fund vom 05.08. — und er lag nicht im Kernel, sondern neben ihm.

    `talos.env` traegt drei Dinge zugleich: den Bot-Token, die API-Schluessel und
    `TALOS_ALLOWED_PRINCIPALS`, also die Liste derer, die dem Agenten ueberhaupt etwas
    sagen duerfen. Der Installer legt sie mit `chmod 600` genau dorthin und fordert den
    Nutzer auf, sie auszufuellen — sie war trotzdem ein gewoehnliches `write_file`-Ziel
    mit ALLOW. Der Kernel blieb intakt; nur seine Identitaetsliste kam aus einer Datei,
    die der Agent selbst beschreiben durfte. Das ist dieselbe Klasse wie `~/.bashrc`:
    eine Rechteerweiterung ueber den Umweg „gewoehnliche Datei".
    """
    from talos.policy import INSTALL_DIR
    from talos.tools import default_manifest

    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    env = str(INSTALL_DIR / "talos.env")

    schreiben = kernel.decide(
        ToolRequest("write_file", OWNER,
                    {"path": env, "content": "TALOS_ALLOWED_PRINCIPALS=telegram:666"})
    )
    assert schreiben.verdict is Verdict.NEEDS_HUMAN

    # Lesen ist haerter als Schreiben: der Token steht darin, und ein Leak laesst sich
    # nicht zuruecknehmen — anders als ein Schreiben, das der Snapshotter sichert.
    lesen = kernel.decide(ToolRequest("read_file", OWNER, {"path": env}))
    assert lesen.verdict is Verdict.DENY

    # Auch nicht auf dem Umweg ueber ein Medienwerkzeug: `see_image` und `grab_frame`
    # haben ein echtes Ziel, also greift derselbe Floor.
    for werkzeug in ("see_image", "grab_frame"):
        assert kernel.decide(ToolRequest(werkzeug, OWNER, {"path": env})).verdict is Verdict.DENY


def test_the_floor_does_not_import_the_module_that_loads_the_file() -> None:
    """Ein Floor, der `config.py` fragt, schuetzt die Datei erst, nachdem sie gelesen
    wurde — und haengt dann an den Werten, die er schuetzen soll. Deshalb liest
    `policy._config_files` die Umgebung selbst."""
    import ast

    from talos import policy

    baum = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    importiert = {
        alias.name for knoten in ast.walk(baum)
        if isinstance(knoten, ast.ImportFrom) and knoten.level == 1
        for alias in knoten.names
    } | {
        knoten.module for knoten in ast.walk(baum)
        if isinstance(knoten, ast.ImportFrom) and knoten.module
    }
    assert "config" not in importiert
    assert any(ort.endswith("talos.env") for ort in policy._config_files())


def test_a_second_instance_protects_its_own_config_file(monkeypatch) -> None:
    """`TALOS_SECRETS_ENV` zeigt bei einer zweiten Instanz woanders hin. Der Floor muss
    DIESE Datei schuetzen, sonst schuetzt er den Standardpfad einer Installation, die es
    auf der Maschine gar nicht gibt.

    Bewusst in einem eigenen Prozess: der Floor wird beim Import gebaut, und genau so
    startet ein Dienst auch. `importlib.reload` waere der naheliegende Weg und ist eine
    Falle — er erzeugt neue Enum-Klassen, waehrend jedes andere Modul die alten haelt.
    Der Versuch hat 49 fremde Tests gerissen, die mit alledem nichts zu tun haben.
    """
    import subprocess
    import sys

    fremd = "/opt/zweiter-agent/geheim.env"
    programm = (
        "from talos.channel import Principal;"
        "from talos.policy import PolicyKernel, ToolRequest;"
        "from talos.tools import default_manifest;"
        "o=Principal('telegram','1');"
        "k=PolicyKernel(default_manifest(), frozenset({o}));"
        f"print(k.decide(ToolRequest('read_file', o, {{'path': {fremd!r}}})).verdict.name)"
    )
    lauf = subprocess.run(
        [sys.executable, "-c", programm],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True, text=True, timeout=60,
        env={"TALOS_SECRETS_ENV": fremd, "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )
    assert lauf.stdout.strip() == "DENY", lauf.stderr[-400:]
