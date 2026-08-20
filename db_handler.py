import json
import os
import queue
import secrets
import sqlite3
import threading
import time
import uuid

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

# --- Trail vocabulary ------------------------------------------------------
#
# One row per state transition, all rows for one physical button press sharing
# a single event_id. Stages are the pipeline components a press passes through;
# states are what happened to it at that stage. Kept as module constants so a
# typo is an AttributeError at import time rather than a string that silently
# never matches a dashboard query.

STAGE_RTL_LISTENER = "RTL_LISTENER"
STAGE_EVENT_PROCESSOR = "EVENT_PROCESSOR"
STAGE_API_HANDLER = "API_HANDLER"

# RTL_LISTENER states
STATE_RECEIVED = "RECEIVED"              # decoded a fob frame, queued it downstream
STATE_DROPPED_REPEAT = "DROPPED_REPEAT"  # frame discarded by the repeat>0 filter
STATE_QUEUE_FULL = "QUEUE_FULL"          # decoded, but the queue rejected it

# EVENT_PROCESSOR states
STATE_UNMAPPED = "UNMAPPED"
STATE_DEBOUNCED = "DEBOUNCED"
STATE_PROCESSED = "PROCESSED"

# API_HANDLER states
STATE_DISPATCHED = "DISPATCHED"  # handed to the thread pool; outcome not known yet
STATE_SUCCESS = "SUCCESS"
STATE_FAILED = "FAILED"

_conn = None
_conn_pid = None  # PID that opened _conn, so a forked child can tell it inherited one
_write_queue = None
_writer_thread = None


