"""Mail als zweiter Eingang — und der Nachweis, dass ein `From:` nichts beweist.

Der Punkt dieser Datei ist nicht, dass Post ankommt. Er ist, dass der Kanal KEINE neue
Erlaubnis-Idee einfuehrt: er holt ab statt zu empfangen, er steht auf `ASK` statt auf
`FULL`, und er baut aus einer unbewiesenen Absenderangabe erst gar keine Identitaet.
"""
from __future__ import annotations

import email as email_lib

import pytest

from talos import mail
from talos.channel import Trust

AUTHSERV = "mx.example.com"
ABSENDER = "operator@example.com"


def brief(
    *,
    von: str = ABSENDER,
    auth: str | None = "mx.example.com; dmarc=pass header.from=example.com",
    betreff: str = "Bitte pruefen",
    rumpf: str = "Wie ist der Stand?",
    extra: dict[str, str] | None = None,
    content_type: str = "text/plain; charset=utf-8",
) -> object:
    kopf = [f"From: {von}", f"Subject: {betreff}", "Message-ID: <a1@example.com>"]
    if auth is not None:
        kopf.append(f"Authentication-Results: {auth}")
    for name, wert in (extra or {}).items():
        kopf.append(f"{name}: {wert}")
    kopf.append(f"Content-Type: {content_type}")
    return email_lib.message_from_string("\n".join(kopf) + "\n\n" + rumpf)


# --- Der Kern: eine Absenderangabe ist eine Behauptung -------------------------------
def test_a_from_header_alone_never_becomes_an_identity() -> None:
    """Der Fall, den die Vorlage als Schwachstelle nennt (GHSA-rxqh-5572-8m77): das
    `From:` waehlt der Absender selbst, und IMAP prueft es nirgends. Eine Erlaubnisliste
    darauf waere mit einer Zeile zu umgehen."""
    gefaelscht, grund = mail.to_letter(brief(auth=None))
    assert gefaelscht is None
    assert mail.NOT_AUTHENTICATED in grund and "no Authentication-Results" in grund


def test_a_failed_check_is_refused_not_downgraded() -> None:
    """Fail-closed: `dmarc=fail` ist keine schwaechere Identitaet, sondern keine."""
    ohne, grund = mail.to_letter(brief(auth="mx; dmarc=fail; spf=fail; dkim=fail"))
    assert ohne is None and mail.NOT_AUTHENTICATED in grund


def test_the_topmost_header_wins_because_our_server_prepends_it() -> None:
    """Die Feinheit, ohne die die ganze Pruefung wertlos ist: der empfangende Server
    stellt seinen Stempel VORAN. Ein Kopf, den der Angreifer selbst mitschickt, sortiert
    darunter — und darf das Urteil nicht drehen."""
    nachricht = email_lib.message_from_string(
        "From: boese@angreifer.example\n"
        "Authentication-Results: mx.example.com; dmarc=fail\n"      # unser Server
        "Authentication-Results: mx.example.com; dmarc=pass\n"      # untergeschoben
        "Subject: x\n\nText\n"
    )
    assert mail.verify_sender(nachricht, "boese@angreifer.example", authserv_id=AUTHSERV)[0] is False


def test_a_foreign_authserv_id_is_skipped_when_ours_is_named() -> None:
    """Mit gesetzter Kennung zaehlt NUR der Stempel des eigenen Servers."""
    nachricht = email_lib.message_from_string(
        "From: operator@example.com\n"
        "Authentication-Results: fremd.example; dmarc=pass\n"
        "Subject: x\n\nText\n"
    )
    assert mail.verify_sender(nachricht, ABSENDER, authserv_id="mx.example.com")[0] is False
    # ⚠️ Diese Zeilen standen einmal umgekehrt hier und haben den Bypass ZEMENTIERT:
    # ohne Kennung galt der oberste Kopf als bewiesen. Ein Kopf, den niemand einem
    # Server zuordnen kann, beweist nichts — auch nicht, wenn er `dmarc=pass` behauptet.
    ok, grund = mail.verify_sender(nachricht, ABSENDER)          # bewusst OHNE Kennung
    assert ok is False and "no authserv-id configured" in grund


def test_spf_and_dkim_count_only_when_aligned_with_the_from_domain() -> None:
    """Ein Bestehen fuer eine FREMDE Domain beweist ueber diesen Absender nichts."""
    passend = brief(auth="mx.example.com; spf=pass smtp.mailfrom=operator@example.com")
    assert mail.verify_sender(passend, ABSENDER, authserv_id=AUTHSERV)[0] is True

    fremd = brief(auth="mx.example.com; spf=pass smtp.mailfrom=jemand@ganz-anders.example")
    assert mail.verify_sender(fremd, ABSENDER, authserv_id=AUTHSERV)[0] is False

    dkim = brief(auth="mx.example.com; dkim=pass header.d=example.com")
    assert mail.verify_sender(dkim, ABSENDER, authserv_id=AUTHSERV)[0] is True
    dkim_fremd = brief(auth="mx.example.com; dkim=pass header.d=ganz-anders.example")
    assert mail.verify_sender(dkim_fremd, ABSENDER, authserv_id=AUTHSERV)[0] is False


def test_a_subdomain_still_counts_as_the_same_domain() -> None:
    nachricht = brief(auth="mx.example.com; spf=pass smtp.mailfrom=operator@mail.example.com")
    assert mail.verify_sender(nachricht, ABSENDER, authserv_id=AUTHSERV)[0] is True


