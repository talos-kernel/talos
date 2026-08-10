"""Ein Standbild aus einem Video — und warum daraus keine Videoanalyse wird.

Das letzte Stueck der Medienkette. Sehen kann Talos laengst; was fehlte, war der Weg
von einem Video dorthin. Der Weg ist bewusst kurz: **ffmpeg zieht genau EIN Bild, danach
greift `see_image` darauf** wie auf jedes andere Foto. Es entsteht kein zweiter
Bildkanal, kein Video-Modell, kein Sonderpfad im Kernel.

⚠️ **Genau ein Frame, nie eine Serie.** Der Fall, den das verhindert, ist real vorgekommen:
auf „analysiere jedes Bild" faechert ffmpeg auf, danach laufen die Bildaufrufe parallel,
die CPU ist voll und der Dienst faellt beim Healthcheck durch. Wer mehrere Stellen
sehen will, ruft mehrmals — jedes Mal durch den Kernel.

⚠️ **Der Ausgabepfad ist KEIN Argument.** Er wird im Kernel abgeleitet
(`policy.frame_output_path`) und liegt immer im Posteingang der Werkstatt. Waere er
frei waehlbar, gaebe es einen zweiten Weg, Inhalt aus einer fremden Datei an eine
gewaehlte Stelle zu schreiben. Der Kernel urteilt trotzdem darueber — abgeleitet heisst
nicht ungeprueft.

⚠️ **Die Quelle ist ein Ziel wie jedes andere.** Ein Video unter `~/.secrets/` faellt
durch, bevor ffmpeg startet. Ohne diese Zeile waere Frame Capture der bequemste Weg am
Secret-Floor vorbei.

Ohne genannten Zeitpunkt wird die **Mitte** genommen, nicht der Anfang: der erste Frame
ist bei den meisten Aufnahmen schwarz oder ein Vorspann, und ein schwarzes Bild zu
beschreiben ist verschwendete Arbeit fuer beide Seiten.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .hearing import duration_seconds
from .policy import frame_output_path, frame_seconds

# Was ueblicherweise als Video ankommt. Die Endung ist eine Behauptung des Namens — sie
# entscheidet nur, ob sich der Versuch lohnt; was drinsteht, sagt ffmpeg.
SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".ogv", ".3gp")
# Grosszuegig fuer ein Handyvideo, eng genug, dass niemand ein Plattenabbild durchreicht.
MAX_VIDEO_BYTES = 256 * 1024 * 1024
# Kein Zeitpunkt und keine messbare Dauer: eine Sekunde hinein. Nicht Null — bei Null
# liefern viele Container den schwarzen Vorlauf.
DEFAULT_SECONDS = 1.0
GRAB_TIMEOUT_S = 120
# Was danach `see_image` bekommt. Groesser als das schadet nur: das Bild wird ohnehin
# base64-kodiert durch die CLI geschoben.
MAX_EDGE = 1920

UNSUPPORTED = "not a video file this can read"
NO_FFMPEG = "frame capture needs ffmpeg — install it with your package manager"
NO_FRAME = "ffmpeg produced no frame — the timestamp is probably past the end of the video"


@dataclass(frozen=True)
class Frame:
    path: str
    at: float
    bytes: int


def timestamp_for(at: object, duration: float) -> float:
    """Der Zeitpunkt, an dem geschnitten wird — genannt, sonst die Mitte."""
    gewuenscht = frame_seconds(at)
    if gewuenscht is not None:
        return gewuenscht
    return round(duration / 2, 3) if duration > 0 else DEFAULT_SECONDS


def grab(
    path: object,
    at: object = "",
    *,
    ffmpeg: str = "ffmpeg",
    timeout_s: int = GRAB_TIMEOUT_S,
    run=subprocess.run,
    probe=duration_seconds,
) -> Frame:
    """Zieht ein Bild aus dem Video. Der Aufrufer ist bereits gegatet."""
    quelle = Path(str(path)).expanduser()
    if quelle.suffix.lower() not in SUFFIXES:
        raise ValueError(f"{UNSUPPORTED}: {quelle.suffix or '(no suffix)'}")
    if not quelle.is_file():
        raise ValueError(f"no such file: {quelle}")
    if quelle.stat().st_size > MAX_VIDEO_BYTES:
        raise ValueError(f"the video is larger than {MAX_VIDEO_BYTES // (1024 * 1024)} MB")

    zeit = timestamp_for(at, float(probe(quelle)))
    # Derselbe Pfad, ueber den der Kernel geurteilt hat — dieselbe Funktion, nicht
    # dieselbe Regel nachgebaut. Nachgebaut hiesse: irgendwann laufen sie auseinander.
    ziel = Path(frame_output_path(path, at))
    ziel.parent.mkdir(parents=True, exist_ok=True)

    argv = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        # `-ss` VOR `-i`: ffmpeg springt dann, statt bis dorthin zu dekodieren — bei
        # einem langen Video ist das der Unterschied zwischen Sekunden und Minuten.
        "-ss", f"{zeit:.3f}",
        "-i", str(quelle),
        # Genau die erste Bildspur, genau ein Bild. `-frames:v 1` ist die Grenze, die
        # aus „ein Standbild" keine Bilderserie werden laesst.
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale='min({MAX_EDGE},iw)':'min({MAX_EDGE},ih)':force_original_aspect_ratio=decrease",
        "-q:v", "2",
        str(ziel),
    ]
    try:
        ergebnis = run(argv, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        raise RuntimeError(NO_FFMPEG) from None

    if not ziel.is_file() or ziel.stat().st_size == 0:
        zeilen = (getattr(ergebnis, "stderr", "") or "").strip().splitlines()
        raise RuntimeError(f"{NO_FRAME}: {zeilen[-1][:200]}" if zeilen else NO_FRAME)
    return Frame(path=str(ziel), at=zeit, bytes=ziel.stat().st_size)


def make_grab_runner(*, ffmpeg: str = "ffmpeg", run=subprocess.run, probe=duration_seconds):
    """Der Runner. Ueber beide Pfade — Quelle und Ergebnis — hat der Kernel geurteilt."""

    def grab_frame(req) -> str:
        bild = grab(
            req.args.get("path", ""),
            req.args.get("at", ""),
            ffmpeg=ffmpeg,
            run=run,
            probe=probe,
        )
        return (
            f"[frame at {bild.at:.1f}s: {bild.path} — {bild.bytes // 1024} kB]\n"
            "Look at it with see_image."
        )

    return grab_frame


def grab_frame_spec():
    """READ — und das ist hier die STRENGERE Einordnung, nicht die bequemere.

    ⚠️ Der erste Entwurf war `WRITE`, weil eine Datei entsteht. Genau das machte den
    Secret-Floor weicher: er urteilt nach dem Effekt, nicht pro Ziel (`policy.decide`,
    Schritt 4). Ein Video unter `~/.secrets/` kam damit als „Schreiben eines Secrets"
    auf `NEEDS_HUMAN` — freigebbar — waehrend dieselbe Aufnahme ueber `hear` ein
    hartes `DENY` bekommt. Frame Capture waere die schwaechere Tuer zum selben Inhalt
    gewesen.

    Massgeblich ist, welches Ziel frei WAEHLBAR ist: die Quelle. Die wird gelesen, also
    ist Lesen die richtige Einordnung — und beim Secret-Floor die haertere. Das zweite
    Ziel, das Bild, liegt per Bauart im Arbeitsbereich, den Stufe 4 ohnehin frei
    beschreiben darf (`policy.FRAME_INBOX` unter `WORKSPACE_DIR`); dort gibt es nichts,
    worueber `WRITE` strenger urteilen koennte. `tests/test_media.py` nagelt genau das
    fest — bricht die Annahme, ist diese Zeile falsch.

    Snapshot und `/undo` bleiben davon unberuehrt: der Executor sichert jedes
    kernel-abgeleitete Ziel, unabhaengig vom Effekt.
    """
    from .manifest import Effect, ToolSpec

    return ToolSpec("grab_frame", Effect.READ, reversible=True)


__all__ = [
    "DEFAULT_SECONDS",
    "MAX_EDGE",
    "MAX_VIDEO_BYTES",
    "NO_FFMPEG",
    "NO_FRAME",
    "SUFFIXES",
    "UNSUPPORTED",
    "Frame",
    "grab",
    "grab_frame_spec",
    "make_grab_runner",
    "timestamp_for",
]
