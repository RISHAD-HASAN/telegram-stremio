"""Main metadata entry point and /set candidate search."""
from __future__ import annotations

import re
import traceback

import Backend
from Backend.helper.encrypt import encode_string
from Backend.helper.metadata.common import (
    ensure_media_ids,
    extract_default_id,
    format_imdb_images,
    format_tmdb_image,
    split_default_id,
)
from Backend.helper.metadata.parse import apply_combined_override
from Backend.helper.metadata.plan import (
    SKIP_COMBINED_NO_SEASON,
    SKIP_EXTRA,
    SKIP_TITLE_MISSING,
    SKIP_UNNAMED_EXTRA,
    plan_media,
)
from Backend.helper.metadata.providers import cinemeta, tmdb
from Backend.helper.metadata.resolvers import (
    resolve_anime_movie,
    resolve_anime_tv,
    resolve_titled_special,
    resolve_movie,
    resolve_series,
)
from Backend.helper.settings_manager import SettingsManager
from Backend.logger import LOGGER


def _is_anime_channel(channel) -> bool:
    anime_channels = SettingsManager.current().anime_channels
    if not anime_channels:
        return False
    target = str(channel).replace("-100", "")
    return any(str(c).strip().replace("-100", "") == target for c in anime_channels)


def _resolve_default_id(override_id, filename) -> str | None:
    for source in (override_id, getattr(Backend, "USE_DEFAULT_ID", None), filename):
        if not source:
            continue
        try:
            found = extract_default_id(source) or (override_id if source is override_id else None)
        except Exception:
            found = None
        if found:
            return found
    return None


# Episode title similarity required to accept a show found through
# providers/episode_lookup.py.
EPISODE_TITLE_MATCH = 0.85
# Lower once an earlier file in the channel confirmed the show: translations
# of episode titles differ between releases and providers.
EPISODE_TITLE_MATCH_SAME_BATCH = 0.70

# channel -> last show confirmed for an upload without a show title
_TITLELESS_SHOW: dict = {}


async def metadata(
    filename: str,
    channel: int,
    msg_id,
    override_id: str = None,
    season_hint: int = None,
    quality_hint: str = None,
) -> dict | None:
    return await resolve_name(
        filename, channel, msg_id,
        anime_channel=_is_anime_channel(channel),
        override_id=override_id,
        season_hint=season_hint,
        quality_hint=quality_hint,
    )


async def resolve_name(
    filename: str,
    channel: int,
    msg_id,
    *,
    anime_channel: bool,
    override_id: str = None,
    season_hint: int = None,
    quality_hint: str = None,
    encoded_string: str = None,
) -> dict | None:
    """Resolve a cleaned file name to metadata.

    Does everything metadata() does except reading the channel's anime
    setting, so indexer.py can call it offline. When encoded_string is not
    given it is built from channel and msg_id.
    """
    default_id = _resolve_default_id(override_id, filename)

    def _plan(show_title=None):
        return plan_media(
            filename,
            anime_channel=anime_channel,
            season_hint=season_hint,
            quality_hint=quality_hint,
            has_explicit_id=bool(default_id),
            show_title=show_title,
        )

    try:
        plan = _plan()
    except Exception as e:
        LOGGER.error(f"Parsing failed for {filename}: {e}\n{traceback.format_exc()}")
        return None

    if plan["skip"] == SKIP_COMBINED_NO_SEASON:
        LOGGER.info(f"Skipping {filename}: contains 'combined'")
        return None
    if plan["skip"] == SKIP_EXTRA:
        LOGGER.info(f"Skipping {filename}: creditless opening or ending, not an episode")
        return None
    if plan["skip"] == SKIP_UNNAMED_EXTRA:
        LOGGER.info(f"Skipping {filename}: an extra of a film, the name does not say which one")
        return None
    if plan["skip"] == SKIP_TITLE_MISSING:
        return await _resolve_without_show_title(
            filename, channel, msg_id, _plan, plan, anime_channel, encoded_string
        )
    if plan["skip"]:
        LOGGER.info(f"No title parsed from: {filename} (parsed={plan['parsed']})")
        return None

    return await _resolve_plan(plan, filename, channel, msg_id, default_id, anime_channel, encoded_string)


