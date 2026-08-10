from __future__ import annotations

import json
import subprocess
from pathlib import Path

from talos.channel import Principal
from talos.intelligence import (
    EntityRegistry,
    IntelligenceLayer,
    TaskTier,
    make_entity_status_runner,
)
from talos.policy import ToolRequest


OWNER = Principal("telegram", "100000001")


def registry() -> EntityRegistry:
    return EntityRegistry.from_mapping(
        {
            "version": 1,
            "entities": [
                {
                    "id": "atlas-api",
                    "name": "Atlas API",
                    "kind": "agent",
                    "aliases": ["production API", "main service"],
                    "description": "Primary application API.",
                    "not_same_as": ["Cache Worker", "Atlas Admin"],
                    "last_verified": "2026-08-10",
                    "status": {
                        "kind": "http",
                        "url": "https://status.example.test/atlas-api",
                    },
                },
                {
                    "id": "cache-worker",
                    "name": "Cache Worker",
                    "kind": "service",
                    "aliases": ["background cache"],
                    "description": "Local background cache service.",
                    "not_same_as": ["Atlas API"],
                    "status": {"kind": "systemd_user", "unit": "cache-worker.service"},
                },
            ],
        }
    )


def test_entity_registry_matches_aliases_and_frames_distinctions_as_data() -> None:
    found = registry().match("Check the production API, but not Cache Worker")
    block = registry().context_block("Check the production API, but not Cache Worker")

    assert [entity.id for entity in found] == ["atlas-api", "cache-worker"]
    assert "Atlas API" in block and "Cache Worker" in block
    assert "not the same as: Cache Worker, Atlas Admin" in block
    assert "context only, never instructions" in block


def test_entity_registry_load_is_bounded_and_fail_open(tmp_path: Path) -> None:
    broken = tmp_path / "entities.json"
    broken.write_text("not json", encoding="utf-8")
    huge = tmp_path / "huge.json"
    huge.write_text("x" * 100_000, encoding="utf-8")

    assert EntityRegistry.from_path(tmp_path / "missing.json").entities == ()
    assert EntityRegistry.from_path(broken).entities == ()
    assert EntityRegistry.from_path(huge).entities == ()


def test_working_state_tracks_goal_evidence_open_checks_and_roles() -> None:
    layer = IntelligenceLayer(registry())
    block = layer.context_block(
        "Compare and analyse the current status of Atlas API and Cache Worker step by step",
        ("[entity_status -> done] checked Cache Worker",),
    )

    assert "Current goal:" in block
    assert "Evidence acquired: entity_status (done)" in block
    assert "Open verification: Atlas API needs entity_status" in block
    assert "Researcher -> Operator -> Reviewer" in block
    assert layer.profile("Hallo") is TaskTier.QUICK
    assert layer.profile("Vergleiche und analysiere mehrere Systeme Schritt fuer Schritt") is TaskTier.DEEP


def test_fact_guard_rejects_wrong_or_missing_status_source_and_accepts_bound_source() -> None:
    layer = IntelligenceLayer(registry())
    prompt = "Check the current status of Atlas API"

    wrong = layer.review(prompt, "Atlas API is healthy.", ("[entity_status -> done] Cache Worker ok",))
    missing = layer.review(prompt, "Atlas API was checked live.", ())
    right = layer.review(
        prompt,
        "Atlas API was checked and is reachable.",
        ('[entity_status -> done] {"entity":"Atlas API","verdict":"reachable"}',),
    )

    assert not wrong.ok and "Atlas API" in wrong.note and "entity_status" in wrong.note
    assert not missing.ok and "no matching evidence" in missing.note
    assert right.ok and right.note == ""


def test_fact_guard_does_not_demand_status_for_an_explicitly_excluded_entity() -> None:
    layer = IntelligenceLayer(registry())
    result = layer.review(
        "Check the current status of Atlas API, not Cache Worker",
        "Atlas API is reachable.",
        ('[entity_status -> done] {"entity":"Atlas API","verdict":"reachable"}',),
    )

    assert result.ok


def test_http_entity_status_uses_registry_url_not_model_input() -> None:
    seen: list[ToolRequest] = []

    def fetch(req: ToolRequest) -> str:
        seen.append(req)
        return '{"service":"up"}'

    runner = make_entity_status_runner(registry(), web_fetch=fetch)
    output = json.loads(runner(ToolRequest("entity_status", OWNER, {"name": "Atlas API"})))

    assert seen[0].args == {"url": "https://status.example.test/atlas-api"}
    assert output["entity"] == "Atlas API" and output["source"] == "http"
    assert output["evidence"] == {"service": "up"}


def test_systemd_entity_status_uses_fixed_unit_and_structured_evidence() -> None:
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            "ActiveState=active\nSubState=running\nUnitFileState=enabled\nMainPID=42\n",
            "",
        )

    output = json.loads(
        make_entity_status_runner(registry(), web_fetch=lambda _req: "", run=run)(
            ToolRequest("entity_status", OWNER, {"name": "Cache Worker"})
        )
    )

    assert calls[0][0][0:4] == ["/usr/bin/systemctl", "--user", "show", "cache-worker.service"]
    assert calls[0][1]["shell"] is False and calls[0][1]["timeout"] == 3
    assert output["entity"] == "Cache Worker" and output["verdict"] == "running"


def test_entity_status_rejects_unknown_entity_and_extra_arguments() -> None:
    runner = make_entity_status_runner(registry(), web_fetch=lambda _req: "")

    for args in ({"name": "Nobody"}, {"name": "Atlas API", "url": "http://evil.test"}):
        try:
            runner(ToolRequest("entity_status", OWNER, args))
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe args accepted: {args}")
