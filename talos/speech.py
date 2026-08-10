"""Sprechen — offline, ohne Konto, und als gewoehnlicher Schreibzugriff gegatet.

Die vierte Medien-Luecke. Hermes' Konfiguration nennt `provider: piper-karlsson`, und
das ist der richtige Weg: **Piper laeuft lokal.** Kein Schluessel, kein Konto, kein
Datenabfluss — der Text, den Talos ausspricht, verlaesst die Maschine nicht. Auf einem
Pi 5 ist ein Satz in unter einer Sekunde fertig.

Das Entscheidende ist, wo das Ergebnis landet: **eine Sprachdatei ist eine Datei.** Damit
ist Sprechen hier ein `WRITE` mit einem echten Ziel — der Kernel urteilt ueber den
Ausgabepfad wie ueber jedes andere Schreiben, der Snapshotter sichert, was dort vorher
lag, und `/undo` nimmt es zurueck. Ein Versuch, die Stimme nach `~/.bashrc` oder in den
eigenen Quelltext zu schreiben, holt die Freigabe des Betreibers, ohne dass dieses Modul
davon etwas wissen muesste.

Genau das ist der Unterschied zu einem Plugin-System: dort erzeugt der Sprach-Baustein
eine Datei irgendwo, und niemand hat je darueber geurteilt.

Der Text ist Modelltext und wird begrenzt — nicht aus Sicherheit, sondern aus Anstand
gegenueber der Maschine: ein Roman als Sprachsynthese blockiert den Pi minutenlang.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Ungefaehr zwei Minuten gesprochen. Laenger ist keine Nachricht mehr, sondern ein Vortrag.
MAX_TEXT_CHARS = 2_000
SPEAK_TIMEOUT_S = 120
# Piper schreibt ausschliesslich WAV. Eine andere Endung waere eine Luege ueber den Inhalt.
SUFFIX = ".wav"

NO_VOICE = (
    "No voice model is installed — speech is unavailable. "
    "Install one with: python -m piper.download_voices <voice> --data-dir <dir>"
)


@dataclass(frozen=True)
class Spoken:
    path: str
    seconds: float
    bytes: int


def find_voice(voice_dir: object) -> str:
    """Das erste Stimmmodell im Verzeichnis — oder leer.

    Bewusst ohne Vorgabe eines Namens: welche Stimme installiert ist, entscheidet der
    Betreiber, und ein fest verdrahteter Dateiname waere wieder eine Annahme ueber eine
    fremde Maschine (dieselbe Falle wie beim hartkodierten `DATA_DIR`).
    """
    ordner = Path(str(voice_dir)).expanduser()
    if not ordner.is_dir():
        return ""
    modelle = sorted(ordner.glob("*.onnx"))
    return str(modelle[0]) if modelle else ""


def wav_seconds(path: Path) -> float:
    """Die Laenge aus dem Kopf der Datei — gemessen, nicht aus der Textlaenge geschaetzt."""
    try:
        import wave

        with wave.open(str(path), "rb") as datei:
            rate = datei.getframerate()
            return round(datei.getnframes() / rate, 1) if rate else 0.0
    except Exception:
        return 0.0


def speak(
    text: object,
    path: object,
    *,
    piper_bin: str,
    voice: str,
    timeout_s: int = SPEAK_TIMEOUT_S,
    run=subprocess.run,
) -> Spoken:
    """Spricht `text` in die Datei `path`. Der Aufrufer ist bereits gegatet."""
    satz = " ".join(str(text).split())[:MAX_TEXT_CHARS]
    if not satz:
        raise ValueError("nothing to say")
    if not voice:
        raise RuntimeError(NO_VOICE)
    ziel = Path(str(path)).expanduser()
    if ziel.suffix.lower() != SUFFIX:
        raise ValueError(f"the output path must end in {SUFFIX} — piper writes WAV")
    ziel.parent.mkdir(parents=True, exist_ok=True)

    ergebnis = run(
        [piper_bin, "-m", voice, "-f", str(ziel)],
        input=satz,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if not ziel.is_file() or ziel.stat().st_size == 0:
        zeilen = (ergebnis.stderr or "").strip().splitlines()
        raise RuntimeError(f"speech failed: {zeilen[-1][:200] if zeilen else 'no audio written'}")
    return Spoken(path=str(ziel), seconds=wav_seconds(ziel), bytes=ziel.stat().st_size)


def make_speak_runner(*, piper_bin: str, voice_dir: object, run=subprocess.run):
    """Der Runner. Das Urteil ueber den Ausgabepfad faellt der Kernel, nicht dieser Code."""

    def speak_text(req) -> str:
        ton = speak(
            req.args.get("text", ""),
            req.args.get("path", ""),
            piper_bin=piper_bin,
            voice=find_voice(voice_dir),
            run=run,
        )
        return f"[spoken: {ton.path} — {ton.seconds}s, {ton.bytes // 1024} kB]"

    return speak_text


def speak_spec():
    """WRITE und umkehrbar: es entsteht eine Datei, der Snapshotter sichert das Vorherige."""
    from .manifest import Effect, ToolSpec

    return ToolSpec("speak", Effect.WRITE, reversible=True)


__all__ = [
    "MAX_TEXT_CHARS",
    "NO_VOICE",
    "SUFFIX",
    "Spoken",
    "find_voice",
    "make_speak_runner",
    "speak",
    "speak_spec",
    "wav_seconds",
]
