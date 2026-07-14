import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

GOVEE_DEVICE_CONTROL_URL = "https://developer-api.govee.com/v1/devices/control"
GOVEE_DEVICE_STATE_URL = "https://developer-api.govee.com/v1/devices/state"
HEADERS = {
    "Govee-API-Key": os.getenv("GOVEE_API_KEY"),
    "Content-Type": "application/json",
}

# The old code had no timeout on any of these requests.request() calls - a Govee
# backend that accepts the TCP connection but never responds (a real cloud-outage
# failure mode, distinct from a clean connection refusal which fails fast already)
# would hang a dispatch-thread-pool worker indefinitely. This is what actually
# bounds worst-case thread-pool occupancy during an outage.
GOVEE_TIMEOUT_SECONDS = float(os.getenv("GOVEE_TIMEOUT_SECONDS", "8"))


def get_govee_device_state(device_mac_addr, device_model):
    url = f"{GOVEE_DEVICE_STATE_URL}?device={device_mac_addr}&model={device_model}"
    response = requests.request("GET", url, headers=HEADERS, timeout=GOVEE_TIMEOUT_SECONDS).json()
    if response.get("message") != "Success":
        raise Exception("Failed at get Govee device status")

    properties = response["data"]["properties"]
    power_state = next((prop["powerState"] for prop in properties if "powerState" in prop), None)

    if not power_state:
        raise Exception("Failed at get Govee device Power State")

    return power_state


def set_govee_power_state(device_mac_addr, device_model, power_state):
    payload = {
        "device": device_mac_addr,
        "model": device_model,
        "cmd": {"name": "turn", "value": power_state},
    }
    response = requests.request(
        "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload, timeout=GOVEE_TIMEOUT_SECONDS
    ).json()
    if response.get("message") != "Success":
        raise Exception("Failed to control Govee device")


def toggle_govee_study_light1_bright_white(event):
    device_mac_addr = os.getenv("GOVEE_DEVICE_STUDY_LIGHT1_MAC_ADDR")
    device_model = os.getenv("GOVEE_MODEL_STUDY_LIGHT1")

    power_state = get_govee_device_state(device_mac_addr=device_mac_addr, device_model=device_model)

    if power_state == "on":
        set_govee_power_state(device_mac_addr=device_mac_addr, device_model=device_model, power_state="off")
    else:
        set_govee_power_state(device_mac_addr=device_mac_addr, device_model=device_model, power_state="on")
        time.sleep(0.2)  # small pause to let the device apply power-on before next commands

        payload_brightness = {
            "device": device_mac_addr,
            "model": device_model,
            "cmd": {"name": "brightness", "value": 100},
        }
        resp_bri = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_brightness, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_bri.get("message") != "Success":
            raise Exception("Failed to set Govee brightness")

        payload_color_temp = {
            "device": device_mac_addr,
            "model": device_model,
            "cmd": {"name": "colorTem", "value": 6500},
        }
        resp_ct = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_color_temp, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_ct.get("message") != "Success":
            raise Exception("Failed to set Govee color temperature")

def toggle_govee_study_light2_bright_white(event):
    device_mac_addr = os.getenv("GOVEE_DEVICE_STUDY_LIGHT2_MAC_ADDR")
    device_model = os.getenv("GOVEE_MODEL_STUDY_LIGHT2")

    power_state = get_govee_device_state(device_mac_addr=device_mac_addr, device_model=device_model)

    if power_state == "on":
        set_govee_power_state(device_mac_addr=device_mac_addr, device_model=device_model, power_state="off")
    else:
        set_govee_power_state(device_mac_addr=device_mac_addr, device_model=device_model, power_state="on")
        time.sleep(0.2)  # small pause to let the device apply power-on before next commands

        payload_brightness = {
            "device": device_mac_addr,
            "model": device_model,
            "cmd": {"name": "brightness", "value": 100},
        }
        resp_bri = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_brightness, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_bri.get("message") != "Success":
            raise Exception("Failed to set Govee brightness")

        payload_color_temp = {
            "device": device_mac_addr,
            "model": device_model,
            "cmd": {"name": "colorTem", "value": 6500},
        }
        resp_ct = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_color_temp, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_ct.get("message") != "Success":
            raise Exception("Failed to set Govee color temperature")


