"""Self-updater, run by start.sh before the server starts.

When an update source is configured (Settings -> Upstream Repo / Branch, or
UPSTREAM_REPO / UPSTREAM_BRANCH in config.env) the working directory is reset
to the newest commit of that branch. Files ignored by .gitignore, such as
config.env, log.txt and session files, are left alone.

Nothing happens when no update source is set. A normal git checkout is never
touched either: update it with "git pull" instead.
"""
import shutil
import sys
from datetime import datetime
from logging import INFO, FileHandler, Formatter, StreamHandler, basicConfig, error as log_error, info as log_info
from os import environ
from pathlib import Path
from subprocess import run as srun

import pytz
from dotenv import load_dotenv

LOG_FILE = "log.txt"
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_BRANCH = "master"
SETTINGS_DB = "dbFyvio"
# Identity of the throwaway repository the updater creates around the files
UPDATER_EMAIL = "updater@localhost"
UPDATER_NAME = "updater"

load_dotenv("config.env")


def _timezone():
    try:
        return pytz.timezone((environ.get("TIMEZONE") or "").strip() or DEFAULT_TIMEZONE)
    except pytz.UnknownTimeZoneError:
        return pytz.timezone(DEFAULT_TIMEZONE)


class ZoneFormatter(Formatter):
    zone = _timezone()

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, self.zone).strftime(datefmt or "%d-%b-%y %I:%M:%S %p")


def _setup_logging() -> None:
    # Every start begins with an empty log file
    Path(LOG_FILE).write_text("", encoding="utf-8")
    formatter = ZoneFormatter("[%(asctime)s] [%(levelname)s] - %(message)s", "%d-%b-%y %I:%M:%S %p")
    handlers = [FileHandler(LOG_FILE, encoding="utf-8"), StreamHandler()]
    for handler in handlers:
        handler.setFormatter(formatter)
    basicConfig(handlers=handlers, level=INFO)


def _source_from_db() -> tuple[str | None, str | None]:
    """Update source saved on the Settings page, read from the tracking database."""
    uris = [u.strip() for u in environ.get("DATABASE", "").replace(",", " ").split() if u.strip()]
    if not uris:
        log_info("update.py: DATABASE is not set, skipping the settings lookup.")
        return None, None
    try:
        from pymongo import MongoClient

        client = MongoClient(uris[0], serverSelectionTimeoutMS=5000)
        try:
            doc = client[SETTINGS_DB]["settings"].find_one({"_id": "app_settings"}) or {}
        finally:
            client.close()
        return (doc.get("upstream_repo") or "").strip() or None, (doc.get("upstream_branch") or "").strip() or None
    except Exception as exc:
        log_error(f"update.py: settings lookup failed ({exc}), falling back to config.env.")
        return None, None


def _git(*args: str, capture: bool = False):
    return srun(["git", *args], capture_output=capture, text=True)


def _display(repo: str) -> str:
    """owner/name of the repository, without any credentials the URL may carry."""
    parts = repo.rstrip("/").removesuffix(".git").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else "the configured repository"


def _made_by_updater() -> bool:
    """True when .git was created by an earlier run of this script."""
    result = _git("config", "--local", "--get", "user.email", capture=True)
    return result.returncode == 0 and result.stdout.strip() == UPDATER_EMAIL


def _update(repo: str, branch: str) -> bool:
    steps = (
        ("init", "-q"),
        ("config", "user.email", UPDATER_EMAIL),
        ("config", "user.name", UPDATER_NAME),
        ("add", "."),
        ("commit", "-q", "-m", "update"),
        ("remote", "add", "origin", repo),
        ("fetch", "-q", "origin"),
        ("reset", "-q", "--hard", f"origin/{branch}"),
    )
    return all(_git(*step).returncode == 0 for step in steps)


def main() -> int:
    _setup_logging()

    db_repo, db_branch = _source_from_db()
    repo = db_repo or environ.get("UPSTREAM_REPO", "").strip() or None
    branch = db_branch or environ.get("UPSTREAM_BRANCH", "").strip() or DEFAULT_BRANCH

    if not repo:
        log_info("No update source configured, starting with the installed code.")
        return 0
    if repo.startswith("-") or branch.startswith("-"):
        log_error("Update skipped: the upstream repo or branch setting is not valid.")
        return 0
    if shutil.which("git") is None:
        log_error("Update skipped: git is not installed.")
        return 0

    if Path(".git").exists():
        if not _made_by_updater():
            log_info("This folder is a git checkout, so the self-update is skipped. Use \"git pull\" to update.")
            return 0
        shutil.rmtree(".git", ignore_errors=True)

    log_info(f"Update source: {_display(repo)} (branch {branch})")
    if _update(repo, branch):
        log_info("Updated to the latest commit.")
        head = _git("rev-parse", "HEAD", capture=True)
        if head.returncode == 0:
            log_info(f"Commit: {head.stdout.strip()}")
    else:
        log_error("Update failed. Check the upstream repo and branch in Settings, then restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
