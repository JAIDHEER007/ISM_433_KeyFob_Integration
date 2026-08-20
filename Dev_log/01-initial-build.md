# 01 — Initial build

**Dates:** 2026-07-10 → 2026-07-14
**Branch:** `main` / `dev`
**Status:** Shipped

Built the working pipeline: fob press over 433.92MHz → RTL-SDR → Govee device
action, running as a single supervised Docker container.

> Reconstructed from commit history and from the rationale recorded in code
> comments, rather than written at the time. Where a comment names a specific
> earlier failure, that is repeated here; nothing else is inferred.

## Commits

| Commit | What |
|---|---|
| `9305ec3` | Initial readme |
| `777ee2b` | Dockerfile + docker-compose |
| `a059a99` | Local/API test scripts |
| `0334fd9` | `home_automation.py` — entry point, shared queue |
| `c386e68` | `rtl433_listener.py` — SDR binding, exponential retry |
| `3c6ef76` | `event_processor.py` — debounce, DB log, dispatch handoff |
| `d2ebddc` | `api_handler.py` — manifest → function mapping |
| `68987f5` | `apis.py` — Govee device control |
| `d945a90` | `logger.py` — rotating logs, 25MB cap |
| `0d5b0be`, `cce4590` | `db_handler.py`, `clean_db.py` — SQLite + retention |

## Decisions

**RTL-SDR + `rtl_433` subprocess, replacing an ESP32 + CC1101 UDP transport.**
No stable maintained Python binding for `rtl_433` exists, so the integration
is line-by-line JSON parsing of `rtl_433 -F json` stdout — the approach its own
example scripts and the Home Assistant integrations use.

**Three processes, not one.** The listener and event processor are separate
`multiprocessing.Process`es sharing only a queue, so a second producer (a
revived CC1101/ESP32 UDP listener, say) can be added as another process feeding
the same queue without touching either existing file.

**`rtl_433` compiled from source, pinned to 25.02.** Matches the version
already validated running natively on this host, so container behavior doesn't
drift from what was tested, and avoids trusting a third-party prebuilt image.

**Docker supervises, not the app.** The older design had `fork()`-based
`daemonize()` and PID-file handling; that was dropped for a foreground process
under Docker's `restart: unless-stopped`, with signal handlers for clean
shutdown. A child process dying unexpectedly exits non-zero so Docker restarts
the whole container.

**Whole USB bus bind-mounted, plus a class-wide cgroup rule.**
`/dev/bus/usb:/dev/bus/usb` with `c 189:* rmw` rather than a specific device
node. A physical replug re-enumerates the dongle to a *new* node, and only the
class-wide grant plus live directory mount makes that new node visible without
restarting the container.

**Heartbeat file + `HEALTHCHECK`.** The listener touches
`$DATA_DIR/heartbeat` whenever `rtl_433` produces output; if it goes stale for
180s (~3× the 60s stats interval) the container reports unhealthy, so an
unreachable SDR is visible from `docker compose ps` without tailing logs.

## Problems fixed during this phase

Recorded in code comments as fixes to the preceding iteration:

- **Unbounded thread creation per DB write.** `insert_event()` spawned a fresh
  `threading.Thread` per call. SQLite serializes writes internally, so parallel
  writers bought no throughput — only thread-creation overhead and
  non-deterministic ordering. Replaced with one writer thread per process
  draining a bounded queue. *(This is the machinery that entry 02 later found
  was never actually running in the child processes.)*
- **No HTTP timeouts.** Every `requests.request()` in `apis.py` was unbounded.
  A Govee backend that accepts a connection but never responds — a real
  cloud-outage mode, distinct from a fast connection refusal — would hang a
  dispatch worker forever. Now `GOVEE_TIMEOUT_SECONDS` (default 8).
- **Unrotated log file.** Replaced with `RotatingFileHandler`, 5MB × 5 backups
  = 25MB hard cap, mirrored to container stdout.
- **Split, mismatched retention constants.** `db_handler.py` declared an unused
  `RETENTION_PERIOD = 86400` that nothing referenced, while `clean_db.py`
  separately defined `864000` (10 days) labelled "1 day" in its comment.
  Unified to one env-overridable `RETENTION_SECONDS`, correctly labelled.
- **Host cron dependency.** Retention moved into a background thread inside the
  daemon so the container is self-contained; `clean_db.py` remains for ad-hoc
  manual runs.

## Test suite

pytest, no dongle and no real Govee calls. Fake event dicts are pushed onto the
queue the event processor consumes, and `apis.requests.request` is
monkeypatched. Covers debounce logic, manifest routing, Govee request shapes,
and the full press → dispatch → DB path.

## Known state at the end of this phase

- The event pipeline worked. Button presses controlled the lights.
- **Nothing was ever written to the database.** The `events` table sat empty
  from 2026-07-14 onward. This went unnoticed for five weeks and was diagnosed
  in entry 02 — the cause was structural, not a schema problem.
- Two tests in `test_apis_request_shapes.py` went stale when the single
  `toggle_govee_study_light_bright_white` action was split into `light1` /
  `light2` variants, and were left failing.
