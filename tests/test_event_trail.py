"""
Tests for the end-to-end event trail itself: the UUIDv7 event_id, the
append-only event_log write path, the JSON payload's queryability, and the
RTL_LISTENER stage rows that rtl433_listener writes before anything reaches
the queue.

The listener tests here drive _read_stdout() with an in-memory stream instead
of a real rtl_433 subprocess, so they exercise the actual parsing/filtering
code without an RTL-SDR dongle.
"""

import io
import json
import os
import queue
import time
import uuid

FOB_ID = "00E1278"

# rtl433_listener reads FOB_IDS at import time and hard-crashes if it's unset
# (os.getenv(...).split(...) on None). It's normally supplied by .env, which is
# gitignored - so on a fresh clone this import would fail during test
# *collection* and take the whole suite down with it, not just this file.
# setdefault keeps a real .env winning when there is one; every test here
# monkeypatches FOB_IDS to what it needs anyway.
os.environ.setdefault("FOB_IDS", FOB_ID)

import db_handler  # noqa: E402
import rtl433_listener  # noqa: E402
from db_handler import (  # noqa: E402
    STAGE_API_HANDLER,
    STAGE_RTL_LISTENER,
    STATE_DROPPED_REPEAT,
    STATE_FAILED,
    STATE_RECEIVED,
    STATE_SUCCESS,
)
from helpers import wait_until  # noqa: E402


# --- UUIDv7 ----------------------------------------------------------------

def test_new_event_id_is_a_well_formed_v7_uuid():
    parsed = uuid.UUID(db_handler.new_event_id())
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_event_ids_encode_current_time_and_sort_chronologically():
    before_ms = int(time.time() * 1000)
    ids = [db_handler.new_event_id() for _ in range(50)]
    after_ms = int(time.time() * 1000)

    # First 48 bits are the big-endian Unix millisecond timestamp.
    first_ts = uuid.UUID(ids[0]).int >> 80
    assert before_ms <= first_ts <= after_ms

    # Generated in order, so lexical string sort must equal generation order.
    # (Within a single millisecond ordering is not guaranteed - see
    # new_event_id's docstring - so this compares by timestamp prefix.)
    timestamps = [uuid.UUID(i).int >> 80 for i in ids]
    assert timestamps == sorted(timestamps)

    assert len(set(ids)) == len(ids), "event ids must be unique"


# --- Write path ------------------------------------------------------------

def test_log_event_appends_rows_and_get_event_trail_returns_them_in_order(isolated_db):
    event_id = db_handler.new_event_id()

    db_handler.log_event(event_id, STAGE_RTL_LISTENER, STATE_RECEIVED,
                         event_key="00E1278:1", payload={"raw": {"button": 1}})
    db_handler.log_event(event_id, STAGE_API_HANDLER, STATE_SUCCESS,
                         event_key="00E1278:1", payload={"function": "toggle_govee_smart_plug"})

    trail = wait_until(lambda: (
        db_handler.get_event_trail(event_id)
        if len(db_handler.get_event_trail(event_id)) == 2 else None
    ))

    assert [r["state"] for r in trail] == [STATE_RECEIVED, STATE_SUCCESS]
    assert trail[0]["payload"] == {"raw": {"button": 1}}
    assert trail[1]["payload"]["function"] == "toggle_govee_smart_plug"
    assert all(r["event_key"] == "00E1278:1" for r in trail)


def test_rows_are_appended_never_updated(isolated_db):
    """The whole point of the model: a later state must not overwrite an
    earlier one, so the history stays intact."""
    event_id = db_handler.new_event_id()

    db_handler.log_event(event_id, STAGE_API_HANDLER, "DISPATCHED", event_key="00E1278:1")
    db_handler.log_event(event_id, STAGE_API_HANDLER, STATE_FAILED, event_key="00E1278:1")

    wait_until(lambda: len(db_handler.get_event_trail(event_id)) == 2)
    assert len(db_handler.get_event_trail(event_id)) == 2


def test_payload_is_queryable_with_sqlite_json_functions(isolated_db):
    """The flexibility that replaced adding columns: ad-hoc payload fields stay
    queryable via JSON1, which is what a future dashboard will lean on."""
    failing_id = db_handler.new_event_id()
    ok_id = db_handler.new_event_id()

    db_handler.log_event(failing_id, STAGE_API_HANDLER, STATE_FAILED, event_key="00E1278:1",
                         payload={"error": "rate limited", "http_status": 429})
    db_handler.log_event(ok_id, STAGE_API_HANDLER, STATE_SUCCESS, event_key="00E1278:2",
                         payload={"duration_seconds": 0.4})

    wait_until(lambda: len(db_handler.get_event_trail(ok_id)) == 1)

    rows = isolated_db._conn.execute(
        "SELECT event_id, json_extract(payload, '$.error'), json_extract(payload, '$.http_status') "
        "FROM event_log WHERE state = ?", (STATE_FAILED,)
    ).fetchall()

    assert rows == [(failing_id, "rate limited", 429)]


