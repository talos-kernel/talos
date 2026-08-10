"""Snapshot/Undo: schreibende Aktionen sind rückrollbar."""
from __future__ import annotations

from pathlib import Path

from talos.snapshot import Snapshotter


def test_restore_returns_modified_file_to_original(tmp_path: Path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("original", encoding="utf-8")
    snap = Snapshotter(tmp_path / ".snap")

    token = snap.take((str(target),))
    target.write_text("verändert", encoding="utf-8")
    snap.restore(token)

    assert target.read_text(encoding="utf-8") == "original"


def test_restore_removes_newly_created_file(tmp_path: Path) -> None:
    target = tmp_path / "neu.txt"  # existiert beim Snapshot noch nicht
    snap = Snapshotter(tmp_path / ".snap")

    token = snap.take((str(target),))
    target.write_text("frisch angelegt", encoding="utf-8")
    snap.restore(token)

    assert not target.exists()
