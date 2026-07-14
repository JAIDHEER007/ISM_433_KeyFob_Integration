"""
Routing tests: an unmapped button code (a combo code not in manifest.json's
valid_events) or an unrecognized fob id should be logged as 'unmapped' and
never reach api_handler's dispatch - using the real manifest.json shipped in
the repo, not a fake/stubbed one.
"""

import time

from helpers import count_events, make_fake_event, wait_until


def test_unmapped_combo_code_is_logged_and_not_dispatched(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    # Button 3 is a combo code (simultaneous 1+2) - not present in
    # manifest.json's valid_events for this fob id.
    q.put(make_fake_event(button=3, timestamp=4000.0))

    wait_until(lambda: count_events(isolated_db._conn, "00E1278:3", "unmapped") == 1)

    time.sleep(0.2)  # give a mis-dispatch a moment to show up, if it were going to
    assert len(mock_govee) == 0


def test_unknown_fob_id_is_logged_as_unmapped(isolated_db, mock_govee, running_event_processor):
    q = running_event_processor
    q.put(make_fake_event(button=1, device_id="FFFFFFF", timestamp=4100.0))

    wait_until(lambda: count_events(isolated_db._conn, "FFFFFFF:1", "unmapped") == 1)

    time.sleep(0.2)
    assert len(mock_govee) == 0
