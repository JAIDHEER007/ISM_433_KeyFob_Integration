"""
Standalone manual-run cleanup script. home_automation.py already runs this
retention job periodically on its own background thread (see
RETENTION_CHECK_INTERVAL_SECONDS there), so this is only for an ad-hoc cleanup
without restarting the container, e.g.:

    docker compose exec home_automation python clean_db.py
"""

import sqlite3
import time

from db_handler import DB_FILE, RETENTION_SECONDS
from logger import logger


def delete_old_events():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        cutoff_time = time.time() - RETENTION_SECONDS
        cursor = conn.cursor()
        cursor.execute("DELETE FROM events WHERE timestamp < ?", (cutoff_time,))
        deleted_events = cursor.rowcount
        cursor.execute("DELETE FROM system_events WHERE timestamp < ?", (cutoff_time,))
        deleted_system_events = cursor.rowcount
        conn.commit()
        logger.info(
            f"Retention cleanup: deleted {deleted_events} events, "
            f"{deleted_system_events} system_events older than {RETENTION_SECONDS}s."
        )
    except Exception as e:
        logger.error(f"Database cleanup error: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    delete_old_events()
