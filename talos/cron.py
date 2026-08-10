"""Cron-Ausdruecke — der Kalender, den ein blosses Intervall nicht kennt.

Ein Intervall kann „alle 90 Minuten". Es kann nicht „werktags um 08:00", und genau das
ist der Unterschied zwischen einem Timer und einem Zeitplan: der eine zaehlt, der andere
kennt den Tag. Ein Vergleich mit vergleichbaren Zeitplanern nannte das als einzige rein funktionale
Luecke, und sie kostet nichts an Sicherheit — ein
Ausdruck ist eine bessere Uhr, keine zusaetzliche Erlaubnis. Was danach laeuft, geht
unveraendert durch den Kernel und unter die unbeaufsichtigte Decke.

Bewusst OHNE Fremdbibliothek: `requirements.txt` traegt genau eine Zeile (`requests`),
und das ist eine Zusicherung an den, der das hier installiert. Ein Kalender-Parser ist
zweihundert Zeilen, die man lesen kann — eine Abhaengigkeit ist es nicht.

Unterstuetzt wird der gewohnte Fuenffelder-Ausdruck `M H DOM MON DOW` mit `*`, Zahlen,
Listen (`1,15`), Bereichen (`8-17`) und Schritten (`*/15`, `8-17/2`). Sonntag ist `0`
UND `7`, wie ueberall. Bewusst NICHT dabei: `@reboot` (haengt an einem Ereignis, nicht
an der Zeit), `L`/`W`/`#` (Sonderzeichen, die kaum jemand richtig liest) und Sekunden —
der kleinste sinnvolle Abstand bleibt eine Minute, wie beim Intervall auch.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# Ein Ausdruck, der im naechsten Jahr nie zutrifft (etwa 31. Februar), ist ein Tippfehler
# und kein Zeitplan. Die Suche bricht dann ab, statt endlos in die Zukunft zu laufen.
MAX_SEARCH_DAYS = 366
_MINUTE = 60

# (Feld, Untergrenze, Obergrenze) in der Reihenfolge des Ausdrucks.
_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),  # 0 und 7 sind beide Sonntag
)

_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class CronError(ValueError):
    """Ein Ausdruck, den niemand ausfuehren kann. Wird dem Betreiber gezeigt, nicht verschluckt."""


def _number(token: str, low: int, high: int, feld: str) -> int:
    wert = _NAMES.get(token.lower(), None)
    if wert is None:
        if not token.isdigit():
            raise CronError(f"{feld}: '{token}' is not a number")
        wert = int(token)
    if not low <= wert <= high:
        raise CronError(f"{feld}: {wert} is outside {low}-{high}")
    return wert


def _field(spec: str, low: int, high: int, feld: str) -> frozenset[int]:
    """Ein Feld zu der Menge der Werte, bei denen es zutrifft."""
    treffer: set[int] = set()
    for teil in spec.split(","):
        teil = teil.strip()
        if not teil:
            raise CronError(f"{feld}: empty entry")
        schritt = 1
        if "/" in teil:
            teil, _, roh = teil.partition("/")
            if not roh.isdigit() or int(roh) < 1:
                raise CronError(f"{feld}: step '{roh}' must be a positive number")
            schritt = int(roh)
        if teil in ("*", ""):
            von, bis = low, high
        elif "-" in teil.lstrip("-"):
            links, _, rechts = teil.partition("-")
            von, bis = _number(links, low, high, feld), _number(rechts, low, high, feld)
            if von > bis:
                raise CronError(f"{feld}: range {teil} runs backwards")
        else:
            von = bis = _number(teil, low, high, feld)
        treffer.update(range(von, bis + 1, schritt))
    return frozenset(treffer)


@dataclass(frozen=True)
class Cron:
    """Ein geparster Ausdruck. Unveraenderlich; die Auswertung haelt keinen Zustand."""

    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    text: str

    def matches(self, moment: time.struct_time) -> bool:
        """Trifft der Ausdruck auf diese Minute zu?

        Tag-des-Monats und Wochentag sind ODER-verknuepft, sobald beide gesetzt sind —
        das ist die Vixie-cron-Regel und ueberrascht jeden, der sie nicht kennt.
        Sie steht hier, weil `0 9 1 * MON` sonst „nur der 1., falls Montag" hiesse,
        waehrend cron „am 1. UND montags" meint.
        """
        if moment.tm_min not in self.minute or moment.tm_hour not in self.hour:
            return False
        if moment.tm_mon not in self.month:
            return False
        wochentag = (moment.tm_wday + 1) % 7  # Python: Mo=0 -> cron: So=0
        tag_gesetzt = len(self.day) < 31
        wtag_gesetzt = len(self.weekday) < 8
        tag_trifft = moment.tm_mday in self.day
        wtag_trifft = wochentag in self.weekday or (wochentag == 0 and 7 in self.weekday)
        if tag_gesetzt and wtag_gesetzt:
            return tag_trifft or wtag_trifft
        return tag_trifft and wtag_trifft

    def next_after(self, moment: float) -> float:
        """Der naechste passende Zeitpunkt NACH `moment`, auf die Minute gerundet.

        Minutenweise vorwaerts statt rechnerisch: langsamer, aber offensichtlich richtig,
        und ein Zeitplan wird selten neu berechnet. Ein Jahr ohne Treffer heisst
        Tippfehler — dann ein Fehler statt einer Endlosschleife.
        """
        kandidat = (int(moment) // _MINUTE + 1) * _MINUTE
        grenze = kandidat + MAX_SEARCH_DAYS * 24 * 3600
        while kandidat <= grenze:
            if self.matches(time.localtime(kandidat)):
                return float(kandidat)
            kandidat += _MINUTE
        raise CronError(f"'{self.text}' never comes round within a year")


def parse(expression: str) -> Cron:
    """`M H DOM MON DOW` — oder `CronError` mit einem Satz, der sagt, was fehlt."""
    felder = str(expression).split()
    if len(felder) != len(_FIELDS):
        raise CronError(
            f"a cron expression has {len(_FIELDS)} fields (minute hour day month weekday), "
            f"got {len(felder)}"
        )
    mengen = [
        _field(roh, low, high, name)
        for roh, (name, low, high) in zip(felder, _FIELDS)
    ]
    for menge, (name, _l, _h) in zip(mengen, _FIELDS):
        if not menge:
            raise CronError(f"{name}: matches nothing")
    return Cron(*mengen, text=" ".join(felder))


def looks_like_cron(text: str) -> bool:
    """Fuenf Felder, von denen mindestens eines cron-typisch aussieht.

    Trennt `/every 15 …` (Minuten) von `/every 0 9 * * MON-FRI …` (Ausdruck), ohne dass
    der Betreiber ein zweites Kommando lernen muss.
    """
    felder = str(text).split()
    if len(felder) < len(_FIELDS):
        return False
    kopf = felder[: len(_FIELDS)]
    return any(any(zeichen in f for zeichen in "*/,-") for f in kopf) or all(
        f.isdigit() or f.lower() in _NAMES for f in kopf
    )


__all__ = ["Cron", "CronError", "MAX_SEARCH_DAYS", "looks_like_cron", "parse"]
