"""Compare two file names: same episode or film, or not.

Replace Mode deletes the Telegram message of the file it replaces. Before it
does, the two names are compared here. They count as the same release target
only when both parse and agree; anything unclear counts as different, so both
files are kept.
"""
from __future__ import annotations

import re
from typing import Optional

from rapidfuzz import fuzz

MOVIE_TITLE_MATCH = 85


def _plan(name: str) -> Optional[dict]:
    try:
        from Backend.helper.metadata.plan import plan_media
        from Backend.helper.name_cleaning import clean_filename

        plan = plan_media(clean_filename(name), anime_channel=True)
    except Exception:
        return None
    if not isinstance(plan, dict) or plan.get("skip"):
        return None
    return plan


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _as_int(value) -> Optional[int]:
    if isinstance(value, bool) or isinstance(value, (list, tuple, dict)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _half_position(plan: dict):
    after = plan.get("watch_after")
    if not after:
        return None
    return (_as_int(after.get("season")), _as_int(after.get("episode")))


def _same_episode(old: dict, new: dict) -> bool:
    if old.get("combined") or new.get("combined"):
        return False
    old_ep, new_ep = _as_int(old.get("episode")), _as_int(new.get("episode"))
    if old_ep is None or new_ep is None or old_ep != new_ep:
        return False
    old_season, new_season = _as_int(old.get("season")), _as_int(new.get("season"))
    if (old_season == 0) != (new_season == 0):
        return False  # special vs. regular episode
    if old_season is not None and new_season is not None and old_season != new_season:
        return False
    if _half_position(old) != _half_position(new):
        return False  # half episode vs. full episode
    return True


def _same_film(old: dict, new: dict) -> bool:
    old_title, new_title = _norm(old.get("title")), _norm(new.get("title"))
    if not old_title or not new_title:
        return False
    if old_title == new_title:
        return True
    return fuzz.ratio(old_title, new_title) >= MOVIE_TITLE_MATCH


def same_release_target(existing_name: str, incoming_name: str, media_type: str) -> bool:
    """True if both names refer to the same episode (tv) or the same film (movie)."""
    if not existing_name or not incoming_name:
        return False
    if _norm(existing_name) == _norm(incoming_name):
        return True
    old, new = _plan(existing_name), _plan(incoming_name)
    if old is None or new is None:
        return False
    if str(media_type or "").lower() in ("tv", "series", "show"):
        return _same_episode(old, new)
    return _same_film(old, new)
