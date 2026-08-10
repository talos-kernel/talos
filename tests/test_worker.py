"""Worker: Denkarbeit läuft neben dem Poll-Loop, Warteschlange ist begrenzt und leerbar."""
from __future__ import annotations

import threading
import time

from talos.worker import Worker


def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_submitted_item_is_handled() -> None:
    seen: list[str] = []
    worker = Worker(handle=seen.append)
    worker.start()
    try:
        assert worker.submit("hallo") is True
        assert _wait(lambda: seen == ["hallo"])
    finally:
        worker.stop()


def test_queue_is_bounded_and_reports_refusal() -> None:
    release = threading.Event()
    worker = Worker(handle=lambda item: release.wait(5), max_queue=2)
    worker.start()
    try:
        worker.submit("laeuft")  # belegt den Thread
        assert _wait(worker.busy)
        assert worker.submit("a") is True
        assert worker.submit("b") is True
        assert worker.submit("zu viel") is False  # voll -> ehrliches Nein, kein Blockieren
    finally:
        release.set()
        worker.stop()


def test_drain_drops_waiting_items_only() -> None:
    release = threading.Event()
    handled: list[str] = []

    def handle(item: str) -> None:
        handled.append(item)
        release.wait(5)

    worker = Worker(handle=handle)
    worker.start()
    try:
        worker.submit("laeuft")
        assert _wait(worker.busy)
        worker.submit("wartet-1")
        worker.submit("wartet-2")
        assert worker.drain() == 2
        assert worker.pending() == 0
        release.set()
        assert _wait(lambda: handled == ["laeuft"])  # der laufende Job bleibt unberührt
    finally:
        release.set()
        worker.stop()


def test_handler_exception_does_not_kill_the_thread() -> None:
    errors: list[str] = []
    seen: list[str] = []

    def handle(item: str) -> None:
        if item == "boom":
            raise RuntimeError("kaputt")
        seen.append(item)

    worker = Worker(handle=handle, on_error=lambda error: errors.append(str(error)))
    worker.start()
    try:
        worker.submit("boom")
        worker.submit("danach")
        assert _wait(lambda: seen == ["danach"])
        assert errors == ["kaputt"]
    finally:
        worker.stop()


# --- Der Wartehinweis: gemessen, nicht geschaetzt -----------------------------------
def test_the_worker_reports_when_the_running_job_started() -> None:
    """Ohne diese Zahl koennte der Kanal nur „bin beschaeftigt" sagen — oder raten."""
    import threading

    from talos.worker import Worker

    laeuft = threading.Event()
    weiter = threading.Event()
    uhr = [100.0]

    def handle(_item: object) -> None:
        laeuft.set()
        weiter.wait(2.0)

    worker = Worker(handle=handle, clock=lambda: uhr[0])
    assert worker.busy_since() is None  # nichts laeuft, also gibt es auch keine Zahl
    worker.start()
    uhr[0] = 142.0
    worker.submit("auftrag")
    assert laeuft.wait(2.0)
    assert worker.busy_since() == 142.0
    weiter.set()
    worker.stop()


def test_the_waiting_notice_carries_only_measured_values() -> None:
    """Keine Restzeit-Schaetzung: die kennt niemand, und sie stuende neben echten Zahlen."""
    from talos.__main__ import queued_text

    text = queued_text(running_s=42.7, waiting=1)
    assert "42s" in text
    assert "/stop" in text
    for erfunden in ("etwa", "ca.", "voraussichtlich", "ETA", "%"):
        assert erfunden not in text
    # Erst ab zwei Wartenden ist „davor" eine Information und keine Selbstverstaendlichkeit.
    assert "davor" not in text
    assert "3 davor" in queued_text(running_s=1.0, waiting=3)
