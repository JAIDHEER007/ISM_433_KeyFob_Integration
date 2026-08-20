"""
Regression tests for the fork bug that made the whole trail useless in
production: home_automation.py called init_db() in the parent before forking
its two children, so each child inherited a non-None _conn, its own init_db()
returned early, and it never started a writer thread (fork copies only the
calling thread). Every write from the listener and the event processor was
queued onto a queue with no consumer - no error, no exception, rows simply
never reached the file. Only the parent's retention writes worked, which is
why the database looked alive while containing nothing.

The end-to-end case runs in a subprocess rather than forking from pytest
directly. Forking out of the pytest process is itself unreliable here: by the
time these tests run it holds a live event_processor thread plus the idle
daemon writer threads conftest deliberately leaves behind, and forking a
multi-threaded process can stall the child on a lock that was held at fork
time and can never be released. That is the same hazard main() now avoids, and
letting it into the test made it flaky roughly one run in three. A fresh
interpreter reproduces production faithfully and deterministically.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import db_handler

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirrors home_automation.main()'s *old*, worst-case ordering on purpose: the
# parent opens the DB and starts its writer thread BEFORE forking. main() no
# longer does this, but init_db() must survive it regardless - that ordering is
# exactly what silently broke production, and a future caller could reintroduce
# it.
FORK_SCENARIO = r"""
import json, multiprocessing, os, sqlite3, sys, time

sys.path.insert(0, {repo_root!r})
import db_handler

DB = {db_file!r}
db_handler.DB_FILE = DB


def child(tag, result_queue):
    db_handler.init_db()
    result_queue.put({{
        "tag": tag,
        "forked": os.getpid() != PARENT_PID,
        "owns_connection": db_handler._conn_pid == os.getpid(),
        "writer_alive": bool(
            db_handler._writer_thread and db_handler._writer_thread.is_alive()
        ),
    }})
    db_handler.log_event(db_handler.new_event_id(), "TEST", "FROM_CHILD_" + tag)

    deadline = time.time() + 10
    while time.time() < deadline:
        if db_handler._conn.execute(
            "SELECT COUNT(*) FROM event_log WHERE state = ?", ("FROM_CHILD_" + tag,)
        ).fetchone()[0]:
            return
        time.sleep(0.02)


if __name__ == "__main__":
    PARENT_PID = os.getpid()
    db_handler.init_db()
    db_handler.log_event(db_handler.new_event_id(), "TEST", "FROM_PARENT")

    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    procs = [ctx.Process(target=child, args=(t, result_queue)) for t in ("A", "B")]
    for p in procs:
        p.start()
    observations = [result_queue.get(timeout=30) for _ in procs]
    for p in procs:
        p.join(timeout=30)

    states = sorted(
        r[0] for r in sqlite3.connect(DB).execute("SELECT state FROM event_log")
    )
    print("RESULT" + json.dumps({{
        "start_method": multiprocessing.get_start_method(),
        "observations": observations,
        "persisted": states,
    }}))
"""


def _run_fork_scenario(tmp_path):
    script = tmp_path / "fork_scenario.py"
    db_file = str(tmp_path / "fork.db")
    script.write_text(FORK_SCENARIO.format(repo_root=str(REPO_ROOT), db_file=db_file))

    # DATA_DIR must be set explicitly rather than inherited: apis.py calls
    # load_dotenv() at import, which pushes the container's DATA_DIR=/app/data
    # from .env into this process's environment. A subprocess inheriting that
    # would try to mkdir /app on the host and die. db_handler reads DATA_DIR at
    # import time, so it has to be right in the environment before launch.
    env = {**os.environ, "DATA_DIR": str(tmp_path)}

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, timeout=90, cwd=str(REPO_ROOT), env=env,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None
    )
    assert line, f"scenario produced no result\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return json.loads(line[len("RESULT"):])


def test_forked_children_open_their_own_connection_and_writer(tmp_path):
    result = _run_fork_scenario(tmp_path)

    assert result["start_method"] == "fork", (
        "this regression only exists under fork; if the platform default changed, "
        "the test is no longer exercising the bug"
    )
    for obs in result["observations"]:
        assert obs["forked"], "child should be a separate process"
        assert obs["owns_connection"], (
            f"child {obs['tag']} reused the connection inherited from the parent "
            "instead of opening its own - init_db()'s PID guard regressed"
        )
        assert obs["writer_alive"], (
            f"child {obs['tag']} has no live writer thread, so its queued writes "
            "would never be drained - this is the bug that lost every trail row"
        )


def test_writes_from_forked_children_actually_reach_the_file(tmp_path):
    """The symptom as it appeared in production: rows queued, never persisted."""
    result = _run_fork_scenario(tmp_path)
    assert result["persisted"] == ["FROM_CHILD_A", "FROM_CHILD_B", "FROM_PARENT"]


def test_init_db_reopens_when_the_owning_pid_does_not_match(isolated_db, tmp_path):
    """The guard itself, without forking: a connection tagged with somebody
    else's PID must be replaced, not reused."""
    inherited = isolated_db._conn
    db_handler._conn_pid = os.getpid() + 100000  # pretend this was inherited

    db_handler.init_db()

    assert db_handler._conn is not inherited, "stale-PID connection should be replaced"
    assert db_handler._conn_pid == os.getpid()
    assert db_handler._writer_thread.is_alive()


def test_init_db_is_a_no_op_when_already_owned_by_this_process(isolated_db):
    existing_conn = isolated_db._conn
    existing_thread = db_handler._writer_thread

    db_handler.init_db()

    assert db_handler._conn is existing_conn, "must not reopen within the same process"
    assert db_handler._writer_thread is existing_thread, "must not start a second writer"


def test_wal_mode_and_busy_timeout_are_set(isolated_db):
    """WAL plus a busy timeout are what keep three concurrent writers from
    colliding with 'database is locked' now that they all really do write."""
    assert isolated_db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert isolated_db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
