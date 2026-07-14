# ISM 433 MHz Key Fob → Govee Automation

Reads button presses from a 4-button Microchip HCS200 KeeLoq key fob via an
RTL-SDR USB dongle and `rtl_433`, debounces them (3s per button), logs every
event to SQLite, and dispatches Govee smart-light/plug actions. Runs as a
single Docker container on a Raspberry Pi. See
`/home/jaidheer/.claude/plans/refactored-wishing-wolf.md` for the full design
rationale (architecture, debounce semantics, chaos-engineering/failure
handling, load testing).

## Setup

1. Copy `.env.example` to `.env` and fill in your Govee API key, device MAC
   addresses/models, and your fob's `id` (from `rtl_433 -F json` - see below).
2. Edit `manifest.json` to map your fob's `"<id>:<button>"` codes to whichever
   `apis.py` function you want each button to trigger. The shipped file has
   placeholder mappings using the id `00E1278` observed during development.
3. Make sure nothing else on the host is using the RTL-SDR dongle (rtl_433
   should not already be running nativel0y).

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
- **Database**: `./data/events.db` - `events` table for button presses
  (`status`: `processed`/`debounced`/`unmapped`, `action_result`:
  `success`/`failed:<reason>`), `system_events` table for dongle
  health/lifecycle events.
- **If the dongle gets unplugged**: check `docker compose logs -f` for an
  `[SDR-DOWN]` diagnosis, or `docker compose ps` for an `unhealthy` container
  status. Replugging the *same* dongle should be picked up automatically
  (no restart needed) - `docker compose restart` is a fallback, not the
  expected first step.
- **Manual DB cleanup**: `docker compose exec home_automation python clean_db.py`
  (the daemon already runs this hourly on its own).
