"""Background worker that runs Loyaltri syncs.

Runs either:
* as its own process (LOYALTRI_SCHEDULER=external, recommended in production):
      python -m services.loyaltri.loyaltri_scheduler          # loop forever
      python -m services.loyaltri.loyaltri_scheduler --once   # one sync now, then exit
* or as a daemon thread inside the API (LOYALTRI_SCHEDULER=embedded, dev default).

Either way the work happens off the API's request path. Manual syncs are
queued by the API (a LOYALTRI_SYNC_RUN row with status 'queued') and picked up
here. A database lease guarantees that only one sync runs at a time even with
several API replicas or workers.
"""
import argparse
import logging
import os
import socket
import sys
import threading
import uuid
from datetime import timedelta
from typing import Callable, Optional

from services.loyaltri import config as lconfig
from services.loyaltri.loyaltri_service import LoyaltriSyncService
from services.loyaltri.loyaltri_sync import SyncStore, parse_dt, utcnow

logger = logging.getLogger(__name__)

LEASE_SECONDS = 30 * 60


class SyncWorker:
    def __init__(self, store: Optional[SyncStore] = None,
                 settings_provider: Callable[[], lconfig.LoyaltriSettings] = lconfig.get_settings,
                 service_factory=LoyaltriSyncService, owner: Optional[str] = None):
        self.store = store or SyncStore()
        self.settings_provider = settings_provider
        self.service_factory = service_factory
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    def _scheduled_run_due(self, settings: lconfig.LoyaltriSettings) -> bool:
        if not settings.sync_enabled or not settings.is_configured:
            return False
        last = self.store.last_attempt()
        if not last:
            return True
        finished = parse_dt(last.get("finished_at"))
        return finished is None or utcnow() - finished >= timedelta(minutes=settings.interval_minutes)

    def tick(self) -> Optional[str]:
        """Do at most one sync. Returns the run id that was executed, if any."""
        self.store.heartbeat(self.owner)
        settings = self.settings_provider()
        # Queued or 'running' runs need attention: a 'running' one may belong
        # to a worker that died, which only the lease holder can tell.
        if not self.store.active_run() and not self._scheduled_run_due(settings):
            return None
        if not self.store.acquire_lease(self.owner, LEASE_SECONDS):
            logger.info("Loyaltri sync skipped: another worker holds the lease")
            return None
        try:
            # Holding the lease: re-check, another worker may have just finished.
            stale = self.store.fail_stale_running()
            if stale:
                logger.warning("Closed %d stale Loyaltri sync run(s)", stale)
            queued = self.store.next_queued()
            if queued:
                run_id = queued["run_id"]
            elif self._scheduled_run_due(settings):
                run_id = self.store.create_run("schedule", None)
            else:
                return None
            self.service_factory(settings, self.store).run(run_id)
            return run_id
        finally:
            try:
                self.store.release_lease(self.owner)
            except Exception:
                logger.exception("Could not release Loyaltri sync lease (expires on its own)")

    def run_forever(self, stop: threading.Event, wake: Optional[threading.Event] = None) -> None:
        logger.info("Loyaltri sync worker %s started", self.owner)
        failures = 0
        while not stop.is_set():
            try:
                self.tick()
                failures = 0
            except Exception as e:
                failures += 1
                # Database down or tables missing: keep running, log quietly.
                if failures == 1 or failures % 20 == 0:
                    logger.warning("Loyaltri sync worker tick failed (%d in a row): %s", failures, e)
            poll = self.settings_provider().poll_seconds
            if wake is not None:
                wake.wait(poll)
                wake.clear()
            else:
                stop.wait(poll)
        logger.info("Loyaltri sync worker %s stopped", self.owner)


# ---------------------------------------------------------------- embedded mode

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_wake = threading.Event()


def start_embedded() -> bool:
    global _thread
    settings = lconfig.get_settings()
    if settings.scheduler_mode != "embedded" or (_thread and _thread.is_alive()):
        return False
    _stop.clear()
    worker = SyncWorker()
    _thread = threading.Thread(target=worker.run_forever, args=(_stop, _wake),
                               name="loyaltri-sync", daemon=True)
    _thread.start()
    return True


def stop_embedded() -> None:
    _stop.set()
    _wake.set()


def wake() -> None:
    """Ask an embedded worker to look for queued runs now instead of at the next poll."""
    _wake.set()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Switrus Loyaltri sync worker")
    parser.add_argument("--once", action="store_true", help="queue and run one sync, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    worker = SyncWorker()
    if args.once:
        problems = lconfig.get_settings().problems()
        if problems:
            print("Loyaltri is not configured:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 2
        worker.store.create_run("manual-cli", None)
        run_id = worker.tick()
        run = worker.store.get_run(run_id) if run_id else None
        print(run or "Another worker is running a sync; try again later.")
        return 0 if run and run["status"] in ("succeeded", "partial") else 1

    stop = threading.Event()
    try:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except (ImportError, ValueError):
        pass
    try:
        worker.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
