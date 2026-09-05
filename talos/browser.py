"""Ein Browser, der lesen darf und sonst nichts — und nur dorthin, wo der Kernel war.

Die Luecke, die beide Pruefer nannten: vergleichbare Agenten haben Browser-Automation, Talos hatte
`web_fetch`. Der Unterschied ist real — `web_fetch` holt Quelltext, aber fuehrt kein
JavaScript aus, und die halbe heutige Web-Seite ist ohne JS eine leere Huelle.

Der uebliche Bau nimmt eine Automatisierung (Playwright, Puppeteer) und laesst den Agenten
klicken, tippen, Formulare abschicken. Genau das passiert hier NICHT, und der Grund ist
derselbe wie ueberall: **ein Klick hat kein ableitbares Ziel.** Der Kernel entscheidet
ueber Handlungen anhand dessen, was sie anfassen; „klicke auf das dritte Element" laesst
sich nicht auf eine Ressource abbilden, an die man eine Erlaubnis binden koennte. Ein
Werkzeug ohne ableitbares Ziel ist bei Talos per Bauart DENY — und das ist hier keine
Einschraenkung aus Verlegenheit, sondern die richtige Antwort: ein Agent, der fremde
Seiten bedient, hat einen Wirkungsweg, den kein Kernel mehr einfaengt.

Was bleibt, ist das, wofuer man einen Browser wirklich braucht: **sehen, was dort steht,
nachdem die Seite sich fertig gebaut hat.** Das ist ein Lesevorgang.

Und er ist enger gefuehrt als bei den beiden anderen. Chromium bekommt den Hostnamen fest
auf genau die Adresse genagelt, die `web.guard_url` geprueft hat, und fuer alles andere
gibt es keine Aufloesung:

    --host-resolver-rules="MAP <host> <ip>, MAP * ~NOTFOUND"

Damit laeuft eine Weiterleitung auf einen anderen Namen ins Leere, ein nachgeladenes
Skript von einem Werbenetz ebenso, und ein DNS-Rebind zwischen Pruefung und Abruf hat
keine Wirkung mehr — die klassische Luecke jedes URL-Filters, der nur den ersten Namen
ansieht. Der Preis ist ehrlich: Seiten, die ihre Inhalte von einem CDN unter anderem
Namen holen, bleiben unvollstaendig. Lieber unvollstaendig als unkontrolliert.

Kein Profil, keine Cookies, keine Erweiterungen, kein Schreiben: jeder Aufruf bekommt ein
frisches, temporaeres Verzeichnis, das danach verschwindet.
"""
from __future__ import annotations

import shutil
import ipaddress
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .web import MAX_URL_CHARS, SafeUrl, guard_url, html_to_text

# Wo Chromium ueblicherweise liegt. Der Betreiber kann etwas anderes setzen; fehlt alles,
# sagt das Werkzeug das klar, statt still nichts zu liefern.
CHROMIUM_CANDIDATES: tuple[str, ...] = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
)
# Eine Seite, die nach zwanzig Sekunden nicht fertig ist, wird es auch nicht mehr.
RENDER_TIMEOUT_S = 25
# Wie lange die Seite ihre Skripte laufen lassen darf, bevor der DOM eingefroren wird.
# Als *virtuelle* Zeit: Chromium spult Timer vor, statt echt zu warten.
VIRTUAL_TIME_MS = 5_000
MAX_PAGE_CHARS = 12_000
PAGE_CUT = " […page truncated]"

NO_BROWSER = (
    "No browser is installed on this machine — the rendering tool is unavailable. "
    "web_fetch still works for pages that do not need JavaScript."
)


def find_browser(candidates: tuple[str, ...] = CHROMIUM_CANDIDATES) -> str:
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return ""


def resolver_rules(safe: SafeUrl) -> str:
    """Der Kaefig: dieser eine Name auf diese eine Adresse, sonst nichts.

    ⚠️ **Die Reihenfolge ist die ganze Regel.** Chromium liest die Liste von links und
    nimmt die ERSTE passende — nicht die spezifischste. Stand das Sternchen vorne, traf
    es auch den geprueften Host, und der Browser lieferte statt der Seite eine
    `DNS_PROBE_FINISHED_NXDOMAIN`-Fehlerseite: Text, der aussah wie ein Ergebnis.

    Das haben die Tests NICHT gefunden, weil sie die falsche Annahme festschrieben
    (`startswith("MAP * ~NOTFOUND")`) — gefunden hat es der erste echte Aufruf gegen
    eine echte Seite. Deshalb prueft der Test jetzt die WIRKUNG (spezifisch vor
    Sternchen) und nicht meine Vermutung ueber sie.
    """
    # Chromium pins ONE address and cannot use Happy Eyeballs after this mapping.
    # Prefer an already checked IPv4 address on dual-stack hosts. An IPv6 literal
    # must be bracketed in Chromium's host-mapping grammar.
    ziel = next((ip for ip in safe.addresses if ipaddress.ip_address(ip).version == 4),
                safe.addresses[0] if safe.addresses else "")
    if not ziel:
        return "MAP * ~NOTFOUND"
    if ":" in ziel:
        ziel = f"[{ziel}]"
    return f"MAP {safe.host} {ziel}, MAP * ~NOTFOUND"


