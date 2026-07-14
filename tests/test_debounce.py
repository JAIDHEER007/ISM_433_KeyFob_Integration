"""
Debounce logic tests - exercises event_processor.py's real debounce dict
through a background thread + fake events. No fob or Govee API involved
beyond the mocked HTTP layer (mock_govee is still required as a fixture
because a 'processed' event triggers a real dispatch attempt through
api_handler -> apis.py).
"""

from helpers import count_events, make_fake_event, wait_until


def test_second_press_within_window_is_debounced(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    t0 = 1000.0

    q.put(make_fake_event(button=1, timestamp=t0))
    wait_until(lambda: count_events(isolated_db._conn, "00E1278:1", "processed") == 1)

    q.put(make_fake_event(button=1, timestamp=t0 + 1))  # 1s later, inside the 3s window
    wait_until(lambda: count_events(isolated_db._conn, "00E1278:1", "debounced") == 1)

    assert count_events(isolated_db._conn, "00E1278:1", "processed") == 1
    assert count_events(isolated_db._conn, "00E1278:1", "debounced") == 1


def test_press_after_window_expires_is_processed_again(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    t0 = 2000.0

    q.put(make_fake_event(button=1, timestamp=t0))
    wait_until(lambda: count_events(isolated_db._conn, "00E1278:1", "processed") == 1)

    q.put(make_fake_event(button=1, timestamp=t0 + 4))  # outside the 3s window
    wait_until(lambda: count_events(isolated_db._conn, "00E1278:1", "processed") == 2)


def test_different_buttons_are_not_debounced_against_each_other(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    t0 = 3000.0

    q.put(make_fake_event(button=1, timestamp=t0))
    q.put(make_fake_event(button=2, timestamp=t0 + 0.1))

    wait_until(lambda: count_events(isolated_db._conn, "00E1278:1", "processed") == 1)
    wait_until(lambda: count_events(isolated_db._conn, "00E1278:2", "processed") == 1)
