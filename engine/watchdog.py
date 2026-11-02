"""Restarts worker threads that die, and reports on them for /api/health.

poll_loop died four times and pe_poll_loop twice, each time for good, and one
death froze /api/status for 19 days while /api/health said "ok". Catching each
known exception inside the loops cannot cover the next one, and catching all of
them would hide bugs. So a bug still ends its thread with a traceback, and the
watchdog starts a new one after the worker's restart delay.

Pass on_exit=db.release so a dead worker's sqlite3 connection is closed at
once, rolling back any transaction it left open. Without it the connection
waits for the cycle collector, and its transaction blocks every writer.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("engine.watchdog")

CHECK_INTERVAL = 30


@dataclass
class _Worker:
    name: str
    target: Callable
    args: tuple
    kwargs: dict
    restart_delay: float
    on_exit: Callable | None = None
    thread: threading.Thread | None = None
    restarts: int = 0
    last_error: str | None = None
    last_death: str | None = None
    died_at: float | None = field(default=None, repr=False)


class Watchdog:
    """Starts named worker threads and restarts any that end before stop_event is set.

    restart_delay is how long a dead worker stays down. For a poller it should be
    its normal interval: every restart calls the rate-limited API again.
    """

    def __init__(self, stop_event: threading.Event, check_interval: float = CHECK_INTERVAL):
        self._stop_event = stop_event
        self._check_interval = check_interval
        self._workers: dict[str, _Worker] = {}
        self._previous_excepthook = None

    def add(self, name: str, target: Callable, args: tuple = (), kwargs: dict | None = None,
            restart_delay: float = 0, on_exit: Callable | None = None) -> None:
        """on_exit runs in the worker's thread as it ends, however it ends."""
        self._workers[name] = _Worker(name, target, args, kwargs or {}, restart_delay, on_exit)

    def start(self) -> None:
        self._previous_excepthook = threading.excepthook
        threading.excepthook = self._on_thread_death
        for worker in self._workers.values():
            self._launch(worker)
        threading.Thread(target=self._loop, name="watchdog", daemon=True).start()

    def status(self) -> dict:
        return {
            w.name: {
                "alive": w.thread is not None and w.thread.is_alive(),
                "restarts": w.restarts,
                "last_error": w.last_error,
                "last_death": w.last_death,
            }
            for w in self._workers.values()
        }

    def _launch(self, worker: _Worker) -> None:
        worker.thread = threading.Thread(target=self._run, args=(worker,), name=worker.name, daemon=True)
        worker.thread.start()

    @staticmethod
    def _run(worker: _Worker) -> None:
        try:
            worker.target(*worker.args, **worker.kwargs)
        finally:
            if worker.on_exit:
                worker.on_exit()

    def _on_thread_death(self, args) -> None:
        # Also logs threads it does not supervise, such as the cache warmup, whose
        # tracebacks otherwise reach only the launchd log.
        error = f"{args.exc_type.__name__}: {args.exc_value}"
        name = args.thread.name if args.thread else "?"
        log.error("Thread %s died: %s", name, error)
        for worker in self._workers.values():
            if worker.thread is args.thread:
                worker.last_error = error
        self._previous_excepthook(args)

    def _loop(self) -> None:
        while not self._stop_event.wait(self._check_interval):
            for worker in self._workers.values():
                self._check(worker)

    def _check(self, worker: _Worker) -> None:
        if worker.thread.is_alive():
            return
        now = time.monotonic()
        if worker.died_at is None:
            worker.died_at = now
            worker.last_death = datetime.now(timezone.utc).isoformat()
            log.error("Worker %s is down, restarting in %ds", worker.name, worker.restart_delay)
        if now - worker.died_at < worker.restart_delay:
            return
        try:
            self._launch(worker)
        except RuntimeError as exc:  # "can't start new thread": try again next check
            log.error("Could not restart worker %s: %s", worker.name, exc)
            return
        worker.died_at = None
        worker.restarts += 1
        log.warning("Worker %s restarted (restart %d)", worker.name, worker.restarts)
