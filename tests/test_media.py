"""Sehen und Sprechen — und der Nachweis, dass beide gewoehnliche Wirkungen sind.

Der Punkt dieser Datei ist nicht, dass die Werkzeuge funktionieren. Er ist, dass sie
KEINE neue Erlaubnis-Idee einfuehren: Sehen ist ein Lesen mit einem Ziel, Sprechen ein
Schreiben mit einem Ziel. Damit gelten die Floors, die es laengst gibt.
"""
from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from talos import speech, vision
from talos.channel import Principal
from talos.manifest import Effect
from talos.policy import TARGET_EXTRACTORS, PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest

OWNER = Principal("telegram", "100000001")
HOME = str(Path.home())


def _png(width: int = 8, height: int = 8) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        koerper = typ + data
        return struct.pack(">I", len(data)) + koerper + struct.pack(">I", zlib.crc32(koerper) & 0xffffffff)

    roh = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(roh)) + chunk(b"IEND", b""))


@dataclass
class _Lauf:
    stdout: str = ""
    stderr: str = ""


# --- Sehen -------------------------------------------------------------------------
def test_the_media_type_comes_from_the_bytes_not_the_name(tmp_path: Path) -> None:
    """Eine Endung ist eine Behauptung des Dateinamens. Waere sie massgeblich, ginge
    beliebiger Inhalt als PNG deklariert an ein fremdes System."""
    assert vision.media_type(_png()[:16]) == "image/png"
    assert vision.media_type(b"\xff\xd8\xff\xe0" + b"x" * 12) == "image/jpeg"
    assert vision.media_type(b"RIFF" + b"1234" + b"WEBP") == "image/webp"
    assert vision.media_type(b"%PDF-1.7 ...") == ""      # kein Bild, auch nicht als .png


def test_a_non_image_is_refused_before_it_leaves_the_machine(tmp_path: Path) -> None:
    getarnt = tmp_path / "bild.png"
    getarnt.write_bytes(b"%PDF-1.7 nicht wirklich ein Bild")
    gesehen: list = []
    with pytest.raises(ValueError) as fehler:
        vision.describe(getarnt, binary="/usr/local/bin/claude",
                        run=lambda *a, **k: gesehen.append(a) or _Lauf())
    assert vision.UNSUPPORTED in str(fehler.value)
    assert gesehen == []      # nichts wurde verschickt


def test_the_image_travels_as_a_content_block_and_the_answer_comes_back(tmp_path: Path) -> None:
    bild = tmp_path / "rot.png"
    bild.write_bytes(_png())
    gesehen: list = []

    def run(argv, input="", **_kw):
        gesehen.append((argv, input))
        return _Lauf(stdout=json.dumps({"type": "result", "result": "Rot"}))

    antwort = vision.describe(bild, "Welche Farbe?", binary="/usr/local/bin/claude", run=run)
    assert antwort == "Rot"

    argv, eingabe = gesehen[0]
    nachricht = json.loads(eingabe.strip())
    bloecke = nachricht["message"]["content"]
    assert bloecke[0]["type"] == "text" and "Welche Farbe?" in bloecke[0]["text"]
    assert bloecke[1]["source"]["media_type"] == "image/png"
    # Der Aufruf traegt DIESELBE Isolation wie das Denken — kein zweites, weicheres Tor.
    assert "--disallowed-tools" in argv and "--strict-mcp-config" in argv