# --- Automatenpost -------------------------------------------------------------------
@pytest.mark.parametrize(
    "von,kopf",
    [
        ("noreply@example.com", {}),
        ("mailer-daemon@example.com", {}),
        (ABSENDER, {"Auto-Submitted": "auto-replied"}),
        (ABSENDER, {"Precedence": "bulk"}),
        (ABSENDER, {"List-Unsubscribe": "<https://x.example/u>"}),
    ],
)
def test_automated_mail_is_dropped_not_answered(von: str, kopf: dict) -> None:
    """Eine beantwortete Abwesenheitsnotiz ist eine Schleife, die zu zweit laeuft."""
    verworfen, grund = mail.to_letter(brief(von=von, extra=kopf))
    assert verworfen is None and grund == "automated mail"


def test_our_own_replies_carry_the_marker_that_stops_the_loop() -> None:
    """Die andere Haelfte derselben Absicht: unsere Antwort weist sich als Automat aus."""
    gesendet: list = []
    kanal = _kanal(smtp=_FakeSMTP(gesendet))
    kanal.send(f"{mail.CHANNEL_NAME}:{ABSENDER}", "Erledigt.")
    assert gesendet[0]["Auto-Submitted"] == "auto-replied"
    assert gesendet[0]["To"] == ABSENDER


# --- Inhalt --------------------------------------------------------------------------
def test_html_only_mail_arrives_empty_instead_of_being_parsed() -> None:
    """Ein HTML-Rumpf ist Fremdinhalt mit eigener Auszeichnung. Ihn zu falten hiesse,
    einen Parser auf Angreifereingabe zu setzen, bevor der Kernel etwas gesehen hat."""
    nur_html = brief(rumpf="<b>klick</b>", content_type="text/html; charset=utf-8")
    assert mail.plain_body(nur_html) == ""


def test_the_body_is_capped() -> None:
    lang = brief(rumpf="A" * (mail.MAX_BODY_CHARS + 5_000))
    gelesen, _ = mail.to_letter(lang, authserv_id=AUTHSERV)
    assert gelesen is not None and len(gelesen.body) == mail.MAX_BODY_CHARS


def test_an_encoded_subject_is_decoded() -> None:
    nachricht = brief(betreff="=?utf-8?B?R3LDvHNzZQ==?=")
    gelesen, _ = mail.to_letter(nachricht, authserv_id=AUTHSERV)
    assert gelesen is not None and gelesen.subject == "Grüsse"


# --- Der Kanal -----------------------------------------------------------------------
class _FakeIMAP:
    def __init__(self, nachrichten: list[bytes]) -> None:
        self._nachrichten = nachrichten
        self.abgemeldet = False

    def login(self, user, password): return ("OK", [b""])
    def select(self, mailbox): return ("OK", [b"1"])
    def search(self, charset, *criteria):
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self._nachrichten)))])

    def fetch(self, num, spec):
        return ("OK", [(b"1 (RFC822 {1})", self._nachrichten[int(num) - 1])])

    def logout(self): self.abgemeldet = True


class _FakeSMTP:
    def __init__(self, senke: list) -> None:
        self._senke = senke

    def login(self, user, password): return None
    def send_message(self, nachricht): self._senke.append(nachricht)
    def quit(self): return None


def _kanal(*, imap=None, smtp=None) -> mail.MailChannel:
    return mail.MailChannel(
        "imap.example.com", "talos@example.com", "geheim",
        authserv_id=AUTHSERV, imap_factory=lambda: imap, smtp_factory=lambda: smtp,
    )


def test_the_channel_asks_it_never_approves() -> None:
    """Eine Mailadresse beweist kein Konto. Die Stufe hat bewusst keinen Setter."""
    kanal = _kanal()
    assert kanal.trust is Trust.ASK
    with pytest.raises(AttributeError):
        kanal.trust = Trust.FULL       # type: ignore[misc]


def test_polling_yields_only_what_was_proven(monkeypatch) -> None:
    """Zwei Nachrichten, eine bewiesen — nur die eine wird zu einem Auftrag."""
    echt = brief().as_bytes()
    gefaelscht = brief(von="boese@angreifer.example", auth=None).as_bytes()
    imap = _FakeIMAP([echt, gefaelscht])
    eingang = _kanal(imap=imap).poll()
    assert len(eingang) == 1
    assert str(eingang[0].principal) == f"mail:{ABSENDER}"
    assert eingang[0].conversation == f"mail:{ABSENDER}"
    assert eingang[0].dedup_key.startswith("mail:msg:")
    assert imap.abgemeldet


def test_a_poll_takes_at_most_a_handful() -> None:
    """Ein volles Postfach darf beim ersten Start keinen Zug mit hundert Auftraegen machen."""
    viele = [brief().as_bytes() for _ in range(mail.MAX_PER_POLL + 7)]
    assert len(_kanal(imap=_FakeIMAP(viele)).poll()) == mail.MAX_PER_POLL


def test_credentials_never_show_up_in_the_repr() -> None:
    assert "geheim" not in repr(_kanal())


def test_delivery_into_a_foreign_channel_is_an_error_not_a_guess() -> None:
    """Die Nachricht enthaelt im Zweifel genau das, was gerade aus einer Datei kam."""
    with pytest.raises(ValueError):
        mail.address_of("telegram:12345")
    with pytest.raises(ValueError):
        mail.address_of("mail:keine-adresse")
