"""
Dynamically dispatches a button-press event to the apis.py function named in
manifest.json, off the hot path via a thread pool so a slow/stuck Govee call
never blocks event_processor's main loop.
"""

import concurrent.futures
import importlib
import json
import time

import db_handler
from logger import logger

with open("manifest.json", "r") as f:
    _manifest = json.load(f)
VALID_EVENTS = set(_manifest["valid_events"])
FUNCTION_MAPPING = _manifest["manifest"]

executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)


def _on_complete(future, event_key, event_id, function_name, dispatched_at):
    """Close out this press's trail with its terminal SUCCESS/FAILED row.

    Replaces the old update_action_result(), which UPDATEd the single event row
    in place and therefore had to re-find it by (event_key, timestamp) - the
    async insert never handed back a row id. Appending against event_id instead
    makes that lookup exact rather than merely "unique enough at this volume".
    """
    duration = round(time.time() - dispatched_at, 3)
    try:
        future.result()
        db_handler.log_event(
            event_id, db_handler.STAGE_API_HANDLER, db_handler.STATE_SUCCESS,
            event_key=event_key,
            payload={"function": function_name, "duration_seconds": duration},
        )
    except Exception as e:
        reason = str(e) or type(e).__name__
        logger.error(f"[GOVEE-FAIL] {event_key}: {reason}")
        db_handler.log_event(
            event_id, db_handler.STAGE_API_HANDLER, db_handler.STATE_FAILED,
            event_key=event_key,
            payload={
                "function": function_name,
                "error": reason,
                "error_type": type(e).__name__,
                "duration_seconds": duration,
            },
        )


def execute_action(event, event_key, event_id):
    """Look up and run the apis.py function mapped to this event_key, async.

    Every exit path writes a trail row, so an EVENT_PROCESSOR/PROCESSED row is
    always followed by an API_HANDLER row saying what became of it. A trail that
    stops at PROCESSED therefore means the dispatch is genuinely still in flight
    (or the process died mid-call), not that a branch forgot to record itself.
    """
    if event_key not in VALID_EVENTS:
        # Defensive: event_processor already filters these out as UNMAPPED and
        # never calls here with one.
        logger.error(f"Ignored invalid event: {event_key}")
        db_handler.log_event(
            event_id, db_handler.STAGE_API_HANDLER, db_handler.STATE_FAILED,
            event_key=event_key,
            payload={"error": "event_key not in manifest valid_events"},
        )
        return

    function_name = FUNCTION_MAPPING.get(event_key)
    if not function_name:
        # A valid_events entry with no manifest mapping. Recorded rather than
        # returning silently as before: the button demonstrably did nothing when
        # pressed, which is a failure from where the user is standing, and a
        # silent return left a trail that just stopped dead.
        logger.error(f"[GOVEE-FAIL] {event_key}: no function mapped in manifest.json")
        db_handler.log_event(
            event_id, db_handler.STAGE_API_HANDLER, db_handler.STATE_FAILED,
            event_key=event_key,
            payload={"error": "no function mapped in manifest.json"},
        )
        return

    try:
        api_module = importlib.import_module("apis")
        api_function = getattr(api_module, function_name)
    except AttributeError:
        logger.error(f"[GOVEE-FAIL] {event_key}: function {function_name} not found in apis.py")
        db_handler.log_event(
            event_id, db_handler.STAGE_API_HANDLER, db_handler.STATE_FAILED,
            event_key=event_key,
            payload={"function": function_name, "error": f"no such function {function_name} in apis.py"},
        )
        return

    dispatched_at = time.time()
    db_handler.log_event(
        event_id, db_handler.STAGE_API_HANDLER, db_handler.STATE_DISPATCHED,
        event_key=event_key, payload={"function": function_name}, timestamp=dispatched_at,
    )

    future = executor.submit(api_function, event)
    future.add_done_callback(
        lambda f: _on_complete(f, event_key, event_id, function_name, dispatched_at)
    )