async def _resolve_without_show_title(
    filename, channel, msg_id, make_plan, plan, anime_channel, encoded_string=None,
) -> dict | None:
    """Resolve an upload that has an episode title but no show title.

    Candidates are the show confirmed for the previous such file in the
    channel, then an episode title lookup. A candidate is accepted only if the
    provider's title for that season and episode matches the file name.
    """
    hint = plan.get("episode_title")
    season, episode = plan.get("season"), plan.get("episode")
    if not hint or season is None or episode is None:
        LOGGER.info(f"Skipping {filename}: no show title and no usable episode title to look it up with")
        return None

    from Backend.helper.metadata.parse import episode_titles_match
    from Backend.helper.metadata.providers.episode_lookup import show_candidates_for_episode_title

    tried = set()

    async def _try(show_title: str, threshold: float) -> dict | None:
        key = show_title.strip().lower()
        if not key or key in tried:
            return None
        tried.add(key)
        try:
            candidate_plan = make_plan(show_title)
        except Exception as e:
            LOGGER.warning(f"Parsing failed for {filename} as '{show_title}': {e}")
            return None
        if candidate_plan["skip"] or candidate_plan["kind"] != "tv":
            return None
        result = await _resolve_plan(
            candidate_plan, filename, channel, msg_id, None, anime_channel, encoded_string
        )
        if not result or result.get("media_type") != "tv":
            return None
        score = episode_titles_match(hint, result.get("episode_title") or "")
        same_number = (
            result.get("season_number") == candidate_plan["season"]
            and result.get("episode_number") == candidate_plan["episode"]
        )
        if score >= threshold and same_number:
            LOGGER.info(
                f"[TITLELESS] '{filename}' -> '{result.get('title')}' "
                f"S{int(season):02d}E{int(episode):02d} (episode title match {score:.2f})"
            )
            _TITLELESS_SHOW[channel] = show_title
            result["title_from_episode_lookup"] = True
            return result
        LOGGER.info(
            f"[TITLELESS] rejected '{show_title}' for '{filename}': "
            f"provider episode title '{result.get('episode_title')}' (match {score:.2f})"
        )
        return None

    previous = _TITLELESS_SHOW.get(channel)
    if previous:
        result = await _try(previous, EPISODE_TITLE_MATCH_SAME_BATCH)
        if result:
            return result

    try:
        candidates = await show_candidates_for_episode_title(hint)
    except Exception as e:
        LOGGER.warning(f"[TITLELESS] lookup failed for '{hint}': {e}")
        candidates = []
    for show_title in candidates:
        result = await _try(show_title, EPISODE_TITLE_MATCH)
        if result:
            return result

    LOGGER.info(
        f"Skipping {filename}: no show title, and no show found whose "
        f"S{int(season):02d}E{int(episode):02d} is titled '{hint}'. "
        f"Add the show name or an IMDb/TMDB id to the caption."
    )
    return None


_GENERIC_EPISODE_TITLE_RE = re.compile(r"(?i)S\d+E\d+|Episode \d+|(?:OVA|OAD|Special)(?: \d+)?")


def _apply_watch_after(result: dict, watch_after: dict, episode_title) -> None:
    """Append the viewing position of an "N.5" special to its title.

    Stremio lists specials in a separate season and cannot sort one between
    two regular episodes, hence "Title (watch after S01E22)".
    """
    label = _watch_after_label(watch_after)
    current = (result.get("episode_title") or "").strip()
    # No provider title: use the one from the file name
    if not current or _GENERIC_EPISODE_TITLE_RE.fullmatch(current):
        current = (episode_title or "").strip() or f"Episode {int(watch_after['episode'])}.5"
    if "watch after" not in current.lower():
        current = f"{current} (watch after {label})"
    result["episode_title"] = current
    result["watch_after"] = {"season": watch_after.get("season"), "episode": int(watch_after["episode"])}


def _watch_after_label(watch_after: dict) -> str:
    episode = int(watch_after["episode"])
    season = watch_after.get("season")
    return f"S{int(season):02d}E{episode:02d}" if season else f"episode {episode}"