def test_unserializable_payload_is_recorded_rather_than_losing_the_row(isolated_db):
    event_id = db_handler.new_event_id()

    db_handler.log_event(event_id, STAGE_API_HANDLER, STATE_FAILED,
                         payload={"exc": ValueError("boom")})

    trail = wait_until(lambda: db_handler.get_event_trail(event_id) or None)
    # default=str keeps the write alive instead of dropping the row entirely.
    assert "boom" in trail[0]["payload"]["exc"]


def test_retention_prunes_event_log(isolated_db):
    old_id = db_handler.new_event_id()
    fresh_id = db_handler.new_event_id()

    db_handler.log_event(old_id, STAGE_API_HANDLER, STATE_SUCCESS,
                         timestamp=time.time() - db_handler.RETENTION_SECONDS - 60)
    db_handler.log_event(fresh_id, STAGE_API_HANDLER, STATE_SUCCESS)
    wait_until(lambda: len(db_handler.get_event_trail(fresh_id)) == 1)

    db_handler.delete_old_events()

    wait_until(lambda: len(db_handler.get_event_trail(old_id)) == 0)
    assert len(db_handler.get_event_trail(fresh_id)) == 1


# --- RTL_LISTENER stage ----------------------------------------------------

def _run_listener_stdout(monkeypatch, tmp_path, lines):
    """Drive the real _read_stdout() over canned rtl_433 JSON lines."""
    monkeypatch.setattr(rtl433_listener, "FOB_MODEL", "Microchip-HCS200")
    monkeypatch.setattr(rtl433_listener, "FOB_IDS", {FOB_ID})
    monkeypatch.setattr(rtl433_listener, "HEARTBEAT_FILE", str(tmp_path / "heartbeat"))

    stream = io.StringIO("".join(json.dumps(line) + "\n" for line in lines))
    state = rtl433_listener._SubprocessState()
    event_queue = queue.Queue()

    rtl433_listener._read_stdout(stream, state, event_queue)
    return event_queue


def test_decoded_press_is_queued_and_logged_as_received(isolated_db, monkeypatch, tmp_path):
    event_queue = _run_listener_stdout(monkeypatch, tmp_path, [
        {"model": "Microchip-HCS200", "id": FOB_ID, "button": 1, "repeat": 0, "battery_ok": 1},
    ])

    event = event_queue.get_nowait()
    assert event["event_id"], "listener must mint the event_id that heads the trail"

    trail = wait_until(lambda: db_handler.get_event_trail(event["event_id"]) or None)
    assert trail[0]["stage"] == STAGE_RTL_LISTENER
    assert trail[0]["state"] == STATE_RECEIVED
    assert trail[0]["event_key"] == f"{FOB_ID}:1"
    # The full decoded frame is retained, so the repeat-filter theory can be
    # checked against real captures later.
    assert trail[0]["payload"]["raw"]["button"] == 1


def test_repeat_frames_are_dropped_but_now_leave_evidence(isolated_db, monkeypatch, tmp_path):
    """Behavior is unchanged - repeat>0 frames still never reach the queue -
    but they are no longer invisible, which is what makes the 'had to press it
    three times' symptom investigable."""
    event_queue = _run_listener_stdout(monkeypatch, tmp_path, [
        {"model": "Microchip-HCS200", "id": FOB_ID, "button": 1, "repeat": 1, "battery_ok": 1},
        {"model": "Microchip-HCS200", "id": FOB_ID, "button": 1, "repeat": 2, "battery_ok": 1},
    ])

    assert event_queue.empty(), "repeat frames must still be filtered out of the pipeline"

    dropped = wait_until(lambda: isolated_db._conn.execute(
        "SELECT COUNT(*) FROM event_log WHERE state = ?", (STATE_DROPPED_REPEAT,)
    ).fetchone()[0] == 2)
    assert dropped

    repeats = [
        json.loads(r[0])["repeat"] for r in isolated_db._conn.execute(
            "SELECT payload FROM event_log WHERE state = ? ORDER BY id", (STATE_DROPPED_REPEAT,)
        ).fetchall()
    ]
    assert repeats == [1, 2]


def test_other_devices_are_not_written_to_the_trail(isolated_db, monkeypatch, tmp_path):
    """Ambient 433MHz traffic must not flood the trail."""
    event_queue = _run_listener_stdout(monkeypatch, tmp_path, [
        {"model": "Acurite-Tower", "id": "9999", "temperature_C": 21.5},
        {"model": "Microchip-HCS200", "id": "DEADBEEF", "button": 1, "repeat": 0},
    ])

    assert event_queue.empty()
    time.sleep(0.2)  # let any erroneous write drain through the writer thread
    assert isolated_db._conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 0
