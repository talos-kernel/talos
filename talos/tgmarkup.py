"""Markdown der Antworten -> Telegram-HTML (`parse_mode=HTML`).

Warum HTML statt legacy-`Markdown`: das Modell schreibt CommonMark (`**fett**`),
legacy-Telegram versteht nur `*fett*` — also standen Sternchen roh im Chat, und
ein `read_file` mit ungerader Unterstrich-Zahl liess Telegram mit 400 ablehnen
(„could not deliver the answer"). HTML hat keine Unterstrich-/Sternchen-Falle:
was hier nicht konvertiert wird, bleibt sichtbarer Text, nie ein Zustellfehler.

Bewusst kleiner Umfang — was Telegrams HTML-Modus hergibt und das Modell
wirklich schreibt: `**fett**`, `inline code`, ```-Blöcke, `>`-Zitate,
`~~durchgestrichen~~`, `[Text](url)`. Kursiv mit Einzel-Sternchen wird nicht
angefasst: `2 * 3` ist kein Satzfehler, den es zu reparieren gilt.
"""
from __future__ import annotations

import html
import re

_FENCED = re.compile(r"```\w*\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")

# Platzhalter aus dem Steuerzeichen-Bereich: kommen in Antworttexten praktisch
# nie vor, und alles dahinter ist bereits fertiges HTML — die spaeteren
# Regeln duerfen es nicht mehr anfassen.
_HOLD = "\x00{}\x00"
_HOLD_RE = re.compile("\x00(\\d+)\x00")


def to_telegram_html(text: str) -> str:
    """Konvertiert das Markdown einer Antwort in Telegram-HTML.

    Rein funktional und total: jeder Eingabetext liefert einen sendbaren
    String; ein Konvertierungsfehler ist per Konstruktion ausgeschlossen
    (extrahieren -> escapen -> taggen -> zuruecksetzen).
    """
    held: list[str] = []

    def hold(fragment: str) -> str:
        held.append(fragment)
        return _HOLD.format(len(held) - 1)

    # 1. Code zuerst herausloesen — sein Inhalt wird escaped, nie formatiert.
    text = _FENCED.sub(lambda m: hold(f"<pre>{html.escape(m.group(1).strip())}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: hold(f"<code>{html.escape(m.group(1))}</code>"), text)

    # 2. Der Rest ist Fliesstext: escapen, dann die Auszeichnung als Tags setzen.
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)

    # 3. Zitatzeilen: nach dem Escapen steht da `&gt; ` am Zeilenanfang.
    #    Zusammenhaengende Zeilen werden EIN Blockquote (Telegram verschachtelt nicht).
    lines = text.split("\n")
    out: list[str] = []
    quote: list[str] = []

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for line in lines:
        if line.startswith("&gt;"):
            quote.append(line[4:].lstrip())
        else:
            flush_quote()
            out.append(line)
    flush_quote()
    text = "\n".join(out)

    # 4. Code zurueck — nach allem, damit keine Regel hineingreift.
    return _HOLD_RE.sub(lambda m: held[int(m.group(1))], text)