async def _resolve_plan(
    plan, filename, channel, msg_id, default_id, anime_channel, encoded_string=None,
) -> dict | None:
    title = plan["title"]
    season = plan["season"]
    episode = plan["episode"]
    year = plan["year"]
    quality = plan["quality"]
    absolute = plan["absolute"]
    combined = plan["combined"]
    part_number = plan["part_number"]
    split_key = plan["split_key"]
    episode_title = plan.get("episode_title")
    watch_after = plan.get("watch_after")
    # Only season 0 lookups use these
    special_kwargs = {}
    if season == 0 and not absolute and (episode_title or watch_after or plan.get("special_counter")):
        special_kwargs = {"episode_title": episode_title, "watch_after": watch_after}
        if plan.get("special_counter"):
            special_kwargs["release_counter"] = True
    elif absolute and plan.get("special_label") is not None:
        special_kwargs = {"special_label": plan["special_label"]}

    if encoded_string is None:
        try:
            encoded_string = await encode_string({"chat_id": channel, "msg_id": msg_id})
        except Exception:
            encoded_string = None

    group_key = f"{channel}:{quality}:{split_key}" if split_key else None
    # Single (non-split) .zip archives
    if group_key is None and filename and filename.lower().rstrip().endswith(".zip"):
        from Backend.helper.split_files import _normalize

        base = filename.rsplit(".", 1)[0]
        group_key = f"{channel}:{quality}:{_normalize(base)}.zip"
        part_number = 1

    try:
        # TV path: classic SxxExx, specials (S00), or absolute/orphan episode
        is_tv = (season is not None and episode is not None) or bool(absolute and episode)
        if is_tv:
            if absolute:
                LOGGER.info(f"Fetching TV metadata (absolute): {title} E{int(episode)} (year={year})")
            else:
                LOGGER.info(f"Fetching TV metadata: {title} S{int(season):02d}E{int(episode):02d} (year={year})")
            result = None
            if plan.get("special_title") and anime_channel and not default_id:
                result = await resolve_titled_special(
                    plan["special_show"], plan["special_title"], encoded_string, year=year, quality=quality,
                )
                if result is not None:
                    # A "Part 2" in the title names an episode, not a piece of a split file
                    group_key, part_number = None, None
            if result is None and not default_id and (anime_channel or absolute):
                result = await resolve_anime_tv(
                    title, season, int(episode), encoded_string,
                    year=year, quality=quality, absolute=absolute, **special_kwargs,
                )
            if result is None and not absolute and season is not None:
                result = await resolve_series(
                    title, int(season), int(episode), encoded_string,
                    year=year, quality=quality, default_id=default_id,
                )
            # Fallback to anime TV if standard series missed
            if result is None and not default_id:
                result = await resolve_anime_tv(
                    title, season, int(episode), encoded_string,
                    year=year, quality=quality, absolute=absolute, **special_kwargs,
                )
            # Absolute on non-anime channel: still try series with season 1
            if result is None and absolute:
                result = await resolve_series(
                    title, 1, int(episode), encoded_string,
                    year=year, quality=quality, default_id=default_id,
                )
                if result:
                    result["absolute_episode"] = int(episode)
                    result["episode_number"] = int(episode)
            # If season 0 special / OVA failed to resolve as TV series, try standalone anime movie / movie
            if result is None and season == 0:
                LOGGER.info(f"[SPECIAL] S00 TV miss for '{title}' -> trying as movie/standalone")
                if anime_channel:
                    result = await resolve_anime_movie(
                        title, encoded_string, year=year, quality=quality
                    )
                if result is None:
                    result = await resolve_movie(
                        title, encoded_string, year=year, quality=quality, default_id=default_id
                    )
            if result is not None and combined:
                apply_combined_override(result, combined)
            # Prefer the title in the file name over a placeholder like "S00E08"
            if (
                result is not None and episode_title and result.get("media_type") == "tv"
                and _GENERIC_EPISODE_TITLE_RE.fullmatch((result.get("episode_title") or "").strip())
            ):
                result["episode_title"] = episode_title
            if result is not None and watch_after and result.get("media_type") == "tv":
                _apply_watch_after(result, watch_after, episode_title)
        else:
            LOGGER.info(f"Fetching Movie metadata: {title} (year={year})")
            result = None
            if not default_id and anime_channel:
                result = await resolve_anime_movie(
                    title, encoded_string, year=year, quality=quality
                )
            if result is None:
                result = await resolve_movie(
                    title, encoded_string, year=year, quality=quality, default_id=default_id
                )
        if result is not None:
            if anime_channel:
                result["is_anime"] = True
            result["group_key"] = group_key
            result["part_number"] = part_number
        if result:
            ensure_media_ids(result, seed=str(result.get("title") or ""))
        return result
    except Exception as e:
        LOGGER.error(f"Error while fetching metadata for {filename}: {e}\n{traceback.format_exc()}")
        return None


# /set candidate search

