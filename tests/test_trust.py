"""Kanal + Kennung als Identitaet, und die Decke darueber. Die Wache fuer Schritt 4.

Zwei Dinge stehen hier vor Gericht. Erstens die Regel, mit der Schritt 4 anfaengt:
**eine Identitaet ist Kanal + Kennung, nie eine Kennung allein**. Zweitens die
Kanal-Decke — dieselbe Bauart wie der Autonomie-Regler, deshalb auch dieselben
Eigenschaftstests: *nie freizuegiger als der Kernel*, *monoton ueber die Stufen*.

Der Grund fuer die Datei ist ein konkreter Befund: `Trust.ASK` versprach im Docstring
„wirkt nicht", im Code lief eine ASK-Nachricht ungebremst durch. Ein Versprechen ohne
Test ist genau so lange wahr, bis jemand daneben etwas umbaut.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from talos import trust as channel_trust
from talos.autonomy import MAX_LEVEL, MIN_LEVEL, AutonomyGovernor, GovernedKernel
from talos.channel import (
    LEGACY_CHANNEL,
    ChannelRegistry,
    Inbound,
    Principal,
    Trust,
    trust_ceiling,
)
from talos.manifest import Effect, ToolSpec
from talos.policy import Decision, PolicyKernel, ToolRequest, Verdict
from talos.tools import default_manifest

OWNER = Principal("telegram", "100000001")
ALLOWED = frozenset({OWNER})
HOME = Path.home()

SEVERITY = {Verdict.ALLOW: 0, Verdict.NEEDS_HUMAN: 1, Verdict.DENY: 2}

READ_SPEC = ToolSpec("read_file", Effect.READ, reversible=True)
WRITE_SPEC = ToolSpec("write_file", Effect.WRITE, reversible=True)
EXEC_SPEC = ToolSpec("run_shell", Effect.EXEC, reversible=False)


class Fake:
    """Ein Kanal ohne Netz — nur Name, Stufe und ein Postausgang."""

    def __init__(self, name: str, trust: Trust, *, boom: bool = False) -> None:
        self.name = name
        self.trust = trust
        self.sent: list[tuple[str, str]] = []
        self.inbox: list[Inbound] = []
        self._boom = boom

    def poll(self) -> list[Inbound]:
        if self._boom:
            raise RuntimeError(f"{self.name} ist abgeklemmt")
        return list(self.inbox)

    def send(self, conversation: str, text: str) -> None:
        self.sent.append((conversation, text))


def inbound(channel: str, user_id: str = "1") -> Inbound:
    principal = Principal(channel, user_id)
    return Inbound(principal, f"{channel}:{user_id}", "hallo", f"{channel}:update:1")


# --- Identitaet: Kanal + Kennung -------------------------------------------------
def test_gleiche_nummer_auf_zwei_kanaelen_ist_nicht_dieselbe_person():
    """Der eine Satz, weswegen es `Principal` gibt."""
    assert Principal("telegram", "100000001") != Principal("discord", "100000001")
    assert Principal("telegram", "100000001") not in frozenset({Principal("discord", "100000001")})


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("telegram:100000001", Principal("telegram", "100000001")),
        ("matrix:@user:server", Principal("matrix", "@user:server")),
        ("100000001", Principal(LEGACY_CHANNEL, "100000001")),
        (100000001, Principal(LEGACY_CHANNEL, "100000001")),
        ("  telegram:7  ", Principal("telegram", "7")),
    ],
    ids=["voll", "matrix-id-mit-doppelpunkt", "nackte-kennung", "zahl", "randweiss"],
)
def test_parse(raw, expected):
    assert Principal.parse(raw) == expected


def test_parse_nimmt_nur_den_ersten_doppelpunkt_als_trenner():
    """Sonst waere jede Kennung, die selbst einen Doppelpunkt enthaelt, unbrauchbar."""
    assert Principal.parse("matrix:@user:server").user_id == "@user:server"


@pytest.mark.parametrize("raw", ("", "   ", ":100000001", "telegram:"))
def test_parse_lehnt_halbe_identitaeten_ab(raw):
    with pytest.raises(ValueError):
        Principal.parse(raw)


def test_str_ist_wieder_parsebar():
    for principal in (OWNER, Principal("matrix", "@user:server")):
        assert Principal.parse(str(principal)) == principal


def test_inbound_kennt_seinen_kanal():
    assert inbound("discord", "9").channel == "discord"


# --- Registry ---------------------------------------------------------------------
def test_doppelter_kanalname_faellt_beim_bauen_auf():
    with pytest.raises(ValueError):
        ChannelRegistry((Fake("telegram", Trust.FULL), Fake("telegram", Trust.ASK)))


def test_unbekannter_kanal_ist_notify_nicht_full():
    """Fail-closed: ein Kanal, den die Registry nicht kennt, hat nichts zu sagen."""
    assert ChannelRegistry(()).trust_of("telegram") is Trust.NOTIFY


def test_send_geht_an_den_kanal_im_namen_der_conversation():
    a, b = Fake("telegram", Trust.FULL), Fake("discord", Trust.ASK)
    ChannelRegistry((a, b)).send("discord:42", "hallo")
    assert a.sent == [] and b.sent == [("discord:42", "hallo")]


@pytest.mark.parametrize("conversation", ("42", "nirgendwo:42"))
def test_send_raet_nicht(conversation):
    """Ein Zustellweg, der bei Unsicherheit raet, ist ein Leck — die Nachricht kann
    genau das enthalten, was gerade aus einer geschuetzten Datei kam."""
    with pytest.raises((ValueError, KeyError)):
        ChannelRegistry((Fake("telegram", Trust.FULL),)).send(conversation, "geheim")


def test_defekter_kanal_haelt_ohne_meldeweg_nicht_still():
    """Ohne Sink fliegt der Fehler — ein verschluckter Kanal sieht aus wie „keine Post"."""
    registry = ChannelRegistry((Fake("telegram", Trust.FULL, boom=True),))
    with pytest.raises(RuntimeError):
        registry.poll_all()


