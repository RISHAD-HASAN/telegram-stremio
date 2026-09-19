import sys
from datetime import datetime
from logging import ERROR, INFO, FileHandler, Formatter, StreamHandler, basicConfig, getLogger

import pytz

IST = pytz.timezone("Asia/Kolkata")


#----- Formatter that renders timestamps in IST
class ISTFormatter(Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, IST)
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

#----- Root logging configuration
formatter = ISTFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
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
LOGGER.info("Logger initialized with IST timezone.")