def chromium_argv(binary: str, safe: SafeUrl, profile: str) -> list[str]:
    """Der vollstaendige Aufruf. Bewusst als reine Funktion — so ist er testbar,
    ohne einen Browser zu starten, und jede Sicherungsflagge steht in einem Test."""
    return [
        binary,
        "--headless=new",
        "--disable-gpu",
        # Kein Profil des Betreibers: keine Cookies, keine angemeldeten Sitzungen, keine
        # Historie. Eine Seite, die Talos oeffnet, sieht einen fabrikneuen Browser.
        f"--user-data-dir={profile}",
        "--incognito",
        "--disable-extensions",
        "--disable-plugins",
        "--no-first-run",
        "--no-default-browser-check",
        # Keine Telemetrie, keine Absturzberichte, keine Hintergrundnetzwerke.
        "--disable-background-networking",
        "--disable-sync",
        "--disable-breakpad",
        "--metrics-recording-only",
        f"--host-resolver-rules={resolver_rules(safe)}",
        f"--virtual-time-budget={VIRTUAL_TIME_MS}",
        "--timeout=15000",
        "--dump-dom",
        safe.url,
    ]


@dataclass(frozen=True)
class Rendered:
    url: str
    text: str


def render(
    url: object,
    *,
    binary: str = "",
    allow_http: bool = False,
    allowed_addresses: frozenset[str] = frozenset(),
    resolve=None,
    run=subprocess.run,
) -> Rendered:
    """Prueft die URL wie `web_fetch`, rendert sie dann — und gibt Text zurueck."""
    safe = guard_url(
        url, allow_http=allow_http, allowed_addresses=allowed_addresses, resolve=resolve
    )
    exe = binary or find_browser()
    if not exe:
        raise RuntimeError(NO_BROWSER)
    with tempfile.TemporaryDirectory(prefix="talos-browse-") as profile:
        try:
            ergebnis = run(
                chromium_argv(exe, safe, profile),
                capture_output=True,
                text=True,
                timeout=RENDER_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Browser timed out after {RENDER_TIMEOUT_S}s. Try web_search for another "
                "official source, then web_fetch; repeating the same render is unlikely to help."
            ) from None
    dom = ergebnis.stdout or ""
    if not dom.strip():
        raise RuntimeError("Browser returned no readable page. Try web_fetch or search for "
                           "another official source; use talos doctor to check the browser.")
    if getattr(ergebnis, "returncode", 0) != 0:
        raise RuntimeError("Browser exited before rendering completed. Try another official source.")
    if 'chrome-error://chromewebdata/' in dom or 'id="main-frame-error"' in dom:
        raise RuntimeError("Browser could not load this page. Try web_fetch or another official source.")
    text = html_to_text(dom)
    if len(text) > MAX_PAGE_CHARS:
        text = text[: MAX_PAGE_CHARS - len(PAGE_CUT)] + PAGE_CUT
    return Rendered(url=safe.url[:MAX_URL_CHARS], text=text)


def make_browse_runner(
    *,
    binary: str = "",
    allow_http: bool = False,
    allowed_addresses: frozenset[str] = frozenset(),
    resolve=None,
    run=subprocess.run,
):
    """Der Runner. Dumm wie alle anderen — die Grenze liegt in `guard_url` und im Kaefig."""

    def browse(req) -> str:
        seite = render(
            req.args.get("url", ""),
            binary=binary,
            allow_http=allow_http,
            allowed_addresses=allowed_addresses,
            resolve=resolve,
            run=run,
        )
        return f"[Rendered: {seite.url}]\n{seite.text}"

    return browse


def browse_spec():
    """READ: die Seite wird gelesen, nicht bedient. Nichts Lokales veraendert sich."""
    from .manifest import Effect, ToolSpec

    return ToolSpec("browse", Effect.READ, reversible=True)


__all__ = [
    "CHROMIUM_CANDIDATES",
    "MAX_PAGE_CHARS",
    "NO_BROWSER",
    "RENDER_TIMEOUT_S",
    "Rendered",
    "browse_spec",
    "chromium_argv",
    "find_browser",
    "make_browse_runner",
    "render",
    "resolver_rules",
]
