"""
Non-fixture test helpers shared across the suite. Kept separate from
conftest.py so they can be imported directly (`from helpers import ...`)
rather than only being available as fixture injections.
"""

import json
import time

import db_handler


class FakeGoveeResponse:
    """Stands in for requests.Response - only .json() is ever called on the
    real thing in apis.py, so that's all that's faked."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def make_fake_event(button, device_id="00E1278", model="Microchip-HCS200",
                     repeat=0, battery_ok=1, timestamp=None, event_id=None):
    """Builds an event dict shaped exactly like rtl433_listener.py's
    normalized output (see _read_stdout in that file), so tests feed
    event_processor the same contract the real listener produces - including
    the event_id the listener now mints to head the trail."""
    return {
        "event_id": event_id if event_id is not None else db_handler.new_event_id(),
        "device_id": device_id,
        "model": model,
        "button": button,
        "repeat": repeat,
        "battery_ok": battery_ok,
        "timestamp": timestamp if timestamp is not None else time.time(),
    }


def wait_until(predicate, timeout=2, interval=0.02):
    """Poll predicate() until it returns a truthy value, or raise TimeoutError.

    Needed because DB writes (db_handler's writer thread) and Govee dispatch
    (api_handler's thread pool) both happen asynchronously relative to
    event_processor's main loop - this mirrors that real eventual consistency,
    just compressed to milliseconds since the mocked HTTP call returns
    instantly.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"condition not met within {timeout}s")


def poll_terminal_state(conn, event_id, timeout=2):
    """Wait for a press's trail to reach a terminal API_HANDLER row and return
    that state ('SUCCESS' or 'FAILED').

    Replaces the old poll_action_result(), which polled the action_result
    column on a single mutable row. Terminal states are the two the dispatch
    callback writes; DISPATCHED is deliberately excluded so this waits for the
    Govee call to actually finish rather than merely be submitted.
    """
    def check():
        row = conn.execute(
            "SELECT state FROM event_log WHERE event_id = ? AND state IN (?, ?)",
            (event_id, db_handler.STATE_SUCCESS, db_handler.STATE_FAILED),
        ).fetchone()
        return row[0] if row else None

    return wait_until(check, timeout=timeout)


def count_states(conn, event_key, state=None):
    """Count trail rows for an event_key, optionally narrowed to one state."""
    query = "SELECT COUNT(*) FROM event_log WHERE event_key = ?"
    params = [event_key]
    if state:
        query += " AND state = ?"
        params.append(state)
    return conn.execute(query, params).fetchone()[0]


def fetch_payload(conn, event_id, state):
    """Return the decoded JSON payload of one trail row, or None if absent."""
    row = conn.execute(
        "SELECT payload FROM event_log WHERE event_id = ? AND state = ?",
        (event_id, state),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


def trail_states(conn, event_id):
    """Return the ordered list of (stage, state) pairs recorded for one press."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            "SELECT stage, state FROM event_log WHERE event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()
    ]