def _candidate_entry(source, title, year, imdb_id, tmdb_id, poster, backdrop, subtitle, media_type=None) -> dict:
    selected_id = imdb_id if (source == "imdb" and imdb_id) else (
        str(tmdb_id) if tmdb_id else (imdb_id or None)
    )
    return {
        "source": source,
        "media_type": media_type,
        "title": title or "",
        "year": year or "",
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "selected_id": selected_id,
        "poster": poster,
        "backdrop": backdrop,
        "subtitle": subtitle,
    }


async def _resolve_id_candidate(default_id, media_type: str) -> dict | None:
    imdb_id, tmdb_id, _explicit_imdb, use_tmdb = split_default_id(default_id)

    if imdb_id and not use_tmdb:
        imdb_type = "movie" if media_type == "movie" else "tvSeries"
        detail = None
        try:
            detail = await cinemeta.cached_detail(imdb_id, imdb_type)
        except Exception as e:
            LOGGER.warning(f"IMDb id candidate resolve failed for '{imdb_id}': {e}")
        images = format_imdb_images(imdb_id)
        if detail and detail.get("title"):
            return _candidate_entry(
                "imdb", detail.get("title", ""), detail.get("releaseDetailed", {}).get("year", ""),
                imdb_id, detail.get("moviedb_id"), detail.get("poster") or images["poster"],
                detail.get("background") or images["backdrop"], "IMDb / Cinemeta", media_type,
            )
        return _candidate_entry(
            "imdb", "", "", imdb_id, None, images["poster"], images["backdrop"],
            "IMDb / Cinemeta", media_type,
        )

    if tmdb_id:
        details = await tmdb.details(media_type, tmdb_id)
        if not details:
            return None
        r_title, r_year = tmdb.tmdb_title_year(details, media_type)
        imdb_ext = getattr(getattr(details, "external_ids", None), "imdb_id", None)
        return _candidate_entry(
            "tmdb", r_title, r_year or "", imdb_ext, tmdb_id,
            format_tmdb_image(getattr(details, "poster_path", None)),
            format_tmdb_image(getattr(details, "backdrop_path", None), "original"),
            "TMDb", media_type,
        )
    return None


