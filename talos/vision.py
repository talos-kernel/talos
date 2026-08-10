"""Sehen — ueber dasselbe Abo, das ohnehin denkt, und mit demselben Floor wie Lesen.

Die dritte der vier Medien-Luecken. Der Weg dorthin war nicht offensichtlich: `claude -p`
nimmt im Textmodus kein Bild, und `--file` erwartet eine `file_id` aus der Cloud, keinen
Pfad von der Platte. Naheliegend waere gewesen, dem Reasoner Datei-Werkzeuge zu geben,
damit er das Bild selbst laedt — das ist der Kardinalfehler aus `CLAUDE.md` und faellt aus.

Der Weg, der bleibt: `--input-format stream-json`. Die CLI nimmt dann eine Nachricht mit
Inhaltsbloecken entgegen, und einer davon darf ein Bild sein. **Talos** laedt die Datei —
gegatet, mit Ziel, durch denselben Kernel wie jeder andere Lesezugriff — und reicht sie
als Base64 hinein. Das Modell sieht ein Bild, nie einen Pfad.

Der Unterschied ist die ganze Sache:

  * Der Kernel kennt das Ziel. `~/.secrets/foto.png` ist ein Secret-Pfad, und Lesen ist
    dort gesperrt — also sieht das Modell es nicht. Haette der Reasoner das Bild selbst
    geladen, gaebe es kein Ziel, das jemand haette pruefen koennen.
  * Der Aufruf traegt dieselbe Isolation wie das Denken: keine Werkzeuge, kein MCP,
    keine Sitzungs-Persistenz. Sehen ist hier ein zweiter Modellaufruf, kein zweites,
    schwaecher gesichertes Tor.
  * Der Medientyp kommt aus den ERSTEN BYTES, nie aus der Endung. Eine Datei `bild.png`,
    die in Wahrheit etwas anderes ist, wuerde sonst als PNG deklariert an ein fremdes
    System geschickt.

Was zurueckkommt, ist die Beschreibung eines Modells von einem Bild, das jemand anders
gemacht hat: **Daten, keine Anweisung.** Steht auf dem Foto „ignoriere deine Regeln",
ist das ein Bildinhalt und kein Befehl — dieselbe Haltung wie bei `web_fetch`.
"""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

# Base64 blaeht um ein Drittel auf, und der Prompt geht als eine Zeile ueber stdin.
# 5 MB Rohbild sind grosszuegig fuer alles, was ein Mensch aus einem Chat schickt.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
VISION_TIMEOUT_S = 180
MAX_QUESTION_CHARS = 500
DEFAULT_QUESTION = "Describe this image factually. Say what you can see, not what it might mean."

# Magische Bytes statt Dateiendung. Die Endung ist eine Behauptung des Dateinamens.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_WEBP_HEAD, _WEBP_TAG = b"RIFF", b"WEBP"

UNSUPPORTED = "not an image this system can read (png, jpeg, gif or webp only)"


def media_type(head: bytes) -> str:
    """Der Typ aus den ersten Bytes — oder leer, wenn es kein bekanntes Bild ist."""
    for magie, typ in _MAGIC:
        if head.startswith(magie):
            return typ
    if head[:4] == _WEBP_HEAD and head[8:12] == _WEBP_TAG:
        return "image/webp"
    return ""


def stream_json_line(*, question: str, data: str, kind: str) -> str:
    """Die eine Zeile, die die CLI als Nachricht liest. Text zuerst, dann das Bild."""
    return json.dumps({
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image", "source": {"type": "base64", "media_type": kind, "data": data}},
            ],
        },
    })


def describe(
    path: object,
    question: object = "",
    *,
    binary: str,
    model: str = "",
    timeout_s: int = VISION_TIMEOUT_S,
    run=subprocess.run,
) -> str:
    """Laedt das Bild (der Aufrufer ist bereits gegatet) und laesst es beschreiben."""
    datei = Path(str(path)).expanduser()
    roh = datei.read_bytes()          # OSError reicht als gewoehnlicher Werkzeugfehler durch
    if len(roh) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    kind = media_type(roh[:16])
    if not kind:
        raise ValueError(UNSUPPORTED)

    frage = " ".join(str(question).split())[:MAX_QUESTION_CHARS] or DEFAULT_QUESTION
    zeile = stream_json_line(question=frage, data=base64.b64encode(roh).decode(), kind=kind)

    from .reasoner import CLAUDE_ISOLATION_ARGV, DISALLOWED_TOOLS_ARGV

    argv = [
        binary,
        "-p",
        "--input-format", "stream-json",
        # Die CLI besteht darauf, dass beide Formate zusammenpassen — und `--verbose`
        # ist bei stream-json Pflicht, sonst bricht sie mit einer Meldung ab, die nach
        # einem Modellfehler aussieht statt nach einem Aufrufproblem.
        "--output-format", "stream-json",
        "--verbose",
        *(("--model", model) if model else ()),
        *CLAUDE_ISOLATION_ARGV,
        *DISALLOWED_TOOLS_ARGV,
    ]
    ergebnis = run(argv, input=zeile + "\n", capture_output=True, text=True, timeout=timeout_s)
    return _result_text(ergebnis.stdout or "") or _fehler(ergebnis)


def _result_text(stdout: str) -> str:
    """Die Antwort aus dem Strom — die letzte `result`-Zeile gewinnt."""
    antwort = ""
    for zeile in stdout.splitlines():
        zeile = zeile.strip()
        if not zeile.startswith("{"):
            continue
        try:
            obj = json.loads(zeile)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("result"), str):
            antwort = obj["result"]
    return antwort.strip()


def _fehler(ergebnis) -> str:
    zeilen = (getattr(ergebnis, "stderr", "") or "").strip().splitlines()
    raise RuntimeError(f"vision failed: {zeilen[-1][:200] if zeilen else 'no answer'}")


def make_vision_runner(*, binary: str, model: str = "", run=subprocess.run):
    """Der Runner. Das Gating liegt beim Kernel — `path` ist ein ganz normales Ziel."""

    def see_image(req) -> str:
        return describe(
            req.args.get("path", ""),
            req.args.get("question", ""),
            binary=binary,
            model=model,
            run=run,
        )

    return see_image


def vision_spec():
    """READ — und deshalb greift der Secret-Floor: `~/.secrets/x.png` wird nicht gesehen."""
    from .manifest import Effect, ToolSpec

    return ToolSpec("see_image", Effect.READ, reversible=True)


__all__ = [
    "DEFAULT_QUESTION",
    "MAX_IMAGE_BYTES",
    "UNSUPPORTED",
    "describe",
    "make_vision_runner",
    "media_type",
    "stream_json_line",
    "vision_spec",
]
