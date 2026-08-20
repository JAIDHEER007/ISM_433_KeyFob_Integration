# 02 — End-to-end event store

**Date:** 2026-08-20
**Branch:** `feature/e2e_event_store` (from `dev`)
**Status:** In review — deployed and verified on the live container

Replaced the single mutable row per button press with an append-only trail:
one row per state transition, every row for one physical press sharing a
UUIDv7 `event_id`, with ad-hoc detail in a JSON `payload` column.

Along the way this uncovered why the database had been empty since July — a
fork bug that silently discarded every write from both child processes.

## Motivation

Two things prompted it:

1. **Ad-hoc fields.** New event sources and new per-stage detail were going to
   need fields the fixed `events` schema didn't have.
2. **The fob needs pressing multiple times.** There was no way to tell whether
   a dead press was lost on the RF side, swallowed by the debounce window, or
   failed at the Govee call — the old schema recorded only the final outcome.

## Decision: SQLite + JSON, not a document store

The original plan was to move to a NoSQL database for schema flexibility. That
was reconsidered and rejected:

| | SQLite + JSON payload | MongoDB |
|---|---|---|
| Ad-hoc fields | `payload` TEXT + `json_extract` — no migration | Native |
| Infrastructure | None — embedded, already present | Second container, ~200–400MB RSS, auth, backups |
| Dashboard queries (failures, counts per day, joins) | SQL's home turf | Weaker fit |
| Failure mode | Fewer moving parts on a box whose job is to keep working | Network dependency |

SQLite has had JSON1 built in for years, so the flexibility that motivated the
move was available without adding a service. The one genuine win Mongo offered
was dropping the writer-thread/queue machinery that exists because SQLite is
single-writer — about 40 lines of already-working code, not worth an external
service on a home-automation host.

TinyDB was also considered and rejected: no server, but it rewrites the whole
JSON file per write and has no concurrency model, with three processes writing.

## Schema

```sql
CREATE TABLE event_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id  TEXT NOT NULL,   -- UUIDv7, ties one press's rows together
    stage     TEXT NOT NULL,   -- RTL_LISTENER | EVENT_PROCESSOR | API_HANDLER
    state     TEXT NOT NULL,
    event_key TEXT,            -- "<fob id>:<button>"
    timestamp REAL NOT NULL,
    date      TEXT,
    payload   TEXT             -- JSON, free-form per stage
);
CREATE INDEX idx_event_log_event_id  ON event_log(event_id);
CREATE INDEX idx_event_log_timestamp ON event_log(timestamp);
CREATE INDEX idx_event_log_state     ON event_log(state);
```

**Append-only.** The old `events` table held one row per press whose
`action_result` was `UPDATE`d in place after dispatch. That could only ever
record the final outcome, and it forced `update_action_result()` to re-find its
row by `(event_key, timestamp)` because the async insert never returned a row
id — "unique enough at this event volume". Appending against `event_id` makes
that lookup exact and keeps the whole history.

**`event_key` stays a real column** because "what did button 1 do" is a
first-class question. Everything genuinely ad-hoc lives in `payload`.

**UUIDv7 over v4.** The first 48 bits are a big-endian Unix millisecond
timestamp, so ids sort chronologically as strings and index with good locality
(appends land at the right edge of the B-tree). Hand-rolled per RFC 9562 §5.7 —
`uuid.uuid7()` is stdlib only from Python 3.14, and the container runs 3.12.
Ordering *within* a single millisecond isn't guaranteed, which is fine at a few
presses per second.

**States by stage:**

- `RTL_LISTENER` — `RECEIVED`, `DROPPED_REPEAT`, `QUEUE_FULL`
- `EVENT_PROCESSOR` — `UNMAPPED`, `DEBOUNCED`, `PROCESSED`
- `API_HANDLER` — `DISPATCHED`, then `SUCCESS` / `FAILED`

Every `execute_action` exit path writes a row, so a trail stopping at
`PROCESSED` means the dispatch is genuinely in flight — not that a branch
forgot to record itself.

## Observability added for the two suspected bugs

Behavior was deliberately left unchanged; only evidence was added.

**`DROPPED_REPEAT`** — the listener discards frames with `repeat > 0` as
protocol retransmissions. HCS200 sets that bit on every frame after the first
in a burst, so if the leading `repeat=0` frame is corrupted, the *entire press*
is discarded and previously left no trace. These frames are now recorded with
the raw decoded JSON before being dropped. A run of `DROPPED_REPEAT` with no
`RECEIVED` before it is that failure, caught.

**`suppressed_by_event_id`** — `PROCESSED_EVENTS` now stores
`(timestamp, event_id)`, so a `DEBOUNCED` row names the press that silenced it,
along with `gap_seconds` and `window_seconds`. Because the debounce window is
armed *before* an asynchronous dispatch, a press whose Govee call later fails
still blocks re-presses for 3 seconds. That row can now be joined to the
suppressing press's `FAILED` outcome.

## The fork bug

Deployed to the container, pressed the fob, and `event_log` stayed empty while
the lights responded normally.

**Symptom.** `data/events.db` had mtime 2026-07-14 despite 13 days of uptime —
so the *old* `events` table hadn't been receiving rows either. Pre-existing,
not a regression.

**Root cause.** `home_automation.py` called `init_db()` in the parent *before*
forking its two children. `multiprocessing` uses `fork` on Linux, so each child
inherited a non-`None` `_conn`, and the guard

