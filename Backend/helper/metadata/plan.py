"""Routing plan for a file name.

`plan_media()` decides what a cleaned name is (movie or episode, search title,
season and episode, absolute numbering, quality, combined range, split part)
without any I/O. `entry.metadata()` runs the plan against the providers and
`indexer.py` reports it, so both read a name the same way.
"""
from __future__ import annotations

import re
from typing import Optional

from Backend.helper.metadata.parse import (
    bundled_special_label,
    is_special_counter,
    clean_anime_search_title,
    extract_absolute_episode,
    extract_episode_title,
    half_episode_position,
    is_absolute_episode,
    is_bonus_extra,
    is_unnamed_movie_extra,
    parse_media_name,
    titled_special,
    tidy_title_separators,
)
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix

SKIP_COMBINED_NO_SEASON = "combined_without_season"
SKIP_EXTRA = "extra"
SKIP_NO_TITLE = "no_title"
SKIP_TITLE_MISSING = "title_missing"
SKIP_UNNAMED_EXTRA = "unnamed_extra"


def plan_media(
    filename: str,
    anime_channel: bool = False,
    season_hint: Optional[int] = None,
    quality_hint: Optional[str] = None,
    has_explicit_id: bool = False,
    show_title: Optional[str] = None,
) -> dict:
    """Return the routing plan for a name that went through clean_filename().

    Keys: skip (None or a SKIP_* reason), kind ("tv" or "movie"), title, season,
    episode, absolute, year, quality, combined, split_key, part_number,
    parse_target, parsed, episode_title, watch_after, special_label,
    special_counter, special_show, special_title.

    show_title is used when the name has no show title of its own
    ("S01E01 - The Green Seat.mkv"). Parser errors propagate to the caller.
    """
    split_info = parse_split_info(filename)
    part_number = split_info[1] if split_info else None
    parse_target = strip_part_suffix(filename) if split_info else filename

    parsed = parse_media_name(parse_target)
    combined = parse_combined_episodes(parse_target)

    plan = {
        "skip": None,
        "kind": None,
        "title": None,
        "season": None,
        "episode": None,
        "absolute": False,
        "year": parsed.get("year"),
        "quality": None,
        "combined": combined,
        "split_key": split_info[0] if split_info else None,
        "part_number": part_number,
        "parse_target": parse_target,
        "parsed": parsed,
        "episode_title": extract_episode_title(parse_target),
        "watch_after": half_episode_position(parse_target),
        "special_label": None,
        "special_counter": False,
        "special_show": None,
        "special_title": None,
    }

    if is_bonus_extra(parse_target):
        plan["skip"] = SKIP_EXTRA
        return plan
    if parsed.get("episode") is None and is_unnamed_movie_extra(parse_target):
        plan["skip"] = SKIP_UNNAMED_EXTRA
        return plan

    # A combined pack needs a season. With an episode number the word is just
    # part of the episode title ("S01E52 - Combined Strength").
    excess = parsed.get("excess")
    if (
        not combined
        and parsed.get("episode") is None
        and excess
        and any("combined" in str(item).lower() for item in (excess if isinstance(excess, list) else [excess]))
    ):
        plan["skip"] = SKIP_COMBINED_NO_SEASON
        return plan

    title = parsed.get("title")
    if not title and parsed.get("title_missing") and show_title:
        title = show_title
    season = parsed.get("season")
    episode = parsed.get("episode")
    quality = parsed.get("quality")

    if season_hint is not None and episode and not season and not isinstance(episode, list):
        season = season_hint

    # If season is 0 and no episode is present, treat as no season
    try:
        if season is not None and int(season) == 0 and episode is None:
            season = None
    except (TypeError, ValueError):
        pass

    if combined:
        season, episode = combined["season"], combined["start"] if combined["start"] is not None else 1
    elif isinstance(season, list) or isinstance(episode, list):
        if isinstance(season, list):
            season = season[0] if season else 1
        if isinstance(episode, list):
            if episode:
                combined = {"season": season if season is not None else 1, "start": episode[0], "end": episode[-1]}
                season, episode = combined["season"], combined["start"] if combined["start"] is not None else 1
            else:
                episode = 1
    elif season and not episode:
        combined = {"season": season, "start": None, "end": None}
        episode = 1

    # Absolute / orphan episode (e.g. "One Piece 1223 720.mkv",
    # "Naruto Shippuden - 016 480p ...", "[Judas] One Piece - 1172.mkv")
    absolute = False
    if episode is None and season is None:
        abs_ep = extract_absolute_episode(parse_target, parsed)
        if abs_ep is not None:
            episode = abs_ep
            absolute = True
            parsed["episode"] = abs_ep
    elif is_absolute_episode(parsed, parse_target):
        absolute = True
        if episode is None:
            episode = extract_absolute_episode(parse_target, parsed)

    # On anime channels, recover absolute from the raw filename when
    # PTN/GuessIt treated a numbered release as a movie (no season/episode).
    if anime_channel and season is None and not absolute and episode is None:
        abs_ep = extract_absolute_episode(parse_target, parsed)
        if abs_ep is not None:
            episode = abs_ep
            absolute = True
            parsed["episode"] = abs_ep

    # The resolution in the name wins; the dimension hint is a fallback.
    if not quality:
        quality = quality_hint or "HD"

    if not title:
        # Only an episode title and no id: searching providers for it would
        # match an unrelated show.
        if parsed.get("title_missing") and not has_explicit_id:
            plan["skip"] = SKIP_TITLE_MISSING
            plan["season"] = season if isinstance(season, int) else None
            plan["episode"] = episode if isinstance(episode, int) else None
            return plan
        # Fallback: use cleaned filename minus extension so valid media is never skipped
        base_name = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", parse_target).strip()
        if base_name:
            title = base_name
        else:
            plan["skip"] = SKIP_NO_TITLE
            return plan

    # Drop the absolute number and release group from the search title.
    if absolute and episode is not None:
        title = clean_anime_search_title(title, int(episode))
    else:
        title = clean_anime_search_title(title, None)
    # Separator dashes are layout ("Show - Subtitle"); hyphenated words stay.
    title = tidy_title_separators(title) or title

    # "Show - 13 (SNS OVA)": an OVA numbered after the last regular episode
    if absolute and isinstance(episode, int):
        label = bundled_special_label(parse_target)
        if label is not None:
            plan["special_label"] = label
            if label and not plan["episode_title"]:
                plan["episode_title"] = label

    is_tv = (season is not None and episode is not None) or bool(absolute and episode)
    # "Show.SP1": the number counts this release's specials
    plan["special_counter"] = bool(
        is_tv and not absolute and season == 0 and is_special_counter(parse_target)
    )
    # "Show OVA - Episode Title": looked up by title in the show's specials.
    # The whole name is used so a "Part 2" in the title is kept.
    if is_tv and not absolute and season == 0 and not combined and not plan["special_counter"]:
        titled = titled_special(filename)
        if titled:
            plan["special_show"], plan["special_title"] = titled
    plan.update(
        kind="tv" if is_tv else "movie",
        title=title,
        season=season,
        episode=episode,
        absolute=absolute,
        quality=quality,
        combined=combined,
    )
    return plan
