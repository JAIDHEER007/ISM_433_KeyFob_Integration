"""
Full pipeline test: fake fob press -> event_processor (debounce + DB log) ->
api_handler (manifest lookup + threaded dispatch) -> apis.py (mocked Govee
HTTP) -> action_result written back onto the event's DB row.

This is the "does a button press actually reach the API call" surface -
proves the whole chain works end to end with zero real API credits spent and
no dependency on the RTL-SDR dongle or real Govee devices.
"""

from helpers import make_fake_event, poll_action_result


def test_button_1_press_dispatches_and_records_success(monkeypatch, isolated_db, mock_govee, running_event_processor):
    # manifest.json maps 00E1278:1 -> toggle_govee_study_light_bright_white
    monkeypatch.setenv("GOVEE_DEVICE_STUDY_LIGHT_MAC_ADDR", "11:22:33:44:55:66")
    monkeypatch.setenv("GOVEE_MODEL_STUDY_LIGHT", "H6159")

    q = running_event_processor
    event = make_fake_event(button=1, timestamp=5000.0)
    q.put(event)

    result = poll_action_result(isolated_db._conn, "00E1278:1", event["timestamp"])
    assert result == "success"
    assert any("developer-api.govee.com" in c["url"] for c in mock_govee)


def test_button_2_press_dispatches_and_records_success(monkeypatch, isolated_db, mock_govee, running_event_processor):
    # manifest.json maps 00E1278:2 -> toggle_govee_smart_plug
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")

    q = running_event_processor
    event = make_fake_event(button=2, timestamp=5100.0)
    q.put(event)

    result = poll_action_result(isolated_db._conn, "00E1278:2", event["timestamp"])
    assert result == "success"


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
    event = make_fake_event(button=2, timestamp=5200.0)  # 00E1278:2 -> toggle_govee_smart_plug
    q.put(event)

    result = poll_action_result(isolated_db._conn, "00E1278:2", event["timestamp"])
    assert result.startswith("failed:")