```python
if _conn is not None:
    return  # already initialized in this process
```

made the child's own `init_db()` a no-op. But `fork()` copies only the calling
thread — the inherited `_writer_thread` object referred to a thread that did
not exist in the child.

Both children queued every write onto a queue with **no consumer**. No error,
no exception: rows accumulated in memory until the bounded queue began
silently discarding the oldest. Only the parent could write, which is why
schema creation and the retention log line worked while the tables stayed
empty.

`init_db()`'s docstring warned about exactly this fork hazard — and the
early-return guard reintroduced it.

**Proof.** A script reproducing the parent-init-then-fork sequence:

```
[parent]  writer_alive=True
[child A] _conn_is_none=False  writer_alive=False
[child B] _conn_is_none=False  writer_alive=False
start method: fork
rows actually persisted: ['FROM_PARENT']
```

### Fix, in three parts

**1. PID-keyed guard.** `init_db()` records `_conn_pid`; a child whose PID
doesn't match rebuilds its connection, queue and writer thread. The inherited
connection is dropped *without* `close()` — the object was duplicated
mid-flight along with whatever locks its internals held, and closing it could
flush the parent's cached pages into a file the parent is still writing. That
leaks one fd in the child, which is far cheaper than corrupting the database.

**2. WAL.** Forced by the fix rather than optional. `journal_mode = OFF` +
`synchronous = OFF` were safe only by accident, because one process ever wrote.
With three real writers, no rollback journal risks a corrupt file rather than a
lost row, and with no busy timeout a writer that finds the DB locked fails
immediately with `database is locked` — trading a silent loss for a noisier
one. Now `journal_mode = WAL`, `synchronous = NORMAL`, `busy_timeout = 5000`.
`journal_mode` persists in the file itself, unlike the other pragmas.

**3. Init after fork.** The parent now opens its connection *after* starting
the children, so it is single-threaded at fork time. Forking a multi-threaded
process risks the child inheriting a lock held at that instant which can never
be released. The PID guard makes the old order survivable; the right order
costs nothing.

### Verification on the live container

Killing the `rtl_433` subprocess makes the **listener child** write an
`sdr_restarting` row — the exact path that was broken:

```
system_events BEFORE:  0 rows
$ kill 12
system_events AFTER:   1 | sdr_restarting | 2026-08-20 11:29:02 | Signal caught, exiting!
```

The SDR recovered automatically after a 2s backoff. `PRAGMA journal_mode`
returns `wal`, and `events.db-wal` / `events.db-shm` are present.

## Files changed

| File | Change |
|---|---|
| `db_handler.py` | `event_log` table, `new_event_id()`, `log_event()`, `get_event_trail()`, PID guard, WAL pragmas. `insert_event`/`update_action_result` removed |
| `rtl433_listener.py` | Mints `event_id`; logs `RECEIVED`, `DROPPED_REPEAT`, `QUEUE_FULL` |
| `event_processor.py` | Logs `UNMAPPED`/`DEBOUNCED`/`PROCESSED`; debounce state carries `event_id` |
| `api_handler.py` | Logs `DISPATCHED`/`SUCCESS`/`FAILED`; every exit path records; takes `event_id` instead of `timestamp` |
| `home_automation.py` | `init_db()` moved after the forks |
| `clean_db.py` | Prunes `event_log` |
| `readme.md` | Event trail section, schema, example queries |
| `tests/` | Migrated to the new schema; added `test_event_trail.py`, `test_multiprocess_writes.py` |

## Testing notes

**27 passed**, stable across 5 consecutive runs. The 2 remaining failures are
the pre-existing stale `apis.py` tests from entry 01 — untouched, they belong
to entry 03.

`test_manifest_routing.py` was also fixed: it asserted button `3` was unmapped,
but the manifest now routes it to `toggle_govee_study_lights_bright_red`. Uses
button `6` (the 2+4 combo) instead.

Two test-harness traps worth remembering:

- **Don't fork out of pytest.** The first version of the multiprocess test
  forked directly from the pytest process, which by then holds a live
  `event_processor` thread plus the idle daemon writer threads conftest leaves
  behind. That is the same multi-threaded-fork hazard the production fix
  removes, and it made the test flaky about one run in three. It now runs the
  scenario in a clean subprocess.
- **Don't inherit `DATA_DIR`.** `apis.py` calls `load_dotenv()` at import,
  pushing the container's `DATA_DIR=/app/data` into the test process's
  environment. A subprocess inheriting it tried to `mkdir /app` on the host and
  died. The scenario now sets `DATA_DIR` explicitly.
- `rtl433_listener.py` reads `FOB_IDS` at import and crashes if unset
  (`os.getenv(...).split(...)` on `None`). `.env` is gitignored, so on a fresh
  clone importing that module breaks *collection* for the whole suite.
  `test_event_trail.py` guards it with `os.environ.setdefault`.

## Open items

- **Repeat filter (`rtl433_listener.py`).** Suspected cause of needing multiple
  presses. Evidence now collectable; fix not yet applied.
- **Debounce armed before dispatch (`event_processor.py`).** A failed Govee
  call leaves the button dead for 3 seconds. Evidence now collectable; fix not
  yet applied.
- The legacy `events` table is left on disk, unread and no longer pruned by
  retention. Drop by hand to reclaim the space.
