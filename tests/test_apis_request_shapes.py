"""
Tests that apis.py builds the correct Govee request shape for each mapped
action, with the HTTP layer mocked - proves the payloads are correct without
spending real API credits or needing real devices to be reachable.
"""

import apis


def test_toggle_smart_plug_flips_current_power_state(monkeypatch, mock_govee):
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")

    apis.toggle_govee_smart_plug({})  # mock_govee's default state fixture reports "off"

    put_calls = [c for c in mock_govee if c["method"] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0]["json"]["device"] == "AA:BB:CC:DD:EE:FF"
    assert put_calls[0]["json"]["cmd"] == {"name": "turn", "value": "on"}


def test_turn_off_all_hits_both_configured_devices(monkeypatch, mock_govee):
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")
    monkeypatch.setenv("GOVEE_DEVICE_STUDY_LIGHT_MAC_ADDR", "11:22:33:44:55:66")
    monkeypatch.setenv("GOVEE_MODEL_STUDY_LIGHT", "H6159")

    apis.turn_off_all({})

    off_targets = {c["json"]["device"] for c in mock_govee if c["json"]["cmd"]["value"] == "off"}
    assert off_targets == {"AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"}


def test_study_light_bright_white_sets_power_brightness_and_color_temp(monkeypatch, mock_govee):
    monkeypatch.setenv("GOVEE_DEVICE_STUDY_LIGHT_MAC_ADDR", "11:22:33:44:55:66")
    monkeypatch.setenv("GOVEE_MODEL_STUDY_LIGHT", "H6159")

    apis.toggle_govee_study_light_bright_white({})  # reported "off" -> takes the turn-on + configure branch

    cmd_names = [c["json"]["cmd"]["name"] for c in mock_govee if c["method"] == "PUT"]
    assert cmd_names == ["turn", "brightness", "colorTem"]

    color_temp_call = next(c for c in mock_govee if c["json"] and c["json"]["cmd"]["name"] == "colorTem")
    assert color_temp_call["json"]["cmd"]["value"] == 6500


def test_get_device_state_raises_on_non_success_message(monkeypatch, mock_govee):
    monkeypatch.setenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setenv("GOVEE_MODEL_SMART_PLUG", "H5080")

    def failing_state(method, url, headers=None, json=None, timeout=None, **kwargs):
        from helpers import FakeGoveeResponse
        return FakeGoveeResponse({"message": "Failure"})

    monkeypatch.setattr("apis.requests.request", failing_state)

    try:
        apis.toggle_govee_smart_plug({})
        assert False, "expected an exception when Govee reports a non-Success message"
    except Exception as e:
        assert "Failed" in str(e)
