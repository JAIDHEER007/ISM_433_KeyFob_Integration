"""
Routing tests: an unmapped button code (a combo code not in manifest.json's
valid_events) or an unrecognized fob id should be logged as UNMAPPED and
never reach api_handler's dispatch - using the real manifest.json shipped in
the repo, not a fake/stubbed one.
"""

import time

from db_handler import STATE_UNMAPPED
from helpers import count_states, make_fake_event, trail_states, wait_until


def test_unmapped_combo_code_is_logged_and_not_dispatched(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    # Button codes are a bitmask of the four physical buttons (1/2/4/8), so a
    # simultaneous press shows up as their sum. 6 is the 2+4 combo, which
    # manifest.json's valid_events does not list. (This previously used 3, the
    # 1+2 combo, which has since been mapped to
    # toggle_govee_study_lights_bright_red - so the test was asserting against
    # a code that is now legitimately routed.)
    event = make_fake_event(button=6, timestamp=4000.0)
    q.put(event)

    wait_until(lambda: count_states(isolated_db._conn, "00E1278:6", STATE_UNMAPPED) == 1)

    time.sleep(0.2)  # give a mis-dispatch a moment to show up, if it were going to
    assert len(mock_govee) == 0
    # The trail ends at UNMAPPED - no API_HANDLER stage was ever entered.
    assert len(trail_states(isolated_db._conn, event["event_id"])) == 1


def test_unknown_fob_id_is_logged_as_unmapped(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    q.put(make_fake_event(button=1, device_id="FFFFFFF", timestamp=4100.0))

    wait_until(lambda: count_states(isolated_db._conn, "FFFFFFF:1", STATE_UNMAPPED) == 1)

    time.sleep(0.2)
    assert len(mock_govee) == 0
