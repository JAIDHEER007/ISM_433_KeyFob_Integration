"""
Debounce logic tests - exercises event_processor.py's real debounce dict
through a background thread + fake events. No fob or Govee API involved
beyond the mocked HTTP layer (mock_govee is still required as a fixture
because a PROCESSED event triggers a real dispatch attempt through
api_handler -> apis.py).
"""

from db_handler import STATE_DEBOUNCED, STATE_PROCESSED
from helpers import count_states, fetch_payload, make_fake_event, wait_until


def test_second_press_within_window_is_debounced(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    t0 = 1000.0

    q.put(make_fake_event(button=1, timestamp=t0))
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_PROCESSED) == 1)

    q.put(make_fake_event(button=1, timestamp=t0 + 1))  # 1s later, inside the 3s window
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_DEBOUNCED) == 1)

    assert count_states(isolated_db._conn, "00E1278:1", STATE_PROCESSED) == 1
    assert count_states(isolated_db._conn, "00E1278:1", STATE_DEBOUNCED) == 1


def test_debounced_row_names_the_press_that_suppressed_it(isolated_db, mock_govee, running_event_processor):
    """The link that makes the multi-press symptom diagnosable: a swallowed
    press records which earlier press silenced it, so its outcome can be
    looked up rather than guessed at."""
    q = running_event_processor
    t0 = 1500.0

    first = make_fake_event(button=1, timestamp=t0)
    q.put(first)
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_PROCESSED) == 1)

    second = make_fake_event(button=1, timestamp=t0 + 1.25)
    q.put(second)
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_DEBOUNCED) == 1)

    payload = fetch_payload(isolated_db._conn, second["event_id"], STATE_DEBOUNCED)
    assert payload["suppressed_by_event_id"] == first["event_id"]
    assert payload["gap_seconds"] == 1.25
    assert payload["window_seconds"] == 3


def test_press_after_window_expires_is_processed_again(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    t0 = 2000.0

    q.put(make_fake_event(button=1, timestamp=t0))
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_PROCESSED) == 1)

    q.put(make_fake_event(button=1, timestamp=t0 + 4))  # outside the 3s window
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_PROCESSED) == 2)


def test_different_buttons_are_not_debounced_against_each_other(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    t0 = 3000.0

    q.put(make_fake_event(button=1, timestamp=t0))
    q.put(make_fake_event(button=2, timestamp=t0 + 0.1))

    wait_until(lambda: count_states(isolated_db._conn, "00E1278:1", STATE_PROCESSED) == 1)
    wait_until(lambda: count_states(isolated_db._conn, "00E1278:2", STATE_PROCESSED) == 1)
