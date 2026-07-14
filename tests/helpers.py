"""
Non-fixture test helpers shared across the suite. Kept separate from
conftest.py so they can be imported directly (`from helpers import ...`)
rather than only being available as fixture injections.
"""

import time


class FakeGoveeResponse:
    """Stands in for requests.Response - only .json() is ever called on the
    real thing in apis.py, so that's all that's faked."""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def make_fake_event(button, device_id="00E1278", model="Microchip-HCS200",
                     repeat=0, battery_ok=1, timestamp=None):
    """Builds an event dict shaped exactly like rtl433_listener.py's
    normalized output (see _read_stdout in that file), so tests feed
    event_processor the same contract the real listener produces."""
    return {
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


def poll_action_result(conn, event_key, timestamp, timeout=2):
    """Wait for api_handler's completion callback to write action_result back
    onto the given event row, and return it ('success' or 'failed:<reason>').

    Deliberately checks row[0] is not None rather than just truthiness of the
    fetched row - a freshly-inserted, not-yet-dispatched row fetches as
    (None,), which is a non-empty (and therefore truthy) tuple.
    """
    def check():
        row = conn.execute(
            "SELECT action_result FROM events WHERE event_key=? AND timestamp=?",
            (event_key, timestamp),
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    return wait_until(check, timeout=timeout)


def count_events(conn, event_key, status=None):
    query = "SELECT COUNT(*) FROM events WHERE event_key=?"
    params = [event_key]
    if status:
        query += " AND status=?"
        params.append(status)
    return conn.execute(query, params).fetchone()[0]
