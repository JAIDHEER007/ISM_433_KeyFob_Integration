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

A fob press is transmitted as a burst of identical frames, so this module is
also where a burst is collapsed back into one press: frames are de-duplicated
by their KeeLoq hop code, which is unique per press. The earlier filter keyed
on the frame's repeat bit instead and lost any press whose leading frame was
corrupted - see Dev_log/04-repeat-frame-drop.md for the measurement.

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
from collections import OrderedDict

import db_handler
from logger import logger

# --- Configuration (env-overridable) ---------------------------------------

FOB_MODEL = os.getenv("FOB_MODEL", "Microchip-HCS200")
FOB_IDS = {i.strip() for i in os.getenv("FOB_IDS").split(",") if i.strip()}

# rtl_433's field name for the HCS200's 32-bit KeeLoq hopping code. It changes
# on every press, so it identifies a physical press outright - which is what
# lets burst de-duplication key on identity instead of on the repeat bit or on
# a time window. See Dev_log/04-repeat-frame-drop.md.
HOP_CODE_FIELD = os.getenv("HOP_CODE_FIELD", "encrypted")

# A code only has to outlive its own burst. The widest burst in the captured
# data spans 2.6s, so 10s is ~4x headroom while staying far below the interval
# at which a fob could plausibly cycle back to the same code.
HOP_CODE_TTL_SECONDS = float(os.getenv("HOP_CODE_TTL_SECONDS", "10"))

# Hard cap so the cache can never grow without bound if TTL eviction somehow
# doesn't keep up (a decoder emitting garbage codes at speed, say). At the real
# frame rate this is never reached; it exists as a memory ceiling, not a tuning
# knob.
HOP_CODE_CACHE_MAX = 512

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


# --- Burst de-duplication --------------------------------------------------
#
# hop-code key -> monotonic time first seen. Module-level, so it survives an
# rtl_433 restart: a burst straddling a restart is still one press. Guarded by
# a lock because the supervisor can start a new stdout reader thread while the
# previous one is still draining a closed pipe.
#
# monotonic() rather than time.time(): this is a TTL, and a wall-clock step
# (NTP correcting a clock that started wrong on a box with no RTC - the normal
# case for the Pi this runs on) would otherwise expire entries early or keep
# them alive indefinitely. Trail rows still record wall-clock time.
_hop_codes = OrderedDict()
_hop_codes_lock = threading.Lock()


def _seen_before(key):
    """True if this hop code was already handled; otherwise record it and return False.

    Check-and-insert is one atomic step so two reader threads can't both
    conclude they saw a frame first and dispatch the same press twice.
    """
    now = time.monotonic()
    with _hop_codes_lock:
        # Insertion order is time order, so eviction stops at the first live
        # entry instead of scanning the whole cache.
        cutoff = now - HOP_CODE_TTL_SECONDS
        while _hop_codes:
            oldest_seen_at = next(iter(_hop_codes.values()))
            if oldest_seen_at > cutoff:
                break
            _hop_codes.popitem(last=False)

        if key in _hop_codes:
            return True

        _hop_codes[key] = now
        while len(_hop_codes) > HOP_CODE_CACHE_MAX:
            _hop_codes.popitem(last=False)
        return False


def _forget(key):
    """Un-record a hop code, so a later frame of the same burst can claim the press."""
    with _hop_codes_lock:
        _hop_codes.pop(key, None)


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

        # De-duplicate the burst by hop code, NOT by the repeat bit. The bit
        # means "this is not the first frame the fob *sent*", which says nothing
        # about whether we received that first frame - so the old `repeat > 0`
        # test discarded the entire press whenever the leading repeat=0 frame
        # was the one lost to interference. That was measured at 52% of presses
        # in the first capture; see Dev_log/04-repeat-frame-drop.md.
        #
        # Keying on the hop code instead means whichever frame arrives first
        # wins, which is exactly what the fob's retransmissions are for. A time
        # window can't substitute: bursts run up to 2.6s while two genuinely
        # distinct presses were seen 0.52s apart, so no threshold separates
        # them. Replays are dropped for free, since a replayed frame carries an
        # already-seen code.
        hop_code = data.get(HOP_CODE_FIELD)
        hop_key = None
        if hop_code is not None:
            # Keyed on device id, deliberately not on button: a frame whose
            # button bits decoded wrong would otherwise look like a new press
            # and fire a second, wrong action. HCS200 frames carry no CRC, so
            # that is worth guarding against - dropping such a frame as a
            # duplicate is the safe direction to be wrong in.
            hop_key = f"{device_id}:{hop_code}"
            duplicate = _seen_before(hop_key)
            dedup_by = "hop_code"
        else:
            # No hop code in the frame - a decoder or a future fob that doesn't
            # report one. Fall back to the old repeat-bit filter, which is
            # lossy but is still better than dispatching every frame of a burst
            # as a separate press. Confined to this branch so the fallback can
            # never affect fobs that do report a code.
            duplicate = repeat > 0
            dedup_by = "repeat_bit"

        if duplicate:
            # Now means what the name always claimed: same press, already
            # forwarded. dedup_by records which rule made the call, so a future
            # regression (or a fob silently falling back) is one query away, and
            # the raw frame is still kept for offline analysis.
            db_handler.log_event(
                event_id,
                db_handler.STAGE_RTL_LISTENER,
                db_handler.STATE_DROPPED_REPEAT,
                event_key=event_key,
                payload={"repeat": repeat, "hop_code": hop_code,
                         "dedup_by": dedup_by, "raw": data},
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
            # hop_code lifted out of the raw frame so RECEIVED and
            # DROPPED_REPEAT rows can be grouped by the same json_extract path
            # - that grouping is the whole diagnosis in entry 04.
            payload={"hop_code": hop_code, "repeat": repeat, "raw": data},
            timestamp=event["timestamp"],
        )
        try:
            event_queue.put(event, block=True, timeout=1)
        except Exception:
            logger.warning("Event queue full - dropping button-press event to avoid unbounded memory growth")
            # Release the hop code so the rest of this burst can retry. Without
            # this the fix would trade one silent loss for another: the first
            # frame claims the press, fails to queue it, and every retransmission
            # that could still have carried it is then dropped as a duplicate.
            # Re-queuing on a later frame is exactly the redundancy the fob
            # provides, now applied to backpressure as well as to lost frames.
            if hop_key is not None:
                _forget(hop_key)
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
