"""Snapshot/Undo — Rückrollbarkeit VOR jeder schreibenden Aktion.

Kernel-Spec v0.2 §2: Snapshot/Undo gehört zur Primärarchitektur. Vor einer write-Aktion
werden die Zielpfade gesichert; schlägt die Aktion fehl (oder der Verifier), wird zurückgerollt.
Bewusst simpel: Dateikopie in ein Snapshot-Verzeichnis. Nicht-existente Ziele werden als
"absent" vermerkt, damit Undo eine neu angelegte Datei wieder entfernt.

Zwei Rückroll-Wege, gleiche Mechanik:
  - automatisch, vom Executor bei Fehlschlag (`Snapshotter.restore`)
  - auf des Betreibers Wunsch nach einem *erfolgreichen* Schreibzugriff (`/undo` -> `restore_entries`)
Der zweite braucht keinen Snapshotter-Zustand, nur die Einträge aus dem Event-Log —
deshalb steht die eigentliche Arbeit in einer freien Funktion.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

Entries = tuple[tuple[str, str | None], ...]  # (originalpfad, gesicherter-pfad|None=absent)


@dataclass(frozen=True)
class SnapshotToken:
    snapshot_id: str
    root: Path
    entries: Entries


def restore_entries(entries: Entries) -> None:
    """Setzt den Zustand von vor dem Snapshot wieder her."""
    for original, backup in entries:
        target = Path(original)
        if backup is None:
            target.unlink(missing_ok=True)  # war absent -> wieder entfernen
        else:
            shutil.copy2(backup, target)


class Snapshotter:
    """Legt Snapshots unter einem Basisverzeichnis ab und rollt sie zurück."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)

    def take(self, targets: tuple[str, ...]) -> SnapshotToken:
        snap_id = uuid.uuid4().hex
        root = self._base / snap_id
        root.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[str, str | None]] = []
        for index, target in enumerate(targets):
            src = Path(target)
            if src.is_file():
                dst = root / f"{index}.bak"
                shutil.copy2(src, dst)
                entries.append((str(src), str(dst)))
            else:
                entries.append((str(src), None))  # existiert (noch) nicht
        return SnapshotToken(snap_id, root, tuple(entries))

    def restore(self, token: SnapshotToken) -> None:
        restore_entries(token.entries)