def toggle_govee_study_lights_bright_red(event):
    sl1_device_mac_addr = os.getenv("GOVEE_DEVICE_STUDY_LIGHT1_MAC_ADDR")
    sl1_device_model = os.getenv("GOVEE_MODEL_STUDY_LIGHT1")
    sl2_device_mac_addr = os.getenv("GOVEE_DEVICE_STUDY_LIGHT2_MAC_ADDR")
    sl2_device_model = os.getenv("GOVEE_MODEL_STUDY_LIGHT2")

    power_state_sl1 = get_govee_device_state(device_mac_addr=sl1_device_mac_addr, device_model=sl1_device_model)
    power_state_sl2 = get_govee_device_state(device_mac_addr=sl2_device_mac_addr, device_model=sl2_device_model)

    if power_state_sl1 == "on":
        set_govee_power_state(device_mac_addr=sl1_device_mac_addr, device_model=sl1_device_model, power_state="off")
    else:
        set_govee_power_state(device_mac_addr=sl1_device_mac_addr, device_model=sl1_device_model, power_state="on")
        time.sleep(0.2)

        payload_brightness = {
            "device": sl1_device_mac_addr,
            "model": sl1_device_model,
            "cmd": {"name": "brightness", "value": 100},
        }
        resp_bri = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_brightness, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_bri.get("message") != "Success":
            raise Exception("Failed to set Govee brightness")

        payload_color = {
            "device": sl1_device_mac_addr,
            "model": sl1_device_model,
            "cmd": {"name": "color", "value": {"r": 255, "g": 0, "b": 0}},
        }
        resp_color = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_color, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_color.get("message") != "Success":
            raise Exception("Failed to set Govee color to red")

    if power_state_sl2 == "on":
        set_govee_power_state(device_mac_addr=sl2_device_mac_addr, device_model=sl2_device_model, power_state="off")
    else:
        set_govee_power_state(device_mac_addr=sl2_device_mac_addr, device_model=sl2_device_model, power_state="on")
        time.sleep(0.2)

        payload_brightness = {
            "device": sl2_device_mac_addr,
            "model": sl2_device_model,
            "cmd": {"name": "brightness", "value": 100},
        }
        resp_bri = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_brightness, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_bri.get("message") != "Success":
            raise Exception("Failed to set Govee brightness")

        payload_color = {
            "device": sl2_device_mac_addr,
            "model": sl2_device_model,
            "cmd": {"name": "color", "value": {"r": 255, "g": 0, "b": 0}},
        }
        resp_color = requests.request(
            "PUT", GOVEE_DEVICE_CONTROL_URL, headers=HEADERS, json=payload_color, timeout=GOVEE_TIMEOUT_SECONDS
        ).json()
        if resp_color.get("message") != "Success":
            raise Exception("Failed to set Govee color to red")

def toggle_govee_smart_plug(event):
    device_mac_addr = os.getenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR")
    device_model = os.getenv("GOVEE_MODEL_SMART_PLUG")

    power_state = get_govee_device_state(device_mac_addr=device_mac_addr, device_model=device_model)
    power_state = "off" if power_state == "on" else "on"

    set_govee_power_state(device_mac_addr=device_mac_addr, device_model=device_model, power_state=power_state)


def turn_off_all(event):
    device_mac_addr_smart_plug = os.getenv("GOVEE_DEVICE_SMART_PLUG_MAC_ADDR")
    device_model_smart_plug = os.getenv("GOVEE_MODEL_SMART_PLUG")

    device_mac_addr_study_light1 = os.getenv("GOVEE_DEVICE_STUDY_LIGHT1_MAC_ADDR")
    device_model_study_light1 = os.getenv("GOVEE_MODEL_STUDY_LIGHT1")
    
    device_mac_addr_study_light2 = os.getenv("GOVEE_DEVICE_STUDY_LIGHT2_MAC_ADDR")
    device_model_study_light2 = os.getenv("GOVEE_MODEL_STUDY_LIGHT2")

    set_govee_power_state(
        device_mac_addr=device_mac_addr_smart_plug, device_model=device_model_smart_plug, power_state="off"
    )
    set_govee_power_state(
        device_mac_addr=device_mac_addr_study_light1, device_model=device_model_study_light1, power_state="off"
    )
    set_govee_power_state(
        device_mac_addr=device_mac_addr_study_light2, device_model=device_model_study_light2, power_state="off"
    )
