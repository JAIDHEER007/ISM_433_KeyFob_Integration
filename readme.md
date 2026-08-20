# ISM 433 MHz Key Fob → Govee Automation

Reads button presses from a 4-button Microchip HCS200 KeeLoq key fob via an
RTL-SDR USB dongle and `rtl_433`, debounces them (3s per button), records an
end-to-end trail of every event to SQLite, and dispatches Govee
smart-light/plug actions. Runs as a single Docker container on a Raspberry Pi.

## Setup

1. Copy `.env.example` to `.env` and fill in your Govee API key, device MAC
   addresses/models, and your fob's `id` (from `rtl_433 -F json` - see below).
2. Edit `manifest.json` to map your fob's `"<id>:<button>"` codes to whichever
   `apis.py` function you want each button to trigger. The shipped file has
   placeholder mappings using the id `00E1278` observed during development.
3. Make sure nothing else on the host is using the RTL-SDR dongle (rtl_433
   should not already be running natively).

## Build & run

```
docker compose up --build -d
docker compose logs -f
```

## Redeploying after a config/code change

Docker's layer cache means routine changes don't trigger a full rebuild -
`rtl_433` is only recompiled from source when the `Dockerfile` itself or
`RTL433_VERSION` changes; editing `.env`, `manifest.json`, or `apis.py` never
touches those layers.

**Only edited `.env`** (new device MAC/model, API key, fob id):
```
docker compose up -d
```
No `--build` needed - compose detects the `.env` content change and recreates
the container to pick it up. **`docker compose restart` will NOT pick up an
`.env` change** - it reuses the already-running container's existing
environment, so `restart` after an `.env` edit silently keeps the old values.
Use `up -d` instead.

**Edited `manifest.json` and/or `apis.py`** (new device mapping, new action):
```
docker compose up --build -d
docker compose logs -f
```
This re-runs the app-code `COPY` layer onward, but the build cache skips the
entire `rtl_433` compile stage and the `pip install` layer (since neither the
`Dockerfile` nor `requirements.txt` changed) - it finishes in a few seconds,
not a full from-scratch build.

**Truly clean rebuild** (only if you suspect stale cache, or bumped
`RTL433_VERSION` in the `Dockerfile`):
```
docker compose build --no-cache
docker compose up -d
```

## Finding your fob's id/button codes

```
rtl_433 -F json
```
Press each button once and note the `id` and `button` fields, e.g.:
```json
{"model":"Microchip-HCS200","id":"00E1278","button":1,"repeat":0, ...}
```

## Testing

An automated test suite (`tests/`, pytest) covers debounce logic, manifest
routing, and the full event → dispatch → DB pipeline - entirely without a
real RTL-SDR dongle or real Govee API calls. Fake button-press dicts are fed
directly into `event_processor`'s queue, and `apis.py`'s Govee HTTP calls are
monkeypatched, so it runs anywhere in milliseconds at zero API cost:

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

No Docker container or hardware needed for this - it's testing application
logic, not the USB/SDR integration itself. See each test file's docstring
for what it covers.

## Operating notes

- **Debounce**: pressing the same button twice within 3 seconds only triggers
  the first press. Different buttons are independent.
- **Logs**: `docker compose logs -f` (also mirrored to `./logs/home_automation.log`,
  rotated at 5MB × 5 backups). Hardware-health lines are tagged
  `[SDR-DOWN]` / `[SDR-RETRY]` / `[SDR-RECOVERED]`; Govee failures are tagged
  `[GOVEE-FAIL]`.
