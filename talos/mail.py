"""Mail als zweiter Eingang — und warum ein `From:` gar nichts beweist.

Der zweite Kanal, der etwas HEREIN lässt. Telegram war bisher der einzige, und beide
Vergleichsagenten haben genau das zuerst genannt.

**Die Bedingung stand vor der Lösung fest: kein Eingang, der Talos von aussen erreichbar
macht.** Telegram holt seine Nachrichten ab, IMAP holt sie ab — ein Webhook dagegen
verlangt einen Port, den die Welt erreichen kann, und macht aus „nur ausgehend" ein
„öffentlich erreichbar". Deshalb IMAP-Abruf und kein SMTP-Empfang. Aus demselben Grund
liegt der WhatsApp-Kanal auf `NOTIFY` (siehe `whatsapp.py`).

⚠️ **`Trust.ASK`, nicht `FULL`.** Eine Telegram-Kennung ist kontogebunden; eine Mailadresse
kann jeder in ein `From:` schreiben, der sie kennt. Der Kanal darf deshalb fragen und
Antworten bekommen, aber nichts freigeben — genau dafür gibt es die Stufe. Die
`trust`-Eigenschaft hat bewusst keinen Setter.

## Warum eine Adressliste allein wertlos wäre

Das `From:` ist vom Absender frei gewählt und wird bei der IMAP-Zustellung **nirgends**
geprüft. Eine Erlaubnisliste, die nur darauf schaut, ist mit einer Zeile zu umgehen:
`From: operator@example.com` schreibt sich von selbst. Das einzige belastbare Merkmal ist der
`Authentication-Results`-Kopf, den der **empfangende** Server (der, in den wir uns
einloggen) nach SPF/DKIM/DMARC selbst stempelt.

Zwei Feinheiten, ohne die die Prüfung wertlos ist:

  1. **Der oberste Kopf zählt.** Der empfangende Server stellt seinen Stempel voran; ein
     Kopf, den der Angreifer selbst mitgeschickt hat, sortiert darunter.
  2. **Fail-closed.** Fehlt der Kopf ganz, gilt der Absender als nicht bewiesen. Wer einen
     Server betreibt, der nicht stempelt, muss das ausdrücklich abschalten — und weiss
     dann, was er tut.

Die Prüflogik ist dem Mail-Adapter von **Hermes Agent** nachgebaut (MIT,
© 2025 Nous Research, `plugins/platforms/email/adapter.py`), dort mit Verweis auf
GHSA-rxqh-5572-8m77. Übernommen ist die Beweisführung, nicht der Code: Hermes' Adapter
hängt an dessen Gateway-Typen, und seine Freigabe-Schicht ist genau die, die Talos nicht
haben will. Der Eingang selbst ist bei Hermes policy-frei — nur deshalb ist die Idee
überhaupt übertragbar.

⚠️ **Automatisch erzeugte Post wird verworfen, nicht beantwortet.** Ein Bounce, eine
Abwesenheitsnotiz oder ein Newsletter, den Talos beantwortet, erzeugt im schlimmsten Fall
eine Schleife, die zu zweit läuft und niemandem auffällt. `Auto-Submitted`, `Precedence`
und die üblichen Absendernamen fallen deshalb still durch.
"""
from __future__ import annotations

import email as email_lib
import email.utils
import imaplib
import re
import smtplib
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage

from .channel import Inbound, Principal, StructuredMessage, Trust

CHANNEL_NAME = "mail"

# Genug für einen Auftrag, zu wenig für eine untergeschobene Bibliothek. Derselbe Gedanke
# wie beim Deckel in `web.py`: was darüber liegt, verpufft ohnehin im Kontextfenster.
MAX_BODY_CHARS = 8_000
MAX_SUBJECT_CHARS = 200
# Wie viele ungelesene Nachrichten ein Abruf höchstens mitnimmt. Ohne Deckel macht ein
# volles Postfach beim ersten Start einen Zug mit hundert Aufträgen daraus.
MAX_PER_POLL = 10
IMAP_TIMEOUT_S = 30
SMTP_TIMEOUT_S = 30

NOT_AUTHENTICATED = "sender not authenticated"

# Absendernamen, hinter denen kein Mensch sitzt.
_AUTOMATED_SENDERS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications@",
    "automated@", "auto-confirm", "auto-reply", "automailer",
)
# Kopfzeilen, die Massen- oder Automatenpost ausweisen (RFC 3834, RFC 2076).
_AUTOMATED_HEADERS = {
    "Auto-Submitted": lambda v: v.strip().lower() != "no",
    "Precedence": lambda v: v.strip().lower() in {"bulk", "list", "junk"},
    "X-Auto-Response-Suppress": bool,
    "List-Unsubscribe": bool,
}

