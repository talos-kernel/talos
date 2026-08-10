"""`talos config` — die Konfiguration lesen und schreiben, ohne einen Editor zu oeffnen.

Der Befehl existiert fuer Skripte und Cron. Er ist deshalb der gefaehrlichste in diesem
Programm: er schreibt genau die Datei, aus der der Kernel seine Identitaetsliste holt.
Was ihn traegt, sind vier Entscheidungen — jede davon eine Antwort auf einen Fehler, den
jemand anders schon gemacht hat (beigesteuert aus dem Verlauf von Hermes).

⚠️ **Was geschrieben werden darf, entscheidet das Schema — nicht der Name.**
`schema.SETTING` ja, `SECRET` und `POLICY` nie. Eine Heuristik („alles mit KEY") faellt
beim ersten Schluessel um, der anders heisst.

⚠️ **`get` gibt fuer ein Geheimnis IMMER dasselbe aus** — gesetzt oder nicht. Sonst ist
der Befehl ein Orakel dafuer, welche Zugaenge eine Maschine hat, und das ist die Auskunft,
die ein Angreifer zuerst braucht. Keine Sternchen nach Laenge, kein Praefix, kein `last4`.

⚠️ **Geschrieben wird atomar, mit den Rechten von Anfang an.** Neben der Zieldatei
entsteht eine neue, sie bekommt ihren Modus **beim Anlegen** (`os.open` mit `0o600`,
nicht `chmod` hinterher), und erst dann ersetzt ein `rename` das Original. Der Umweg ueber
„erst schreiben, dann chmod" hat in Hermes echte Token fuer einen Moment world-readable
gemacht — ein Zeitfenster reicht.

⚠️ **Ein Wert darf keine Zeile beenden.** Ein `\\n` im Wert haengt eine zweite Zeile an,
und die kann jeden anderen Schluessel setzen — auch die Rechteliste. Ein schreibbarer
Schluessel waere damit ein Schreibrecht auf alle. Das prueft `schema._one_line`.

Und eine Grenze, die dieser Befehl NICHT ueberschreiten kann: er laeuft am Terminal, als
der Mensch, dem die Datei gehoert. Der Agent erreicht ihn nicht — seine Shell ist
eingesperrt und sieht die Datei nicht, sein `write_file` faellt am Secret-Floor durch.
Beides zusammen ist die Grenze; dieser Befehl ist nur die eine Haelfte.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import schema
from .ux import SYM_FAIL, SYM_OK

USAGE = """\
  talos config list                 every key, its kind and whether it is set
  talos config get <KEY>            the value — secrets always answer [REDACTED]
  talos config set <KEY> <VALUE>    settings only; secrets and policy are refused
  talos config validate             check the file against the schema

  Options: --file <path>   which config file to work on
