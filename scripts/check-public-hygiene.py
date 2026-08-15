#!/usr/bin/env python3
"""Fail when the tracked public tree contains likely secrets or private endpoints."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

# Private-address fixtures are allowlisted by exact file and host. A new host in the same
# test or example still fails instead of inheriting a broad path exemption.
ALLOWED_ENDPOINT_FIXTURE_HOSTS = {
    "redteam.py": frozenset({"100.64.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1"}),
    "talos/catalog.py": frozenset({"localhost"}),
    "talos/web.py": frozenset({"169.254.169.254"}),
    "tests/test_browser.py": frozenset({"127.0.0.1", "192.168.1.1"}),
    "tests/test_first_run.py": frozenset({"localhost"}),
    "tests/test_web.py": frozenset({
        "127.0.0.1", "127.9.9.9", "169.254.169.254", "::1", "localhost",
    }),
}

# Exact private project/person markers, stored only as case-folded SHA-256 digests so the
# public guard does not repeat the identifiers it is meant to keep out of the tree.
BLOCKED_MARKER_DIGESTS = frozenset({
    "0afa9d9a1b780339772946ac0dfd4b98e5e56b34a117cac353985be7a6a50ac7",
    "b5143f0410a1d4d905bfe2290fd4c965e1e1ece5bc99a6b7ee5f663a366256ca",
    "c8d536fa90e0f08ece57e02d258f95221656edf172e5b577132c54cbb48da03d",
    "3a5a2512949399115565867a73a413ec6ba215c8f2df385f78b33238a6639b7c",
    "e1608f75c5d7813f3d4031cb30bfb786507d98137538ff8e128a6ff74e84e643",
    "4676c685dbf2380d0339a9a87c931e0bba3c8488262dce8324c55b71f90bb629",
    "48f9460fe0dc9f272e7414963dd2b52287ec07d872d665d2e9364c957f163ab0",
    "7aecf6e598c1f48798dada5d00a0f578ab5f9bc1b9e66bce9ac176fe058cf104",
    "7cd59327d99d11138461861191fd25b85a9132da71e8e71818c7fa6802368cd8",
})

# Some tests need realistic credential shapes to prove redaction. Only those exact inert
# byte strings are allowed; another value in the same file still fails the scan.
ALLOWED_SECRET_FIXTURE_DIGESTS = {
    "redteam.py": frozenset({
        "12351f9c9f61c6d488609dee4947b562f015fcb412e20446e7de8f1b7842b80e",
        "1531c88150d0f8eb0f25e27b19802bdaff4797c0919648f0b3357900b4f74cf7",
        "f5e04418bceaf421cf1663b9a70f9f18352ab07a01adfe85b1bcbb546b472e39",
    }),
    "tests/test_api_reasoner.py": frozenset({
        "049d9c4444034c244144dd2008374b162afd7ac90c75320dd84e12c1ec4f9458",
        "f3ba3a7e865391cbfef71d86ab75893025f2d351f43dde8ccac62c2dc1723c88",
    }),
    "tests/test_credentials.py": frozenset({
        "3eb9ff68623bde249c57b58d404b9f3aac3268e3d1fa1a47b0b6999e46d7d139",
        "f1b6cd0adfe68809a716388675998748384e785eaf1f6332a5cd66bd0844d8f0",
    }),
    "tests/test_introspection.py": frozenset({
        "f5e04418bceaf421cf1663b9a70f9f18352ab07a01adfe85b1bcbb546b472e39",
    }),
    "tests/test_recall.py": frozenset({
        "1a5d44a2dca19669d72edf4c4f1c27c4c1ca4b4408fbb17f6ce4ad452d78ddb3",
        "6a25420a359fed6b9fe1fbab2a1dcdb13975dedb15afaaa9692cae289211a271",
        "71556738cf1db87f6f434f22cf0fa889a2482cf8fa27d3e576de8a775d954ef7",
        "8bcac7908eb950419537b91e19adc83ce2c9cbfdacf4f81157fdadfec11f7017",
        "a6899f4aaf3750917c8fdea30ce1d85ac36c1b21c795bd80dac5a11b9de368f0",
        "ac45140678a593d6baacb495322175b26ee644dccafad0aa1dd293ca8873ce4b",
        "ecf50d53244c37238955e81a5a622f3d23b6b322f78bb31f73924860e6bc4bf6",
    }),
    "tests/test_report.py": frozenset({
        "d2430cf46cbb95ef55a08483d4b7fc84c808591ea6d2784f8d78f01ff81072ee",
    }),
    "tests/test_setup_wizard.py": frozenset({
        "41f0ca4b8543648cee4e676773ee78d45034c48f70dc3ccb9974316da945e1f9",
        "6a8219220ae41ad0d9938e8cd2f65f8c1f515769b6b86d1f3bea0f591be87114",
        "ae3835d83840555d511563e66d7556731c258469041314250bdfcfdefacfd7d0",
        "fb594133029efc5439a745824c088a6a59acc2fcbf78369aa09296618cff21ad",
    }),
    "tests/test_telegram_ux.py": frozenset({
        "3a1be4ebcfb5e066c2fb26c1e8752978a3e6d225bbad636783fdf3828e02d5e2",
    }),
    "tests/test_vault_tools.py": frozenset({
        "31f893086035a0a34d1a032882e551f711baa528b9c2789bc16a2804fe348b2d",
        "fb50d8dc4bbb96a8c96a79d5efca425273b1e0c1d4391d425333fcae07e46964",
    }),
}

SECRET_PATTERNS = (
    (re.compile(r"\b[sr]k-[A-Za-z0-9_-]{16,}"), "API key shaped value"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access-key shaped value"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "GitHub token shaped value"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "GitHub token shaped value"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "Slack token shaped value"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key shaped value"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"), "GitLab token shaped value"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "Hugging Face token shaped value"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), "JWT shaped value"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}"), "bot token shaped value"),
)
PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")

PUBLICATION_PATTERNS = (
    re.compile(r"\bseparate\s+private\s+(?:repo(?:sitory)?|history)\b", re.IGNORECASE),
    re.compile(r"\bfull\s+private\s+history\b", re.IGNORECASE),
    re.compile(r"\bprivate[- ]deployment\b", re.IGNORECASE),
)

URL = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
WORD = re.compile(r"\b[A-Z][A-Z0-9_-]*\b", re.IGNORECASE)
PUBLICATION_SUFFIXES = {".md", ".rst", ".txt", ".html", ".sh"}


def _files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private_host(host: str) -> bool:
    lowered = host.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith((".local", ".lan", ".internal", ".ts.net")):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return not address.is_global


def _findings(path: Path, text: str) -> list[str]:
    relative = path.relative_to(ROOT).as_posix()
    findings: list[str] = []

    for pattern in PUBLICATION_PATTERNS:
        if pattern.search(text):
            findings.append("obsolete private-public publication claim")
            break

    # A public path leaks an identifier just as surely as file content does.
    for match in WORD.finditer(relative + "\n" + text):
        if _digest(match.group(0).casefold().encode("utf-8")) in BLOCKED_MARKER_DIGESTS:
            findings.append("blocked private marker")
            break

    allowed_fixtures = ALLOWED_SECRET_FIXTURE_DIGESTS.get(relative, frozenset())
    for match in PRIVATE_KEY_HEADER.finditer(text):
        if _digest(match.group(0).encode("utf-8")) not in allowed_fixtures:
            findings.append("private key block")
            break

    for pattern, label in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if _digest(match.group(0).encode("utf-8")) not in allowed_fixtures:
                findings.append(label)
                break

    allowed_hosts = ALLOWED_ENDPOINT_FIXTURE_HOSTS.get(relative, frozenset())
    for match in URL.finditer(text):
        host = urlsplit(match.group(0).rstrip(".,);]")).hostname
        if host:
            host = host.rstrip(".").lower()
            if _private_host(host) and host not in allowed_hosts:
                findings.append(f"private endpoint host: {host}")

    if path.suffix.lower() in PUBLICATION_SUFFIXES or relative.startswith("scripts/"):
        for match in EMAIL.finditer(text):
            domain = match.group(1).lower()
            reserved = domain.endswith(".example") or domain in {
                "example.com", "example.org", "example.net",
            }
            if not reserved:
                findings.append(f"non-reserved author/contact address: {domain}")

    return findings


def main() -> int:
    failures: list[str] = []
    for path in _files():
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT).as_posix()
        sensitive_name = path.name == ".env" or path.suffix.lower() in {
            ".key", ".pem", ".p12", ".pfx",
        }
        if path.name != ".env.example" and sensitive_name:
            failures.append(f"{relative}: sensitive filename")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        failures.extend(f"{relative}: {finding}" for finding in _findings(path, text))

    if failures:
        print("Public hygiene check failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("Public hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
