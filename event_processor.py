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

# In-memory debounce state: event_key -> timestamp of last processed press.
# Resets on process restart - acceptable known limitation for this project's scale.
PROCESSED_EVENTS = {}


def event_processor(event_queue):
    db_handler.init_db()  # each process gets its own connection - see db_handler.init_db() docstring
    logger.info("Event processor running...")

    while True:
        event = event_queue.get()

        device_id = event.get("device_id")
        button = event.get("button")
        timestamp = event.get("timestamp")
        event_key = f"{device_id}:{button}"

        if event_key not in VALID_EVENTS:
            db_handler.insert_event(
                event_key, event.get("model"), button, event.get("repeat"),
                event.get("battery_ok"), timestamp, status="unmapped",
            )
            continue

        last_event_time = PROCESSED_EVENTS.get(event_key)
        if last_event_time is not None and timestamp - last_event_time < IGNORE_DURATION:
            db_handler.insert_event(
                event_key, event.get("model"), button, event.get("repeat"),
                event.get("battery_ok"), timestamp, status="debounced",
            )
            continue

        PROCESSED_EVENTS[event_key] = timestamp
        db_handler.insert_event(
            event_key, event.get("model"), button, event.get("repeat"),
            event.get("battery_ok"), timestamp, status="processed",
        )

        execute_action(event, event_key, timestamp)