"""


def parse_env(text: str) -> dict[str, str]:
    """Eine flache KEY=VALUE-Datei. Kommentare und Leerzeilen fliegen raus.

    Bewusst kein `shlex`, kein Ausdrucks-Parser: was hier gelesen wird, wird auch
    wieder geschrieben, und ein Format, das mehr kann als es zeigt, verliert dabei
    genau das Mehr.
    """
    werte: dict[str, str] = {}
    for zeile in text.splitlines():
        blank = zeile.strip()
        if not blank or blank.startswith("#") or "=" not in blank:
            continue
        name, _, wert = blank.partition("=")
        werte[name.strip()] = wert.strip().strip('"').strip("'")
    return werte


def read_file(path: Path) -> dict[str, str]:
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write_key(path: Path, name: str, value: str) -> None:
    """Setzt einen Schluessel — atomar, symlink-sicher, 0600 von Anfang an.

    ⚠️ `os.open(..., O_NOFOLLOW)` auf der TEMPORAeREN Datei: liegt dort ein Symlink,
    schriebe ein gewoehnliches `open()` an dessen Ziel — und das Ziel bestimmt, wer den
    Symlink legen durfte, nicht wer den Befehl gibt.

    Die vorhandene Datei wird zeilenweise uebernommen, damit Kommentare und fremde
    Eintraege ueberleben. Wer eine Konfiguration umschreibt und dabei die Notizen des
    Betreibers verliert, wird beim naechsten Mal von Hand editiert.
    """
    zeilen = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    neu, ersetzt = [], False
    for zeile in zeilen:
        blank = zeile.strip()
        if blank and not blank.startswith("#") and blank.partition("=")[0].strip() == name:
            if not ersetzt:
                neu.append(f"{name}={value}")
                ersetzt = True
            continue                       # doppelte Eintraege desselben Schluessels fallen weg
        neu.append(zeile)
    if not ersetzt:
        neu.append(f"{name}={value}")
    inhalt = "\n".join(neu).rstrip("\n") + "\n"

    temp = path.with_name(path.name + ".neu")
    temp.unlink(missing_ok=True)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as datei:
            datei.write(inhalt)
            datei.flush()
            os.fsync(datei.fileno())
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    os.replace(temp, path)


def _target(argv: list[str]) -> tuple[Path, list[str]]:
    """`--file` heraustrennen. Ohne Angabe die Datei, die auch der Agent liest."""
    rest = list(argv)
    if "--file" in rest:
        i = rest.index("--file")
        if i + 1 >= len(rest):
            raise ValueError("--file needs a path")
        pfad = Path(rest[i + 1]).expanduser()
        del rest[i:i + 2]
        return pfad, rest
    from .config import LOCAL_ENV, SECRETS_ENV

    return (SECRETS_ENV if SECRETS_ENV.is_file() else LOCAL_ENV), rest


def cmd_list(werte: dict[str, str], schreiben) -> int:
    schreiben("\n")
    for eintrag in schema.KEYS:
        gesetzt = bool(werte.get(eintrag.name, "").strip())
        # ⚠️ Auch hier keine Auskunft ueber Geheimnisse: „—" heisst nicht „leer",
        # es heisst „darueber wird nichts gesagt".
        zustand = "—" if eintrag.kind == schema.SECRET else ("set" if gesetzt else "unset")
        schreiben(f"  {eintrag.name:34} {eintrag.kind:8} {zustand}\n")
    schreiben(f"\n  {len(schema.KEYS)} keys — settings are writable, secrets and policy are not\n\n")
    return 0


def cmd_get(name: str, werte: dict[str, str], schreiben) -> int:
    eintrag = schema.get(name)
    if eintrag is None:
        schreiben(f"  {SYM_FAIL} unknown key: {name}\n")
        return 1
    if not eintrag.readable:
        # Gleiche Ausgabe, gleicher Rueckgabewert — ob gesetzt oder nicht.
        schreiben(f"{schema.REDACTED}\n")
        return 0
    schreiben(f"{werte.get(eintrag.name, eintrag.default)}\n")
    return 0


def cmd_set(name: str, value: str, path: Path, schreiben) -> int:
    eintrag = schema.get(name)
    if eintrag is None:
        schreiben(f"  {SYM_FAIL} unknown key: {name}\n")
        return 1
    if eintrag.kind == schema.POLICY:
        schreiben(
            f"  {SYM_FAIL} {eintrag.name} decides who may command this agent or what the "
            f"kernel lets through.\n"
            f"    It is not settable from a command line — not even with a confirmation, "
            f"because a\n    confirmation is exactly what gets clicked away. Edit the file "
            f"yourself, or run `talos setup`.\n"
        )
        return 1
    if eintrag.kind == schema.SECRET:
        schreiben(
            f"  {SYM_FAIL} {eintrag.name} is a secret. A value on a command line lands in "
            f"the shell history,\n    in `ps` for every user on this machine, and in any "
            f"process listing a monitor keeps.\n    Use `talos setup` — it reads it without "
            f"echoing it.\n"
        )
        return 1
    try:
        sauber = eintrag.validate(value) if eintrag.validate else value
    except ValueError as fehler:
        schreiben(f"  {SYM_FAIL} {eintrag.name}: {fehler}\n")
        return 1
    write_key(path, eintrag.name, sauber)
    schreiben(f"  {SYM_OK} {eintrag.name} set in {path}\n")
    return 0


def cmd_validate(werte: dict[str, str], path: Path, schreiben) -> int:
    """Prueft, was pruefbar ist — und sagt beim Rest ehrlich, dass er ungeprueft bleibt."""
    fehler: list[str] = []
    for name, wert in sorted(werte.items()):
        eintrag = schema.get(name)
        if eintrag is None or eintrag.validate is None:
            continue
        try:
            eintrag.validate(wert)
        except ValueError as problem:
            fehler.append(f"{name}: {problem}")
    fremd = schema.unknown(werte)

    schreiben(f"\n  {path}\n")
    for zeile in fehler:
        schreiben(f"    {SYM_FAIL} {zeile}\n")
    if fremd:
        # Kein Fehler: eine Konfiguration darf Zeilen tragen, die Talos nicht liest.
        schreiben(f"    · not read by talos: {', '.join(fremd)}\n")
    if not werte:
        schreiben(f"    {SYM_FAIL} empty or missing\n")
        return 1
    if not fehler:
        schreiben(f"    {SYM_OK} {len(werte)} entries, no schema violations\n")
    schreiben("\n")
    return 1 if fehler else 0


def run_config(argv: list[str] | None = None, *, out=None) -> int:
    schreiben = (out or sys.stdout).write
    try:
        pfad, rest = _target(list(argv or []))
    except ValueError as fehler:
        schreiben(f"  {SYM_FAIL} {fehler}\n")
        return 1
    unterbefehl = rest[0] if rest else ""
    werte = read_file(pfad)

    if unterbefehl == "list":
        return cmd_list(werte, schreiben)
    if unterbefehl == "get" and len(rest) >= 2:
        return cmd_get(rest[1], werte, schreiben)
    if unterbefehl == "set" and len(rest) >= 3:
        return cmd_set(rest[1], " ".join(rest[2:]), pfad, schreiben)
    if unterbefehl == "validate":
        return cmd_validate(werte, pfad, schreiben)
    schreiben("\n" + USAGE + "\n")
    return 0 if not unterbefehl else 1


__all__ = [
    "USAGE",
    "cmd_get",
    "cmd_list",
    "cmd_set",
    "cmd_validate",
    "parse_env",
    "read_file",
    "run_config",
    "write_key",
]
