import sys
from importlib.util import find_spec

# Packages the server cannot start without, as import name -> name on PyPI
_REQUIRED_PACKAGES = {
    "dotenv": "python-dotenv",
    "pytz": "pytz",
    "pyrogram": "pyrofork",
    "motor": "motor",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "jinja2": "jinja2",
    "httpx": "httpx",
    "aiohttp": "aiohttp",
}


def _environment_problem() -> str | None:
    if sys.version_info < (3, 11):
        found = ".".join(map(str, sys.version_info[:3]))
        return f"Telegram Stremio needs Python 3.11 or newer; this is Python {found}."
    missing = [pypi for module, pypi in _REQUIRED_PACKAGES.items() if find_spec(module) is None]
    if missing:
        return (
            "Missing Python packages: " + ", ".join(missing) + ".\n"
            "Install them with: pip install -r requirements.txt"
        )
    return None


_problem = _environment_problem()
if _problem:
    # "python -m Backend" has "-m" as argv[0] while the package is loading:
    # end with the message alone there, and raise a normal error on import
    if sys.argv and sys.argv[0] == "-m":
        raise SystemExit(_problem)
    raise ImportError(_problem)

from datetime import datetime
from time import time

from Backend.logger import TIMEZONE as timezone
from Backend.helper.database import Database

# Shared application state
now = datetime.now(timezone)
StartTime = time()

USE_DEFAULT_ID: str = None
MANUAL_SESSION: dict = None
db = Database()

__version__ = "5.3.5"
