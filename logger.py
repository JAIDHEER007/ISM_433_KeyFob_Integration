import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "home_automation.log")

# 5MB per file, 5 backups = 25MB hard cap on disk, unlike the old unrotated file handler
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger("home_automation")
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
    file_handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATEFMT))
    logger.addHandler(file_handler)

    # Container stdout is what `docker compose logs -f` shows - the first place
    # a headless-Pi user looks, so mirror everything there too.
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATEFMT))
    logger.addHandler(stream_handler)

    logger.propagate = False
