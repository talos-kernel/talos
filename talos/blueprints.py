"""Automatisierungs-Blueprints — die No-Cron-Schicht ueber `schedule.py`.

Ein Zeitplan aus `/every` verlangt, dass der Betreiber Minuten zaehlt oder einen
Fuenffelder-Ausdruck tippt. Beides steht zwischen der Absicht („morgens um halb neun")
und dem Auftrag. Ein Blueprint schliesst die Luecke: eine benannte, installierbare
Automatisierungs-Definition aus einer JSON-Datei, deren Zeitangabe menschenlesbar ist —
„every morning 08:30", „weekdays 18:00", „every 2 hours", „monday 09:00".

JSON, nicht YAML: `requirements.txt` traegt keinen YAML-Parser, und das ist eine
Zusicherung an den, der das hier installiert. Ein Blueprint ist ein flaches Objekt
(`name`, `description`, `when`, `prompt`; optional `continuity`, `monitor`, `probe` —
siehe `continuity.py`) — dafuer reicht die Standardbibliothek.

Drei Regeln halten die Schicht ehrlich:

  1. **Kein eigener Ausfuehrungspfad.** Installieren schreibt ueber
     `ScheduleStore.add` — denselben Weg wie `/every`. Der Ticker, der faellige
     Auftraege durch Kernel und `UnattendedCeiling` schickt, weiss nichts von
     Blueprints und muss es auch nicht: ein installierter Blueprint darf exakt so
     viel wie ein getippter Zeitplan — also unbeaufsichtigt WENIGER als ein getippter
     Auftrag.
  2. **Kein Cron im Gesicht des Betreibers.** Der Parser bildet die Sprache auf einen
     Cron-Ausdruck oder ein Intervall ab; was er nicht lesen kann, sagt er mit einem
     Satz, der brauchbare Beispiele nennt — wie `cron.CronError`, dem Betreiber gezeigt,
     nie verschluckt.
  3. **Der Stand gehoert dem Betreiber.** Welche Blueprints installiert sind, steht in
     einer JSON-Datei neben den anderen Laufzeitdaten (0600), nicht im Zeitplan selbst.
     Der Eintrag dort verknuepft Namen und Zeitplan-ID — sonst liesse sich ein
     Blueprint nicht sauber entfernen, ohne fremde Auftraege zu erraten.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .schedule import MAX_INTERVAL_S, MIN_INTERVAL_S, ScheduleStore, Task, validate_probe

MAX_NAME_CHARS = 40
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_INTERVAL = re.compile(r"every (\d+) (minutes?|mins?|hours?|hrs?)")

_USAGE = (
    "try 'every morning 08:30', 'weekdays 18:00', 'every 2 hours' or 'monday 09:00'"
)

_DAY_NAMES = {
    "monday": "MON", "tuesday": "TUE", "wednesday": "WED", "thursday": "THU",
    "friday": "FRI", "saturday": "SAT", "sunday": "SUN",
}


class BlueprintError(ValueError):
    """Eine Angabe, aus der kein Zeitplan wird. Wird dem Betreiber gezeigt, nicht verschluckt."""


def _uhrzeit(token: str) -> tuple[int, int]:
    treffer = _TIME.match(token)
    if treffer is None:
        raise BlueprintError(
            f"'{token}' is not a time of day — use HH:MM (24h), e.g. 08:30"
        )
    return int(treffer.group(1)), int(treffer.group(2))


def _taeglich(stunde: int, minute: int, wochentage: str = "*") -> dict:
    return {"cron": f"{minute} {stunde} * * {wochentage}"}


def parse_when(text: str) -> dict:
    """Menschenlesbare Zeitangabe -> Felder fuer `ScheduleStore.add`.

    Kalenderformen („every morning 08:30", „weekdays 18:00", „monday 09:00") werden
    ein Cron-Ausdruck, Abstandsformen („every 2 hours") ein Intervall. Beides endet im
    selben `next_run` der Zeitplan-DB — die Unterscheidung faellt hier und nirgendwo
    sonst. Was nicht lesbar ist, gibt einen Satz zurueck, der brauchbare Formen nennt.
    """
    roh = " ".join(str(text).split()).lower()
    if not roh:
        raise BlueprintError(f"empty schedule — {_USAGE}")

    abstand = _INTERVAL.fullmatch(roh)
    if abstand is not None:
        stunden = abstand.group(2).startswith("h")
        seconds = int(abstand.group(1)) * (3600 if stunden else 60)
        if not MIN_INTERVAL_S <= seconds <= MAX_INTERVAL_S:
            raise BlueprintError(
                f"the interval must be between {MIN_INTERVAL_S // 60} minute and "
                f"{MAX_INTERVAL_S // 3600 // 24} days — '{text}' is not"
            )
        return {"interval_s": seconds}
    if roh in ("every hour", "hourly"):
        return {"interval_s": 3600}
    if roh == "every minute":
        return {"interval_s": 60}

    # Ein fuehrendes „every" ist Schmuck („every monday" = „monday"); „at" ebenso.
    woerter = [w for w in roh.split() if w != "at"]
    if woerter and woerter[0] == "every":
        woerter = woerter[1:]
    kopf = woerter[0] if woerter else ""
    rest = woerter[1:]

    if kopf in ("morning", "evening"):
        vorgabe = "07:00" if kopf == "morning" else "18:00"
        if len(rest) > 1:
            raise BlueprintError(f"too many words in '{text}' — {_USAGE}")
        stunde, minute = _uhrzeit(rest[0]) if rest else _uhrzeit(vorgabe)
        return _taeglich(stunde, minute)
    if kopf in ("daily", "day", "weekdays", "weekday", "weekends", "weekend") or kopf in _DAY_NAMES:
        if len(rest) != 1:
            raise BlueprintError(f"'{text}' needs a time of day, e.g. '{kopf} 09:00'")
        stunde, minute = _uhrzeit(rest[0])
        if kopf in ("daily", "day"):
            return _taeglich(stunde, minute)
        if kopf.startswith("weekday"):
            return _taeglich(stunde, minute, "MON-FRI")
        if kopf.startswith("weekend"):
            return _taeglich(stunde, minute, "SAT,SUN")
        return _taeglich(stunde, minute, _DAY_NAMES[kopf])
    raise BlueprintError(f"cannot read '{text}' as a schedule — {_USAGE}")


@dataclass(frozen=True)
class Blueprint:
    """Eine installierbare Automatisierungs-Definition. `prompt` ist Betreiber-Text,
    der spaeter wie ein getippter Auftrag durch den Kernel geht — nichts anderes."""

    name: str
    description: str
    when: str
    prompt: str
    # Gedaechtnis und Sonde (`continuity.py`). Betreiber-Schalter wie `prompt`: sie
    # werden an `ScheduleStore.add` durchgereicht und erteilen dort nichts — die Sonde
    # ist ein `run_shell` unter derselben Decke wie der Lauf.
    continuity: bool = False
    monitor: bool = False
    probe: str = ""

    def schedule_fields(self) -> dict:
        """Felder fuer `ScheduleStore.add` — die Schalter nur, wenn sie gesetzt sind.

        Ein Blueprint ohne Schalter erzeugt exakt den Eintrag, den er vorher erzeugte;
        das haelt die Zusicherung „ein Blueprint IST ein /every" nachpruefbar.
        """
        felder = parse_when(self.when)
        if self.continuity:
            felder["continuity"] = True
        if self.monitor:
            felder.update(monitor=True, probe=self.probe)
        return felder


@dataclass(frozen=True)
class Catalog:
    """Was das Verzeichnis hergibt — und was verworfen wurde, samt Grund.

    Wie bei den Skills: ein still fehlender Blueprint sieht aus wie einer, der ignoriert
    wird. Deshalb nennt der Katalog auch die Verworfenen.
    """

    blueprints: tuple[Blueprint, ...]
    rejected: tuple[str, ...] = ()

    def get(self, name: str) -> Blueprint:
        for blueprint in self.blueprints:
            if blueprint.name == name:
                return blueprint
        raise BlueprintError(
            f"no blueprint named '{name}' — /blueprints lists what is available"
        )


def _load_one(path: Path) -> Blueprint:
    try:
        roh = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as fehler:
        raise BlueprintError(f"{path.name}: not readable JSON ({fehler})") from None
    if not isinstance(roh, dict):
        raise BlueprintError(f"{path.name}: a blueprint is a JSON object")
    name = str(roh.get("name") or path.stem)[:MAX_NAME_CHARS]
    if not _NAME.match(name):
        raise BlueprintError(f"{path.name}: '{name}' is not a usable name (a-z, 0-9, '-')")
    prompt = " ".join(str(roh.get("prompt") or "").split())
    if not prompt:
        raise BlueprintError(f"{path.name}: a blueprint needs a 'prompt'")
    when = str(roh.get("when") or "").strip()
    try:
        parse_when(when)  # die Zeitangabe IST der Punkt — der Grund gehoert zur Datei
    except BlueprintError as fehler:
        raise BlueprintError(f"{path.name}: {fehler}") from None
    continuity = _flag_field(roh, "continuity", path)
    monitor = _flag_field(roh, "monitor", path)
    probe = roh.get("probe", "")
    if not isinstance(probe, str):
        raise BlueprintError(f"{path.name}: 'probe' must be a string — one shell command")
    try:
        # Dieselbe Pruefung wie im Store, nur frueher: der Betreiber soll den Grund beim
        # Schreiben der Datei sehen, nicht erst beim Installieren.
        probe = validate_probe(probe, monitor=monitor)
    except ValueError as fehler:
        raise BlueprintError(f"{path.name}: {fehler}") from None
    return Blueprint(
        name=name,
        description=str(roh.get("description") or "").strip(),
        when=when,
        prompt=prompt,
        continuity=continuity,
        monitor=monitor,
        probe=probe,
    )


def _flag_field(roh: dict, key: str, path: Path) -> bool:
    """Nur JSON-`true`/`false`. Ein "yes" oder eine 1 ist ein Tippfehler, den der
    Betreiber sehen soll — nicht ein Schalter, der zufaellig an oder aus ist."""
    value = roh.get(key, False)
    if not isinstance(value, bool):
        raise BlueprintError(f"{path.name}: '{key}' must be true or false")
    return value


def load(directory: Path) -> Catalog:
    """Alle `*.json` im Verzeichnis. Fehlende Verzeichnisse sind still — wie bei Skills."""
    pfad = Path(directory)
    if not pfad.is_dir():
        return Catalog(())
    gefunden: list[Blueprint] = []
    verworfen: list[str] = []
    for datei in sorted(pfad.glob("*.json")):
        try:
            gefunden.append(_load_one(datei))
        except BlueprintError as fehler:
            verworfen.append(str(fehler))
    return Catalog(tuple(gefunden), tuple(verworfen))


class BlueprintBook:
    """Haelt, was installiert ist — und ordnet Blueprints ihren Zeitplan-Eintraegen zu.

    Der Zeitplan-Speicher kennt keine Blueprints (und soll es nicht): die Zuordnung
    Name <-> Zeitplan-ID steht in einer eigenen JSON-Datei (0600). Damit entfernt
    `remove` genau den eigenen Eintrag und erraet nie einen fremden. Fail-open wie der
    Zeitplan-Speicher selbst: eine kaputte Stand-Datei heisst „nichts installiert",
    nie „Agent steht".
    """

    def __init__(self, directory: Path, state_path: Path, schedules: ScheduleStore) -> None:
        self._directory = Path(directory)
        self._state_path = Path(state_path)
        self._schedules = schedules
        self._lock = threading.Lock()

    def catalog(self) -> Catalog:
        return load(self._directory)

    def installed(self) -> dict[str, dict]:
        """Name -> Stand (Zeitplan-ID, Konversation, aktiv). Kaputt heisst leer."""
        try:
            roh = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(roh, dict):
            return {}
        return {str(k): v for k, v in roh.items() if isinstance(v, dict)}

    def install(self, name: str, *, conversation: str, principal: str) -> Task:
        """Legt den Zeitplan-Eintrag an und vermerkt ihn. Doppelt installieren ist ein
        Fehler, kein zweiter Eintrag — sonst wuerde jeder Irrtum einen Auftrag mehr."""
        blueprint = self.catalog().get(name)
        with self._lock:
            stand = self.installed()
            if blueprint.name in stand:
                raise BlueprintError(
                    f"'{blueprint.name}' is already installed — "
                    f"/blueprint remove {blueprint.name} takes it out first"
                )
            task = self._schedules.add(
                conversation=conversation,
                principal=principal,
                prompt=blueprint.prompt,
                **blueprint.schedule_fields(),
            )
            if task is None:
                raise BlueprintError("the schedule store refused the entry")
            stand[blueprint.name] = {
                "task_id": task.id,
                "conversation": conversation,
                "principal": principal,
                "enabled": True,
            }
            self._write(stand)
            return task

    def remove(self, name: str) -> None:
        """Nimmt Blueprint und Zeitplan-Eintrag heraus. Der Eintrag wird ueber SEINE
        Konversation geloescht — dieselbe Scoping-Regel wie bei `/unschedule`."""
        with self._lock:
            stand = self.installed()
            eintrag = stand.pop(name, None)
            if eintrag is None:
                raise BlueprintError(f"'{name}' is not installed")
            if eintrag.get("enabled"):
                self._schedules.remove(
                    str(eintrag.get("task_id") or ""),
                    conversation=str(eintrag.get("conversation") or ""),
                )
            self._write(stand)

    def enable(self, name: str) -> Task:
        """Stellt den Zeitplan-Eintrag wieder her. Ein deaktivierter Blueprint hat
        bewusst KEINEN stummen Eintrag in der Zeitplan-DB — ein Eintrag, der nicht
        feuern soll, gehoert dort nicht hin."""
        blueprint = self.catalog().get(name)
        with self._lock:
            stand = self.installed()
            eintrag = stand.get(blueprint.name)
            if eintrag is None:
                raise BlueprintError(f"'{name}' is not installed")
            if eintrag.get("enabled"):
                raise BlueprintError(f"'{name}' is already active")
            task = self._schedules.add(
                conversation=str(eintrag["conversation"]),
                principal=str(eintrag["principal"]),
                prompt=blueprint.prompt,
                **blueprint.schedule_fields(),
            )
            if task is None:
                raise BlueprintError("the schedule store refused the entry")
            eintrag.update({"task_id": task.id, "enabled": True})
            self._write(stand)
            return task

    def disable(self, name: str) -> None:
        """Haelt den Blueprint aus, ohne ihn zu vergessen: der Stand bleibt, der
        Zeitplan-Eintrag geht. Reaktivieren ist dann ein Schalter, kein Neuaufbau."""
        with self._lock:
            stand = self.installed()
            eintrag = stand.get(name)
            if eintrag is None:
                raise BlueprintError(f"'{name}' is not installed")
            if not eintrag.get("enabled"):
                raise BlueprintError(f"'{name}' is already inactive")
            self._schedules.remove(
                str(eintrag.get("task_id") or ""),
                conversation=str(eintrag.get("conversation") or ""),
            )
            eintrag["enabled"] = False
            self._write(stand)

    def next_run(self, name: str) -> float | None:
        """Der naechste Lauf eines aktiven Blueprints — `None` bei inaktiv/unbekannt."""
        eintrag = self.installed().get(name)
        if eintrag is None or not eintrag.get("enabled"):
            return None
        task_id = str(eintrag.get("task_id") or "")
        for task in self._schedules.list_for(str(eintrag.get("conversation") or "")):
            if task.id == task_id:
                return task.next_run
        return None

    def _write(self, stand: dict) -> None:
        pfad = self._state_path
        pfad.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as datei:
            json.dump(stand, datei, indent=2, sort_keys=True)
            datei.write("\n")
        os.chmod(pfad, 0o600)


def describe_next(ts: float | None) -> str:
    """„Tue 02.09 08:30" — dieselbe Form, mit der `/every` den ersten Lauf nennt."""
    if ts is None:
        return "not scheduled"
    return time.strftime("%a %d.%m %H:%M", time.localtime(ts))


__all__ = [
    "Blueprint",
    "BlueprintBook",
    "BlueprintError",
    "Catalog",
    "describe_next",
    "load",
    "parse_when",
]
