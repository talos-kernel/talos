"""Markdown der Antworten -> Telegram-HTML (parse_mode=HTML).

Der echte Anlass: legacy-Markdown zeigte `**fett**` roh und starb an jedem
`read_file` (ungerade Unterstriche -> 400 -> „could not deliver the answer").
HTML kennt weder Unterstrich- noch Sternchen-Fallen — was hier nicht konvertiert
wird, bleibt sichtbarer Text, nie ein Zustellfehler.
"""
from __future__ import annotations

from talos.tgmarkup import to_telegram_html


def test_plain_text_passes_unchanged() -> None:
    assert to_telegram_html("Der VPS läuft.") == "Der VPS läuft."


def test_html_specials_are_escaped() -> None:
    assert to_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_bold() -> None:
    assert to_telegram_html("**Erledigt** ✅") == "<b>Erledigt</b> ✅"


def test_inline_code() -> None:
    assert to_telegram_html("alle `rm`-Läufe mit rc=0") == "alle <code>rm</code>-Läufe mit rc=0"


def test_snake_case_stays_literal() -> None:
    # Genau dieser Text starb unter legacy-Markdown — unter HTML ist er nur Text.
    assert to_telegram_html("Stopped at: read_file — error") == "Stopped at: read_file — error"


def test_fenced_block_becomes_pre() -> None:
    out = to_telegram_html("Dann:\n```bash\nrm -rf /x\n```\nfertig.")
    assert out == "Dann:\n<pre>rm -rf /x</pre>\nfertig."


def test_fenced_block_content_is_escaped_not_formatted() -> None:
    out = to_telegram_html("```\na < b **roh**\n```")
    assert out == "<pre>a &lt; b **roh**</pre>"


def test_inline_code_content_is_escaped_not_formatted() -> None:
    assert to_telegram_html("`**kein fett** <`") == "<code>**kein fett** &lt;</code>"


def test_unclosed_fence_stays_visible_text() -> None:
    out = to_telegram_html("Anfang ```ohne Ende")
    assert "Anfang" in out and "ohne Ende" in out and "<pre>" not in out


def test_blockquote() -> None:
    out = to_telegram_html('> „Unser Material war blockiert.“\n\nDanach Text.')
    assert out == "<blockquote>„Unser Material war blockiert.“</blockquote>\n\nDanach Text."


def test_multiline_blockquote_is_one_block() -> None:
    out = to_telegram_html("> Zeile eins\n> Zeile zwei")
    assert out == "<blockquote>Zeile eins\nZeile zwei</blockquote>"


def test_strikethrough() -> None:
    assert to_telegram_html("~~überholt~~ neu") == "<s>überholt</s> neu"


def test_link() -> None:
    out = to_telegram_html("[Talos](https://talos-agent.ch) live")
    assert out == '<a href="https://talos-agent.ch">Talos</a> live'


def test_emojis_pass_through() -> None:
    assert to_telegram_html("🛠 2 tool calls · ⏱ 94s") == "🛠 2 tool calls · ⏱ 94s"


def test_a_realistic_report() -> None:
    text = (
        "**Erledigt und verifiziert** ✅\n\n"
        "• **Caches gelöscht** — `rm` mit rc=0\n"
        "• Disk: 362 GB → 46 GB\n\n"
        "> Bereich ~40–45 GB erreicht."
    )
    out = to_telegram_html(text)
    assert "<b>Erledigt und verifiziert</b> ✅" in out
    assert "<code>rm</code>" in out
    assert "<blockquote>Bereich ~40–45 GB erreicht.</blockquote>" in out
    assert "•" in out and "→" in out