_AUTH_METHOD = re.compile(r"\b(dmarc|dkim|spf)\s*=\s*([a-z]+)", re.IGNORECASE)
_AUTH_PROP = re.compile(
    r"\b(header\.from|header\.d|smtp\.mailfrom|smtp\.from|envelope-from)\s*=\s*([^\s;]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Letter:
    """Eine gelesene Nachricht, auf das reduziert, was der Agent braucht."""

    sender: str
    subject: str
    body: str
    message_id: str


def _domain_of(address: str) -> str:
    _, _, domain = str(address).strip().lower().rpartition("@")
    return domain.strip("<>[] ")


def _aligned(left: str, right: str) -> str | bool:
    """Domain-Gleichheit einschliesslich Unterdomain — `mail.example.com` zu `example.com`."""
    left, right = str(left).strip().lower(), str(right).strip().lower()
    if not left or not right:
        return False
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def verify_sender(message, from_address: str, *, authserv_id: str = "") -> tuple[bool, str]:
    """Ist die Absenderdomain vom eigenen Server bestätigt worden?

    Gibt `(bewiesen, Grund)` zurück. Der Grund wandert ins Ereignisprotokoll — eine
    Absage ohne nachlesbaren Grund wäre bei einem Kanal, der still verwirft, nicht
    zu debuggen.
    """
    from_domain = _domain_of(from_address)
    if not from_domain:
        return False, "missing From domain"

    koepfe = message.get_all("Authentication-Results") or []
    if not koepfe:
        return False, "no Authentication-Results header"

    # ⚠️ OHNE eigene Kennung ist NICHTS bewiesen — das ist der wichtigste Satz hier.
    #
    # Der erste Entwurf nahm ohne `authserv_id` den obersten Kopf als den eigenen. Das
    # ist genau der Bypass, den dieses Modul verhindern soll: `From:` UND
    # `Authentication-Results: irgendwas; dmarc=pass` schreibt der Absender selbst, und
    # ein Kopf, den niemand einem Server zuordnen kann, beweist nichts — er behauptet
    # nur. Ein externes Audit (Hermes, 05.08.) hat es reproduziert: gefaelschter Kopf,
    # akzeptiert. Der Test, der das absicherte, hat den unsicheren Zustand ZEMENTIERT.
    #
    # Fail-closed heisst hier: ohne konfigurierten `TALOS_MAIL_AUTHSERV_ID` ist der
    # Absender unbewiesen. Der Kanal steht ohnehin auf `Trust.ASK` — unbewiesen bedeutet
    # also nicht „stumm", sondern „darf fragen, gilt aber als niemand".
    if not str(authserv_id).strip():
        return False, (
            "no authserv-id configured — an Authentication-Results header nobody can "
            "attribute proves nothing. Set TALOS_MAIL_AUTHSERV_ID to the name your own "
            "receiving server stamps."
        )

    # `get_all` bewahrt die Reihenfolge. Der empfangende Server stellt seinen Stempel
    # voran, also ist der ERSTE der eigene — ein mitgeschickter sortiert darunter.
    vertrauter = ""
    for roh in koepfe:
        wert = " ".join(str(roh).split())
        if authserv_id:
            serv = wert.split(";", 1)[0].strip().lower()
            if serv != authserv_id.strip().lower() and not _aligned(serv, authserv_id):
                continue
        vertrauter = wert
        break
    if not vertrauter:
        return False, "no Authentication-Results from the configured authserv-id"

    verfahren = {m.lower(): r.lower() for m, r in _AUTH_METHOD.findall(vertrauter)}
    felder = {p.lower(): v.strip().strip('"') for p, v in _AUTH_PROP.findall(vertrauter)}

    # DMARC ist das stärkste Merkmal: es erzwingt die Übereinstimmung mit `From:` selbst.
    if verfahren.get("dmarc") == "pass":
        return True, "dmarc=pass"
    if verfahren.get("spf") == "pass":
        roh = felder.get("smtp.mailfrom") or felder.get("smtp.from") or felder.get("envelope-from", "")
        if _aligned(_domain_of(roh) if "@" in roh else roh, from_domain):
            return True, "spf=pass aligned"
    if verfahren.get("dkim") == "pass" and _aligned(felder.get("header.d", ""), from_domain):
        return True, "dkim=pass aligned"
    return False, f"no aligned pass ({verfahren or 'no method'})"


def is_automated(message, from_address: str) -> bool:
    """Steckt hinter dieser Nachricht kein Mensch? Dann gibt es nichts zu beantworten."""
    unten = str(from_address).lower()
    if any(muster in unten for muster in _AUTOMATED_SENDERS):
        return True
    for kopf, verdaechtig in _AUTOMATED_HEADERS.items():
        wert = message.get(kopf)
        if wert and verdaechtig(str(wert)):
            return True
    return False


def _decoded(raw: object) -> str:
    """Kopfzeile in Klartext — MIME-kodierte Wörter aufgelöst, sonst unverändert."""
    if raw is None:
        return ""
    try:
        return str(make_header(decode_header(str(raw))))
    except (ValueError, UnicodeDecodeError):
        return str(raw)


def plain_body(message) -> str:
    """Nur der Textteil. HTML wird NICHT gerendert.

    Ein HTML-Rumpf ist Fremdinhalt mit eigener Auszeichnung; ihn zu Text zu falten hiesse,
    einen Parser auf Angreifereingabe zu setzen, bevor der Kernel überhaupt etwas gesehen
    hat. Wer nur HTML schickt, bekommt eine leere Nachricht — und die fällt auf.
    """
    if not message.is_multipart():
        if message.get_content_type() != "text/plain":
            return ""
        return _payload_text(message)
    for teil in message.walk():
        if teil.get_content_type() == "text/plain" and "attachment" not in str(
            teil.get("Content-Disposition", "")
        ).lower():
            text = _payload_text(teil)
            if text:
                return text
    return ""


def _payload_text(part) -> str:
    try:
        roh = part.get_payload(decode=True)
    except (AssertionError, ValueError):
        return ""
    if not isinstance(roh, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return roh.decode(charset, "replace")
    except LookupError:
        return roh.decode("utf-8", "replace")


def to_letter(message, *, authserv_id: str = "") -> tuple[Letter | None, str]:
    """Rohnachricht -> `Letter`, oder `(None, Grund)`. Der Grund ist immer nachlesbar."""
    absender = email.utils.parseaddr(_decoded(message.get("From")))[1].strip().lower()
    if not absender:
        return None, "no usable From address"
    if is_automated(message, absender):
        return None, "automated mail"
    bewiesen, grund = verify_sender(message, absender, authserv_id=authserv_id)
    if not bewiesen:
        return None, f"{NOT_AUTHENTICATED}: {grund}"

    betreff = _decoded(message.get("Subject"))[:MAX_SUBJECT_CHARS]
    rumpf = " ".join(plain_body(message).split())[:MAX_BODY_CHARS]
    if not betreff and not rumpf:
        return None, "empty message"
    kennung = str(message.get("Message-ID") or "").strip() or f"no-id:{absender}:{betreff}"
    return Letter(sender=absender, subject=betreff, body=rumpf, message_id=kennung), grund


def address_of(conversation: str) -> str:
    """`mail:a@b.ch` -> `a@b.ch`. Fremder Kanal -> Fehler statt Zustellung ins Blaue."""
    name, _, rest = str(conversation).partition(":")
    if name != CHANNEL_NAME or "@" not in rest:
        raise ValueError(f"nicht dieser Kanal: {conversation!r}")
    return rest


class MailChannel:
    """`Channel`-Implementierung: holt per IMAP ab, antwortet per SMTP.

    Zugangsdaten bleiben vollständig hier drin — kein Attribut, das ein `repr` zeigt,
    keine Fehlermeldung, die sie trägt.
    """

    name = CHANNEL_NAME

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        *,
        smtp_host: str = "",
        authserv_id: str = "",
        mailbox: str = "INBOX",
        imap_factory=None,
        smtp_factory=None,
        timeout_s: int = IMAP_TIMEOUT_S,
    ) -> None:
        if not str(host).strip():
            raise ValueError("TALOS_MAIL_HOST is empty")
        if not str(user).strip() or "@" not in str(user):
            raise ValueError("TALOS_MAIL_USER must be a mail address")
        if not str(password):
            raise ValueError("TALOS_MAIL_PASSWORD is empty")
        self._host = str(host).strip()
        self._smtp_host = str(smtp_host).strip() or self._host
        self._user = str(user).strip()
        self._password = str(password)
        self._authserv_id = str(authserv_id).strip()
        self._mailbox = str(mailbox).strip() or "INBOX"
        self._timeout_s = int(timeout_s)
        self._imap = imap_factory or (
            lambda: imaplib.IMAP4_SSL(self._host, timeout=self._timeout_s)
        )
        self._smtp = smtp_factory or (
            lambda: smtplib.SMTP_SSL(self._smtp_host, timeout=SMTP_TIMEOUT_S)
        )
        # Betreff und Kennung der letzten Nachricht je Absender — nur für die Einordnung
        # der Antwort in denselben Verlauf. Kanal-eigener Zustand wie Telegrams Offset.
        self._threads: dict[str, tuple[str, str]] = {}

    def __repr__(self) -> str:
        # Explizit, damit ein späterer Umbau auf `dataclass` das Passwort nicht in Logs trägt.
        return f"MailChannel(user={self._user!r}, host={self._host!r})"

    @property
    def trust(self) -> Trust:
        """Immer `ASK` — ohne Setter.

        Eine Mailadresse beweist kein Konto. Wer diese Stufe anhebt, macht aus „jeder, der
        die Adresse kennt, darf fragen" ein „jeder, der die Adresse kennt, darf wirken".
        """
        return Trust.ASK

    def poll(self) -> list[Inbound]:
        """Ungelesene Post abholen. Alles, was nicht durchkommt, wird still verworfen —
        aber als gelesen markiert, damit derselbe Absender nicht jede Runde erneut
        durchfällt."""
        verbindung = self._imap()
        try:
            verbindung.login(self._user, self._password)
            verbindung.select(self._mailbox)
            zustand, daten = verbindung.search(None, "UNSEEN")
            if zustand != "OK" or not daten or not daten[0]:
                return []
            nummern = daten[0].split()[:MAX_PER_POLL]
            eingang: list[Inbound] = []
            for nummer in nummern:
                zustand, teile = verbindung.fetch(nummer, "(RFC822)")
                if zustand != "OK" or not teile:
                    continue
                roh = next(
                    (t[1] for t in teile if isinstance(t, tuple) and isinstance(t[1], bytes)),
                    None,
                )
                if roh is None:
                    continue
                brief, _grund = to_letter(
                    email_lib.message_from_bytes(roh), authserv_id=self._authserv_id
                )
                if brief is None:
                    continue
                self._threads[brief.sender] = (brief.message_id, brief.subject)
                eingang.append(
                    Inbound(
                        principal=Principal(CHANNEL_NAME, brief.sender),
                        conversation=f"{CHANNEL_NAME}:{brief.sender}",
                        text=(
                            f"{brief.subject}\n\n{brief.body}" if brief.subject else brief.body
                        ),
                        dedup_key=f"{CHANNEL_NAME}:msg:{brief.message_id}",
                    )
                )
            return eingang
        finally:
            try:
                verbindung.logout()
            except Exception:  # noqa: BLE001 — ein misslungener Abschied darf den Zug nicht kippen
                pass

    def send(self, conversation: str, text: str) -> None:
        """Antwortet dem Absender. Ein Fehler fliegt sofort — eine leise verlorene
        Antwort ist schlimmer als eine laute Absage."""
        empfaenger = address_of(conversation)
        kennung, betreff = self._threads.get(empfaenger, ("", ""))
        nachricht = EmailMessage()
        nachricht["From"] = self._user
        nachricht["To"] = empfaenger
        nachricht["Subject"] = f"Re: {betreff}" if betreff else "Talos"
        if kennung:
            nachricht["In-Reply-To"] = kennung
            nachricht["References"] = kennung
        # Damit eine Antwort auf diese Antwort nicht als Automatenpost bei uns selbst
        # wieder hereinkommt, aber fremde Automaten uns in Ruhe lassen.
        nachricht["Auto-Submitted"] = "auto-replied"
        nachricht.set_content(str(text))
        verbindung = self._smtp()
        try:
            verbindung.login(self._user, self._password)
            verbindung.send_message(nachricht)
        finally:
            try:
                verbindung.quit()
            except Exception:  # noqa: BLE001
                pass

    def send_structured(self, conversation: str, message: StructuredMessage) -> None:
        """Fällt auf den Textteil zurück. Knöpfe gibt es in Mail nicht, und eine
        nachgebaute Tastatur aus Links wäre eine Freigabe per Klick auf einen Link,
        den jeder weiterleiten kann."""
        self.send(conversation, message.text)


__all__ = [
    "CHANNEL_NAME",
    "MAX_BODY_CHARS",
    "MAX_PER_POLL",
    "NOT_AUTHENTICATED",
    "Letter",
    "MailChannel",
    "address_of",
    "is_automated",
    "plain_body",
    "to_letter",
    "verify_sender",
]