def test_defekter_kanal_stoppt_mit_meldeweg_die_anderen_nicht():
    kaputt = Fake("telegram", Trust.FULL, boom=True)
    heil = Fake("discord", Trust.FULL)
    heil.inbox = [inbound("discord", "9")]
    seen: list[str] = []
    registry = ChannelRegistry((kaputt, heil), on_error=lambda name, _e: seen.append(name))
    assert [u.channel for u in registry.poll_all()] == ["discord"]
    assert seen == ["telegram"]


@pytest.mark.parametrize("level", tuple(Trust))
def test_jede_stufe_hat_einen_klartext(level):
    assert trust_ceiling(level).strip()


# --- die Decke selbst -------------------------------------------------------------
@pytest.mark.parametrize(
    "level, spec, expected",
    [
        (Trust.FULL, READ_SPEC, Verdict.ALLOW),
        (Trust.FULL, WRITE_SPEC, Verdict.ALLOW),
        (Trust.FULL, EXEC_SPEC, Verdict.ALLOW),
        (Trust.ASK, READ_SPEC, Verdict.ALLOW),
        (Trust.ASK, WRITE_SPEC, Verdict.DENY),
        (Trust.ASK, EXEC_SPEC, Verdict.DENY),
        (Trust.ASK, None, Verdict.DENY),
        (Trust.NOTIFY, READ_SPEC, Verdict.DENY),
        (Trust.NOTIFY, WRITE_SPEC, Verdict.DENY),
    ],
    ids=lambda v: str(v),
)
def test_decke(level, spec, expected):
    assert channel_trust.ceiling(level, spec).verdict is expected


@pytest.mark.parametrize("junk", ("full", None, -1, 99, object()))
def test_unlesbare_stufe_ist_die_strengste(junk):
    """Eine Decke, die bei Unkenntnis durchlaesst, ist keine."""
    assert channel_trust.ceiling(junk, READ_SPEC).verdict is Verdict.DENY


