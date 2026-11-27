"""Kitsu anime metadata provider (with ani.zip mappings for IMDb/TMDb/episode art)."""
from __future__ import annotations

import asyncio
import re
from typing import List, Optional

import httpx
from rapidfuzz import fuzz

from Backend.helper.metadata.common import (
    ensure_media_ids,
    KITSU_CACHE,
    KITSU_THRESHOLD,
    cached_call,
    logo_from_imdb,
    normalize_rating,
    parse_year_range,
    strip_html,
)
from Backend.logger import LOGGER

KITSU_URL = "https://kitsu.io/api/edge"
ANIZIP_URL = "https://api.ani.zip/mappings"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
    "User-Agent": "Telegram-Stremio (+https://github.com/RISHAD-HASAN/telegram-stremio)",
}


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)
        return _client


RETRY_WAITS = (1.0, 3.0)
MAX_RETRY_AFTER = 10.0


class _Unavailable(Exception):
    """The provider did not answer; says nothing about whether the entry exists."""


def _is_busy(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


async def _get(url: str, params: Optional[dict] = None) -> httpx.Response:
    """GET that waits and retries when the provider is busy or unreachable.

    Raises _Unavailable when it still fails, so a rate limit is never read as
    "this show has no data".
    """
    last = "no reply"
    for attempt in range(len(RETRY_WAITS) + 1):
        wait = RETRY_WAITS[attempt] if attempt < len(RETRY_WAITS) else None
        try:
            client = await _get_client()
            resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if not _is_busy(resp.status_code):
                return resp
            last = f"HTTP {resp.status_code}"
            try:
                asked = float(resp.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                asked = 0.0
            if wait is not None and asked > 0:
                wait = min(asked, MAX_RETRY_AFTER)
        if wait is None:
            break
        await asyncio.sleep(wait)
    raise _Unavailable(last)


def _normalize(title: str) -> str:
    if not title:
        return ""
    t = title.lower().strip()
    t = re.sub(r"^\b(the|a|an)\b\s+", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    try:
        return max(fuzz.token_set_ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0
    except Exception:
        return 0.0


def _title_score(query: str, attrs: dict) -> float:
    """Score the query against a Kitsu entry's title variants."""
    from Backend.helper.metadata.common import score_candidate_aliases
    titles = attrs.get("titles") or {}
    primary = (
        attrs.get("canonicalTitle")
        or titles.get("en")
        or titles.get("en_jp")
        or titles.get("ja_jp")
        or ""
    )
    # Official translations are strong aliases; abbreviations and slugs are
    # weak ones. Kitsu lists "Demon Slayer" as an abbreviation for the 2016
    # show "Onigiri", so treating abbreviations as exact matches would pick
    # the wrong series whenever the real franchise is also in the results.
    aliases = list(titles.values()) if isinstance(titles, dict) else []
    weak_aliases = list(attrs.get("abbreviatedTitles") or [])
    slug = attrs.get("slug")
    if slug:
        weak_aliases.append(str(slug).replace("-", " "))
    return score_candidate_aliases(
        query, None, primary, 0,
        aliases=aliases,
        weak_aliases=weak_aliases,
        year_reliable=False,
        year_lower_bound=True,
    )


def _season_queries(title: str, season: Optional[int]) -> List[str]:
    if season and int(season) > 1:
        return [f"{title} Season {season}", f"{title} {season}", title]
    return [title]


async def _kitsu_search(
    query: str, subtype: Optional[str] = None, year: Optional[int] = None,
) -> Optional[dict]:
    try:
        params = {"filter[text]": query, "page[limit]": 8}
        if subtype:
            params["filter[subtype]"] = subtype
        if year:
            params["filter[seasonYear]"] = str(year)
        resp = await _get(f"{KITSU_URL}/anime", params=params)
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or []
        return data
    except Exception as e:
        LOGGER.warning(f"[KITSU] search failed for '{query}': {e}")
        return None


def _start_year(attrs: dict) -> Optional[int]:
    try:
        return int(str(attrs.get("startDate") or "")[:4])
    except ValueError:
        return None


# Films are matched by title only, so the bar is higher than for series:
# sharing the franchise name must not be enough.
KITSU_MOVIE_THRESHOLD = 0.70
# Below this score specials, OVAs and ONAs are searched as well.
KITSU_MOVIE_CONFIDENT = 0.85
_FILM_LIKE_SUBTYPES = ("movie", "special", "ova", "ona")


def _leading_words_match(query: str, attrs: dict) -> bool:
    """True if the query equals the leading words of one of the entry's titles
    (a release that shortens a long title to its first words)."""
    from Backend.helper.metadata.common import normalize_title
    words = normalize_title(query).split()
    if len(words) < 2 or len("".join(words)) < 6:
        return False
    titles = attrs.get("titles") or {}
    names = [attrs.get("canonicalTitle")] + (list(titles.values()) if isinstance(titles, dict) else [])
    for name in names:
        cand = normalize_title(str(name or "")).split()
        if len(cand) > len(words) and cand[: len(words)] == words:
            return True
    return False


async def _search_scored(
    title: str, season: Optional[int] = None, movie: bool = False, year: Optional[int] = None,
    film_like: bool = False,
) -> tuple:
    """Return (row, score) for the best Kitsu match, or (None, score).

    movie      search the "movie" subtype only
    film_like  search all subtypes, keep movies, specials, OVAs and ONAs
    default    series search, movies and music videos excluded
    """
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    kind = "film" if film_like else ("movie" if movie else "tv")
    cache_key = f"kitsu::{kind}::{title}::{season}::{year or ''}"
    threshold = KITSU_MOVIE_THRESHOLD if (movie or film_like) else KITSU_THRESHOLD

    async def _produce():
        best = None
        best_score = 0.0
        first_rows: list = []
        subtype = "movie" if (movie and not film_like) else None

        def consider(rows) -> list:
            nonlocal best, best_score
            usable = []
            for row in rows:
                attrs = row.get("attributes") or {}
                row_subtype = (attrs.get("subtype") or "").lower()
                if film_like:
                    if row_subtype not in _FILM_LIKE_SUBTYPES:
                        continue
                elif movie:
                    if row_subtype not in ("movie", ""):
                        continue
                elif row_subtype in ("movie", "music"):
                    # Prefer TV for series searches
                    continue
                usable.append(row)
                score = _title_score(title, attrs)
                if year and _start_year(attrs) == year:
                    # Kitsu names remakes "Fruits Basket (2019)".
                    score = max(score, _title_score(f"{title} ({year})", attrs)) + 0.05
                if not movie and not film_like and row_subtype in ("special", "ova"):
                    # A recap / special named after its series must not beat
                    # the series itself on a near-tie.
                    score -= 0.03
                if score > best_score:
                    best_score = score
                    best = row
            return usable

        for index, query in enumerate(_season_queries(title, None if (movie or film_like) else season)):
            usable = consider(await _kitsu_search(query, subtype=subtype) or [])
            if index == 0:
                first_rows = usable
            if best_score >= 0.92:
                break

        # Text search returns one page ordered by relevance and popularity, so
        # a new entry of an old franchise can be missing from it. When the name
        # states a year the best match does not start in, search that year too.
        if year and (best is None or _start_year(best.get("attributes") or {}) != year):
            consider(await _kitsu_search(title, subtype=subtype, year=year) or [])

        if best and best_score >= threshold:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] match '{title}' -> '{attrs.get('canonicalTitle')}' "
                f"[{best.get('id')}] score={best_score:.2f}"
            )
            return best, best_score
        if not movie and not film_like:
            # Shortened release name: accept a leading-words match if it is
            # the only one in the result set.
            leading = [r for r in first_rows if _leading_words_match(title, r.get("attributes") or {})]
            if len(leading) == 1:
                attrs = leading[0].get("attributes") or {}
                LOGGER.info(
                    f"[KITSU] match '{title}' -> '{attrs.get('canonicalTitle')}' "
                    f"[{leading[0].get('id')}] by its leading words"
                )
                return leading[0], threshold
        if best:
            attrs = best.get("attributes") or {}
            LOGGER.info(
                f"[KITSU] low-confidence for '{title}': '{attrs.get('canonicalTitle')}' "
                f"score={best_score:.2f}"
            )
        return None, best_score

    result = await cached_call(KITSU_CACHE, cache_key, "kitsu_search", _produce)
    if isinstance(result, (tuple, list)) and len(result) == 2:
        return result[0], float(result[1] or 0.0)
    return None, 0.0


async def search_anime(
    title: str, season: Optional[int] = None, movie: bool = False, year: Optional[int] = None,
) -> Optional[dict]:
    """Best Kitsu row for a title. A year from the file name breaks ties
    between remakes ("Fruits.Basket.2019.S01E01")."""
    row, _ = await _search_scored(title, season=season, movie=movie, year=year)
    return row


async def get_anizip_mappings(kitsu_id: Optional[int] = None, anidb_id: Optional[int] = None) -> Optional[dict]:
    """ani.zip document for a Kitsu id, or for an AniDB id when given instead."""
    key, value = ("anidb_id", anidb_id) if kitsu_id is None else ("kitsu_id", kitsu_id)
    cache_key = f"anizip::{value}" if key == "kitsu_id" else f"anizip::anidb::{value}"

    async def _produce():
        resp = await _get(ANIZIP_URL, params={key: value})
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        raise _Unavailable(f"HTTP {resp.status_code}")

    try:
        return await cached_call(KITSU_CACHE, cache_key, "anizip", _produce)
    except Exception as e:
        # Not cached: the next file of the show asks again.
        LOGGER.warning(f"[KITSU] ani.zip mappings failed for {key}={value}: {e}")
        return None


async def _kitsu_anime(kitsu_id) -> Optional[dict]:
    """One Kitsu entry by id."""
    cache_key = f"kitsu-anime::{kitsu_id}"

    async def _produce():
        try:
            resp = await _get(f"{KITSU_URL}/anime/{int(kitsu_id)}")
            return ((resp.json() or {}).get("data") or None) if resp.status_code == 200 else None
        except Exception as e:
            LOGGER.warning(f"[KITSU] entry {kitsu_id} failed: {e}")
            return None

    return await cached_call(KITSU_CACHE, cache_key, "kitsu-anime", _produce)


async def _parent_series(anidb_id) -> tuple:
    """(Kitsu entry, ani.zip document) of the series an OVA entry belongs to, or (None, None)."""
    from Backend.helper.metadata.episode_maps import parent_series_entry

    parent = await parent_series_entry(anidb_id)
    if not parent:
        return None, None
    doc = await get_anizip_mappings(anidb_id=parent.anidb_id) or {}
    kitsu_id = (doc.get("mappings") or {}).get("kitsu_id")
    row = await _kitsu_anime(kitsu_id) if kitsu_id else None
    return (row, doc) if row else (None, None)


def _anizip_image(images, cover_type: str) -> str:
    for img in images or []:
        if str(img.get("coverType", "")).lower() == cover_type.lower() and img.get("url"):
            return img["url"]
    return ""


def _poster(attrs: dict, images: list) -> str:
    poster = attrs.get("posterImage") or {}
    return (
        poster.get("original")
        or poster.get("large")
        or poster.get("medium")
        or _anizip_image(images, "Poster")
        or ""
    )


def _backdrop(attrs: dict, images: list) -> str:
    cover = attrs.get("coverImage") or {}
    return (
        cover.get("original")
        or cover.get("large")
        or _anizip_image(images, "Fanart")
        or _anizip_image(images, "Banner")
        or ""
    )


def _common_payload(row: dict, doc: dict, title: str) -> dict:
    attrs = row.get("attributes") or {}
    mappings = (doc or {}).get("mappings") or {}
    tmdb_id = mappings.get("themoviedb_id")
    try:
        tmdb_id = int(tmdb_id) if tmdb_id else None
    except (ValueError, TypeError):
        tmdb_id = None

    titles = attrs.get("titles") or {}
    images = (doc or {}).get("images") or []
    rate = normalize_rating(
        (float(attrs["averageRating"]) / 10.0) if attrs.get("averageRating") else 0
    )
    year, year_end = parse_year_range(attrs.get("startDate"), attrs.get("endDate"))
    duration = attrs.get("episodeLength")
    imdb_id = mappings.get("imdb_id")
    # Prefer English display title; keep original/romaji separately for search
    english = titles.get("en") or titles.get("en_us") or titles.get("en_jp") or ""
    original = (
        attrs.get("canonicalTitle")
        or titles.get("ja_jp")
        or titles.get("en_jp")
        or title
    )
    display = english or original or title
    logo = _anizip_image(images, "Clearlogo") or logo_from_imdb(imdb_id)
    payload = {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "title": display,
        "title_english": english or display,
        "original_title": original if original != display else "",
        "year": year,
        "year_end": year_end,
        "rate": rate,
        "description": strip_html(_pick_english_text(attrs.get("synopsis"), attrs.get("description")) or ""),
        "poster": _poster(attrs, images),
        "backdrop": _backdrop(attrs, images),
        "logo": logo,
        "genres": [],
        "cast": [],
        "runtime": f"{duration} min" if duration else "",
        "kitsu_id": int(row["id"]) if row.get("id") is not None else None,
    }
    return ensure_media_ids(payload, seed=f"kitsu:{row.get('id')}")


def _pick_english_text(*candidates, allow_fallback: bool = True) -> str:
    """Prefer English / latin-script text. Optionally fall back to any language."""
    latin = re.compile(r"[A-Za-z]")
    best_any = ""
    for raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, dict):
            for key in ("en", "en_us", "en_jp", "x-jat", "romaji"):
                val = raw.get(key)
                if val and str(val).strip():
                    return str(val).strip()
            for val in raw.values():
                s = str(val or "").strip()
                if not s:
                    continue
                if latin.search(s):
                    return s
                if not best_any:
                    best_any = s
            continue
        s = str(raw).strip()
        if not s:
            continue
        if latin.search(s):
            return s
        if not best_any:
            best_any = s
    return best_any if allow_fallback else ""


def _is_mostly_cjk(text: str) -> bool:
    if not text:
        return False
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")
    latin = re.compile(r"[A-Za-z]")
    s = str(text)
    return bool(cjk.search(s)) and not bool(latin.search(s))


def _needs_english(text: str) -> bool:
    """True when field is empty, generic, or non-English (CJK-only)."""
    if not text or not str(text).strip():
        return True
    s = str(text).strip()
    if s.startswith(("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")) and "E" in s[:6]:
        return True
    if s.startswith("Episode "):
        return True
    return _is_mostly_cjk(s)


def _episode_title_fallback(season: int, episode: int, absolute: bool = False) -> str:
    if absolute or season is None:
        return f"Episode {episode}"
    return f"S{int(season):02d}E{int(episode):02d}"


def _anizip_episode_fields(ep: dict) -> dict:
    """Extract preferred English fields from a single ani.zip episode object."""
    if not ep:
        return {}
    title = _pick_english_text(ep.get("title"), allow_fallback=False)
    overview = _pick_english_text(
        ep.get("overview"), ep.get("summary"), allow_fallback=False
    )
    image = (ep.get("image") or "").strip()
    rating = None
    raw_r = ep.get("rating")
    if raw_r is not None and str(raw_r).strip() != "":
        try:
            rating = float(raw_r)
        except (TypeError, ValueError):
            rating = None
    return {
        "episode_title": title or "",
        "episode_overview": overview or "",
        "episode_backdrop": image,
        "episode_released": ep.get("airDate") or ep.get("airdate") or "",
        "episode_rating": rating,
    }


async def _tvdb_episode_fields(tvdb_id: int, season: int, episode: int, absolute_hint=None) -> dict:
    """Cached TVDB episode fields (English). Empty dict if unavailable."""
    try:
        from Backend.helper.metadata.providers import tvdb as tvdb_mod
        if not tvdb_mod.tvdb_api_key():
            return {}
        ep = None
        if absolute_hint and (season is None or int(season) < 1):
            ep = await tvdb_mod.episode_by_absolute(int(tvdb_id), int(absolute_hint))
        if ep is None:
            ep = await tvdb_mod.episode_by_number(int(tvdb_id), int(season if season is not None else 1), int(episode))
        if not ep:
            return {}
        name = (ep.get("name") or "").strip()
        overview = (ep.get("overview") or "").strip()
        image = ""
        for key in ("image", "filename"):
            raw = ep.get(key)
            if raw:
                image = raw if str(raw).startswith("http") else f"https://artworks.thetvdb.com{raw}"
                break
        rating = None
        for rk in ("siteRating", "rating", "score"):
            if ep.get(rk) is not None:
                try:
                    rating = float(ep[rk])
                    break
                except (TypeError, ValueError):
                    continue
        out = {
            "episode_title": name if name and not _is_mostly_cjk(name) else "",
            "episode_overview": overview if overview and not _is_mostly_cjk(overview) else "",
            "episode_backdrop": image,
            "episode_released": (ep.get("aired") or ep.get("firstAired") or ""),
            "episode_rating": rating,
        }
        # If absolute resolved S/E, surface them
        if ep.get("seasonNumber") is not None:
            try:
                out["season_number"] = int(ep["seasonNumber"])
            except (TypeError, ValueError):
                pass
        if ep.get("number") is not None:
            try:
                out["episode_number"] = int(ep["number"])
            except (TypeError, ValueError):
                pass
        return out
    except Exception as e:
        LOGGER.debug(f"[KITSU] TVDB episode fields failed: {e}")
        return {}


async def _tmdb_episode_fields(tmdb_id: int, season: int, episode: int) -> dict:
    """Cached TMDB episode fields (language=en-US client). Empty dict if unavailable."""
    try:
        from Backend.helper.metadata.providers import tmdb as tmdb_mod
        from Backend.helper.metadata.common import format_tmdb_image
        if not tmdb_mod.tmdb_api_key():
            return {}
        ep = await tmdb_mod.episode_details(int(tmdb_id), int(season), int(episode))
        if not ep:
            return {}
        name = getattr(ep, "name", None) or ""
        overview = getattr(ep, "overview", None) or ""
        still = getattr(ep, "still_path", None)
        air = getattr(ep, "air_date", None)
        rating = None
        vote = getattr(ep, "vote_average", None)
        if vote is not None:
            try:
                rating = float(vote)
            except (TypeError, ValueError):
                rating = None
        return {
            "episode_title": name if name and not _is_mostly_cjk(name) else "",
            "episode_overview": overview if overview and not _is_mostly_cjk(overview) else "",
            "episode_backdrop": format_tmdb_image(still, "original") if still else "",
            "episode_released": str(air) if air else "",
            "episode_rating": rating,
        }
    except Exception as e:
        LOGGER.debug(f"[KITSU] TMDB episode fields failed: {e}")
        return {}


async def _tmdb_series_english(tmdb_id: int) -> dict:
    """Cached TMDB series-level English overview/title."""
    try:
        from Backend.helper.metadata.providers import tmdb as tmdb_mod
        if not tmdb_mod.tmdb_api_key():
            return {}
        tv = await tmdb_mod.details("tv", int(tmdb_id))
        if not tv:
            return {}
        overview = getattr(tv, "overview", None) or ""
        name = getattr(tv, "name", None) or ""
        return {
            "description": overview if overview and not _is_mostly_cjk(overview) else "",
            "title_english": name if name and not _is_mostly_cjk(name) else "",
        }
    except Exception as e:
        LOGGER.debug(f"[KITSU] TMDB series english failed: {e}")
        return {}


async def _tvdb_series_english(tvdb_id: int) -> dict:
    try:
        from Backend.helper.metadata.providers import tvdb as tvdb_mod
        if not tvdb_mod.tvdb_api_key():
            return {}
        series = await tvdb_mod.series_details(int(tvdb_id))
        if not series:
            return {}
        overview = (series.get("overview") or "").strip()
        name = (series.get("name") or "").strip()
        return {
            "description": overview if overview and not _is_mostly_cjk(overview) else "",
            "title_english": name if name and not _is_mostly_cjk(name) else "",
        }
    except Exception as e:
        LOGGER.debug(f"[KITSU] TVDB series english failed: {e}")
        return {}


def _merge_field(current: str, incoming: str) -> str:
    """Keep current if already good English; else take incoming if better."""
    if not _needs_english(current):
        return current
    if incoming and not _needs_english(incoming):
        return incoming
    if not current and incoming:
        return incoming
    return current or incoming or ""


async def _resolve_episode_meta(
    payload: dict,
    *,
    anizip_ep: dict,
    season_number: int,
    episode_number: int,
    absolute: bool,
    absolute_hint,
    tvdb_id,
    tmdb_id,
) -> dict:
    """Per-field cascade for episode details.

    Order per field:
      1) ani.zip (English)
      2) TVDB (English, cached)
      3) TMDB (English, cached)
      4) ani.zip any language / generic fallback
    """
    # 1) ani.zip English
    az = _anizip_episode_fields(anizip_ep or {})
    title = az.get("episode_title") or ""
    overview = az.get("episode_overview") or ""
    backdrop = az.get("episode_backdrop") or ""
    released = az.get("episode_released") or ""
    rating = az.get("episode_rating")

    need_title = _needs_english(title)
    need_overview = _needs_english(overview)
    need_backdrop = not bool(backdrop)
    need_rating = rating is None

    # 2) TVDB only for missing fields
    if (need_title or need_overview or need_backdrop) and tvdb_id:
        try:
            tvdb_id_int = int(tvdb_id)
        except (TypeError, ValueError):
            tvdb_id_int = None
        if tvdb_id_int:
            tv = await _tvdb_episode_fields(
                tvdb_id_int, season_number, episode_number, absolute_hint=absolute_hint
            )
            if tv.get("season_number") is not None and absolute:
                season_number = int(tv["season_number"])
                payload["season_number"] = season_number
            if tv.get("episode_number") is not None and absolute:
                episode_number = int(tv["episode_number"])
                payload["episode_number"] = episode_number
            title = _merge_field(title, tv.get("episode_title") or "")
            overview = _merge_field(overview, tv.get("episode_overview") or "")
            if need_backdrop and tv.get("episode_backdrop"):
                backdrop = tv["episode_backdrop"]
                need_backdrop = False
            if need_rating and tv.get("episode_rating") is not None:
                rating = tv["episode_rating"]
                need_rating = False
            released = released or tv.get("episode_released") or ""
            need_title = _needs_english(title)
            need_overview = _needs_english(overview)

    # 3) TMDB only for still-missing fields
    if (need_title or need_overview or need_backdrop) and tmdb_id:
        try:
            tmdb_id_int = int(tmdb_id)
        except (TypeError, ValueError):
            tmdb_id_int = None
        if tmdb_id_int and season_number is not None:
            tm = await _tmdb_episode_fields(tmdb_id_int, int(season_number), episode_number)
            title = _merge_field(title, tm.get("episode_title") or "")
            overview = _merge_field(overview, tm.get("episode_overview") or "")
            if need_backdrop and tm.get("episode_backdrop"):
                backdrop = tm["episode_backdrop"]
                need_backdrop = False
            if need_rating and tm.get("episode_rating") is not None:
                rating = tm["episode_rating"]
                need_rating = False
            released = released or tm.get("episode_released") or ""
            need_title = _needs_english(title)
            need_overview = _needs_english(overview)

    # 4) Last resort: ani.zip any language (ja etc.)
    if need_title:
        title = _pick_english_text((anizip_ep or {}).get("title"), allow_fallback=True) or title
    if need_overview:
        title_ov = _pick_english_text(
            (anizip_ep or {}).get("overview"),
            (anizip_ep or {}).get("summary"),
            allow_fallback=True,
        )
        overview = overview or title_ov or ""

    if not title:
        title = _episode_title_fallback(season_number, episode_number, absolute=absolute)

    # Last visual fallback: series backdrop/poster if episode still has no still
    if not backdrop:
        backdrop = (payload.get("backdrop") or payload.get("poster") or "") or ""

    payload["episode_title"] = title
    payload["episode_overview"] = overview or ""
    payload["episode_backdrop"] = backdrop or ""
    payload["episode_released"] = released or payload.get("episode_released") or ""
    if rating is not None:
        try:
            payload["episode_rating"] = float(rating)
        except (TypeError, ValueError):
            pass
    return payload


async def _resolve_series_description(payload: dict, tvdb_id, tmdb_id) -> dict:
    """Series description: Kitsu EN first (already in payload), then TVDB, then TMDB, then keep JA."""
    desc = payload.get("description") or ""
    if not _needs_english(desc):
        return payload

    if tvdb_id:
        try:
            tv = await _tvdb_series_english(int(tvdb_id))
            if tv.get("description"):
                payload["description"] = tv["description"]
                desc = payload["description"]
            if tv.get("title_english") and _is_mostly_cjk(payload.get("title") or ""):
                payload["title"] = tv["title_english"]
                payload["title_english"] = tv["title_english"]
        except Exception:
            pass

    if _needs_english(desc) and tmdb_id:
        try:
            tm = await _tmdb_series_english(int(tmdb_id))
            if tm.get("description"):
                payload["description"] = tm["description"]
            if tm.get("title_english"):
                payload["title_english"] = tm["title_english"]
                if _is_mostly_cjk(payload.get("title") or ""):
                    payload["title"] = tm["title_english"]
        except Exception:
            pass
    return payload


def _tvdb_art_url(path: str) -> str:
    if not path:
        return ""
    p = str(path)
    if p.startswith("http"):
        return p
    return f"https://artworks.thetvdb.com{p}" if p.startswith("/") else f"https://artworks.thetvdb.com/{p}"


def _pick_tvdb_artwork(artworks: list, type_ids: set) -> str:
    for art in artworks or []:
        try:
            if int(art.get("type") or 0) in type_ids and art.get("image"):
                return _tvdb_art_url(art["image"])
        except (TypeError, ValueError):
            continue
    return ""


async def _resolve_series_art(payload: dict, tvdb_id, tmdb_id) -> dict:
    """Poster / backdrop / logo fallbacks: Kitsu/ani.zip -> TVDB -> TMDB.

    Only fills fields that are still empty. All remote calls are cached in providers.
    """
    need_poster = not (payload.get("poster") or "").strip()
    need_backdrop = not (payload.get("backdrop") or "").strip()
    need_logo = not (payload.get("logo") or "").strip()
    need_rate = not payload.get("rate")

    if not (need_poster or need_backdrop or need_logo or need_rate):
        return payload

    # TVDB series
    if tvdb_id and (need_poster or need_backdrop or need_logo or need_rate):
        try:
            from Backend.helper.metadata.providers import tvdb as tvdb_mod
            if tvdb_mod.tvdb_api_key():
                series = await tvdb_mod.series_details(int(tvdb_id))
                if series:
                    artworks = series.get("artworks") or []
                    if need_poster:
                        poster = (
                            _pick_tvdb_artwork(artworks, {2, 14, 27})
                            or _tvdb_art_url(series.get("image") or "")
                        )
                        if poster:
                            payload["poster"] = poster
                            need_poster = False
                    if need_backdrop:
                        backdrop = (
                            _pick_tvdb_artwork(artworks, {3, 15, 19})
                            or _tvdb_art_url(series.get("background") or series.get("fanart") or "")
                        )
                        if backdrop:
                            payload["backdrop"] = backdrop
                            need_backdrop = False
                    if need_logo:
                        logo = _pick_tvdb_artwork(artworks, {25, 23})
                        if logo:
                            payload["logo"] = logo
                            need_logo = False
                    if need_rate:
                        site = series.get("siteRating") or series.get("rating")
                        if site is not None:
                            try:
                                payload["rate"] = float(site)
                                need_rate = False
                            except (TypeError, ValueError):
                                pass
                    payload.setdefault("tvdb_id", int(tvdb_id))
        except Exception as e:
            LOGGER.debug(f"[KITSU] TVDB series art failed: {e}")

    # TMDB series
    if tmdb_id and (need_poster or need_backdrop or need_logo or need_rate):
        try:
            from Backend.helper.metadata.providers import tmdb as tmdb_mod
            from Backend.helper.metadata.common import format_tmdb_image
            if tmdb_mod.tmdb_api_key():
                tv = await tmdb_mod.details("tv", int(tmdb_id))
                if tv:
                    if need_poster and getattr(tv, "poster_path", None):
                        payload["poster"] = format_tmdb_image(tv.poster_path)
                        need_poster = False
                    if need_backdrop and getattr(tv, "backdrop_path", None):
                        payload["backdrop"] = format_tmdb_image(tv.backdrop_path, "original")
                        need_backdrop = False
                    if need_logo:
                        logo = tmdb_mod.get_tmdb_logo(getattr(tv, "images", None))
                        if logo:
                            payload["logo"] = logo
                            need_logo = False
                    if need_rate and getattr(tv, "vote_average", None):
                        try:
                            payload["rate"] = float(tv.vote_average)
                            need_rate = False
                        except (TypeError, ValueError):
                            pass
                    payload.setdefault("tmdb_id", int(tmdb_id))
        except Exception as e:
            LOGGER.debug(f"[KITSU] TMDB series art failed: {e}")

    return payload


async def _enrich_from_tvdb(
    payload: dict,
    tvdb_id: int,
    season_number: int,
    episode_number: int,
    *,
    absolute_hint: int | None = None,
) -> dict:
    """Thin wrapper: series art is handled by _resolve_series_art."""
    return await _resolve_series_art(payload, tvdb_id, payload.get("tmdb_id"))


def _find_anizip_episode(episodes: dict, season, episode, absolute: bool) -> dict:
    """Locate the ani.zip episode entry for an absolute or S/E number.

    When absolute=True, prefer entries that carry absoluteEpisodeNumber so we
    get the rich TVDB-style seasonNumber/episodeNumber instead of the sparse
    AniDB-only keys that often collide with the same numeric string.
    """
    if not episodes:
        return {}
    ep_num = int(episode)

    # When looking up by absolute number, prefer the entry that actually
    # carries absoluteEpisodeNumber (these are the ones with season/episode).
    if absolute or season is None:
        for candidate in episodes.values():
            try:
                if int(candidate.get("absoluteEpisodeNumber") or -1) == ep_num:
                    return candidate
            except (TypeError, ValueError):
                continue

    # 1) Direct key match (AniDB-style sequential / absolute key)
    if str(ep_num) in episodes:
        return episodes[str(ep_num)] or {}

    # 2) Match absoluteEpisodeNumber (also useful when not in absolute mode)
    for candidate in episodes.values():
        try:
            if int(candidate.get("absoluteEpisodeNumber") or -1) == ep_num:
                return candidate
        except (TypeError, ValueError):
            continue

    # 3) Match episode / episodeNumber fields
    for key, candidate in episodes.items():
        try:
            cand_ep = candidate.get("episodeNumber") if candidate.get("episodeNumber") is not None else candidate.get("episode")
            if cand_ep is not None and int(cand_ep) == ep_num:
                if absolute or season is None:
                    # S00E13 is not absolute episode 13
                    if _is_anizip_special(key, candidate):
                        continue
                    return candidate
                cand_s = candidate.get("seasonNumber") if candidate.get("seasonNumber") is not None else candidate.get("season")
                if cand_s is not None and int(cand_s) == int(season):
                    return candidate
        except (TypeError, ValueError):
            continue

    # 4) Season + relative episode when both known
    if season is not None and not absolute:
        for candidate in episodes.values():
            try:
                cand_s = candidate.get("seasonNumber") if candidate.get("seasonNumber") is not None else candidate.get("season")
                cand_ep = candidate.get("episodeNumber") if candidate.get("episodeNumber") is not None else candidate.get("episode")
                if cand_s is not None and cand_ep is not None:
                    if int(cand_s) == int(season) and int(cand_ep) == ep_num:
                        return candidate
            except (TypeError, ValueError):
                continue
    return {}

def _anizip_regular_seasons(episodes: dict) -> set:
    """TVDB season numbers carried by the regular (non-special) entries."""
    seasons = set()
    for key, cand in (episodes or {}).items():
        if not cand or _is_anizip_special(key, cand):
            continue
        try:
            seasons.add(int(cand.get("seasonNumber")))
        except (TypeError, ValueError):
            continue
    return seasons


def _find_anizip_season_episode(episodes: dict, season, episode) -> dict:
    """The regular entry that is exactly TVDB season `season`, episode `episode` ({} if none)."""
    for key, cand in (episodes or {}).items():
        if not cand or _is_anizip_special(key, cand):
            continue
        try:
            if int(cand.get("seasonNumber")) == int(season) and int(cand.get("episodeNumber")) == int(episode):
                return cand
        except (TypeError, ValueError):
            continue
    return {}


async def _find_absolute_in_later_seasons(anidb_id, absolute: int, first_season_count: int) -> dict:
    """Episode entry for an absolute number that runs past season 1 when each
    season is a separate entry ("Show - 27" being season 2 episode 1).

    Absolute numbers in file names count regular episodes only, so the seasons
    are walked in order and their regular episodes counted. {} when the number
    is still in season 1, a season on the way is unknown, or the number is past
    the last one.
    """
    from Backend.helper.metadata.episode_maps import later_season_entries

    before = int(first_season_count)
    for entry in await later_season_entries(anidb_id):
        doc = await get_anizip_mappings(anidb_id=entry.anidb_id) or {}
        episodes = doc.get("episodes") or {}
        regular = sorted(
            (int(k), v) for k, v in episodes.items()
            if v and str(k).isdigit() and not _is_anizip_special(k, v)
        )
        if not regular:
            return {}
        if int(absolute) > before + len(regular):
            before += len(regular)
            continue
        cand = regular[int(absolute) - before - 1][1]
        try:
            season, number = int(cand.get("seasonNumber")), int(cand.get("episodeNumber"))
        except (TypeError, ValueError):
            return {}
        return cand if season >= 2 and number >= 1 else {}
    return {}


async def _find_later_season_episode(title: str, season: int, episode: int, base_kitsu_id, base_ids: dict) -> dict:
    """Episode entry for season >= 2 when Kitsu keeps each season as a
    separate anime ("Hell's Paradise", "Hell's Paradise Season 2").

    The season's own entry is used only if ani.zip maps it to the same
    TVDB/TMDB show as the base entry.
    """
    base_tvdb = base_ids.get("tvdb_id")
    base_tmdb = base_ids.get("tmdb_id")
    if not base_tvdb and not base_tmdb:
        return {}
    cache_key = f"kitsu::season-episode::{base_kitsu_id}::{season}"

    async def _produce():
        seen = {str(base_kitsu_id)}
        by_season = []
        for query in _season_queries(title, season)[:2]:
            for row in await _kitsu_search(query) or []:
                rid = str(row.get("id"))
                attrs = row.get("attributes") or {}
                if rid in seen or (attrs.get("subtype") or "").lower() in ("movie", "music"):
                    continue
                seen.add(rid)
                doc = await get_anizip_mappings(row.get("id")) or {}
                ids = _ids_from_anizip(doc)
                same_show = (base_tvdb and str(ids.get("tvdb_id")) == str(base_tvdb)) or (
                    base_tmdb and str(ids.get("tmdb_id")) == str(base_tmdb)
                )
                if not same_show:
                    continue
                entries = doc.get("episodes") or {}
                if int(season) in _anizip_regular_seasons(entries):
                    # A season can be split over several entries (two cours).
                    by_season.append({"kitsu_id": row.get("id"), "episodes": entries})
        return {"by_season": by_season}

    try:
        hit = await cached_call(KITSU_CACHE, cache_key, "kitsu_season_episode", _produce)
    except Exception as e:
        LOGGER.debug(f"[KITSU] season entry lookup failed for '{title}' S{season}: {e}")
        return {}
    if not hit:
        return {}

    found, source = {}, None
    for entry in hit["by_season"]:
        found = _find_anizip_season_episode(entry["episodes"], season, episode)
        if found:
            source = entry["kitsu_id"]
            break
    if not found:
        # ani.zip can list part of a season as specials. Fall back to the
        # Nth entry of a season that starts at episode 1.
        for entry in hit["by_season"]:
            first = entry["episodes"].get("1") or {}
            aligned = str(first.get("seasonNumber")) == str(int(season)) and str(first.get("episodeNumber")) == "1"
            cand = entry["episodes"].get(str(int(episode))) or {}
            if aligned and cand:
                found, source = cand, entry["kitsu_id"]
                break
    if found:
        LOGGER.info(
            f"[KITSU] '{title}' S{int(season):02d}E{int(episode):02d}: episode data from "
            f"season entry kitsu={source}"
        )
    return found


def _is_anizip_special(key, ep: dict) -> bool:
    if str(key).upper().startswith("S"):
        return True
    try:
        return int((ep or {}).get("seasonNumber")) == 0
    except (TypeError, ValueError):
        return False


def _find_anizip_special(
    episodes: dict,
    episode,
    title_hint: Optional[str] = None,
    watch_after: Optional[dict] = None,
    release_counter: bool = False,
) -> tuple:
    """Find a season 0 episode in ani.zip data. Returns (entry, how).

    Plain numeric keys are regular episodes and are never used here. Tried in
    order:
      1. the episode title from the file name (releases and providers number
         specials differently)
      2. for an "N.5" file, the special that aired after episode N
      3. the season 0 number from the file name

    release_counter is set for names like "Show.SP1", where the number counts
    the specials of the release. When that number lands on a short clip or on
    nothing, the n-th full-length special is used instead.
    """
    from Backend.helper.metadata.parse import episode_titles_match

    specials = {k: v for k, v in (episodes or {}).items() if v and _is_anizip_special(k, v)}
    if not specials:
        return {}, None

    def _title_score(cand) -> Optional[float]:
        """Best match of the file's episode title against the entry's titles (None: nothing to compare)."""
        titles = cand.get("title")
        names = [n for n in (titles.values() if isinstance(titles, dict) else [titles]) if n]
        if not title_hint or not names:
            return None
        return max(episode_titles_match(title_hint, name) for name in names)

    if title_hint:
        best, best_score = None, 0.0
        for cand in specials.values():
            score = _title_score(cand) or 0.0
            if score > best_score:
                best, best_score = cand, score
        if best is not None and best_score >= 0.85:
            return best, "title"

    numbered = True
    if watch_after:
        numbered = bool(watch_after.get("numbered"))
        try:
            position = int(watch_after.get("episode")) + 1
        except (TypeError, ValueError):
            position = None
        if position is not None and not numbered:
            hits = []
            for cand in specials.values():
                try:
                    if int(cand.get("absoluteEpisodeNumber") or -1) == position:
                        hits.append(cand)
                except (TypeError, ValueError):
                    continue
            if len(hits) == 1:
                return hits[0], "position"

    if numbered and episode is not None:
        ep_num = int(episode)
        found = None
        for cand in specials.values():
            try:
                if int(cand.get("seasonNumber")) == 0 and int(cand.get("episodeNumber")) == ep_num:
                    found = cand
                    break
            except (TypeError, ValueError):
                continue
        # No TVDB numbering at all: keys are S1, S2, ...
        if found is None and not any(c.get("seasonNumber") is not None for c in specials.values()):
            found = specials.get(f"S{ep_num}")
        if release_counter and (found is None or _is_short_special(found)):
            full_length = _full_length_specials(specials)
            if len(full_length) >= ep_num >= 1:
                return full_length[ep_num - 1], "release counter"
        if found:
            # Same number, different title: not this special.
            score = _title_score(found)
            if score is not None and score < 0.5:
                return {}, "number-title-mismatch"
            return found, "number"
    return {}, None


# Minutes below which a special counts as a short clip
SHORT_SPECIAL_MINUTES = 10


def _special_minutes(entry: dict) -> Optional[float]:
    for key in ("runtime", "length"):
        try:
            value = float(entry.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _is_short_special(entry: dict) -> bool:
    minutes = _special_minutes(entry)
    return minutes is not None and minutes < SHORT_SPECIAL_MINUTES


def _has_special_slot(entry: dict) -> bool:
    """True when the entry carries a season 0 episode number."""
    try:
        return int(entry.get("seasonNumber")) == 0 and int(entry.get("episodeNumber")) >= 1
    except (TypeError, ValueError):
        return False


def _full_length_specials(specials: dict) -> list:
    """Specials of episode length, in the order of their "S<n>" keys.

    When the show's specials carry season 0 numbers, an extra without one
    (a promotional or web-only video) has no slot to be stored in and is
    left out.
    """
    def order(item):
        digits = "".join(ch for ch in str(item[0]) if ch.isdigit())
        return int(digits) if digits else 10 ** 6

    slots_known = any(_has_special_slot(entry) for entry in specials.values())
    out = []
    for key, entry in sorted(specials.items(), key=order):
        minutes = _special_minutes(entry)
        if not (str(key).upper().startswith("S") and minutes is not None and minutes >= SHORT_SPECIAL_MINUTES):
            continue
        if slots_known and not _has_special_slot(entry):
            continue
        out.append(entry)
    return out


def _tvdb_special_entries(episodes: dict) -> dict:
    """Entries that carry a TVDB season 0 number."""
    out = {}
    for key, cand in (episodes or {}).items():
        try:
            if cand and int(cand.get("seasonNumber")) == 0 and int(cand.get("episodeNumber")) >= 0:
                out[str(key)] = cand
        except (TypeError, ValueError):
            continue
    return out


def _label_in_titles(label: str, names) -> bool:
    """True when every word of the label occurs in one of the names."""
    words = set(_normalize(label).split())
    return bool(words) and any(words <= set(_normalize(n).split()) for n in names if n)


async def _find_bundled_special(episodes: dict, anidb_id, absolute: int, regular_count: int, label: str) -> tuple:
    """Find the special a release numbered as regular episode `absolute`. Returns (entry, how).

    Disc releases append the OVA to the season ("Show - 13 (SNS OVA)" after a
    12-episode season). Tried in order:
      1. a special of the matched entry whose title is the label
      2. the series' OVA / special entry named like the label
      3. the OVA / special episode that TVDB gives this absolute number
    """
    from Backend.helper.metadata.episode_maps import special_entries_for

    if label:
        found, how = _find_anizip_special(_tvdb_special_entries(episodes), None, title_hint=label)
        if found and how == "title":
            return found, "title"

    position = absolute - regular_count
    by_label, by_number = [], []
    for sibling in (await special_entries_for(anidb_id))[:8]:
        doc = await get_anizip_mappings(anidb_id=sibling.anidb_id) or {}
        specials = _tvdb_special_entries(doc.get("episodes"))
        if not specials:
            continue
        for cand in specials.values():
            try:
                if int(cand.get("absoluteEpisodeNumber") or -1) == absolute:
                    by_number.append(cand)
            except (TypeError, ValueError):
                continue
        names = [sibling.name] + list((doc.get("titles") or {}).values())
        if label and _label_in_titles(label, names):
            cand = specials.get(str(position)) or (next(iter(specials.values())) if len(specials) == 1 else None)
            if cand:
                by_label.append(cand)
    if len(by_label) == 1:
        return by_label[0], "label"
    if len(by_number) == 1:
        return by_number[0], "absolute number"
    return {}, None


def _ids_from_anizip(doc: dict) -> dict:
    """Extract cross-db IDs from ani.zip mappings block."""
    mappings = (doc or {}).get("mappings") or {}
    out = {}
    for key, dest in (
        ("anidb_id", "anidb_id"),
        ("anilist_id", "anilist_id"),
        ("mal_id", "mal_id"),
        ("thetvdb_id", "tvdb_id"),
        ("themoviedb_id", "tmdb_id"),
        ("imdb_id", "imdb_id"),
    ):
        val = mappings.get(key)
        if val is None:
            continue
        if key == "imdb_id":
            out[dest] = str(val).strip() or None
            continue
        try:
            out[dest] = int(val)
        except (TypeError, ValueError):
            continue
    return out

async def fetch_anime_tv(
    title: str,
    season,
    episode: int,
    encoded_string,
    year=None,
    quality=None,
    absolute: bool = False,
    episode_title: Optional[str] = None,
    watch_after: Optional[dict] = None,
    special_label: Optional[str] = None,
    release_counter: bool = False,
) -> dict | None:
    """Resolve an episode through Kitsu and ani.zip.

    episode_title and watch_after are only used for season 0: the episode
    title from the file name and, for "N.5" files, the episode it follows.
    special_label is set (possibly empty) when an absolute-numbered name is
    marked as an OVA or special. release_counter is set when a season 0
    number counts the release's own specials ("Show.SP1").
    """
    is_abs = bool(absolute)
    row = await search_anime(title, movie=False, year=year)
    if not row:
        return None

    kitsu_id = row.get("id")
    doc = await get_anizip_mappings(kitsu_id) if kitsu_id else {}
    episodes = (doc or {}).get("episodes") or {}
    extra_ids = _ids_from_anizip(doc)

    is_special = (not is_abs) and season is not None and int(season) == 0
    special_how = None
    # Set when other providers should be tried first (see resolve_anime_tv)
    weak_reason = None
    if not any(extra_ids.get(k) for k in ("tvdb_id", "tmdb_id", "imdb_id")) and not episodes:
        weak_reason = "Kitsu entry has no TVDB/TMDB/IMDb mapping and no episode list"
    if is_special:
        ep, special_how = _find_anizip_special(episodes, episode, episode_title, watch_after, release_counter)
    elif not is_abs and season is not None and int(season) >= 2:
        # Numeric keys count from the start of this entry, which may or may
        # not be the requested season.
        seasons = _anizip_regular_seasons(episodes)
        if int(season) in seasons:
            # One entry covering several seasons
            ep = _find_anizip_season_episode(episodes, season, episode)
        elif seasons == {1}:
            # Entry is season 1 only: look for the later season's own entry
            ep = await _find_later_season_episode(title, int(season), int(episode), kitsu_id, extra_ids)
            if not ep:
                weak_reason = f"Kitsu has no season {int(season)} for this show"
        else:
            ep = _find_anizip_episode(episodes, season, episode, absolute=False)
    else:
        ep = _find_anizip_episode(episodes, season, episode, absolute=is_abs)

    use_season = season
    use_episode = int(episode)

    # Numeric keys are the regular episodes; specials, credits, trailers and
    # other extras carry a letter prefix.
    regular_count = len(
        [k for k, v in episodes.items() if v and str(k).isdigit() and not _is_anizip_special(k, v)]
    )
    if is_abs and regular_count and int(episode) > regular_count:
        # A special numbered after the last regular episode belongs in season 0.
        bundled, how = {}, None
        if special_label is not None:
            bundled, how = await _find_bundled_special(
                episodes, extra_ids.get("anidb_id"), int(episode), regular_count, special_label,
            )
        if not bundled and _tvdb_special_entries({"ep": ep}):
            # TVDB counts this special as the next absolute episode. Without
            # an OVA marker in the name that is only safe when the series has
            # no later season the number could belong to.
            from Backend.helper.metadata.episode_maps import has_later_season

            try:
                same_number = int(ep.get("absoluteEpisodeNumber") or -1) == int(episode)
            except (TypeError, ValueError):
                same_number = False
            if same_number and (
                special_label is not None or not await has_later_season(extra_ids.get("anidb_id"))
            ):
                bundled, how = ep, "absolute number"
        if bundled:
            LOGGER.info(
                f"[KITSU] '{title}' E{int(episode)} is a special numbered after the season "
                f"(matched by {how}) -> S00E{int(bundled['episodeNumber']):02d}"
            )
            ep, special_how = bundled, how
            is_abs, is_special = False, True
            use_season, use_episode = 0, int(bundled["episodeNumber"])

    if is_special and ep:
        # The provider's number for the special wins over the file's.
        try:
            if int(ep.get("seasonNumber")) == 0 and ep.get("episodeNumber") is not None:
                provider_num = int(ep["episodeNumber"])
                if provider_num != use_episode:
                    LOGGER.info(
                        f"[KITSU] special '{title}' S00E{use_episode:02d} matched by {special_how} "
                        f"-> provider S00E{provider_num:02d}"
                    )
                use_episode = provider_num
        except (TypeError, ValueError):
            pass

    if is_special and not _tvdb_special_entries({"ep": ep}):
        # The matched entry is the OVA itself: Anime-Lists knows its TVDB slot.
        from Backend.helper.metadata.episode_maps import special_entry_slot

        own_count = len([k for k, v in episodes.items() if v and not str(k).upper().startswith("S")])
        slot = await special_entry_slot(extra_ids.get("anidb_id"), int(episode), own_count)
        if slot is not None:
            if slot != use_episode:
                LOGGER.info(f"[KITSU] special '{title}' S00E{use_episode:02d} -> TVDB S00E{slot:02d} (Anime-Lists)")
            use_episode, special_how = slot, "anime-lists"

    if is_abs:
        if ep.get("seasonNumber") is not None:
            try:
                use_season = int(ep["seasonNumber"])
            except (TypeError, ValueError):
                pass
        if ep.get("episodeNumber") is not None:
            try:
                use_episode = int(ep["episodeNumber"])
            except (TypeError, ValueError):
                pass
        elif ep.get("absoluteEpisodeNumber") is not None:
            try:
                use_episode = int(ep["absoluteEpisodeNumber"])
            except (TypeError, ValueError):
                pass

    # ani.zip sometimes sets seasonNumber but leaves episodeNumber equal to the
    # absolute number (e.g. Naruto abs 36 -> S02E36 instead of S02E01). Treat
    # that as unmapped so Anime-Lists / anibridge can apply the real offset.
    anizip_looks_absolute = False
    if is_abs and ep:
        try:
            rel = ep.get("episodeNumber")
            abs_n = ep.get("absoluteEpisodeNumber")
            if rel is not None:
                rel_i = int(rel)
                if rel_i == int(episode):
                    anizip_looks_absolute = True
                elif abs_n is not None and rel_i == int(abs_n):
                    anizip_looks_absolute = True
            elif ep.get("seasonNumber") is not None and abs_n is not None:
                # season present but no relative episode, still suspect
                anizip_looks_absolute = True
        except (TypeError, ValueError):
            pass

    needs_fallback = is_abs and (
        not ep
        or ep.get("seasonNumber") is None
        or anizip_looks_absolute
        or (
            ep.get("absoluteEpisodeNumber") is None
            and ep.get("seasonNumber") is None
        )
    )

    # Always try episode_maps on absolute episodes when we have provider IDs.
    # Prefer a real mapped S/E (with offset) over ani.zip when they disagree,
    # Anime-Lists is authoritative for shows like Naruto / One Piece.
    map_hit = None
    if is_abs:
        try:
            from Backend.helper.metadata.episode_maps import resolve_absolute_episode

            map_hit = await resolve_absolute_episode(
                int(episode),
                anidb_id=extra_ids.get("anidb_id"),
                anilist_id=extra_ids.get("anilist_id"),
                mal_id=extra_ids.get("mal_id"),
            )
        except Exception as e:
            LOGGER.debug(f"[KITSU] episode_maps resolve failed: {e}")
            map_hit = None

    if map_hit and (
        needs_fallback
        or (
            map_hit.get("season_number") is not None
            and map_hit.get("episode_number") is not None
            and not map_hit.get("tvdb_absolute")
            and (
                use_season is None
                or int(use_episode) == int(episode)
                or int(map_hit["season_number"]) != int(use_season or 0)
                or int(map_hit["episode_number"]) != int(use_episode)
            )
        )
    ):
        if map_hit.get("season_number") is not None:
            use_season = int(map_hit["season_number"])
        if map_hit.get("episode_number") is not None:
            use_episode = int(map_hit["episode_number"])
        for key in ("tvdb_id", "tmdb_id", "imdb_id"):
            if map_hit.get(key) and not extra_ids.get(key):
                extra_ids[key] = map_hit[key]
        if map_hit.get("tvdb_absolute") and map_hit.get("tvdb_id"):
            extra_ids["tvdb_id"] = map_hit["tvdb_id"]
        LOGGER.info(
            f"[KITSU] episode_maps override for '{title}' abs={episode} "
            f"-> S{use_season}E{use_episode} (source={map_hit.get('source')})"
        )
    elif map_hit and map_hit.get("tvdb_absolute") and map_hit.get("tvdb_id"):
        # Absolute-on-TVDB: keep absolute episode number, surface tvdb id
        for key in ("tvdb_id", "tmdb_id", "imdb_id"):
            if map_hit.get(key) and not extra_ids.get(key):
                extra_ids[key] = map_hit[key]
        extra_ids["tvdb_id"] = map_hit["tvdb_id"]

    if (
        is_abs
        and use_season in (None, 1)
        and int(use_episode) == int(episode)
        and regular_count
        and int(episode) > regular_count
    ):
        # The number runs past this entry's last episode and nothing mapped
        # it: it may count through the later seasons, each its own entry.
        later = await _find_absolute_in_later_seasons(extra_ids.get("anidb_id"), int(episode), regular_count)
        if later:
            ep = later
            use_season, use_episode = int(later["seasonNumber"]), int(later["episodeNumber"])
            LOGGER.info(
                f"[KITSU] '{title}' abs={episode} -> S{use_season}E{use_episode} (later season entry)"
            )

    season_number = int(use_season) if use_season is not None else 1
    episode_number = int(use_episode)

    if is_abs and (not ep or ep.get("seasonNumber") is None) and season_number == 1 and episode_number == int(episode):
        LOGGER.info(
            f"[KITSU] Absolute episode {episode} not fully mapped for '{title}' "
            f"(kitsu={kitsu_id}) — indexing with S{season_number}E{episode_number}"
        )

    # A number that counts inside an OVA entry is not the series' absolute number.
    series_absolute = is_abs and season_number != 0

    attrs = row.get("attributes") or {}
    display_title = (
        (attrs.get("titles") or {}).get("en")
        or (attrs.get("titles") or {}).get("en_jp")
        or attrs.get("canonicalTitle")
        or title
    )
    own_title = display_title
    show_row, show_doc = row, doc or {}
    if season_number == 0:
        # An OVA with its own entry is stored under its series, so the show
        # keeps the series' name and poster whichever file is indexed first.
        parent_row, parent_doc = await _parent_series(extra_ids.get("anidb_id"))
        if parent_row:
            show_row, show_doc = parent_row, parent_doc
            parent_attrs = parent_row.get("attributes") or {}
            display_title = (
                (parent_attrs.get("titles") or {}).get("en")
                or (parent_attrs.get("titles") or {}).get("en_jp")
                or parent_attrs.get("canonicalTitle")
                or display_title
            )
    payload = _common_payload(show_row, show_doc, display_title)
    for key, val in extra_ids.items():
        if val and not payload.get(key):
            payload[key] = val

    payload.update({
        "media_type": "tv",
        "season_number": season_number,
        "episode_number": episode_number,
        "quality": quality,
        "encoded_string": encoded_string,
        "absolute_episode": int(episode) if series_absolute else None,
    })
    if is_special:
        payload["special_match"] = special_how
    if weak_reason:
        payload["weak_match"] = weak_reason

    # Series description: Kitsu EN -> TVDB -> TMDB -> JA
    payload = await _resolve_series_description(
        payload,
        payload.get("tvdb_id") or extra_ids.get("tvdb_id"),
        payload.get("tmdb_id") or extra_ids.get("tmdb_id"),
    )

    # Series art / rating: Kitsu -> ani.zip (already in payload) -> TVDB -> TMDB
    tvdb_id = payload.get("tvdb_id") or extra_ids.get("tvdb_id")
    tmdb_id = payload.get("tmdb_id") or extra_ids.get("tmdb_id")
    try:
        payload = await _resolve_series_art(payload, tvdb_id, tmdb_id)
    except Exception as e:
        LOGGER.debug(f"[KITSU] series art skip: {e}")

    # Episode fields: ani.zip EN -> TVDB -> TMDB -> JA (per field, cached APIs)
    abs_hint = int(episode) if series_absolute else None
    payload = await _resolve_episode_meta(
        payload,
        anizip_ep=ep or {},
        # Season 0 is a valid value here
        season_number=int(season_number if payload.get("season_number") is None else payload["season_number"]),
        episode_number=int(payload.get("episode_number") or episode_number),
        absolute=series_absolute,
        absolute_hint=abs_hint,
        tvdb_id=payload.get("tvdb_id") or extra_ids.get("tvdb_id"),
        tmdb_id=payload.get("tmdb_id") or extra_ids.get("tmdb_id"),
    )
    if special_how == "anime-lists" and re.fullmatch(r"S\d+E\d+", payload.get("episode_title") or ""):
        # No provider title for the slot: the OVA's own name says more.
        payload["episode_title"] = own_title

    return payload


async def fetch_anime_movie(title, encoded_string, year=None, quality=None) -> Optional[dict]:
    row, score = await _search_scored(title, movie=True)
    if score < KITSU_MOVIE_CONFIDENT:
        # TV films and one-off releases are often typed special, OVA or ONA
        # on Kitsu and never show up in a movie-only search.
        other, other_score = await _search_scored(title, film_like=True)
        if other is not None and other_score > score:
            row = other
    if not row:
        LOGGER.info(f"[KITSU] No movie match for '{title}'")
        return None

    try:
        kitsu_id = int(row["id"])
    except (TypeError, ValueError, KeyError):
        return None

    doc = await get_anizip_mappings(kitsu_id) or {}
    payload = _common_payload(row, doc, title)
    payload.update({"media_type": "movie", "quality": quality, "encoded_string": encoded_string})
    return await _confirm_movie_tmdb_id(payload)


async def _confirm_movie_tmdb_id(payload: dict) -> dict:
    """Make sure a film carries its own TMDB id.

    ani.zip sometimes gives a film the TMDB id of its parent series. The IMDb
    id is reliable, so Cinemeta is asked for the matching TMDB movie id.
    """
    imdb_id = payload.get("imdb_id")
    if not (isinstance(imdb_id, str) and imdb_id.startswith("tt")):
        return payload
    try:
        from Backend.helper.metadata.providers import cinemeta

        detail = await cinemeta.cached_detail(imdb_id, "movie")
        movie_id = int((detail or {}).get("moviedb_id") or 0)
    except Exception as exc:
        LOGGER.debug(f"[KITSU] Cinemeta id check failed for {imdb_id}: {exc}")
        return payload
    if movie_id and movie_id != payload.get("tmdb_id"):
        LOGGER.info(
            f"[KITSU] {imdb_id}: TMDB id {payload.get('tmdb_id')} is not the film's; using {movie_id}"
        )
        payload["tmdb_id"] = movie_id
    return payload