def new_event_id():
    """Generate a UUIDv7 string: a time-ordered id for one physical button press.

    Chosen over UUIDv4 because the first 48 bits are a big-endian Unix
    millisecond timestamp, so ids sort chronologically as plain strings, index
    with good locality (appends land at the right edge of the B-tree instead of
    scattering), and carry their own creation time - a trail row can be dated
    even if its timestamp column were ever lost.

    Hand-rolled rather than pulled from a dependency: uuid.uuid7() is stdlib
    only from Python 3.14 and this runs on 3.13, and the alternative (the uuid6
    package) is ~30 lines of code to add a whole requirement for. Layout is
    RFC 9562 section 5.7, using the all-random method for rand_a/rand_b:

        48 bits  unix_ts_ms
         4 bits  version (0b0111)
        12 bits  rand_a
         2 bits  variant (0b10)
        62 bits  rand_b

    Note the all-random fill means ordering *within* a single millisecond is
    not guaranteed - fine here, where the entire system handles a few presses
    per second at absolute worst and cross-millisecond ordering is what the
    trail actually depends on.
    """
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    raw = bytearray(ts_ms.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = 0x70 | (raw[6] & 0x0F)  # version 7 in the high nibble of byte 6
    raw[8] = 0x80 | (raw[8] & 0x3F)  # RFC 4122 variant in the top 2 bits of byte 8
    return str(uuid.UUID(bytes=bytes(raw)))


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

    The guard below is keyed on the owning PID rather than merely "is _conn
    set", because the plain None-check silently defeated everything above.
    home_automation.py initializes the DB in the parent before forking, so a
    child inherited a non-None _conn and its own init_db() call returned
    immediately - never opening a connection, and never starting a writer
    thread, since fork() copies only the calling thread. Both children were
    therefore queueing every write onto a queue with no consumer: no
    connection error, no exception, just rows accumulating in memory until the
    bounded queue began discarding them. That is why nothing the listener or
    event processor recorded ever reached the file, while the parent's own
    retention writes worked fine.
    """
    global _conn, _write_queue, _writer_thread, _conn_pid

    if _conn is not None and _conn_pid == os.getpid():
        return  # already initialized in *this* process

    if _conn is not None:
        # Inherited across fork. Drop the references without close()-ing: the
        # object was duplicated mid-flight along with whatever locks its
        # internals held, and closing it here could flush the parent's cached
        # pages into a file the parent is still writing to. Letting it be
        # garbage collected unclosed leaks one fd in the child, which is a far
        # cheaper problem than corrupting the database.
        _conn = None
        _write_queue = None
        _writer_thread = None

    if DATA_DIR and not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)

    _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    _conn_pid = os.getpid()
    cursor = _conn.cursor()

    # The append-only trail. Replaces the old `events` table, which held one
    # mutable row per press whose action_result was UPDATEd in place after
    # dispatch - that shape could only ever record the *final* outcome, and it
    # needed update_action_result() to re-find its row by (event_key, timestamp)
    # because the async insert never handed back a row id. Appending one row per
    # transition instead makes the full history queryable, removes the
    # match-on-timestamp hack, and means a new field at any stage is a payload
    # key rather than a schema migration.
    #
    # An `events` table from before this change is left untouched on disk rather
    # than migrated or dropped: nothing reads it any more, and the only
    # deployment had zero rows in it at cutover.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            state TEXT NOT NULL,
            event_key TEXT,
            timestamp REAL NOT NULL,
            date TEXT,
            payload TEXT
        )
    ''')

    # event_id: the trail lookup ("show me everything that happened to this press").
    # timestamp: retention deletes and any time-range dashboard query.
    # state: "show me the failures", the headline dashboard query.
    # event_key stays a real column rather than a payload key because "what did
    # button 1 do" is a first-class question; everything genuinely ad-hoc lives
    # in payload, queryable via json_extract(payload, '$.field') without a migration.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_event_log_event_id ON event_log(event_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_event_log_timestamp ON event_log(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_event_log_state ON event_log(state)")

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            detail TEXT,
            timestamp REAL NOT NULL,
            date TEXT
        )
    ''')

    # The old pragmas were `journal_mode = OFF` + `synchronous = OFF`, chosen
    # for write speed on the assumption that a hobby logger can afford to lose
    # recent rows in a crash. That assumption was safe only by accident: the
    # fork bug meant just one process ever actually wrote. With the PID guard
    # above, all three processes now write concurrently, and those settings
    # become actively wrong:
    #
    #   journal_mode = OFF removes the rollback journal, so an interrupted
    #   write cannot be undone - with multiple writers that risks a corrupt
    #   file rather than merely a lost row.
    #
    #   With no busy timeout, a writer that finds the database locked by
    #   another process fails immediately with "database is locked". The
    #   writer thread would catch and log it, and the row would be gone -
    #   trading a silent loss for a noisier one.
    #
    # WAL is built for exactly this shape (multiple processes, one writer at a
    # time, readers never blocked), and with synchronous = NORMAL it stays
    # fast: a power cut may drop the last transactions but cannot corrupt the
    # file. At a few button presses a day the write cost is irrelevant.
    #
    # Note journal_mode is persisted in the database file itself, unlike the
    # other pragmas - setting it once here sticks across restarts.
    cursor.execute("PRAGMA journal_mode = WAL;")
    cursor.execute("PRAGMA synchronous = NORMAL;")
    cursor.execute("PRAGMA busy_timeout = 5000;")  # wait out another process's write, don't fail
    cursor.execute("PRAGMA temp_store = MEMORY;")
    cursor.execute("PRAGMA cache_size = -5000;")
    cursor.execute("PRAGMA optimize;")
    _conn.commit()

    _write_queue = queue.Queue(maxsize=500)
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True)
    _writer_thread.start()


def _writer_loop():
    """The only thread in this process allowed to write through _conn."""
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


def log_event(event_id, stage, state, event_key=None, payload=None, timestamp=None):
    """Append one state-transition row to the trail. The single write primitive.

    payload is any JSON-serializable dict of stage-specific detail - this is the
    flexibility that motivated the whole change. Adding a field at any stage
    needs no schema change, and it stays queryable via SQLite's built-in JSON1
    functions, e.g.:

        SELECT event_id, json_extract(payload, '$.error') FROM event_log
         WHERE state = 'FAILED' ORDER BY timestamp DESC;

    Serialized here on the calling thread rather than inside the queued job so
    that a caller mutating its dict afterwards can't change what gets written,
    and default=str keeps a stray non-serializable value (an exception object,
    say) from killing the write outright.
    """
    if timestamp is None:
        timestamp = time.time()
    date_str = time.strftime("%m%d%Y", time.localtime(timestamp))

    payload_json = None
    if payload is not None:
        try:
            payload_json = json.dumps(payload, default=str)
        except (TypeError, ValueError) as e:
            logger.warning(f"Could not serialize payload for {event_id}/{state}: {e}")
            payload_json = json.dumps({"payload_serialization_error": str(e)})

    def job():
        _conn.execute(
            "INSERT INTO event_log (event_id, stage, state, event_key, timestamp, date, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, stage, state, event_key, timestamp, date_str, payload_json),
        )
        _conn.commit()

    _submit(job)


def get_event_trail(event_id):
    """Return the full ordered trail for one press as a list of dicts.

    Reads directly on the caller's thread rather than going through the writer
    queue - the writer thread owns writes so they stay serialized, but SQLite
    handles a concurrent read on the same connection fine, and a read that had
    to queue behind pending writes would be useless for interactive debugging.

    Rows still in the writer queue won't appear yet; this is the same eventual
    consistency every other reader of this DB already lives with.
    """
    rows = _conn.execute(
        "SELECT event_id, stage, state, event_key, timestamp, date, payload "
        "FROM event_log WHERE event_id = ? ORDER BY id",
        (event_id,),
    ).fetchall()

    return [
        {
            "event_id": r[0],
            "stage": r[1],
            "state": r[2],
            "event_key": r[3],
            "timestamp": r[4],
            "date": r[5],
            "payload": json.loads(r[6]) if r[6] else None,
        }
        for r in rows
    ]


def insert_system_event(event_type, detail, timestamp=None):
    """Queue a hardware/lifecycle event (sdr_restarting/sdr_recovered/
    sdr_giveup_notice), kept in its own table separate from the button-press
    trail so 'what did I press' and 'is my hardware healthy' don't mix. These
    have no event_id because they belong to no single press."""
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
    """Delete event_log and system_events rows older than RETENTION_SECONDS.

    At this project's actual event volume (a single bedroom fob), retained
    rows amount to a few MB even over many days - this was never a real disk
    risk regardless of retention policy. Kept anyway as a correctness/hygiene
    measure, run periodically from home_automation.py's background thread.

    Prunes by row timestamp, so a trail spanning the cutoff loses only its
    older rows. Harmless in practice: every row of one press is written within
    seconds of the others, so a partial trail would need a press straddling the
    exact cutoff instant.
    """
    def job():
        cutoff_time = time.time() - RETENTION_SECONDS
        cursor = _conn.cursor()
        cursor.execute("DELETE FROM event_log WHERE timestamp < ?", (cutoff_time,))
        deleted_events = cursor.rowcount
        cursor.execute("DELETE FROM system_events WHERE timestamp < ?", (cutoff_time,))
        deleted_system_events = cursor.rowcount
        _conn.commit()
        logger.info(
            f"Retention cleanup: deleted {deleted_events} event_log rows, "
            f"{deleted_system_events} system_events older than {RETENTION_SECONDS}s."
        )

    _submit(job)