@pytest.mark.parametrize("level", tuple(Trust) + ("muell", None))
@pytest.mark.parametrize("spec", (READ_SPEC, WRITE_SPEC, EXEC_SPEC, None))
@pytest.mark.parametrize(
    "verdict", (Verdict.ALLOW, Verdict.NEEDS_HUMAN, Verdict.DENY), ids=lambda v: v.value
)
def test_decke_ist_nie_freizuegiger_als_das_urteil(level, spec, verdict):
    """Die eine Eigenschaft, die zaehlt: keine Stufe vergibt Rechte."""
    base = Decision(verdict, "kernel")
    assert SEVERITY[channel_trust.apply(level, base, spec).verdict] >= SEVERITY[verdict]


@pytest.mark.parametrize("spec", (READ_SPEC, WRITE_SPEC, EXEC_SPEC, None))
def test_decke_ist_monoton_ueber_die_stufen(spec):
    """Hoehere Stufe darf nie strenger sein als eine niedrigere — sonst ist es keine Leiter."""
    severities = [SEVERITY[channel_trust.ceiling(t, spec).verdict] for t in sorted(Trust)]
    assert severities == sorted(severities, reverse=True), severities


def test_full_ist_exakt_das_kernel_urteil():
    for verdict in Verdict:
        base = Decision(verdict, "kernel")
        assert channel_trust.apply(Trust.FULL, base, WRITE_SPEC) is base


def test_ask_sagt_nein_und_nicht_vielleicht():
    """NEEDS_HUMAN waere eine Selbstblockade: freigeben kann dieser Kanal per Definition
    nicht, die Anfrage laege bis zum TTL-Ablauf herum und saehe aus wie ein Defekt."""
    assert channel_trust.ceiling(Trust.ASK, WRITE_SPEC).verdict is Verdict.DENY


# --- verdrahtet: die Decke sitzt im Kernel-Pfad, nicht im Conductor ---------------
def kernel() -> PolicyKernel:
    return PolicyKernel(default_manifest(), ALLOWED)


def governed(level: int, trust: Trust) -> GovernedKernel:
    return GovernedKernel(kernel(), AutonomyGovernor(level), lambda _c: trust)


WRITE = ToolRequest("write_file", OWNER, {"path": str(HOME / "notes.md"), "content": "x"})
READ = ToolRequest("read_file", OWNER, {"path": str(HOME / "notes.md")})


def test_ask_kanal_schreibt_auch_auf_stufe_5_nicht():
    """Der Befund, wegen dem es diese Datei gibt: Regler ganz offen, Kanal trotzdem zu."""
    assert governed(MAX_LEVEL, Trust.FULL).decide(WRITE).verdict is Verdict.ALLOW
    assert governed(MAX_LEVEL, Trust.ASK).decide(WRITE).verdict is Verdict.DENY


def test_ask_kanal_darf_lesen():
    assert governed(MAX_LEVEL, Trust.ASK).decide(READ).verdict is Verdict.ALLOW


def test_notify_kanal_wirkt_gar_nicht():
    for req in (READ, WRITE):
        assert governed(MAX_LEVEL, Trust.NOTIFY).decide(req).verdict is Verdict.DENY


@pytest.mark.parametrize("level", range(MIN_LEVEL, MAX_LEVEL + 1))
@pytest.mark.parametrize("trust", tuple(Trust), ids=lambda t: t.name)
def test_keine_kombination_aus_regler_und_decke_ist_freizuegiger_als_der_kernel(level, trust):
    """Zwei Decken uebereinander bleiben Decken — auch gemeinsam vergeben sie nichts."""
    base = kernel()
    both = governed(level, trust)
    for req in (READ, WRITE, ToolRequest("run_shell", OWNER, {"command": "date"})):
        assert SEVERITY[both.decide(req).verdict] >= SEVERITY[base.decide(req).verdict], req.tool


def test_trust_of_hat_keinen_vorgabewert():
    """Ein vergessener Parameter darf kein Objekt ergeben, nie eine stille Decke aus Papier."""
    with pytest.raises(TypeError):
        GovernedKernel(kernel(), AutonomyGovernor(MAX_LEVEL))  # type: ignore[call-arg]
