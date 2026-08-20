"""
Consumes normalized fob-button events from the shared queue, debounces them,
logs every event to the DB, and hands valid/non-debounced presses off to
api_handler for dispatch.

Runs as its own multiprocessing.Process (kept separate from rtl433_listener.py
- see that file's module docstring for why).
"""

import json
import time

import db_handler
from api_handler import execute_action
from logger import logger

IGNORE_DURATION = 3  # seconds - first press within this window wins, matching the debounce ask

with open("manifest.json", "r") as f:
    _manifest = json.load(f)
VALID_EVENTS = set(_manifest["valid_events"])

# In-memory debounce state: event_key -> (timestamp, event_id) of the last
# accepted press. The event_id half exists purely for the trail - it lets a
# DEBOUNCED row name the press that suppressed it, turning "why did nothing
# happen when I pressed it" into a single query.
# Resets on process restart - acceptable known limitation for this project's scale.
PROCESSED_EVENTS = {}


def event_processor(event_queue):
    db_handler.init_db()  # each process gets its own connection - see db_handler.init_db() docstring
    logger.info("Event processor running...")

    while True:
        event = event_queue.get()

        # Normally minted by rtl433_listener, which already logged RECEIVED
        # under it. Falling back to a fresh id keeps the trail well-formed for a
        # future second producer (see this module's docstring) that doesn't set
        # one - such a press simply has no RTL_LISTENER stage in its trail.
        event_id = event.get("event_id") or db_handler.new_event_id()

        device_id = event.get("device_id")
        button = event.get("button")
        timestamp = event.get("timestamp")
        event_key = f"{device_id}:{button}"

        # The decoded fob fields that used to be dedicated columns. They live in
        # the JSON payload now, so a future fob or decoder that reports
        # something extra needs no schema change to have it recorded.
        details = {
            "model": event.get("model"),
            "button": button,
            "repeat": event.get("repeat"),
            "battery_ok": event.get("battery_ok"),
        }

        if event_key not in VALID_EVENTS:
            db_handler.log_event(
                event_id, db_handler.STAGE_EVENT_PROCESSOR, db_handler.STATE_UNMAPPED,
                event_key=event_key, payload=details, timestamp=timestamp,
            )
            continue

        last_press = PROCESSED_EVENTS.get(event_key)
        if last_press is not None and timestamp - last_press[0] < IGNORE_DURATION:
            last_timestamp, last_event_id = last_press
            db_handler.log_event(
                event_id, db_handler.STAGE_EVENT_PROCESSOR, db_handler.STATE_DEBOUNCED,
                event_key=event_key,
                payload={
                    **details,
                    "gap_seconds": round(timestamp - last_timestamp, 3),
                    "window_seconds": IGNORE_DURATION,
                    "suppressed_by_event_id": last_event_id,
                },
                timestamp=timestamp,
            )
            continue

        # Note the debounce window is armed here, *before* dispatch, and dispatch
        # is asynchronous - so a press whose Govee call later fails still blocks
        # re-presses for the next IGNORE_DURATION seconds. Behavior is unchanged
        # in this commit; the suppressed_by_event_id above is what now makes it
        # provable, by letting a DEBOUNCED row be joined to the FAILED outcome of
        # the press that silenced it.
        PROCESSED_EVENTS[event_key] = (timestamp, event_id)
        db_handler.log_event(
            event_id, db_handler.STAGE_EVENT_PROCESSOR, db_handler.STATE_PROCESSED,
            event_key=event_key, payload=details, timestamp=timestamp,
        )

        execute_action(event, event_key, event_id)
