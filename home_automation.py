"""
Orchestrator: wires up the RTL-SDR listener and event processor as separate
processes sharing one queue, plus a background retention-cleanup thread.

Unlike the old home_automation.py, there's no fork()-based daemonize()/PID-file
logic here - Docker's restart policy supervises this process now, so it just
runs in the foreground and relies on signal handlers for clean shutdown.
"""

import multiprocessing
import signal
import sys
import threading
import time

import db_handler
from event_processor import event_processor
from logger import logger
from rtl433_listener import rtl433_listener

# Folded in from the old clean_db.py - runs the retention job on a background
# thread inside this daemon instead of relying on host cron, so the container
# is fully self-contained.
RETENTION_CHECK_INTERVAL_SECONDS = 3600

_shutdown = threading.Event()


def _retention_loop():
    while not _shutdown.is_set():
        db_handler.delete_old_events()
        _shutdown.wait(RETENTION_CHECK_INTERVAL_SECONDS)


def _cleanup(listener_process, processor_process):
    logger.info("Shutting down home automation system...")
    _shutdown.set()

    listener_process.terminate()
    processor_process.terminate()

    listener_process.join(timeout=10)
    processor_process.join(timeout=10)

    logger.info("Shutdown complete.")


def main():
    # NOTE: deliberately no init_db() before the forks below. init_db() starts a
    # writer thread, and forking a multi-threaded process is a genuine hazard -
    # only the calling thread survives into the child, so any lock the writer
    # thread happened to hold at that instant (SQLite's, or the allocator's) is
    # inherited already-locked and can never be released, deadlocking the child.
    # This parent needs a DB connection only for the retention thread, so it
    # opens one after the children exist and it is single-threaded at fork time.
    #
    # init_db()'s PID guard means getting this order wrong is no longer silently
    # fatal the way it was - but the right order costs nothing.

    # Bounded (maxsize=1000) as a belt-and-suspenders cap - human button mashing
    # can't realistically fill this, but it protects against a pathological
    # producer-side bug (e.g. a listener bug that stops filtering) growing
    # memory unbounded instead of hitting backpressure.
    event_queue = multiprocessing.Queue(maxsize=1000)

    logger.info("Starting home automation processes...")

    listener_process = multiprocessing.Process(target=rtl433_listener, args=(event_queue,), daemon=True)
    processor_process = multiprocessing.Process(target=event_processor, args=(event_queue,), daemon=True)
    listener_process.start()
    processor_process.start()

    logger.info(f"RTL-SDR listener PID: {listener_process.pid}, Event processor PID: {processor_process.pid}")

    db_handler.init_db()  # this process's own connection, used by the retention thread
    threading.Thread(target=_retention_loop, daemon=True).start()

    def _signal_handler(signum, frame):
        logger.warning(f"Received signal {signum}, shutting down...")
        _cleanup(listener_process, processor_process)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        while True:
            time.sleep(1)
            if not listener_process.is_alive() or not processor_process.is_alive():
                # A child dying unexpectedly means a bug, not a handled failure
                # (rtl_433 itself dying/hanging is already handled inside
                # rtl433_listener.py's own supervisor loop without killing the
                # process). Exit non-zero so Docker's restart policy brings the
                # whole container back up fresh.
                logger.critical("A child process died unexpectedly - exiting so Docker can restart the container.")
                _cleanup(listener_process, processor_process)
                sys.exit(1)
    except KeyboardInterrupt:
        _cleanup(listener_process, processor_process)


if __name__ == "__main__":
    main()