async def _search_candidates(query: str, media_type: str, year: int | None = None, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []

    default_id = extract_default_id(query)
    if default_id:
        candidate = await _resolve_id_candidate(default_id, media_type)
        return [candidate] if candidate else []

    imdb_type = "movie" if media_type == "movie" else "tvSeries"
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    try:
        imdb_hits = await cinemeta.search_title_multi(query=query, type=imdb_type, limit=limit)
        for hit in imdb_hits:
            hid = hit.get("id")
            if not hid or ("imdb", hid) in seen:
                continue
            seen.add(("imdb", hid))
            images = format_imdb_images(hid)
            results.append(_candidate_entry(
                "imdb", hit.get("title", ""), hit.get("year", ""),
                hid, None, hit.get("poster") or images["poster"], images["backdrop"],
                "IMDb / Cinemeta", media_type,
            ))
    except Exception as e:
        LOGGER.warning(f"IMDb {media_type} candidate search failed for '{query}': {e}")

    try:
        tmdb_results = await tmdb.raw_search(query, media_type, year if media_type == "movie" else None)
        for item in (tmdb_results or [])[:limit]:
            tid = getattr(item, "id", None)
            if not tid or ("tmdb", str(tid)) in seen:
                continue
            seen.add(("tmdb", str(tid)))
            imdb_id = await tmdb.external_imdb_id(media_type, tid)
            r_title, r_year = tmdb.tmdb_title_year(item, media_type)
            results.append(_candidate_entry(
                "tmdb", r_title, r_year or "", imdb_id, tid,
                format_tmdb_image(getattr(item, "poster_path", None)),
                format_tmdb_image(getattr(item, "backdrop_path", None), "original"),
                "TMDb", media_type,
            ))
    except Exception as e:
        LOGGER.warning(f"TMDb {media_type} candidate search failed for '{query}': {e}")

    return results[:limit]


async def search_movie_candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    return await _search_candidates(query, "movie", year, limit)


async def search_tv_candidates(query: str, limit: int = 8) -> list[dict]:
    return await _search_candidates(query, "tv", None, limit)


async def search_any_candidates(query: str, year: int | None = None, limit: int = 8) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    default_id = extract_default_id(query)
    if default_id:
        out: list[dict] = []
        seen: set[tuple] = set()
        for mt in ("movie", "tv"):
            candidate = await _resolve_id_candidate(default_id, mt)
            if not candidate or not candidate.get("title"):
                continue
            key = (candidate.get("imdb_id"), str(candidate.get("tmdb_id")), mt)
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
        return out
    results = await search_movie_candidates(query, year, limit)
    results += await search_tv_candidates(query, limit)
    return results


def build_id_link(imdb_id=None, tmdb_id=None, media_type: str = "movie") -> str | None:
    if imdb_id and str(imdb_id).startswith("tt"):
        return f"https://www.imdb.com/title/{imdb_id}/"
    if tmdb_id is not None and str(tmdb_id).lstrip("-").isdigit() and int(tmdb_id) > 0:
        path = "movie" if media_type == "movie" else "tv"
        return f"https://www.themoviedb.org/{path}/{tmdb_id}"
    return None


def caption_with_id(caption: str, metadata_info: dict) -> str | None:
    link = build_id_link(
        metadata_info.get("imdb_id"), metadata_info.get("tmdb_id"),
        metadata_info.get("media_type", "movie"),
    )
    if not link:
        return None
    base = (caption or "").strip()
    if extract_default_id(base):
        return None
    return f"{base}\n{link}" if base else link


def _to_selection_payload(data: dict, media_type: str) -> dict:
    return {
        "tmdb_id": data.get("tmdb_id"),
        "imdb_id": data.get("imdb_id"),
        "title": data.get("title"),
        "release_year": data.get("year"),
        "rating": data.get("rate"),
        "description": data.get("description"),
        "poster": data.get("poster"),
        "backdrop": data.get("backdrop"),
        "logo": data.get("logo"),
        "genres": data.get("genres", []),
        "cast": data.get("cast", []),
        "runtime": data.get("runtime"),
        "media_type": media_type,
    }


async def fetch_selected_movie_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    if not selected_id:
        return None
    data = await resolve_movie(
        title="manual-rescan", encoded_string=None, year=None, quality=None, default_id=selected_id
    )
    return _to_selection_payload(data, "movie") if data else None


async def fetch_selected_tv_metadata(selected_id: str) -> dict | None:
    selected_id = str(selected_id).strip()
    imdb_id, tmdb_id, _, use_tmdb = split_default_id(selected_id)
    if not imdb_id and not tmdb_id:
        return None

    imdb_tv = None
    if imdb_id and not use_tmdb:
        try:
            imdb_tv = await cinemeta.get_detail(imdb_id=imdb_id, media_type="tvSeries")
        except Exception:
            imdb_tv = None
            use_tmdb = True

    if use_tmdb or not imdb_tv:
        if not tmdb_id and imdb_tv and imdb_tv.get("moviedb_id"):
            try:
                tmdb_id = int(imdb_tv["moviedb_id"])
            except Exception:
                tmdb_id = None
        if not tmdb_id:
            return None
        tv = await tmdb.details("tv", tmdb_id)
        if not tv:
            return None
        first_air = getattr(tv, "first_air_date", None)
        runtime = ""
        if getattr(tv, "episode_run_time", None):
            runtime = f"{tv.episode_run_time[0]} min"
        return {
            "tmdb_id": tv.id,
            "imdb_id": getattr(getattr(tv, "external_ids", None), "imdb_id", None),
            "title": tv.name,
            "release_year": getattr(first_air, "year", 0) if first_air else 0,
            "rating": getattr(tv, "vote_average", 0) or 0,
            "description": tv.overview or "",
            "poster": format_tmdb_image(tv.poster_path),
            "backdrop": format_tmdb_image(tv.backdrop_path, "original"),
            "logo": tmdb.get_tmdb_logo(getattr(tv, "images", None)),
            "genres": [g.name for g in (tv.genres or [])],
            "cast": [
                getattr(c, "name", None) or getattr(c, "original_name", None)
                for c in (getattr(getattr(tv, "credits", None), "cast", None) or [])
            ],
            "runtime": str(runtime),
            "media_type": "tv",
        }

    images = format_imdb_images(imdb_id)
    return {
        "tmdb_id": int(imdb_tv.get("moviedb_id")) if imdb_tv.get("moviedb_id") else None,
        "imdb_id": imdb_id,
        "title": imdb_tv.get("title", ""),
        "release_year": imdb_tv.get("releaseDetailed", {}).get("year", 0),
        "rating": imdb_tv.get("rating", {}).get("star", 0),
        "description": imdb_tv.get("plot", ""),
        "poster": images["poster"],
        "backdrop": images["backdrop"],
        "logo": images["logo"],
        "genres": imdb_tv.get("genre", []),
        "cast": imdb_tv.get("cast", []),
        "runtime": str(imdb_tv.get("runtime") or ""),
        "media_type": "tv",
    }
