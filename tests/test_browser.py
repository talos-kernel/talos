"""Der rendernde Browser — und der Kaefig, der ihn enger fuehrt als eine URL-Pruefung.

Kein Browser wird gestartet: geprueft wird der AUFRUF (jede Sicherungsflagge steht als
Zusicherung da) und was mit dem Ergebnis passiert. Ein Test, der Chromium wirklich
anwirft, prueft das Netz und nicht diese Datei.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from talos import browser
from talos.channel import Principal
from talos.policy import TARGET_EXTRACTORS, PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest
from talos.web import UrlRefusedError

OWNER = Principal("telegram", "100000001")


@dataclass
class _Lauf:
    stdout: str = ""
    stderr: str = ""


def _resolver(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


def _run_mit(dom: str, gesehen: list | None = None):
    def run(argv, **_kw):
        if gesehen is not None:
            gesehen.append(argv)
        return _Lauf(stdout=dom)
    return run


# --- Der Kaefig: der eigentliche Punkt ---------------------------------------------
def test_the_browser_can_resolve_nothing_but_the_checked_host() -> None:
    """Die Grenze, die eine blosse URL-Pruefung NICHT zieht: eine Weiterleitung, ein
    nachgeladenes Skript oder ein DNS-Rebind zwischen Pruefung und Abruf landet sonst
    irgendwo. Hier loest ausser dem geprueften Namen gar nichts auf."""
    from talos.web import SafeUrl

    regeln = browser.resolver_rules(SafeUrl("https://example.com/a", "example.com", 443, ("93.184.216.34",)))
    # ⚠️ Reihenfolge ist die ganze Regel: Chromium nimmt die ERSTE passende, nicht die
    # spezifischste. Stand das Sternchen vorne, sperrte es auch den geprueften Host aus
    # — und der Browser lieferte eine Fehlerseite, die wie ein Ergebnis aussah. Genau
    # das hat die frueehere Fassung dieses Tests festgeschrieben statt gefunden.
    assert regeln.index("MAP example.com 93.184.216.34") < regeln.index("MAP * ~NOTFOUND")
    assert regeln.endswith("MAP * ~NOTFOUND")   # und alles andere loest nicht auf


def test_without_a_verified_address_nothing_resolves_at_all() -> None:
    """Fail-closed: keine Adresse, kein Netz — nicht etwa 'dann eben ungefiltert'."""
    from talos.web import SafeUrl

    assert browser.resolver_rules(SafeUrl("https://x/a", "x", 443, ())) == "MAP * ~NOTFOUND"


def test_dual_stack_pins_verified_ipv4_and_ipv6_only_is_bracketed() -> None:
    from talos.web import SafeUrl

    v6 = "2606:4700:4700::1111"
    safe = SafeUrl("https://example.com", "example.com", 443, (v6, "93.184.216.34"))
    assert browser.resolver_rules(safe) == "MAP example.com 93.184.216.34, MAP * ~NOTFOUND"
    ipv6 = SafeUrl(safe.url, safe.host, safe.port, (v6,))
    assert browser.resolver_rules(ipv6) == f"MAP example.com [{v6}], MAP * ~NOTFOUND"


def test_timeout_is_actionable_without_exposing_browser_argv() -> None:
    import subprocess

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
    with pytest.raises(RuntimeError) as error:
        browser.render("https://example.com", binary="/bin/chromium", resolve=_resolver, run=timeout)
    assert 'web_search' in str(error.value) and '25s' in str(error.value)
    assert '--user-data-dir' not in str(error.value) and '/bin/chromium' not in str(error.value)


def test_browser_error_page_is_not_returned_as_source_evidence() -> None:
    with pytest.raises(RuntimeError, match='could not load'):
        browser.render("https://example.com", binary="/bin/chromium", resolve=_resolver,
                       run=_run_mit('<html><div id="main-frame-error">ERR_NAME_NOT_RESOLVED</div></html>'))


def test_every_hardening_flag_is_actually_passed() -> None:
    """Zusicherungen auf die EIGENSCHAFT, nicht auf die Reihenfolge der Argumente."""
    from talos.web import SafeUrl

    argv = browser.chromium_argv(
        "/usr/bin/chromium",
        SafeUrl("https://example.com/a", "example.com", 443, ("93.184.216.34",)),
        "/tmp/profil",
    )
    for flagge in ("--headless=new", "--incognito", "--disable-extensions",
                   "--disable-background-networking", "--dump-dom"):
        assert flagge in argv
    assert "--user-data-dir=/tmp/profil" in argv   # frisches Profil, keine Cookies
    assert argv[-1] == "https://example.com/a"     # die URL zuletzt, nie als Flagge


def test_a_refused_url_never_reaches_the_browser() -> None:
    """Der Browser haengt hinter derselben Netz-Grenze wie `web_fetch` — nicht daneben."""
    gesehen: list = []
    for boese in ("http://127.0.0.1/x", "file:///etc/passwd", "https://192.168.1.1/x"):
        with pytest.raises(UrlRefusedError):
            browser.render(boese, binary="/usr/bin/chromium", run=_run_mit("<html></html>", gesehen))
    assert gesehen == []   # kein einziger Start


# --- Das Ergebnis ------------------------------------------------------------------
def test_the_rendered_dom_comes_back_as_text() -> None:
    seite = browser.render(
        "https://example.com/",
        binary="/usr/bin/chromium",
        resolve=_resolver,
        run=_run_mit("<html><body><h1>Bronzering</h1><script>1</script></body></html>"),
    )
    assert "Bronzering" in seite.text
    assert "<h1>" not in seite.text      # Text, kein Markup
    assert "script" not in seite.text.lower()


def test_a_huge_page_is_bounded() -> None:
    """Fremder Text betritt den Lauf begrenzt — wie jedes Werkzeug-Ergebnis."""
    seite = browser.render(
        "https://example.com/",
        binary="/usr/bin/chromium",
        resolve=_resolver,
        run=_run_mit("<html><body>" + ("wort " * 20_000) + "</body></html>"),
    )
    assert len(seite.text) <= browser.MAX_PAGE_CHARS
    assert seite.text.endswith("truncated]")


def test_an_empty_render_is_an_error_not_an_empty_answer() -> None:
    """Sonst meldet Talos 'die Seite ist leer', wo in Wahrheit der Start scheiterte."""
    with pytest.raises(RuntimeError):
        browser.render(
            "https://example.com/", binary="/usr/bin/chromium", resolve=_resolver,
            run=lambda *_a, **_k: _Lauf(stdout="", stderr="cannot create profile"),
        )


def test_a_missing_browser_says_so_and_names_the_alternative() -> None:
    """Ohne Browser eine klare Absage — mit dem Hinweis, was stattdessen geht.

    Bewusst OHNE `render()`: dort haengt das Ergebnis davon ab, ob auf DIESER Maschine
    ein Chromium liegt. Genau so ein Test lief auf dem Mac gruen (kein Browser) und auf
    dem Pi rot (Browser da) — er prueft dann nicht die Zusicherung, sondern die
    Installation. CLAUDE.md nennt das als Falle: ein Test, der auf einer Maschine still
    nichts prueft, ist schlimmer als keiner.
    """
    assert browser.find_browser(candidates=()) == ""
    assert "web_fetch" in browser.NO_BROWSER


# --- Im Kernel ---------------------------------------------------------------------
def test_the_tool_is_declared_read_and_has_an_extractor() -> None:
    """Ein Werkzeug ohne Extractor ist per Bauart DENY — der Eintrag muss also stehen,
    und READ ist richtig: die Seite wird gelesen, nicht bedient."""
    from talos.manifest import Effect

    spec = default_manifest().get("browse")
    assert spec is not None and spec.effect is Effect.READ
    assert "browse" in TARGET_EXTRACTORS
    assert TARGET_EXTRACTORS["browse"]({"url": "https://example.com"}) == ()


def test_rendering_is_allowed_without_asking() -> None:
    """Lesen ist frei — sonst waere jeder Seitenaufruf eine Freigabe-Runde, und
    Freigaben, die man reflexhaft erteilt, sind die, die spaeter durchgewunken werden."""
    kernel = PolicyKernel(default_manifest(), frozenset({OWNER}))
    entschieden = kernel.decide(ToolRequest("browse", OWNER, {"url": "https://example.com"}))
    assert entschieden.verdict is Verdict.ALLOW


def test_a_delegated_run_may_browse_but_a_typed_one_may_do_more(tmp_path: Path) -> None:
    """Der Browser ist genau das, wofuer ein Untergebener gedacht ist: nachsehen."""
    from talos.autonomy import AutonomyGovernor, GovernedKernel
    from talos.channel import Trust
    from talos.subagent import ReadOnlyCeiling

    ceiling = ReadOnlyCeiling()
    kernel = GovernedKernel(
        PolicyKernel(default_manifest(), frozenset({OWNER})),
        AutonomyGovernor(5), lambda _c: Trust.FULL, delegated=ceiling,
    )
    lesen = ToolRequest("browse", OWNER, {"url": "https://example.com"})
    with ceiling.active():
        assert kernel.decide(lesen).verdict is Verdict.ALLOW
