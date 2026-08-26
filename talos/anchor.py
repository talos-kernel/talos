"""Der Anker fuer die Hash-Kette — der Kopf, an dem Tail-Truncation sichtbar wird.

`eventlog.verify()` beweist, dass keine Zeile veraendert und keine mittige geloescht
wurde. Was es grundsaetzlich nicht sehen kann (sein eigener Docstring sagt es): das
Abschneiden des ENDES. Die letzten n Zeilen zu loeschen hinterlaesst eine kuerzere, in
sich stimmige Kette — ohne einen ausserhalb festgehaltenen Kopf ist das unsichtbar.

Dieser Befehl ist der Festhaltepunkt: er verifiziert die Kette, merkt sich Kopf-Hash
und Zeilenzahl append-only in `data/anchors.jsonl` und schlaegt Alarm, wenn die Zahl
sinkt. Mit `--send` geht der Digest zusaetzlich an den Betreiber-Chat, mit `--mail`
zusaetzlich per Mail an die Owner-Adresse der Allowlist — dann liegt der Anker auch
ausserhalb der Maschine, deren Platte man nicht trauen muss. Beide Wege sind reine
Versandwege; eine neue Empfangsflaeche entsteht dadurch nicht.

⚠️ Ehrliche Grenze, dieselbe wie beim Log selbst: local root kann die Kette NEU
berechnen und den Anker dazu. Der Anker macht das leise Abschneiden sichtbar, nicht
den vollstaendigen Neuaufbau unmoeglich — und behauptet auch nur das.

Exit-Codes folgen `doctor`: 0 ok, 1 kritisch (gebrochene Kette, gesunkene Zeilenzahl,
fehlgeschlagener Versand), 2 falsche Benutzung. Warnungen (Kopf seit >24h unveraendert)
aendern den Exit nicht — ein Cron-Waechter soll an echten Befunden scheitern, nicht an
einem ruhigen Sonntag.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

from .config import DATA_DIR, EVENTLOG_DB
from .ux import SYM_FAIL, SYM_TALOS

# Danach gilt ein unveraenderter Kopf als „der Dienst schreibt nicht mehr" — ein Tag
# ohne ein einziges Ereignis ist bei einem laufenden Agenten kein normaler Zustand.
STALE_AFTER_S = 24 * 3600
# Der Kopf wird in Ausgabe und Digest gekuerzt; die volle Laenge steht in anchors.jsonl.
HEAD_CHARS = 16

ANCHORS_FILE = DATA_DIR / "anchors.jsonl"


def _head(db_path: Path) -> tuple[str, float]:
    """Letzter `chain_hash` und Zeitstempel der juengsten Zeile — direkt gelesen.

    `EventLog.recent()` liefert den chain_hash nicht mit, und das Log-Modul bekommt
    deswegen keine neue Methode: der Leser hier braucht genau zwei Spalten. Nur SELECT,
    kein Schreiben — die einzige Datei, die dieser Befehl anlegt, ist der Anker selbst.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT chain_hash, ts FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return "", 0.0
    return str(row[0] or ""), float(row[1])


def _previous(anchors_path: Path) -> dict | None:
    """Der letzte gueltige Anker-Eintrag — oder None, wenn es noch keinen gibt.

    Kaputte Zeilen werden uebersprungen statt den Lauf zu brechen: die Datei ist
    append-only und von Hand editierbar, ein Befund darf nicht am eigenen Archiv sterben.
    """
    if not anchors_path.is_file():
        return None
    last: dict | None = None
    for raw in anchors_path.read_text(encoding="utf-8").splitlines():
        zeile = raw.strip()
        if not zeile:
            continue
        try:
            record = json.loads(zeile)
        except ValueError:
            continue
        if isinstance(record, dict):
            last = record
    return last


