"""
Dynamically dispatches a button-press event to the apis.py function named in
manifest.json, off the hot path via a thread pool so a slow/stuck Govee call
never blocks event_processor's main loop.
"""

import concurrent.futures
import importlib
import json

import db_handler
from logger import logger

with open("manifest.json", "r") as f:
    _manifest = json.load(f)
VALID_EVENTS = set(_manifest["valid_events"])
FUNCTION_MAPPING = _manifest["manifest"]

executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)


def _on_complete(future, event_key, timestamp):
    try:
        future.result()
        db_handler.update_action_result(event_key, timestamp, "success")
    except Exception as e:
        reason = str(e) or type(e).__name__
        logger.error(f"[GOVEE-FAIL] {event_key}: {reason}")
        db_handler.update_action_result(event_key, timestamp, f"failed:{reason}")


def execute_action(event, event_key, timestamp):
    """Look up and run the apis.py function mapped to this event_key, async."""
    if event_key not in VALID_EVENTS:
        logger.error(f"Ignored invalid event: {event_key}")
        return

    function_name = FUNCTION_MAPPING.get(event_key)
    if not function_name:
        return  # combo/unmapped code - logged as 'unmapped' by event_processor already, no action

    try:
        api_module = importlib.import_module("apis")
        api_function = getattr(api_module, function_name)
    except AttributeError:
        logger.error(f"[GOVEE-FAIL] {event_key}: function {function_name} not found in apis.py")
        db_handler.update_action_result(event_key, timestamp, f"failed:no such function {function_name}")
        return

    future = executor.submit(api_function, event)
    future.add_done_callback(lambda f: _on_complete(f, event_key, timestamp))
