"""
Full pipeline test: fake fob press -> event_processor (debounce + trail) ->
api_handler (manifest lookup + threaded dispatch) -> apis.py (mocked Govee
HTTP) -> terminal SUCCESS/FAILED row appended to the same event_id's trail.

This is the "does a button press actually reach the API call" surface -
proves the whole chain works end to end with zero real API credits spent and
no dependency on the RTL-SDR dongle or real Govee devices.
"""

from db_handler import (
    STAGE_API_HANDLER,
    STAGE_EVENT_PROCESSOR,
    STATE_DISPATCHED,
    STATE_FAILED,
    STATE_PROCESSED,
    STATE_SUCCESS,
)
from helpers import fetch_payload, make_fake_event, poll_terminal_state, trail_states


def test_button_1_press_dispatches_and_records_success(monkeypatch, isolated_db, mock_govee, running_event_processor):
    # manifest.json maps 00E1278:1 -> toggle_govee_study_light1_bright_white
    monkeypatch.setenv("GOVEE_DEVICE_STUDY_LIGHT1_MAC_ADDR", "11:22:33:44:55:66")
    monkeypatch.setenv("GOVEE_MODEL_STUDY_LIGHT1", "H6159")

    q = running_event_processor
    event = make_fake_event(button=1, timestamp=5000.0)
    q.put(event)

    assert poll_terminal_state(isolated_db._conn, event["event_id"]) == STATE_SUCCESS
    assert any("developer-api.govee.com" in c["url"] for c in mock_govee)


def test_button_4_press_dispatches_and_records_success(monkeypatch, isolated_db, mock_govee, running_event_processor):
    # manifest.json maps 00E1278:4 -> toggle_govee_smart_plug
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")

    q = running_event_processor
    event = make_fake_event(button=4, timestamp=5100.0)
    q.put(event)

    assert poll_terminal_state(isolated_db._conn, event["event_id"]) == STATE_SUCCESS


def test_successful_press_leaves_a_complete_ordered_trail(monkeypatch, isolated_db, mock_govee, running_event_processor):
    """The headline of this feature: one event_id, every stage, in order."""
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")

    q = running_event_processor
    event = make_fake_event(button=4, timestamp=5150.0)
    q.put(event)

    poll_terminal_state(isolated_db._conn, event["event_id"])

    # No RTL_LISTENER row here: these tests inject onto the queue directly
    # rather than running rtl_433, which is where RECEIVED is written.
    assert trail_states(isolated_db._conn, event["event_id"]) == [
        (STAGE_EVENT_PROCESSOR, STATE_PROCESSED),
        (STAGE_API_HANDLER, STATE_DISPATCHED),
        (STAGE_API_HANDLER, STATE_SUCCESS),
    ]

    dispatched = fetch_payload(isolated_db._conn, event["event_id"], STATE_DISPATCHED)
    assert dispatched["function"] == "toggle_govee_smart_plug"

    success = fetch_payload(isolated_db._conn, event["event_id"], STATE_SUCCESS)
    assert success["function"] == "toggle_govee_smart_plug"
    assert success["duration_seconds"] >= 0


def test_govee_failure_is_recorded_as_failed_not_silently_dropped(
    monkeypatch, isolated_db, mock_govee, running_event_processor
):
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")

    def failing_request(method, url, headers=None, json=None, timeout=None, **kwargs):
        from helpers import FakeGoveeResponse
        return FakeGoveeResponse({"message": "Failure"})

    monkeypatch.setattr("apis.requests.request", failing_request)

    q = running_event_processor
    event = make_fake_event(button=4, timestamp=5200.0)  # 00E1278:4 -> toggle_govee_smart_plug
    q.put(event)

    assert poll_terminal_state(isolated_db._conn, event["event_id"]) == STATE_FAILED

    payload = fetch_payload(isolated_db._conn, event["event_id"], STATE_FAILED)
    assert "Failed" in payload["error"]
    assert payload["error_type"] == "Exception"
    assert payload["function"] == "toggle_govee_smart_plug"
