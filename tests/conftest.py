"""
Shared pytest fixtures for the ISM-433 keyfob pipeline test suite.

None of these tests touch a real RTL-SDR dongle or the real Govee API. The RF
side is simulated by pushing fake event dicts (the same shape
rtl433_listener.py normally builds - see helpers.make_fake_event) directly
onto the queue event_processor.event_processor() consumes; the Govee side is
simulated by monkeypatching apis.requests.request. No production module is
edited to make any of this work - db_handler.py's DATA_DIR/DB_FILE and
manifest.json's file path are existing seams, and monkeypatch's job is
exactly to substitute attributes for the duration of one test.
"""

import os
import queue
import threading

import pytest

# rtl433_listener reads FOB_IDS at import time and hard-crashes if it's unset
# (os.getenv(...).split(...) on None). It's normally supplied by .env, which is
# gitignored - and conftest is imported before any test module, so without this
# a fresh clone fails during collection and takes the whole suite down. Same
# guard as test_event_trail.py; setdefault keeps a real .env winning.
os.environ.setdefault("FOB_IDS", "00E1278")

import db_handler  # noqa: E402
import event_processor as event_processor_module  # noqa: E402
import rtl433_listener  # noqa: E402
from helpers import FakeGoveeResponse  # noqa: E402


@pytest.fixture
def isolated_db(tmp_path):
    """Give each test its own throwaway SQLite file instead of the real
    ./data/events.db.

    db_handler.DB_FILE is a plain module attribute computed once at import
    time from the DATA_DIR env var - by the time a test runs, setting the env
    var would be too late, so this retargets the attribute directly instead.
    init_db() is a no-op once _conn is already set, so the module globals are
    reset to force a fresh connection + writer thread scoped to this test.
    """
    db_handler._conn = None
    db_handler._conn_pid = None
    db_handler._write_queue = None
    db_handler._writer_thread = None
    db_handler.DB_FILE = str(tmp_path / "test_events.db")
    db_handler.init_db()
    yield db_handler
    # Retire the writer thread rather than abandoning it. An earlier version of
    # this fixture closed the connection and left each test's writer parked on
    # its queue forever; because _writer_loop read the module-global queue and
    # _conn is opened with check_same_thread=False, those orphans went on
    # picking up later tests' jobs and running them against a connection they
    # didn't own. Two threads on one sqlite connection segfaults the
    # interpreter - it surfaced as the suite dying mid-run once the file grew
    # past roughly thirty db-backed tests, with no failing test to point at.
    db_handler.shutdown_writer()


@pytest.fixture(autouse=True)
def reset_debounce_state():
    """PROCESSED_EVENTS is in-memory, module-level state in event_processor.py
    - clear it between tests so one test's presses can't debounce another's."""
    event_processor_module.PROCESSED_EVENTS.clear()


@pytest.fixture(autouse=True)
def reset_hop_code_cache():
    """Same problem, one stage earlier: rtl433_listener._hop_codes is
    module-level and deliberately outlives an rtl_433 restart, so without this
    a hop code used by one test would be seen as a duplicate by the next and
    that test's press would vanish."""
    rtl433_listener._hop_codes.clear()


@pytest.fixture
def mock_govee(monkeypatch):
    """Replace apis.requests.request so no real HTTP call ever leaves the
    machine - no Govee credits spent, no dependency on real devices existing
    or being powered on. Records every call for assertions.

    Defaults to reporting devices as currently "off" so toggle-style actions
    (which read state, then flip it) take the "turn on" branch by default;
    override by monkeypatching apis.requests.request again within a test if a
    specific test needs different behavior (see test_apis_request_shapes.py).
    """
    calls = []

    def fake_request(method, url, headers=None, json=None, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "json": json, "timeout": timeout})
        if "/state" in url:
            return FakeGoveeResponse({
                "message": "Success",
                "data": {"properties": [{"powerState": "off"}]},
            })
        return FakeGoveeResponse({"message": "Success"})

    monkeypatch.setattr("apis.requests.request", fake_request)
    return calls


@pytest.fixture
def running_event_processor():
    """Runs the real event_processor() loop on a background daemon thread,
    fed from a plain queue.Queue (event_processor only ever calls .get() on
    it, so a plain Queue works fine and avoids multiprocessing IPC/pickling
    since everything runs in-process here).

    event_processor() has no shutdown mechanism by design (it's meant to run
    for the life of its process under normal operation) - the thread is left
    running as a daemon, same as it would be reaped by Ctrl-C on the real
    container; each test uses its own private queue so this is harmless.
    """
    q = queue.Queue()
    thread = threading.Thread(
        target=event_processor_module.event_processor, args=(q,), daemon=True
    )
    thread.start()
    yield q