def test_an_oversized_image_is_refused(tmp_path: Path) -> None:
    riesig = tmp_path / "gross.png"
    riesig.write_bytes(_png() + b"\x00" * (vision.MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError):
        vision.describe(riesig, binary="/usr/local/bin/claude", run=lambda *a, **k: _Lauf())


def test_seeing_is_a_read_with_a_real_target() -> None:
    """Der Grund, warum TALOS die Datei laedt und nicht der Reasoner: nur so gibt es
    ueberhaupt ein Ziel, ueber das der Kernel urteilen kann."""
    spec = default_manifest().get("see_image")
    assert spec is not None and spec.effect is Effect.READ
    assert TARGET_EXTRACTORS["see_image"]({"path": "/tmp/x.png"}) == ("/tmp/x.png",)


def test_an_image_inside_the_secrets_folder_is_not_shown() -> None:
    """Der Floor greift ohne eine Zeile Sonderbehandlung im Sicht-Modul."""
    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    entschieden = kernel.decide(
        ToolRequest("see_image", OWNER, {"path": f"{HOME}/.secrets/foto.png"})
    )
    assert entschieden.verdict is Verdict.DENY


# --- Sprechen ----------------------------------------------------------------------
def test_speaking_is_a_write_with_the_output_path_as_target() -> None:
    spec = default_manifest().get("speak")
    assert spec is not None and spec.effect is Effect.WRITE
    assert TARGET_EXTRACTORS["speak"]({"path": "/tmp/t.wav"}) == ("/tmp/t.wav",)


def test_speaking_into_a_persistence_path_asks_the_operator() -> None:
    """Eine Stimmdatei nach `~/.config/systemd/` ist ein Persistenz-Schreibzugriff —
    und wird als solcher behandelt, ohne dass `speech.py` davon weiss."""
    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    entschieden = kernel.decide(
        ToolRequest("speak", OWNER, {"text": "hallo", "path": f"{HOME}/.config/systemd/x.wav"})
    )
    assert entschieden.verdict is Verdict.NEEDS_HUMAN


def test_the_output_must_be_a_wav(tmp_path: Path) -> None:
    """Piper schreibt WAV. Eine andere Endung waere eine Luege ueber den Inhalt."""
    with pytest.raises(ValueError):
        speech.speak("hallo", tmp_path / "t.mp3", piper_bin="/x/piper", voice="/v.onnx",
                     run=lambda *a, **k: _Lauf())


def test_empty_text_is_a_plain_tool_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        speech.speak("   ", tmp_path / "t.wav", piper_bin="/x/piper", voice="/v.onnx",
                     run=lambda *a, **k: _Lauf())


def test_without_a_voice_model_it_says_how_to_install_one(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as fehler:
        speech.speak("hallo", tmp_path / "t.wav", piper_bin="/x/piper", voice="",
                     run=lambda *a, **k: _Lauf())
    assert "download_voices" in str(fehler.value)


def test_a_silent_failure_is_an_error_not_an_empty_file(tmp_path: Path) -> None:
    """Sonst meldet Talos „gesprochen" und es liegt eine leere Datei da."""
    ziel = tmp_path / "t.wav"
    with pytest.raises(RuntimeError):
        speech.speak("hallo", ziel, piper_bin="/x/piper", voice="/v.onnx",
                     run=lambda *a, **k: _Lauf(stderr="model not found"))


def test_the_voice_is_found_by_looking_not_by_guessing_a_name(tmp_path: Path) -> None:
    """Ein fest verdrahteter Modellname waere wieder eine Annahme ueber eine fremde
    Maschine — dieselbe Falle wie beim hartkodierten DATA_DIR."""
    assert speech.find_voice(tmp_path) == ""
    (tmp_path / "de_DE-thorsten-medium.onnx").write_bytes(b"x")
    assert speech.find_voice(tmp_path).endswith("de_DE-thorsten-medium.onnx")


def test_run_reads_no_name_that_does_not_exist() -> None:
    """Der Fall, den KEIN Unit-Test hatte und der den Dienst umwarf.

    Die Verdrahtung in `__main__.run()` las `selection` — eine Variable, die dort nicht
    existiert (sie ist Parameter einer INNEREN Funktion). Alle 1215 Tests blieben gruen,
    weil keiner `run()` anfasst; erst der Dienst fiel mit `NameError` um und startete in
    einer Schleife neu, waehrend die Suite gruen dastand.

    `symtable` beantwortet das exakt: es kennt die Bindungen jedes Gueltigkeitsbereichs
    und verwechselt — anders als `co_names` — Attributnamen nicht mit freien Namen. Was
    `run` global liest und was es im Modul nicht gibt, ist ein `NameError`, der nur auf
    seinen Aufruf wartet.
    """
    import builtins
    import symtable

    from talos import __main__ as hauptmodul

    quelle = Path(hauptmodul.__file__).read_text(encoding="utf-8")
    tabelle = symtable.symtable(quelle, hauptmodul.__file__, "exec")
    lauf = tabelle.lookup("run").get_namespace()
    fehlend = sorted(
        s.get_name() for s in lauf.get_symbols()
        if s.is_global() and s.is_referenced()
        and s.get_name() not in vars(hauptmodul) and not hasattr(builtins, s.get_name())
    )
    assert not fehlend, f"run() liest Namen, die es nicht gibt: {fehlend}"


# --- Hoeren: dieselbe Bauart wie Sehen ----------------------------------------------
class _Stueck:
    def __init__(self, text): self.text = text


class _Motor:
    def __init__(self, stuecke=("Hallo", "wie geht es"), dauer=12.0):
        self.stuecke, self.dauer, self.gesehen = stuecke, dauer, []

    def transcribe(self, pfad, language=None, beam_size=1):
        self.gesehen.append((pfad, language))
        return [_Stueck(s) for s in self.stuecke], type("I", (), {"duration": self.dauer})()


def _audio(tmp_path, name="nachricht.oga", groesse=2048):
    p = tmp_path / name
    p.write_bytes(b"OggS" + b"\x00" * groesse)
    return p


def test_hearing_is_a_read_with_the_file_as_target() -> None:
    """Eine Aufnahme ist eine Datei. Damit gilt derselbe Floor wie beim Sehen, ohne dass
    das Hoer-Modul davon wissen muesste."""
    from talos import hearing

    spec = default_manifest().get("hear")
    assert spec is not None and spec.effect is Effect.READ
    assert TARGET_EXTRACTORS["hear"]({"path": "/tmp/a.oga"}) == ("/tmp/a.oga",)
    assert hearing.DEFAULT_MODEL


def test_a_recording_in_the_secrets_folder_is_not_transcribed() -> None:
    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    entschieden = kernel.decide(
        ToolRequest("hear", OWNER, {"path": f"{HOME}/.secrets/mitschnitt.oga"})
    )
    assert entschieden.verdict is Verdict.DENY


def test_what_was_said_comes_back(tmp_path: Path) -> None:
    from talos import hearing

    motor = _Motor()
    gehoert = hearing.transcribe(_audio(tmp_path), engine=motor, probe=lambda p: 12.0)
    assert gehoert.text == "Hallo wie geht es"
    assert gehoert.seconds == 12.0


def test_a_recording_that_is_too_long_is_refused_before_it_starts(tmp_path: Path) -> None:
    """Eine Stunde Audio beschaeftigt den Pi minutenlang, und der Zug wartet solange."""
    from talos import hearing

    motor = _Motor()
    with pytest.raises(ValueError) as fehler:
        hearing.transcribe(_audio(tmp_path), engine=motor, probe=lambda p: 5_000.0)
    assert "too long" in str(fehler.value)
    assert motor.gesehen == []          # es wurde nicht einmal angefangen


def test_something_that_is_not_audio_is_refused(tmp_path: Path) -> None:
    from talos import hearing

    with pytest.raises(ValueError) as fehler:
        hearing.transcribe(_audio(tmp_path, "notiz.txt"), engine=_Motor())
    assert hearing.UNSUPPORTED in str(fehler.value)


def test_a_missing_engine_names_the_command_instead_of_crashing(tmp_path: Path) -> None:
    """Eine optionale Faehigkeit darf den Dienst beim Hochfahren nicht umwerfen."""
    import builtins

    from talos import hearing

    echt = builtins.__import__

    def ohne(name, *a, **kw):
        if name == "faster_whisper":
            raise ImportError("nope")
        return echt(name, *a, **kw)

    builtins.__import__ = ohne
    try:
        with pytest.raises(RuntimeError) as fehler:
            hearing.transcribe(_audio(tmp_path), probe=lambda p: 5.0)
    finally:
        builtins.__import__ = echt
    assert "pip install faster-whisper" in str(fehler.value)
    assert "nothing spoken leaves the machine" in str(fehler.value)


def test_silence_is_reported_as_silence_not_as_an_empty_answer(tmp_path: Path) -> None:
    from talos import hearing

    runner = hearing.make_hear_runner(engine=_Motor(stuecke=(), dauer=3.0), probe=lambda p: 3.0)
    antwort = runner(type("R", (), {"args": {"path": str(_audio(tmp_path))}})())
    assert "no speech" in antwort


# --- Standbild aus einem Video: zwei Ziele, ein Bild -------------------------------
def _video(tmp_path, name="clip.mp4", groesse=4096):
    p = tmp_path / name
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * groesse)
    return p


@pytest.fixture
def inbox(tmp_path: Path, monkeypatch) -> Path:
    """Der Posteingang, in den ein Standbild faellt — hier ein eigener pro Test.

    Ohne das schreibt die Suite in den ECHTEN `workspace/inbox/` des Repositories
    (der Pfad haengt an `policy.FRAME_INBOX`, nicht an `tmp_path`), und ein
    fehlgeschlagener Test laesst sein Bild dort liegen. Genau das ist passiert.
    Der Test, der die READ-Einordnung traegt, patcht bewusst NICHT — er muss den
    echten Pfad sehen.
    """
    from talos import policy

    ziel = tmp_path / "inbox"
    monkeypatch.setattr(policy, "FRAME_INBOX", ziel)
    return ziel


class _Ffmpeg:
    """Ein ffmpeg-Double, das tut, was ffmpeg tut: eine Datei am letzten Argument."""

    def __init__(self, *, schreibt=True):
        self.schreibt, self.aufrufe = schreibt, []

    def __call__(self, argv, **kw):
        self.aufrufe.append(list(argv))
        if self.schreibt:
            ziel = Path(argv[-1])
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_bytes(b"\xff\xd8\xff" + b"\x00" * 512)
        return type("E", (), {"stdout": "", "stderr": "moov atom not found"})()


def test_a_frame_is_a_write_with_the_video_and_the_picture_as_targets() -> None:
    """Beide Pfade, und beide aus einem Grund.

    Die QUELLE, weil ein Video sonst der bequemste Weg am Secret-Floor vorbei waere.
    Das ERGEBNIS, weil dort eine Datei entsteht — auch wenn ihren Pfad niemand waehlen
    darf, urteilt der Kernel darueber wie ueber jedes andere Schreiben.
    """
    from talos.policy import frame_output_path

    spec = default_manifest().get("grab_frame")
    assert spec is not None and spec.effect is Effect.READ and spec.reversible

    ziele = TARGET_EXTRACTORS["grab_frame"]({"path": "/tmp/clip.mp4", "at": 12.5})
    assert ziele == ("/tmp/clip.mp4", frame_output_path("/tmp/clip.mp4", 12.5))
    assert ziele[1].endswith("/inbox/frame-clip-12.5s.jpg")


def test_a_video_in_the_secrets_folder_never_becomes_a_picture() -> None:
    """Ohne die Quelle als Ziel waere Frame Capture der Weg am Secret-Floor vorbei:
    Video lesen, Bild schreiben, `see_image` darauf — die Sperre haette gehalten und
    trotzdem nichts genuetzt."""
    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    entschieden = kernel.decide(
        ToolRequest("grab_frame", OWNER, {"path": f"{HOME}/.secrets/aufnahme.mp4"})
    )
    assert entschieden.verdict is Verdict.DENY


def test_the_model_cannot_choose_where_the_picture_lands(tmp_path: Path, inbox: Path) -> None:
    """Ein mitgeschicktes Ausgabefeld wird nicht einmal angesehen — sonst gaebe es
    einen zweiten Weg, Inhalt aus einer fremden Datei an eine gewaehlte Stelle zu
    schreiben."""
    from talos import frames

    ffmpeg = _Ffmpeg()
    bild = frames.grab(
        _video(tmp_path),
        "",
        run=ffmpeg,
        probe=lambda p: 10.0,
    )
    assert Path(bild.path).parent == inbox
    ziele = TARGET_EXTRACTORS["grab_frame"](
        {"path": str(_video(tmp_path)), "out": f"{HOME}/.bashrc", "path_out": "/etc/passwd"}
    )
    assert len(ziele) == 2 and ziele[1] == bild.path


def test_the_kernel_judges_exactly_the_file_that_appears(tmp_path: Path, inbox: Path) -> None:
    """Der eine Test, der die ganze Bauart traegt: haetten Kernel und Runner zwei
    Rechenwege fuer denselben Pfad, urteilte der Kernel ueber eine Datei, die so nie
    entsteht — und die echte entstuende ungeprueft."""
    from talos import frames
    from talos.policy import frame_output_path

    ffmpeg = _Ffmpeg()
    for at in ("", 7, 3.25, "nonsense", -4, "1e400"):
        bild = frames.grab(_video(tmp_path), at, run=ffmpeg, probe=lambda p: 20.0)
        assert bild.path == frame_output_path(_video(tmp_path), at)


def test_a_video_name_cannot_walk_out_of_the_inbox() -> None:
    """Der Name kommt aus einer Datei, die jemand anders benannt hat."""
    from talos.policy import FRAME_INBOX, frame_output_path

    for boese in ("../../etc/passwd.mp4", "/etc/cron.d/x.mp4", "a/../../b.mp4", "....mp4"):
        ziel = Path(frame_output_path(boese))
        assert ziel.parent == FRAME_INBOX
        assert ".." not in ziel.name


def test_without_a_timestamp_it_takes_the_middle(tmp_path: Path, inbox: Path) -> None:
    """Der erste Frame ist bei den meisten Aufnahmen schwarz."""
    from talos import frames

    ffmpeg = _Ffmpeg()
    bild = frames.grab(_video(tmp_path), run=ffmpeg, probe=lambda p: 30.0)
    assert bild.at == 15.0
    assert "-ss" in ffmpeg.aufrufe[0]
    assert ffmpeg.aufrufe[0][ffmpeg.aufrufe[0].index("-ss") + 1] == "15.000"


def test_without_a_measurable_duration_it_takes_one_second(tmp_path: Path, inbox: Path) -> None:
    from talos import frames

    ffmpeg = _Ffmpeg()
    bild = frames.grab(_video(tmp_path), run=ffmpeg, probe=lambda p: 0.0)
    assert bild.at == frames.DEFAULT_SECONDS


def test_it_is_one_frame_and_never_a_series(tmp_path: Path, inbox: Path) -> None:
    """Ein real vorgekommener Fall: auf „analysiere jedes Bild" faechert ffmpeg auf,
    die Bildaufrufe laufen parallel, die CPU ist voll und der Dienst faellt beim
    Healthcheck durch."""
    from talos import frames

    ffmpeg = _Ffmpeg()
    bild = frames.grab(_video(tmp_path), run=ffmpeg, probe=lambda p: 60.0)
    argv = ffmpeg.aufrufe[0]
    assert argv[argv.index("-frames:v") + 1] == "1"
    assert argv[argv.index("-map") + 1] == "0:v:0"
    assert argv.index("-ss") < argv.index("-i")      # springen statt dekodieren


def test_something_that_is_not_a_video_is_refused_before_ffmpeg_starts(tmp_path: Path) -> None:
    from talos import frames

    ffmpeg = _Ffmpeg()
    with pytest.raises(ValueError) as fehler:
        frames.grab(_video(tmp_path, "notiz.txt"), run=ffmpeg)
    assert frames.UNSUPPORTED in str(fehler.value)
    assert ffmpeg.aufrufe == []


def test_a_video_that_is_too_large_is_refused_before_it_starts(
    tmp_path: Path, inbox: Path, monkeypatch
) -> None:
    from talos import frames

    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(frames, "MAX_VIDEO_BYTES", 8)
    with pytest.raises(ValueError) as fehler:
        frames.grab(_video(tmp_path), run=ffmpeg, probe=lambda p: 5.0)
    assert "larger than" in str(fehler.value)
    assert ffmpeg.aufrufe == []


def test_no_frame_is_reported_honestly_not_as_success(tmp_path: Path, inbox: Path) -> None:
    """Ein Zeitpunkt hinter dem Ende liefert kein Bild — und darf nicht so aussehen."""
    from talos import frames

    with pytest.raises(RuntimeError) as fehler:
        frames.grab(_video(tmp_path), 9_000, run=_Ffmpeg(schreibt=False), probe=lambda p: 5.0)
    assert frames.NO_FRAME.split(" —")[0] in str(fehler.value)


def test_a_missing_ffmpeg_names_the_command_instead_of_crashing(tmp_path: Path, inbox: Path) -> None:
    from talos import frames

    def fehlt(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "ffmpeg")

    with pytest.raises(RuntimeError) as fehler:
        frames.grab(_video(tmp_path), run=fehlt, probe=lambda p: 5.0)
    assert frames.NO_FFMPEG in str(fehler.value)


def test_the_answer_points_at_the_picture_so_seeing_can_follow(tmp_path: Path, inbox: Path) -> None:
    from talos import frames

    runner = frames.make_grab_runner(run=_Ffmpeg(), probe=lambda p: 8.0)
    antwort = runner(type("R", (), {"args": {"path": str(_video(tmp_path))}})())
    assert "see_image" in antwort and "/inbox/frame-clip" in antwort


def test_the_read_verdict_rests_on_the_picture_landing_in_the_workspace() -> None:
    """Der Waechter ueber der Einordnung in `frames.grab_frame_spec`.

    `grab_frame` ist READ, obwohl eine Datei entsteht — tragfaehig nur, solange diese
    Datei in einem Bereich landet, ueber den WRITE nicht strenger urteilen wuerde.
    Rutschte der Posteingang je unter einen Secret-, Persistenz- oder Systempfad, waere
    ein Schreiben ohne Freigabe daraus geworden, und dieser Test faellt vorher.
    """
    from talos.policy import (
        PERSISTENCE_PREFIXES,
        SECRET_PREFIXES,
        SYSTEM_PREFIXES,
        WORKSPACE_DIR,
        _hits,
        frame_output_path,
    )

    namen = ("clip.mp4", "../../etc/passwd.mp4", f"{HOME}/.secrets/x.mp4", "/etc/shadow.mp4")
    for name in namen:
        ziel = frame_output_path(name, 3)
        assert Path(ziel).is_relative_to(WORKSPACE_DIR)
        for floor in (SECRET_PREFIXES, PERSISTENCE_PREFIXES, SYSTEM_PREFIXES):
            assert not _hits(ziel, floor), f"{ziel} trifft einen Floor — READ ist dann falsch"


def test_a_secret_video_is_denied_and_not_merely_asked_about() -> None:
    """Der Unterschied, den die Einordnung ausmacht: als WRITE waere dieselbe Aufnahme
    freigebbar gewesen — waehrend `hear` sie hart verweigert. Zwei Tueren zu demselben
    Inhalt mit zwei Haerten sind eine Tuer zu viel."""
    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    video = kernel.decide(ToolRequest("grab_frame", OWNER, {"path": f"{HOME}/.secrets/a.mp4"}))
    audio = kernel.decide(ToolRequest("hear", OWNER, {"path": f"{HOME}/.secrets/a.oga"}))
    assert video.verdict is audio.verdict is Verdict.DENY


def test_every_tool_in_the_manifest_has_a_runner_in_the_composition_root() -> None:
    """Falle 7 aus `CLAUDE.md`, eine Stufe schaerfer.

    `symtable` beweist, dass `run()` keine Namen liest, die es nicht gibt — aber nicht,
    dass jedes angebotene Werkzeug dort auch verdrahtet ist. Ein Werkzeug im Manifest
    ohne Runner faellt erst auf, wenn das Modell es zum ersten Mal aufruft: der Kernel
    gibt frei, der Executor sucht ins Leere. Ein gruener Lauf haette das nie gezeigt,
    weil kein Test die Verdrahtung anfasst.

    Statisch gelesen statt ausgefuehrt: `run()` braucht ein Bot-Token und eine
    Modell-CLI, und ein Test, der beides verlangt, laeuft irgendwann nirgends mehr.
    """
    import ast

    from talos import __main__ as hauptmodul
    from talos import web

    baum = ast.parse(Path(hauptmodul.__file__).read_text(encoding="utf-8"))
    lauf = next(k for k in ast.walk(baum) if isinstance(k, ast.FunctionDef) and k.name == "run")
    verdrahtet: set[str] = set()
    for knoten in ast.walk(lauf):
        if isinstance(knoten, ast.Dict):
            verdrahtet |= {
                s.value for s in knoten.keys
                if isinstance(s, ast.Constant) and isinstance(s.value, str)
            }
    # Drei Gruppen kommen als `**…` herein — ihre Namen stehen nicht im Quelltext, also
    # werden sie an derselben Quelle gefragt, aus der `run()` sie zur Laufzeit nimmt.
    from talos import tools as werkzeuge

    verdrahtet |= set(web.make_web_runners(search_api_key=""))
    verdrahtet |= set(werkzeuge.RUNNERS)
    verdrahtet |= set(werkzeuge.make_vault_runners(Path("/tmp"), "qmd"))

    fehlend = sorted(s.name for s in default_manifest().tools if s.name not in verdrahtet)
    assert not fehlend, f"Werkzeuge im Manifest ohne Runner in run(): {fehlend}"
