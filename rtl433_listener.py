"""
Reads key-fob button events from an RTL-SDR dongle via the rtl_433 CLI tool.

Replaces the old ESP32+CC1101 UDP transport (udp_client.py). No stable/maintained
rtl_433 Python binding exists - subprocess + line-by-line JSON parsing of
`rtl_433 -F json` stdout is the standard approach used by rtl_433's own example
scripts and Home Assistant integrations.

Runs as its own multiprocessing.Process, deliberately kept separate from
event_processor.py and connected only via the shared event_queue - this is what
lets a future second producer (e.g. a revived CC1101/ESP32 UDP listener) be
added as another process feeding the same queue, without touching this file or
the consumer at all.

Also acts as a supervisor: rtl_433 is restarted with exponential backoff if it
dies or hangs, and failures are diagnosed and surfaced (see Chaos Engineering
section of the design plan) rather than silently retried forever.
"""

import json
import os
import signal
import subprocess
import threading
import time

import db_handler
from logger import logger

# --- Configuration (env-overridable) ---------------------------------------

FOB_MODEL = os.getenv("FOB_MODEL", "Microchip-HCS200")
FOB_IDS = {i.strip() for i in os.getenv("FOB_IDS").split(",") if i.strip()}

# rtl_433 protocol id for Microchip HCS200/HCS300 KeeLoq (OOK variant). Confirmed
# against rtl_433 25.02's protocol list; if it ever mismatches after an rtl_433
# upgrade, run `rtl_433 -R help | grep -i hcs200` to find the current number.
# Restricting to this one decoder is a pure CPU/noise efficiency filter - it does
# NOT restrict by device id, so a second HCS200 fob with a different id decodes
# fine under the same restriction.
RTL433_PROTOCOL_ID = os.getenv("RTL433_PROTOCOL_ID", "131")

STATS_INTERVAL_SECONDS = int(os.getenv("RTL433_STATS_INTERVAL", "60"))
HANG_TIMEOUT_SECONDS = STATS_INTERVAL_SECONDS * 2.5

BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 60
HEALTHY_RESET_SECONDS = 300  # sustained healthy run before backoff resets to base
CONSEC_FAILURES_BEFORE_SLOWDOWN = 5
SLOW_REANNOUNCE_INTERVAL_SECONDS = 300  # re-announce [SDR-DOWN] at most this often

HEARTBEAT_FILE = os.path.join(os.getenv("DATA_DIR", "."), "heartbeat")

# Known rtl_433 fatal stderr phrases -> human-readable diagnosis. Always logged
# alongside the raw stderr text, never in place of it - avoids overfitting to
# messages that turn out to be from a different rtl_433 version.
_DIAGNOSES = [
    ("No supported devices found",
     "USB device not found - check it's plugged in, try a different port/cable."),
    ("usb_claim_interface",
     "device found but busy - another process or the kernel DVB driver may have "
     "claimed it; check `lsusb` and `fuser`."),
    ("LIBUSB_ERROR_NO_DEVICE",
     "device disappeared while running - likely unplugged or a power brownout."),
    ("usb_open error",
     "device disappeared while running - likely unplugged or a power brownout."),
]


def _diagnose(stderr_text):
    for phrase, message in _DIAGNOSES:
        if phrase in stderr_text:
            return message
    return None


class _SubprocessState:
    """Shared state between the supervisor loop and the reader threads."""

    def __init__(self):
        self.last_output_at = time.time()
        self.proc = None
        self.lock = threading.Lock()
        self.last_stderr = ""


_last_heartbeat_write = 0


def _touch_heartbeat():
    # Throttled to at most once/sec - a rapid button mash could otherwise hit
    # the SD card with a write per stdout line; the healthcheck's staleness
    # threshold (180s) has plenty of margin for this.
    global _last_heartbeat_write
    now = time.time()
    if now - _last_heartbeat_write < 1:
        return
    _last_heartbeat_write = now
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(now))
    except OSError as e:
        logger.error(f"Could not write heartbeat file {HEARTBEAT_FILE}: {e}")