- **Database**: `./data/events.db` - see [Event trail](#event-trail) below.
- **If the dongle gets unplugged**: check `docker compose logs -f` for an
  `[SDR-DOWN]` diagnosis, or `docker compose ps` for an `unhealthy` container
  status. Replugging the *same* dongle should be picked up automatically
  (no restart needed) - `docker compose restart` is a fallback, not the
  expected first step.
- **Manual DB cleanup**: `docker compose exec home_automation python clean_db.py`
  (the daemon already runs this hourly on its own).

## Event trail

`./data/events.db` holds two tables.

**`event_log`** is an append-only trail: **one row per state transition**, and
every row belonging to one physical button press shares a single `event_id`
(a UUIDv7, so ids sort chronologically and carry their own creation time).
Nothing is ever updated in place, so the full history of a press survives.

| column | meaning |
|---|---|
| `event_id` | UUIDv7 - ties every row of one press together |
| `stage` | `RTL_LISTENER` / `EVENT_PROCESSOR` / `API_HANDLER` |
| `state` | what happened at that stage (below) |
| `event_key` | `"<fob id>:<button>"` |
| `timestamp`, `date` | epoch float, plus an `MMDDYYYY` string for eyeballing |
| `payload` | JSON - stage-specific detail |

States by stage:

- `RTL_LISTENER` — `RECEIVED` (decoded and queued), `DROPPED_REPEAT` (frame
  discarded by the `repeat > 0` filter), `QUEUE_FULL` (decoded but the pipeline
  rejected it)
- `EVENT_PROCESSOR` — `UNMAPPED`, `DEBOUNCED`, `PROCESSED`
- `API_HANDLER` — `DISPATCHED`, then `SUCCESS` or `FAILED`

A healthy press reads `RECEIVED → PROCESSED → DISPATCHED → SUCCESS`. Every
`execute_action` exit path writes a row, so a trail that stops at `PROCESSED`
means the Govee call is genuinely still in flight, not that a branch forgot to
record itself.

New fields go in `payload` — no schema migration, and still queryable via
SQLite's built-in `json_extract`. That flexibility is why this stayed on SQLite
rather than moving to a document store.

**`system_events`** is unchanged: dongle health/lifecycle
(`sdr_restarting` / `sdr_recovered` / `sdr_giveup_notice`). These belong to no
single press, so they have no `event_id`.

### Querying it

```
docker compose exec home_automation sqlite3 -header -column /app/data/events.db
```

Full trail for one press:
```sql
SELECT stage, state, datetime(timestamp,'unixepoch','localtime') AS t, payload
  FROM event_log WHERE event_id = '<uuid>' ORDER BY id;
```

Recent failures, with the reason pulled out of the JSON payload:
```sql
SELECT datetime(timestamp,'unixepoch','localtime') AS t, event_key,
       json_extract(payload,'$.function') AS fn,
       json_extract(payload,'$.error')    AS error
  FROM event_log WHERE state = 'FAILED' ORDER BY timestamp DESC LIMIT 20;
```

Presses that were swallowed by the debounce window, and which press silenced
each one:
```sql
SELECT datetime(timestamp,'unixepoch','localtime') AS t, event_key,
       json_extract(payload,'$.gap_seconds')            AS gap,
       json_extract(payload,'$.suppressed_by_event_id') AS suppressed_by
  FROM event_log WHERE state = 'DEBOUNCED' ORDER BY timestamp DESC LIMIT 20;
```

Frames dropped by the repeat filter. A run of these with **no `RECEIVED` row
just before them** means an entire press was discarded because its leading
`repeat=0` frame didn't decode — worth checking if a button ever needs pressing
twice:
```sql
SELECT datetime(timestamp,'unixepoch','localtime') AS t, event_key, state
  FROM event_log
 WHERE state IN ('RECEIVED','DROPPED_REPEAT')
 ORDER BY timestamp DESC LIMIT 40;
```

Retention (`RETENTION_SECONDS`, default 10 days) prunes `event_log` and
`system_events` by row timestamp.

> **Note on upgrading:** this replaced an older `events` table that held one
> mutable row per press with an `action_result` column updated in place. An
> existing `events` table is left untouched on disk rather than migrated —
> nothing reads it any more, and retention no longer prunes it. Drop it by hand
> if you want the space back.