def _record(anchors_path: Path, record: dict) -> None:
    """Haengt eine Zeile an. Append-only, Modus 0o600 vom ersten Byte an.

    `os.open` mit Modus statt `chmod` hinterher — dieselbe Lektion wie in `configcli`:
    zwischen Anlegen und Rechtsetzen liegt ein Fenster, und ein Fenster reicht.
    """
    anchors_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(anchors_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as datei:
        datei.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _zeit(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _digest(record: dict, findings: tuple[str, ...], warnings: tuple[str, ...]) -> str:
    """Der kompakte Text fuer den Betreiber-Chat: Zeit, Zahl, gekuerzter Kopf, Urteil."""
    kopf = record["head_hash"][:HEAD_CHARS] or "-"
    zeilen = [
        f"{SYM_TALOS} anchor {_zeit(record['ts'])} · {record['count']} events · "
        f"head {kopf} · verify {'ok' if record['verify_ok'] else 'BROKEN'}"
    ]
    zeilen += [f"critical: {f}" for f in findings]
    zeilen += [f"warn: {w}" for w in warnings]
    return "\n".join(zeilen)


def _build_sender_for(config) -> Callable[[str], None]:
    """Der Versandweg aus einer geladenen Konfiguration.

    Owner-Chat ist der erste Telegram-Eintrag der Allowlist: wer befehlen darf, bekommt
    auch den Anker. Token und Kennung werden hier nirgends ausgegeben — der Client selbst
    schwaerzt das Token in Fehlertexten (`TelegramClient._call`), dieser Weg fuegt nichts
    hinzu, das es verraten koennte.
    """
    if not config.bot_token:
        raise ValueError("no telegram token configured — cannot send the anchor")
    owner = next(
        (p for p in sorted(config.allowed_principals, key=str) if p.channel == "telegram"),
        None,
    )
    if owner is None:
        raise ValueError(
            "no telegram principal in TALOS_ALLOWED_PRINCIPALS — no owner chat to send to"
        )
    try:
        chat_id = int(owner.user_id)
    except ValueError:
        raise ValueError(f"owner chat id is not a number: {owner.user_id!r}") from None

    from .telegram import TelegramClient

    client = TelegramClient(config.bot_token, config.poll_timeout_s)

    def send(text: str) -> None:
        client.send_message(chat_id, text)

    return send


def _build_sender() -> Callable[[str], None]:
    """Der echte Versandweg — erst aufgebaut, wenn `--send` ihn auch verlangt."""
    from .config import load_config

    return _build_sender_for(load_config())


def _build_mail_sender_for(config) -> Callable[[str], None]:
    """Der Mail-Versandweg aus einer geladenen Konfiguration.

    Empfaenger ist der erste Mail-Eintrag der Allowlist — derselbe Gedanke wie beim
    Chat: wer befehlen darf, bekommt auch den Anker. Der Versand selbst laeuft ueber
    den bestehende SMTP-Pfad des Mail-Kanals (`MailChannel.send`); Zugangsdaten
    bleiben dort drin und tauchen hier nirgends auf.
    """
    if not config.mail_host or not config.mail_user or not config.mail_password:
        raise ValueError("mail is not configured — cannot mail the anchor")
    owner = next(
        (p for p in sorted(config.allowed_principals, key=str) if p.channel == "mail"),
        None,
    )
    if owner is None:
        raise ValueError(
            "no mail principal in TALOS_ALLOWED_PRINCIPALS — no owner address to mail to"
        )

    from .mail import MailChannel

    channel = MailChannel(
        config.mail_host,
        config.mail_user,
        config.mail_password,
        smtp_host=config.mail_smtp_host,
        authserv_id=config.mail_authserv_id,
    )

    def send(text: str) -> None:
        channel.send(f"mail:{owner.user_id}", text)

    return send


def _build_mail_sender() -> Callable[[str], None]:
    """Der echte Mail-Versandweg — erst aufgebaut, wenn `--mail` ihn verlangt."""
    from .config import load_config

    return _build_mail_sender_for(load_config())


def run_anchor(
    argv: list[str] | None = None,
    *,
    stdout=None,
    db_path: Path | None = None,
    anchors_path: Path | None = None,
    now: float | None = None,
    sender: Callable[[str], None] | None = None,
    mail_sender: Callable[[str], None] | None = None,
) -> int:
    """`talos anchor [--send] [--mail]`. Verifiziert, verankert, vergleicht."""
    argumente = list(argv or [])
    schreiben = (stdout or sys.stdout).write
    if "--help" in argumente or "-h" in argumente:
        schreiben(
            "  usage: talos anchor [--send] [--mail]\n"
            "  pins the chain head to data/anchors.jsonl and compares it with the\n"
            "  previous anchor; --send also delivers the digest to the owner chat,\n"
            "  --mail also mails it to the owner address.\n"
        )
        return 0
    fremd = [a for a in argumente if a not in ("--send", "--mail")]
    if fremd:
        schreiben(f"  unknown option: {fremd[0]} — usage: talos anchor [--send] [--mail]\n")
        return 2

    moment = time.time() if now is None else float(now)
    db = Path(db_path) if db_path is not None else Path(EVENTLOG_DB)
    ziel = Path(anchors_path) if anchors_path is not None else ANCHORS_FILE
    if not db.is_file():
        schreiben(f"\n  no event log yet at {db} — nothing to anchor.\n\n")
        return 0

    from .eventlog import EventLog

    log = EventLog(db)
    broken = log.verify()
    count = log.count()
    protected = log.protected_count()
    head_hash, _head_ts = _head(db)

    findings: list[str] = []
    warnings: list[str] = []
    if broken is not None:
        findings.append(f"chain broken — first altered entry id {broken}")
    prev = _previous(ziel)
    if prev is not None:
        try:
            prev_count = int(prev.get("count", 0))
            prev_ts = float(prev.get("ts") or 0.0)
        except (TypeError, ValueError):
            prev_count, prev_ts = 0, 0.0
        prev_head = str(prev.get("head_hash") or "")
        if count < prev_count:
            # Genau der Fall, fuer den der Anker existiert: das Log ist kuerzer als beim
            # letzten Festhalten, und verify kann das von sich aus nicht sehen.
            findings.append(
                f"tail truncation suspected — {prev_count} entries were anchored, "
                f"{count} remain"
            )
        if head_hash and prev_head == head_hash and moment - prev_ts > STALE_AFTER_S:
            stunden = (moment - prev_ts) / 3600
            warnings.append(
                f"head unchanged for {stunden:.0f}h — the service may not be running"
            )

    record = {
        "ts": moment,
        "count": count,
        "head_hash": head_hash,
        "verify_ok": broken is None,
    }
    _record(ziel, record)

    # Der Befund steht IMMER auf stdout — auch mit --send. Ein Zustellweg, der die
    # einzige Anzeige waere, macht den Cron-Lauf blind, sobald das Netz fehlt.
    schreiben(
        f"anchor ts={int(moment)} count={count} protected={protected} "
        f"head={head_hash[:HEAD_CHARS] or '-'} "
        f"verify={'ok' if broken is None else f'broken:{broken}'}\n"
    )
    for warnung in warnings:
        schreiben(f"warn {warnung}\n")
    for befund in findings:
        schreiben(f"critical {befund}\n")
    status = "critical" if findings else ("warn" if warnings else "ok")
    schreiben(f"status {status}\n")

    versand_fehler = False
    if "--send" in argumente:
        try:
            verschicken = sender if sender is not None else _build_sender()
            verschicken(_digest(record, tuple(findings), tuple(warnings)))
        except Exception as fehler:
            # Ein Versandfehler ist kritisch fuer diesen Aufruf: wer --send in einen
            # Cron schreibt, will den Anker AUS der Maschine heraus haben.
            schreiben(f"{SYM_FAIL} send failed: {fehler}\n")
            versand_fehler = True
        else:
            schreiben("sent owner chat\n")
    if "--mail" in argumente:
        try:
            verschicken = mail_sender if mail_sender is not None else _build_mail_sender()
            verschicken(_digest(record, tuple(findings), tuple(warnings)))
        except Exception as fehler:
            schreiben(f"{SYM_FAIL} mail failed: {fehler}\n")
            versand_fehler = True
        else:
            schreiben("sent owner mail\n")
    return 1 if findings or versand_fehler else 0


__all__ = [
    "ANCHORS_FILE",
    "HEAD_CHARS",
    "STALE_AFTER_S",
    "run_anchor",
]
