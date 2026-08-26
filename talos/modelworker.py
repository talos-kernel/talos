"""Der Modell-Worker: Denken hinter einer eigenen UID, ohne Schluessel im Agenten.

Warum er existiert: die haerteste Grenze ist nicht der Kernel, sondern das Dateisystem
(CLAUDE.md). Solange der Agent-Prozess die Provider-Schluessel in seiner Umgebung
traegt, reicht EIN Fehler in ihm — nicht im Kernel — um sie zu verlieren. Dieser
Daemon nimmt dem Agenten die Schluessel ganz: er laeuft als eigener Benutzer
(`talos-model`), liest seine Zugangsdaten aus `/etc/talos/model.env` und spricht mit
den Anbietern. Der Agent schickt Anfragen ueber einen Unix-Socket; ueber den Draht
gehen Prompts und Antworten, nie Schluessel.

Was der Worker BEWUSST nicht hat: kein Werkzeug-Code, keine Shell, kein Dateisystem
ausser seiner Env-Datei. Er ist ein dummes Rohr zum Anbieter — dieselbe Rolle, die
`ApiReasoner` heute im Agenten spielt, nur hinter einer UID-Grenze. ⚠️ Der Code selbst
setzt KEINE Rechte (kein setuid): die Trennung ist eine Installations-Entscheidung
(systemd `User=talos-model`, Verzeichnis- und Datei-Eigentum — siehe `deploy/` und
`docs/model-worker.md`). Dieser Prozess spricht nur Socket.

Protokoll — JSON-Lines, eine Zeile pro Richtung, eine Anfrage pro Verbindung:

    →  {"provider": "openai-api", "model": "…",
        "messages": [{"role": "system"|"user", "content": "…"}],
        "params": {"timeout_s": 180}}
    ←  {"ok": true,  "text": "…", "model": "…"}
    ←  {"ok": false, "kind": "rate_limited", "message": "(Reasoner error: …)"}

`kind` ist exakt die `ReasonerFailure`-Taxonomie aus `api_reasoner.py`, damit die
Fallback-Kette ueber den Socket unveraendert funktioniert. Unbekannte Felder im Frame
werden verworfen, unbekannte Rollen ebenfalls; ein kaputter Frame bekommt
`invalid_request` als Antwort und kostet die Verbindung — nie den Daemon.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .api_reasoner import (
    KIND_KEY_REJECTED,
    KIND_NETWORK_FAILED,
    NETWORK_FAILED,
    SUPPORTED_PROVIDERS,
    ApiReasoner,
    ReasonerFailure,
    _SECRETISH,
)
from .credentials import CredentialStore, MissingKey, from_lookup

__all__ = [
    "DEFAULT_ENV",
    "DEFAULT_SOCKET",
    "ENV_FILE_VAR",
    "KIND_INVALID",
    "SOCKET_ENV_VAR",
    "build_store",
    "handle_frame",
    "main",
    "serve",
]

DEFAULT_SOCKET = "/run/talos/model.sock"
DEFAULT_ENV = "/etc/talos/model.env"
SOCKET_ENV_VAR = "TALOS_MODEL_WORKER_SOCKET"
ENV_FILE_VAR = "TALOS_MODEL_WORKER_ENV"

# Rahmen-Deckel: eine Anfrage traegt den Gespraechskontext. Die Grenze schuetzt den
# Daemon gegen einen Client, der unbegrenzt Bytes kippt — inhaltlich begrenzt sie
# nichts, was ein echter Zug braucht.
MAX_FRAME_BYTES = 8 * 1024 * 1024
# Wer verbindet, aber nichts schickt, blockiert die Schleife hoechstens so lange.
READ_TIMEOUT_S = 30.0
# Der Zeit-Deckel einer Anfrage, wenn der Client keinen nennt (derselbe Wert, den der
# Agent als Vorgabe traegt) — und die harte Obergrenze, damit kein Frame den Daemon
# beliebig lange an einen Anbieter bindet.
DEFAULT_TIMEOUT_S = 180
MAX_TIMEOUT_S = 900

# Keine Anbieter-Art: der Frame selbst war unlesbar. Der Agent bildet sie auf
# HTTP_FAILED ab — nicht fallbackbar, weil jeder Hop denselben Frame ablehnen wuerde.
KIND_INVALID = "invalid_request"


def _read_env_file(path: Path) -> dict[str, str]:
    """Simple KEY=VALUE-Datei. Fehlt sie -> leer (der Bestand ist dann eben leer).

    Bewusst eine eigene, dreizeilige Leseroutine statt `config._read_env_file`: die
    Agent-Konfiguration in den Worker zu importieren hiesse, deren saemtliche Pfade
    und Zustaende mitzuziehen — der Worker soll WENIGER wissen als der Agent.
    """
    werte: dict[str, str] = {}
    if not path.is_file():
        return werte
    for zeile in path.read_text(encoding="utf-8").splitlines():
        s = zeile.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        name, _, wert = s.partition("=")
        werte[name.strip()] = wert.strip()
    return werte


def build_store(env_path: str, environ: Mapping[str, str]) -> CredentialStore:
    """Der Schluesselbestand des Workers: Prozess-Env schlaegt die Env-Datei.

    Derselbe Bauplan wie im Agenten (`from_lookup` ueber den Katalog): ein Anbieter
    ohne hinterlegten Schluessel taucht gar nicht auf, lokale Anbieter tragen nur
    eine Adresse. Die Fehlerklasse „Schluessel des einen an den anderen" ist hier
    bereits durch den gemeinsamen Code ausgeschlossen.
    """
    werte = _read_env_file(Path(env_path))

    def lookup(name: str) -> str:
        return environ.get(name) or werte.get(name, "")

    return from_lookup(lookup)


def _build_reasoner(
    provider: str, model: str, store: CredentialStore, *, timeout_s: int
) -> ApiReasoner:
    """Der Reasoner des Workers. `worker=""` ist Pflicht, nicht Vorgabe: stuende in
    der Worker-Umgebung selbst ein `TALOS_MODEL_WORKER`, reichte der Worker seine
    Anfragen ueber einen Socket WEITER — die UID-Trennung liefe im Kreis, und der
    Schluessel laege doch wieder in dem Prozess, der spricht."""
    return ApiReasoner(provider, model, store, timeout_s=timeout_s, worker="")


def _invalid(detail: str) -> dict[str, Any]:
    return {"ok": False, "kind": KIND_INVALID,
            "message": f"(Model worker: invalid request — {detail[:200]})"}


def _fehler(kind: str, message: str) -> dict[str, Any]:
    return {"ok": False, "kind": kind, "message": message}


def _scrub(value: object, store: CredentialStore) -> str:
    """Kein hinterlegter Schluessel und nichts Geheimnisfoermiges in einer Meldung —
    dieselbe Regel wie im Agenten, hier gegen den letzten Fang (`except Exception`)."""
    text = str(value)
    for schluessel in store.all_keys():
        text = text.replace(schluessel, "***")
    return _SECRETISH.sub("***", text).strip()[:300]


def _split_messages(messages: object) -> tuple[str, str] | None:
    """(system, user) aus der Nachrichtenliste — oder `None` bei unbrauchbarem Frame.

    Mehrere Bloecke derselben Rolle werden mit Leerzeile verbunden. Unbekannte
    Rollen (etwa `assistant`) fallen WEG statt geglaubt zu werden: der Agent schickt
    system+user, und was er nicht schickt, darf der Worker nicht als Vorgeschichte
    erfinden — ein eingeschmuggelter `assistant`-Block waere eine Anweisung, die nie
    durch den Agenten lief.
    """
    if not isinstance(messages, list):
        return None
    system_teile: list[str] = []
    user_teile: list[str] = []
    for eintrag in messages:
        if not isinstance(eintrag, dict):
            return None
        rolle = eintrag.get("role")
        inhalt = eintrag.get("content")
        if not isinstance(inhalt, str):
            return None
        if rolle == "system":
            system_teile.append(inhalt)
        elif rolle == "user":
            user_teile.append(inhalt)
        # Unbekannte Rollen: verworfen (siehe Docstring).
    if not any(teil.strip() for teil in user_teile):
        return None
    return "\n\n".join(system_teile), "\n\n".join(user_teile)


def _timeout(params: object) -> int:
    """`timeout_s` aus `params`, gedeckelt. Alles andere in `params` faellt weg."""
    if not isinstance(params, dict):
        return DEFAULT_TIMEOUT_S
    try:
        wert = int(params.get("timeout_s"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return min(max(1, wert), MAX_TIMEOUT_S)


def handle_frame(
    raw: bytes,
    store: CredentialStore,
    *,
    build: Callable[..., ApiReasoner] | None = None,
) -> dict[str, Any]:
    """Eine Anfrage-Zeile → der Antwort-Frame. Wirft NIE: die Verbindung traegt den
    Fehler, der Daemon ueberlebt jeden Frame — das ist die Robustheits-Zusage."""
    try:
        frame = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return _invalid("kein lesbares JSON")
    if not isinstance(frame, dict):
        return _invalid("der Frame ist kein Objekt")
    # Nur diese vier Felder werden gelesen — alles andere im Frame wird verworfen,
    # nicht geglaubt. Ein `admin`- oder `shell`-Feld des Callers existiert hier nicht.
    provider = frame.get("provider")
    model = frame.get("model")
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        # Kein Rueckfall auf einen anderen Anbieter: der Caller hat den Empfaenger
        # seiner Daten BENANNT (dieselbe Regel wie `credentials.CredentialStore`).
        return _invalid("unbekannter oder fehlender provider")
    if not isinstance(model, str) or not model.strip():
        return _invalid("model fehlt")
    teile = _split_messages(frame.get("messages"))
    if teile is None:
        return _invalid("messages fehlen oder tragen keinen user-Inhalt")
    system, message = teile
    timeout_s = _timeout(frame.get("params"))
    hersteller = build if build is not None else _build_reasoner
    try:
        reasoner = hersteller(provider, model.strip(), store, timeout_s=timeout_s)
    except MissingKey:
        # Der WORKER hat keinen Schluessel fuer diesen Anbieter. Dieselbe Klasse wie
        # ein abgelehnter Schluessel: die Fallback-Kette des Agenten darf es beim
        # naechsten Anbieter versuchen.
        return _fehler(
            KIND_KEY_REJECTED,
            f"(Reasoner error: the model worker holds no key for {provider!r}.)",
        )
    except ValueError as ungueltig:
        return _invalid(str(ungueltig))
    try:
        text = reasoner.reason_composed(system, message)
    except ReasonerFailure as failure:
        # Bereits klassifiziert und bereinigt — die Art reist unverfaelscht zurueck,
        # damit die Kette des Agenten ueber den Socket dieselben Entscheidungen
        # trifft wie auf dem Direktweg.
        return _fehler(failure.kind, failure.message)
    except Exception as error:
        # Der letzte Fang: die Meldung geht nie roh raus — Bibliotheks-Details
        # koennen gespiegelte Header (und damit Schluessel) tragen.
        return _fehler(
            KIND_NETWORK_FAILED, NETWORK_FAILED.format(detail=_scrub(error, store))
        )
    return {"ok": True, "text": text, "model": reasoner.model}


class _RahmenZuGross(Exception):
    """Die Anfrage-Zeile ueberschreitet MAX_FRAME_BYTES ohne Zeilenende."""


def _lese_zeile(verbindung: socket.socket) -> bytes | None:
    """Eine Zeile vom Client. `None`: Timeout oder Verbindung ohne Inhalt."""
    puffer = bytearray()
    while True:
        if len(puffer) > MAX_FRAME_BYTES:
            raise _RahmenZuGross
        try:
            stueck = verbindung.recv(65536)
        except socket.timeout:
            return None
        if not stueck:
            return bytes(puffer) if puffer else None
        puffer += stueck
        if b"\n" in puffer:
            return bytes(puffer.split(b"\n", 1)[0])


def _bediene(
    verbindung: socket.socket,
    store: CredentialStore,
    build: Callable[..., ApiReasoner] | None,
) -> None:
    """Eine Verbindung: eine Zeile rein, eine Zeile raus. Jeder Fehler wird zur
    Antwort — der Accept-Loop sieht davon nichts (Garbage wedgt den Daemon nie)."""
    try:
        verbindung.settimeout(READ_TIMEOUT_S)
        try:
            roh = _lese_zeile(verbindung)
        except _RahmenZuGross:
            antwort: dict[str, Any] = _invalid("frame ueberschreitet das Limit")
        else:
            if roh is None:
                return  # Verbindung ohne Inhalt — nichts zu beantworten
            antwort = handle_frame(roh, store, build=build)
    except Exception:
        # handle_frame wirft nie; bleibt trotzdem etwas liegen (z.B. recv-OSError),
        # ist die Antwort ein klassifizierter Netzfehler statt eines toten Clients.
        antwort = _fehler(
            KIND_NETWORK_FAILED, NETWORK_FAILED.format(detail="worker-internal error")
        )
    try:
        verbindung.sendall(json.dumps(antwort).encode("utf-8") + b"\n")
    except OSError:
        pass


def _best_effort_owner(pfad: Path) -> None:
    """Socket `talos:talos-model`, WENN die Rechte reichen — sonst still.

    Laueft der Worker wie vorgesehen als `talos-model`, braucht chown root; der
    erwartete Weg ist dann die setgid-Gruppe des Verzeichnisses (der Socket erbt
    `talos-model`, siehe docs/model-worker.md). Die Sicherheit traegt die
    Installation, nicht dieser Versuch — ein Misserfolg ist erwartet und kein Fehler.
    """
    try:
        shutil.chown(pfad, user="talos", group="talos-model")
    except (OSError, LookupError):
        pass


def serve(
    socket_path: str = DEFAULT_SOCKET,
    env_path: str = DEFAULT_ENV,
    *,
    environ: Mapping[str, str] | None = None,
    build: Callable[..., ApiReasoner] | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Der Daemon: binden, Rechte setzen, Anfragen nacheinander bedienen.

    Sequentiell bewusst: der Agent denkt ohnehin nur einen Zug gleichzeitig
    (`ApiReasoner` lehnt den zweiten ab), und ein Worker ohne Threads hat keine
    geteilten Zustaende. `stop` erlaubt Tests ein sauberes Ende; im Dienst laeuft
    die Schleife, bis systemd den Prozess beendet.
    """
    store = build_store(env_path, os.environ if environ is None else environ)
    pfad = Path(socket_path)
    if pfad.exists():
        # Nur ein liegengebliebener Socket wird ersetzt. Jede andere Datei unter
        # diesem Namen gehoert jemandem — sie zu loeschen waere ein Zugriff, den
        # dieser Daemon nicht hat und nicht haben soll.
        if not stat.S_ISSOCK(pfad.stat().st_mode):
            raise RuntimeError(f"{pfad} existiert und ist kein Socket")
        pfad.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(pfad))
        # 0660: Besitzer und Gruppe duerfen sprechen, der Rest der Maschine nicht.
        # Der Agent kommt ueber die GRUPPE herein (`talos` in `talos-model`).
        os.chmod(pfad, 0o660)
        _best_effort_owner(pfad)
        server.listen(8)
        server.settimeout(0.25)
        while stop is None or not stop.is_set():
            try:
                verbindung, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                continue  # z.B. EMFILE: haesslich, aber kein Grund zu sterben
            with verbindung:
                _bediene(verbindung, store, build)
    finally:
        server.close()
        try:
            if pfad.is_socket():
                pfad.unlink()
        except OSError:
            pass


def main() -> None:
    """Einstieg fuer `python -m talos.modelworker` — alles Weitere ist Env."""
    serve(
        os.environ.get(SOCKET_ENV_VAR) or DEFAULT_SOCKET,
        os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV,
    )


if __name__ == "__main__":
    main()
