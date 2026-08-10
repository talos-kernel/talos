"""Hoeren — und warum es dieselbe Bauart hat wie Sehen.

Eine Sprachnachricht ist eine Datei. Damit ist Verstehen ein `READ` mit einem echten Ziel:
der Kernel urteilt ueber den Pfad wie ueber jedes andere Lesen, der Secrets-Floor greift
ohne eine Zeile Sonderbehandlung, und ein Mitschnitt aus `~/.secrets/` faellt durch, ohne
dass dieses Modul davon wissen muesste.

**Lokal, nicht in der Cloud.** Was gesprochen wurde, ist oft das Privateste am ganzen Tag.
Es an ein fremdes Modell zu schicken, um es zu verstehen, waere die eine Stelle, an der
Talos den Grundsatz „laeuft auf deiner Maschine" gegen Bequemlichkeit tauschte. Deshalb
`faster-whisper` auf dem eigenen Rechner.

⚠️ **Das Modell wird beim ERSTEN Aufruf geladen, nicht beim Start.** Ein Agent, der ohne
Sprachnachricht laeuft, soll keine hundert Megabyte im Speicher halten — und einer, dem
das Paket fehlt, soll nicht beim Hochfahren umfallen, sondern beim Aufruf sagen, was
fehlt. Dieselbe Regel wie bei der Suche.

⚠️ **Die Dauer wird VOR dem Verstehen begrenzt**, nicht danach. Eine Stunde Audio
beschaeftigt einen Raspberry Pi minutenlang, und der Zug wartet solange. Was zu lang ist,
wird abgelehnt statt angefangen.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Was Telegram als Sprachnachricht schickt (`.oga`), plus das Uebliche. Die Endung ist
# eine Behauptung des Namens — sie entscheidet nur, ob es sich zu VERSUCHEN lohnt.
SUFFIXES = (".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".webm", ".mp4")
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# Eine Sprachnachricht, kein Podcast. Darueber lohnt sich der Zug nicht mehr: der Pi
# rechnet dann laenger, als der Betreiber auf eine Antwort warten will.
MAX_SECONDS = 600
DEFAULT_MODEL = "base"
PROBE_TIMEOUT_S = 20

NO_ENGINE = (
    "hearing needs the faster-whisper package — install it with `pip install faster-whisper`. "
    "It runs locally; nothing spoken leaves the machine."
)
TOO_LONG = "the recording is longer than {limit} s — too long to be worth a turn"
UNSUPPORTED = "not an audio file this can read"


@dataclass(frozen=True)
class Heard:
    text: str
    seconds: float
    model: str


def duration_seconds(path: object, *, ffprobe: str = "ffprobe", run=subprocess.run) -> float:
    """Laenge in Sekunden — oder 0.0, wenn sie sich nicht feststellen laesst.

    Absichtlich `ffprobe` und nicht die Kopfzeilen selbst: eine Datei kann eine Dauer
    behaupten, die nicht stimmt, und der Deckel soll am ECHTEN Inhalt haengen.
    """
    try:
        lauf = run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
        )
        return float(str(getattr(lauf, "stdout", "")).strip() or 0.0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _load(model: str):
    """Das Modell, beim ersten Aufruf geladen und danach behalten."""
    from faster_whisper import WhisperModel

    if _load.cache.get(model) is None:                       # type: ignore[attr-defined]
        # `int8` statt `float32`: auf ARM ohne GPU ist das der Unterschied zwischen
        # „wartet man ab" und „laesst man laufen", bei kaum hoerbarem Qualitaetsverlust.
        _load.cache[model] = WhisperModel(model, device="cpu", compute_type="int8")  # type: ignore[attr-defined]
    return _load.cache[model]                                # type: ignore[attr-defined]


_load.cache = {}                                             # type: ignore[attr-defined]


def transcribe(
    path: object,
    *,
    model: str = DEFAULT_MODEL,
    language: str = "",
    max_seconds: int = MAX_SECONDS,
    engine=None,
    probe=duration_seconds,
) -> Heard:
    """Versteht eine Aufnahme. Der Aufrufer ist bereits gegatet."""
    ziel = Path(str(path)).expanduser()
    if ziel.suffix.lower() not in SUFFIXES:
        raise ValueError(f"{UNSUPPORTED}: {ziel.suffix or '(no suffix)'}")
    if not ziel.is_file():
        raise ValueError(f"no such file: {ziel}")
    if ziel.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError(f"the recording is larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    dauer = float(probe(ziel))
    if dauer > max_seconds:
        raise ValueError(TOO_LONG.format(limit=max_seconds))

    if engine is None:
        try:
            engine = _load(model)
        except ImportError:
            raise RuntimeError(NO_ENGINE) from None
    stuecke, info = engine.transcribe(str(ziel), language=language or None, beam_size=1)
    text = " ".join(str(getattr(s, "text", "")).strip() for s in stuecke).strip()
    gemessen = float(getattr(info, "duration", 0.0) or dauer)
    return Heard(text=text, seconds=gemessen, model=model)


def make_hear_runner(*, model: str = DEFAULT_MODEL, engine=None, probe=duration_seconds):
    """Der Runner. Das Urteil ueber den Pfad faellt der Kernel, nicht dieser Code."""

    def hear(req) -> str:
        gehoert = transcribe(
            req.args.get("path", ""),
            model=model,
            language=str(req.args.get("language") or ""),
            engine=engine,
            probe=probe,
        )
        if not gehoert.text:
            return f"[no speech in the recording — {gehoert.seconds:.0f} s]"
        return f"[heard, {gehoert.seconds:.0f} s]\n{gehoert.text}"

    return hear


def hear_spec():
    """READ mit dem Dateipfad als Ziel — genau wie Sehen. Es entsteht nichts Neues."""
    from .manifest import Effect, ToolSpec

    return ToolSpec("hear", Effect.READ, reversible=True)


__all__ = [
    "DEFAULT_MODEL",
    "MAX_AUDIO_BYTES",
    "MAX_SECONDS",
    "NO_ENGINE",
    "SUFFIXES",
    "TOO_LONG",
    "UNSUPPORTED",
    "Heard",
    "duration_seconds",
    "hear_spec",
    "make_hear_runner",
    "transcribe",
]
