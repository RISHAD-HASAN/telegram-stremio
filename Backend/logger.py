import sys
from datetime import datetime
from logging import ERROR, INFO, FileHandler, Formatter, StreamHandler, basicConfig, getLogger
from os import getenv, path

import pytz
from dotenv import load_dotenv

DEFAULT_TIMEZONE = "Asia/Kolkata"

# The logger is imported before Backend.config, so config.env is read here too
load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"))


# Time zone for log lines and bot messages: TIMEZONE in config.env, e.g. "Europe/Berlin"
def _load_timezone():
    name = (getenv("TIMEZONE") or "").strip() or DEFAULT_TIMEZONE
    try:
        return pytz.timezone(name), None
    except pytz.UnknownTimeZoneError:
        return pytz.timezone(DEFAULT_TIMEZONE), name


TIMEZONE, _unknown_timezone = _load_timezone()


# Formatter that renders timestamps in the configured time zone
class ZoneFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, TIMEZONE)
        return dt.strftime(datefmt or "%d-%b-%y %I:%M:%S %p")


# Ensure stdout and stderr handle utf-8 cleanly on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Root logging configuration
formatter = ZoneFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
file_handler = FileHandler("log.txt", encoding="utf-8", errors="replace")
stream_handler = StreamHandler(sys.stdout)
file_handler.setFormatter(formatter)
stream_handler.setFormatter(formatter)

basicConfig(handlers=[file_handler, stream_handler], level=INFO)

getLogger("httpx").setLevel(ERROR)
getLogger("pyrogram").setLevel(ERROR)
getLogger("fastapi").setLevel(ERROR)

LOGGER = getLogger(__name__)
LOGGER.setLevel(INFO)
LOGGER.info(f"Logger initialized, time zone {TIMEZONE.zone}.")
if _unknown_timezone:
    LOGGER.warning(f"TIMEZONE \"{_unknown_timezone}\" is not a known time zone name, using {DEFAULT_TIMEZONE}.")
