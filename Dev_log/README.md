# Dev log

Development history for the ISM 433MHz key fob → Govee integration: what was
built in each phase, which decisions were made and why, and what was found
along the way.

This is the *engineering* record. For how to run and operate the system, see
the top-level [`readme.md`](../readme.md); this folder deliberately does not
duplicate it.

## Entries

| # | Phase | Date | Branch | Status |
|---|---|---|---|---|
| [01](01-initial-build.md) | Initial build — RTL-SDR pipeline, Docker, tests | 2026-07-10 → 07-14 | `main` / `dev` | Shipped |
| [02](02-e2e-event-store.md) | End-to-end event store (UUIDv7 trail) + fork-bug fix | 2026-08-20 | `feature/e2e_event_store` | Shipped |
| [03](03-govee-api-outputs.md) | Govee API response logging | — | `feature/govee_api_outputs` | Planned |
| [04](04-repeat-frame-drop.md) | Lost presses — `repeat > 0` filter drops whole bursts | 2026-08-20 | `bug-fix/repeat-frame-drop` | Fixed |

## The runtime pipeline

One Docker container, three processes, connected by a bounded queue:

```
  RTL-SDR dongle (USB)
          │
          │  rtl_433 -R 131 -F json      (subprocess, supervised + restarted with backoff)
          ▼
  ┌───────────────────┐
  │ rtl433_listener   │  parses JSON, filters to known fob id/model,
  │ (child process)   │  collapses each burst to one press by hop code,
  │                   │  mints the UUIDv7 event_id  ──► RECEIVED
  └─────────┬─────────┘
            │  multiprocessing.Queue (maxsize=1000)
            ▼
  ┌───────────────────┐
  │ event_processor   │  manifest lookup + 3s debounce
  │ (child process)   │  ──► UNMAPPED / DEBOUNCED / PROCESSED
  └─────────┬─────────┘
            │  in-process call
            ▼
  ┌───────────────────┐
  │ api_handler       │  manifest → apis.py function,
  │ (thread pool, 5)  │  ──► DISPATCHED, then SUCCESS / FAILED
  └─────────┬─────────┘
            │  HTTPS
            ▼
     Govee developer API

  home_automation.py (parent process) supervises all of the above and runs
  the hourly retention job.
```

Every stage appends to the same `event_log` trail, keyed by one `event_id`
per physical press. See [entry 02](02-e2e-event-store.md).

## Conventions

- One branch per feature, branched from `dev`.
- Code comments explain *why*, especially where the current shape replaced
  something that looked reasonable and wasn't. That history is load-bearing —
  several decisions here exist to prevent a specific past failure.
- Tests must not require the SDR dongle, a real fob, or real Govee credits.
