from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-public-hygiene.py"
pytestmark = pytest.mark.skipif(
    not SCRIPT.exists(),
    reason="repository-only hygiene checker is excluded from release archives",
)
if SCRIPT.exists():
    SPEC = importlib.util.spec_from_file_location("public_hygiene", SCRIPT)
    assert SPEC is not None and SPEC.loader is not None
    PUBLIC_HYGIENE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(PUBLIC_HYGIENE)


def test_example_environment_file_does_not_exempt_a_new_token_shape() -> None:
    fake = "TOKEN=ghp_" + "A" * 40
    findings = PUBLIC_HYGIENE._findings(ROOT / ".env.example", fake)
    assert "GitHub token shaped value" in findings


def test_known_inert_test_fixtures_remain_allowlisted_exactly() -> None:
    path = ROOT / "tests" / "test_api_reasoner.py"
    findings = PUBLIC_HYGIENE._findings(path, path.read_text(encoding="utf-8"))
    assert "provider key shaped value" not in findings


def test_blocked_marker_in_a_path_is_detected() -> None:
    marker = "PathMarkerProbe"
    original = PUBLIC_HYGIENE.BLOCKED_MARKER_DIGESTS
    setattr(PUBLIC_HYGIENE, "BLOCKED_MARKER_DIGESTS", frozenset({
        PUBLIC_HYGIENE._digest(marker.casefold().encode("utf-8")),
    }))
    try:
        findings = PUBLIC_HYGIENE._findings(ROOT / f"{marker}.md", "harmless")
    finally:
        setattr(PUBLIC_HYGIENE, "BLOCKED_MARKER_DIGESTS", original)
    assert "blocked private marker" in findings


def test_url_literals_and_malformed_hosts_do_not_crash_the_publication_check() -> None:
    path = ROOT / 'probe.py'
    assert not PUBLIC_HYGIENE._findings(path, r'https://example.com\x1b[0m')
    assert not PUBLIC_HYGIENE._findings(path, 'https://[2606:4700:4700::1111]/')
    assert any('malformed URL' in item for item in
               PUBLIC_HYGIENE._findings(path, 'https://' + '[broken/'))


@pytest.mark.parametrize("kind", ("RSA ", "EC ", "OPENSSH ", ""))
def test_standard_private_key_headers_are_detected(kind: str) -> None:
    header = f"-----BEGIN {kind}PRIVATE KEY-----"
    findings = PUBLIC_HYGIENE._findings(ROOT / "probe.bin", header)
    assert "private key block" in findings
