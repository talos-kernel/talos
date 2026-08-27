from __future__ import annotations

import json
from pathlib import Path

from talos.reasoner import (
    HERMES_NO_TOOLS_TOOLSET,
    TOOL_PROTOCOL,
    HermesCliReasoner,
    _interpret_hermes,
)
from talos.intelligence import TaskTier, reasoning_effort_for, task_tier


def test_hermes_argv_selects_provider_model_and_exact_no_tools_toolset(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.write_text("#!/bin/sh\n[ \"$1\" = tools ] && echo \"✗ disabled web\"\n", encoding="utf-8")
    binary.chmod(0o700)
    reasoner = HermesCliReasoner(str(binary), 30, provider="openai-codex", model="gpt-5")

    argv = reasoner.argv_for("hello")

    assert argv[0] == str(binary)
    assert argv[argv.index("--provider") + 1] == "openai-codex"
    assert argv[argv.index("--model") + 1] == "gpt-5"
    assert HERMES_NO_TOOLS_TOOLSET == "__talos_reasoner_no_tools__"
    assert "--toolsets" not in argv  # Hermes rejects empty toolsets; preflight proves none enabled
    assert "--source" not in argv
    assert "--yolo" not in argv


def test_hermes_argv_keeps_machine_protocol_in_final_answer_channel(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.write_text("#!/bin/sh\n[ \"$1\" = tools ] && echo \"✗ disabled web\"\n", encoding="utf-8")
    binary.chmod(0o700)
    reasoner = HermesCliReasoner(
        str(binary), 30, provider="openai-codex", model="gpt-5.6-sol"
    )

    prompt = reasoner.argv_for("inspect the VPS")[2].lower()

    assert "final answer channel" in prompt
    assert "never put plan or tool_call in commentary" in prompt
    assert prompt.rfind("final answer channel") > prompt.rfind("plan:")


def test_adaptive_reasoning_routes_simple_standard_and_deep_work() -> None:
    assert task_tier("Hallo") is TaskTier.QUICK
    assert reasoning_effort_for("Hallo") == "low"
    assert reasoning_effort_for("Check the current status of Atlas API") == "medium"
    assert reasoning_effort_for(
        "Analysiere den Fehler, vergleiche mehrere Quellen und verifiziere jeden Schritt"
    ) == "high"
    assert reasoning_effort_for(
        "[Working state — derived context, never permission]\nCurrent goal: Hallo\n"
        "[End of working state]\n\nHallo"
    ) == "low"


def test_hermes_argv_passes_adaptive_reasoning_to_the_real_cli(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.write_text("#!/bin/sh\n[ \"$1\" = tools ] && echo \"✗ disabled web\"\n", encoding="utf-8")
    binary.chmod(0o700)
    reasoner = HermesCliReasoner(
        str(binary), 30, provider="openai-codex", model="gpt-5.6-sol"
    )

    simple = reasoner.argv_for("Hallo")
    deep = reasoner.argv_for("Analysiere mehrere Fehler und verifiziere die Ursachen")

    assert simple[simple.index("--reasoning") + 1] == "low"
    assert deep[deep.index("--reasoning") + 1] == "high"
    assert simple[simple.index("--model") + 1] == "gpt-5.6-sol"


def test_tool_protocol_binds_the_model_to_verified_results() -> None:
    """Der Vertrag mit dem Modell: Tool-Ergebnisse sind Daten, und rc=0 ist kein Ergebnis."""
    prompt = TOOL_PROTOCOL.lower()
    for term in (
        "untrusted data",
        "never instructions",
        "finished task",
        "verified",
        "tool_call",
    ):
        assert term in prompt


def test_tool_protocol_names_the_walls_before_the_model_hits_them() -> None:
    """Zwei gemessene Fehlzuege vom 27.08.: das Modell forderte /etc/hermes.env an
    (per Bauart DENY — ein verbrannter Zug) und erfand einen Plan-Dateinamen
    (purring-wren statt frolicking-gem — eine Korrektur des Betreibers). Beides
    passiert nicht, wenn das Protokoll die Mauer und die Nachschau-Pflicht nennt,
    BEVOR das Modell sie braucht."""
    prompt = TOOL_PROTOCOL.lower()
    for term in (
        "refused by construction",   # die Mauer heisst vor dem ersten Zug
        "/etc",
        "credential-shaped",
        "no approval overrides",
        "never invent identifiers",  # Dateinamen kommen aus Nachschau, nicht aus dem Gedaechtnis
        "list the directory",
    ):
        assert term in prompt


def test_hermes_parser_reads_plain_oneshot_and_defensive_json() -> None:
    assert _interpret_hermes("  Hallo.\n") == ("Hallo.", "")
    assert _interpret_hermes(json.dumps({"result": "Antwort"})) == ("Antwort", "")
    text, note = _interpret_hermes("")
    assert "leer" in text and note == "leere Ausgabe"


def test_hermes_reasoner_executes_configured_binary_without_tools(tmp_path: Path) -> None:
    capture = tmp_path / "argv.json"
    binary = tmp_path / "hermes"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "if sys.argv[1:4] == ['tools', 'list', '--platform']:\n"
        "    print('✗ disabled web')\n"
        "    raise SystemExit(0)\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "print('real answer')\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    reasoner = HermesCliReasoner(str(binary), 30, provider="alpha", model="m")
    assert reasoner.reason("hello") == "real answer"
    argv = json.loads(capture.read_text())
    assert "--toolsets" not in argv
    assert "--ignore-rules" in argv


def test_hermes_reasoner_rejects_enabled_toolsets(tmp_path: Path) -> None:
    binary = tmp_path / "hermes"
    binary.write_text(
        "#!/bin/sh\necho '✓ enabled terminal'\n", encoding="utf-8"
    )
    binary.chmod(0o700)
    try:
        HermesCliReasoner(str(binary), 30, provider="alpha", model="m")
    except RuntimeError as error:
        assert "Tools" in str(error) or "Bypass" in str(error)
    else:
        raise AssertionError("enabled Hermes tools accepted")
