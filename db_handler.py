import os
import queue
import sqlite3
import threading
import time

from logger import logger

DATA_DIR = os.getenv("DATA_DIR", ".")
DB_FILE = os.path.join(DATA_DIR, "events.db")

# Single source of truth for retention (the old code had this split across two
# files with mismatched values: db_handler.py declared an unused
# RETENTION_PERIOD = 86400 that nothing referenced, while clean_db.py separately
# defined RETENTION_PERIOD = 864000 (actually 10 days) mislabeled "1 day" in its
# comment). Default here matches the old *actual* behavior (10 days), correctly
# labeled, and overridable via env var.
RETENTION_SECONDS = int(os.getenv("RETENTION_SECONDS", str(10 * 24 * 60 * 60)))

_conn = None
_write_queue = None
_writer_thread = None


def init_db():
    """Open this process's own SQLite connection and start its dedicated writer thread.

    Must be called explicitly, once, from inside each process that needs DB
    access (home_automation.py's main, and the rtl433_listener/event_processor
    child process entry points) - NOT automatically at module import time.
    home_automation.py forks its child processes via multiprocessing, and
    fork() duplicates whatever's already open in the parent's memory; if a
    connection were opened before forking, children would inherit a copy of
    the *same* connection object, which SQLite does not support safely across
    processes. Calling init_db() only after each process has actually started
    guarantees every process gets its own independent connection.

    Also fixes a real bug from the old code: insert_event() used to spawn a
    brand-new raw threading.Thread per call, unbounded. SQLite serializes
    writes internally anyway, so parallel writer threads bought zero
    throughput and only added thread-creation overhead plus non-deterministic
    completion order. Replaced with one dedicated writer thread per process,
    draining a small bounded queue - bounded thread count, and backpressure
    (drop-oldest with a logged warning) if the writer ever falls behind.
    """
    global _conn, _write_queue, _writer_thread

    if _conn is not None:
        return  # already initialized in this process

    if DATA_DIR and not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

    _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    cursor = _conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            model TEXT,
            button INTEGER,
            repeat INTEGER,
            battery_ok INTEGER,
            timestamp REAL NOT NULL,
            date TEXT,
            status TEXT NOT NULL,
            action_result TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            detail TEXT,
            timestamp REAL NOT NULL,
            date TEXT
        )
    ''')

    # Same lightweight-optimization pragmas as the old code - this is a
    # low-throughput hobby logger, favoring write speed over crash durability.
    cursor.execute("PRAGMA synchronous = OFF;")
    cursor.execute("PRAGMA journal_mode = OFF;")
    cursor.execute("PRAGMA temp_store = MEMORY;")
    cursor.execute("PRAGMA cache_size = -5000;")
    cursor.execute("PRAGMA optimize;")
    _conn.commit()

    _write_queue = queue.Queue(maxsize=500)
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True)
    _writer_thread.start()


def _writer_loop():
    """The only thread in this process allowed to touch _conn."""
    while True:
        job = _write_queue.get()
        try:
            job()
        except Exception as e:
            logger.error(f"Database write error: {e}")


def _submit(job):
    """Queue a zero-arg callable for the writer thread to execute.

    Bounded (maxsize=500): a pathological producer-side bug (e.g. an RF noise
    storm falsely decoding many packets) hits backpressure here instead of
    growing memory unbounded. If the queue is ever full, drop the oldest
    pending write and log a warning rather than blocking the caller forever.
    """
    try:
        _write_queue.put(job, block=True, timeout=1)
    except queue.Full:
        logger.warning("DB writer queue full - dropping oldest pending write to avoid unbounded growth")
        try:
            _write_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _write_queue.put_nowait(job)
        except queue.Full:
            logger.error("DB writer queue still full after dropping oldest - discarding this write")


def insert_event(event_key, model, button, repeat, battery_ok, timestamp, status):
    """Queue a button-press event row. status is one of 'processed'/'debounced'/'unmapped'."""
    date_str = time.strftime("%m%d%Y", time.localtime(timestamp))

    def job():
        _conn.execute(
            "INSERT INTO events (event_key, model, button, repeat, battery_ok, timestamp, date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_key, model, button, repeat, battery_ok, timestamp, date_str, status),
        )
        _conn.commit()

    _submit(job)


def update_action_result(event_key, timestamp, result):
    """Record the outcome of dispatching a button-press event's Govee action.

    Matched on (event_key, timestamp) rather than row id, since insert_event()
    is queued asynchronously and doesn't hand back a row id synchronously -
    the (event_key, timestamp) pair is unique enough at this event volume.
    result is 'success' or 'failed:<short reason>'.
    """
    def job():
        _conn.execute(
            "UPDATE events SET action_result = ? WHERE event_key = ? AND timestamp = ?",
            (result, event_key, timestamp),
        )
        _conn.commit()

    _submit(job)


def insert_system_event(event_type, detail, timestamp=None):
    """Queue a hardware/lifecycle event (sdr_down/sdr_restarting/sdr_recovered/
    sdr_giveup_notice), kept in its own table separate from button-press events
    so 'what did I press' and 'is my hardware healthy' don't mix."""
    if timestamp is None:
        timestamp = time.time()
    date_str = time.strftime("%m%d%Y", time.localtime(timestamp))

    def job():
        _conn.execute(
            "INSERT INTO system_events (event_type, detail, timestamp, date) VALUES (?, ?, ?, ?)",
            (event_type, detail, timestamp, date_str),
        )
        _conn.commit()

    _submit(job)


def delete_old_events():
    """Delete events and system_events older than RETENTION_SECONDS.

    At this project's actual event volume (a single bedroom fob), retained
    rows amount to a few MB even over many days - this was never a real disk
    risk regardless of retention policy. Kept anyway as a correctness/hygiene
    measure, run periodically from home_automation.py's background thread.
    """
    def job():
        cutoff_time = time.time() - RETENTION_SECONDS
        cursor = _conn.cursor()
        cursor.execute("DELETE FROM events WHERE timestamp < ?", (cutoff_time,))
        deleted_events = cursor.rowcount
        cursor.execute("DELETE FROM system_events WHERE timestamp < ?", (cutoff_time,))
        deleted_system_events = cursor.rowcount
        _conn.commit()
        logger.info(
            f"Retention cleanup: deleted {deleted_events} events, "
            f"{deleted_system_events} system_events older than {RETENTION_SECONDS}s."
        )

    _submit(job)