def _read_stdout(stream, state, event_queue):
    for line in iter(stream.readline, ""):
        line = line.strip()
        if not line:
            continue
        with state.lock:
            state.last_output_at = time.time()
        _touch_heartbeat()

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # stats lines / anything unparseable - already counted as a heartbeat above

        model = data.get("model")
        device_id = data.get("id")
        if model != FOB_MODEL or device_id not in FOB_IDS:
            # Some other decoded device, or not one of our fobs. Deliberately
            # NOT written to the trail - ambient 433MHz traffic (neighbours'
            # sensors, car fobs) is continuous, and logging it would bury the
            # rows that matter under noise.
            continue

        # This is the head of the trail: every downstream row for this press
        # carries this same id, which is what makes the end-to-end query work.
        event_id = db_handler.new_event_id()
        button = data.get("button")
        event_key = f"{device_id}:{button}"
        repeat = data.get("repeat", 0)

        if repeat > 0:
            # Protocol-level retransmission of the same physical press, not a
            # new one - dropped here, as before. Now recorded first, because
            # this filter is a prime suspect for the "had to press it three
            # times" symptom: HCS200 sets the repeat bit on every frame after
            # the first in a burst, so if the leading repeat=0 frame is the one
            # that gets corrupted, this branch silently discards the entire
            # press. A run of DROPPED_REPEAT rows with no RECEIVED row before
            # them is exactly that failure, and previously left no evidence at
            # all. The raw frame is kept so the theory can be checked against
            # real captures rather than re-derived from behavior.
            db_handler.log_event(
                event_id,
                db_handler.STAGE_RTL_LISTENER,
                db_handler.STATE_DROPPED_REPEAT,
                event_key=event_key,
                payload={"repeat": repeat, "raw": data},
            )
            continue

        event = {
            "event_id": event_id,
            "device_id": device_id,
            "model": model,
            "button": button,
            "repeat": repeat,
            "battery_ok": data.get("battery_ok"),
            "timestamp": time.time(),
        }
        db_handler.log_event(
            event_id,
            db_handler.STAGE_RTL_LISTENER,
            db_handler.STATE_RECEIVED,
            event_key=event_key,
            payload={"raw": data},
            timestamp=event["timestamp"],
        )
        try:
            event_queue.put(event, block=True, timeout=1)
        except Exception:
            logger.warning("Event queue full - dropping button-press event to avoid unbounded memory growth")
            # Trail row so a press lost to backpressure is distinguishable from
            # one that was never decoded at all - the RECEIVED row above says
            # the RF side worked, this one says the pipeline dropped it.
            db_handler.log_event(
                event_id,
                db_handler.STAGE_RTL_LISTENER,
                db_handler.STATE_QUEUE_FULL,
                event_key=event_key,
            )
    stream.close()


def _read_stderr(stream, state):
    for line in iter(stream.readline, ""):
        line = line.strip()
        if not line:
            continue
        with state.lock:
            state.last_output_at = time.time()
            state.last_stderr = line
        logger.warning(f"rtl_433 stderr: {line}")
    stream.close()


def _spawn_rtl433():
    cmd = [
        "rtl_433",
        "-R", RTL433_PROTOCOL_ID,
        "-M", f"stats:1:{STATS_INTERVAL_SECONDS}",
        "-F", "json",
    ]
    logger.info(f"Starting rtl_433: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def rtl433_listener(event_queue):
    """Process entry point: supervises rtl_433, normalizes its JSON output onto event_queue."""
    db_handler.init_db()  # each process gets its own connection - see db_handler.init_db() docstring
    logger.info("RTL-SDR listener starting...")

    stop_event = threading.Event()

    def _handle_sigterm(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    consecutive_failures = 0
    backoff = BACKOFF_BASE_SECONDS
    down_since = None
    last_giveup_announce = 0
    healthy_since = None

    while not stop_event.is_set():
        state = _SubprocessState()
        proc = _spawn_rtl433()
        state.proc = proc

        stdout_thread = threading.Thread(target=_read_stdout, args=(proc.stdout, state, event_queue), daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, args=(proc.stderr, state), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        # Monitor this subprocess instance until it dies, hangs, or we're asked to stop.
        while not stop_event.is_set():
            time.sleep(1)

            if proc.poll() is not None:
                break  # process exited on its own

            with state.lock:
                silence = time.time() - state.last_output_at
            if silence > HANG_TIMEOUT_SECONDS:
                logger.warning(
                    f"[SDR-RETRY] No output from rtl_433 for {silence:.0f}s "
                    f"(> {HANG_TIMEOUT_SECONDS:.0f}s threshold) - treating as hung, restarting."
                )
                break

            # Sustained healthy run - reset backoff and clear any prior down-state.
            if healthy_since is None:
                healthy_since = time.time()
            elif time.time() - healthy_since > HEALTHY_RESET_SECONDS:
                if consecutive_failures > 0 or down_since is not None:
                    duration = time.time() - down_since if down_since else 0
                    logger.info(f"[SDR-RECOVERED] after {duration:.0f}s downtime")
                    db_handler.insert_system_event("sdr_recovered", f"downtime={duration:.0f}s")
                consecutive_failures = 0
                backoff = BACKOFF_BASE_SECONDS
                down_since = None

        if stop_event.is_set():
            # Asked to stop while proc was still healthy/running - terminate it here
            # so we don't leave rtl_433 orphaned, holding the USB device.
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            break

        # Subprocess is dead or judged hung - clean it up.
        healthy_since = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        with state.lock:
            diagnosis = _diagnose(state.last_stderr)
            raw = state.last_stderr

        consecutive_failures += 1
        if down_since is None:
            down_since = time.time()

        reason = f"{diagnosis} (raw: {raw})" if diagnosis else (raw or "rtl_433 exited/hung with no stderr output")
        db_handler.insert_system_event("sdr_restarting", reason)

        if consecutive_failures <= CONSEC_FAILURES_BEFORE_SLOWDOWN:
            logger.warning(f"[SDR-RETRY] attempt {consecutive_failures}: {reason} - retrying in {backoff}s")
        else:
            now = time.time()
            if now - last_giveup_announce > SLOW_REANNOUNCE_INTERVAL_SECONDS:
                duration = now - down_since
                logger.critical(
                    f"[SDR-DOWN] dongle unreachable for {duration:.0f}s - "
                    f"please check it's plugged in and re-seat it. Last reason: {reason}"
                )
                db_handler.insert_system_event("sdr_giveup_notice", f"duration={duration:.0f}s reason={reason}")
                last_giveup_announce = now

        stop_event.wait(backoff)
        backoff = min(backoff * 2, BACKOFF_CAP_SECONDS)

    logger.info("RTL-SDR listener shut down.")
