"""Verifier: TOCTOU-Bindung erkennt Veränderung zwischen Freigabe und Lauf."""
from __future__ import annotations

from pathlib import Path

from talos import verifier


def test_recheck_true_when_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "bin.sh"
    target.write_text("echo hi", encoding="utf-8")
    bindings = verifier.bind((str(target),))
    assert verifier.recheck(bindings) is True


def test_recheck_false_when_content_changed(tmp_path: Path) -> None:
    target = tmp_path / "bin.sh"
    target.write_text("echo hi", encoding="utf-8")
    bindings = verifier.bind((str(target),))
    target.write_text("rm -rf /", encoding="utf-8")  # untergeschoben
    assert verifier.recheck(bindings) is False


def test_recheck_false_when_file_appears(tmp_path: Path) -> None:
    target = tmp_path / "later.txt"  # beim Bind absent
    bindings = verifier.bind((str(target),))
    target.write_text("jetzt da", encoding="utf-8")
    assert verifier.recheck(bindings) is False


def test_verify_result_compares_expected() -> None:
    assert verifier.verify_result(42, 42) is True
    assert verifier.verify_result(42, 7) is False
