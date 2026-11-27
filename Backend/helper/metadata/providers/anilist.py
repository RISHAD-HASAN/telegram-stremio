"""AniList & MyAnimeList metadata provider for anime enrichment."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from Backend.helper.metadata.common import _LRUDict
from Backend.logger import LOGGER

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_rate_lock = asyncio.Lock()
_last_request_time: float = 0.0
_RATE_LIMIT_DELAY = 0.8  # Max ~75 requests/min (AniList limit is 90/min)

_SEARCH_CACHE: dict = _LRUDict(maxsize=1000)
_ID_CACHE: dict = _LRUDict(maxsize=1000)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Telegram-Stremio (+https://github.com/RISHAD-HASAN/telegram-stremio)",
}

ANILIST_SEARCH_QUERY = """
query ($search: String, $year: Int) {
  Page(page: 1, perPage: 10) {
    media(search: $search, seasonYear: $year, type: ANIME, sort: SEARCH_MATCH) {
      id
      idMal
      title {
        romaji
        english
        native
      }
      format
      status
      episodes
      duration
      season
      seasonYear
      averageScore
      meanScore
      popularity
      bannerImage
      coverImage {
        extraLarge
        large
        medium
      }
      studios(isMain: true) {
        nodes {
          id
          name
        }
      }
      genres
      synopsis: description(asHtml: false)
      siteUrl
    }
  }
}
"""

ANILIST_GET_BY_ID_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    idMal
    title {
      romaji
      english
      native
    }
    format
    status
    episodes
    duration
    season
    seasonYear
    averageScore
    meanScore
    popularity
    bannerImage
    coverImage {
      extraLarge
      large
      medium
    }
    studios(isMain: true) {
      nodes {
        id
        name
      }
    }
    genres
    synopsis: description(asHtml: false)
    siteUrl
  }
}
"""

ANILIST_RELATIONS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    relations {
      edges {
        relationType
        node {
          id
          idMal
          title {
            romaji
            english
            native
          }
          type
          format
          status
          episodes
          seasonYear
        }
      }
    }
  }
}
"""


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_HEADERS)
        return _client


async def _rate_limited_post(payload: dict) -> Optional[dict]:
    """Execute GraphQL POST with rate limit throttling and 429 backoff."""
    global _last_request_time
    client = await _get_client()

    for attempt in range(3):
        async with _rate_lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            if elapsed < _RATE_LIMIT_DELAY:
                await asyncio.sleep(_RATE_LIMIT_DELAY - elapsed)
            _last_request_time = time.monotonic()

        try:
            resp = await client.post(ANILIST_GRAPHQL_URL, json=payload)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after_hdr = resp.headers.get("Retry-After")
                retry_seconds = float(retry_after_hdr) if retry_after_hdr else (2.0 + attempt * 2.0)
                LOGGER.debug(f"[AniList] Rate limited (429). Pausing for {retry_seconds:.1f}s before retry...")
                await asyncio.sleep(retry_seconds)
                continue

            LOGGER.debug(f"[AniList] Request returned status {resp.status_code}: {resp.text[:120]}")
            return None

        except httpx.RequestError as e:
            LOGGER.debug(f"[AniList] Network error on attempt {attempt + 1}: {e}")
            await asyncio.sleep(1.0)

    return None


def _format_media(item: Dict[str, Any]) -> Dict[str, Any]:
    if not item:
        return {}

    title_obj = item.get("title") or {}
    romaji = title_obj.get("romaji") or ""
    english = title_obj.get("english") or ""
    native = title_obj.get("native") or ""

    primary_title = english or romaji or native

    studios_nodes = (item.get("studios") or {}).get("nodes") or []
    studio_name = studios_nodes[0].get("name") if studios_nodes else ""

    cover_obj = item.get("coverImage") or {}
    poster = cover_obj.get("extraLarge") or cover_obj.get("large") or cover_obj.get("medium") or ""
    backdrop = item.get("bannerImage") or ""

    avg_score = item.get("averageScore")
    anilist_score = f"{avg_score}%" if avg_score is not None else None
    mal_score = round(avg_score / 10.0, 1) if avg_score is not None else None

    raw_status = item.get("status") or ""
    status_map = {
        "FINISHED": "Finished Airing",
        "RELEASING": "Currently Airing",
        "NOT_YET_RELEASED": "Not Yet Aired",
        "CANCELLED": "Cancelled",
        "HIATUS": "On Hiatus",
    }
    status = status_map.get(raw_status, raw_status.title() if raw_status else "")

    synopsis = item.get("synopsis") or ""
    if synopsis:
        synopsis = re.sub(r"<[^>]+>", "", synopsis).strip()

    return {
        "anilist_id": item.get("id"),
        "mal_id": item.get("idMal"),
        "title": primary_title,
        "romaji_title": romaji,
        "english_title": english,
        "native_title": native,
        "studio": studio_name,
        "status": status,
        "format": item.get("format"),
        "episodes": item.get("episodes"),
        "duration_min": item.get("duration"),
        "season_year": item.get("seasonYear"),
        "anilist_score": anilist_score,
        "anilist_score_raw": avg_score,
        "mal_score": mal_score,
        "poster": poster,
        "backdrop": backdrop,
        "genres": item.get("genres") or [],
        "description": synopsis,
        "site_url": item.get("siteUrl") or f"https://anilist.co/anime/{item.get('id')}",
        "mal_url": f"https://myanimelist.net/anime/{item.get('idMal')}" if item.get("idMal") else None,
    }


async def search_anilist(query: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Search AniList GraphQL by title and optional release year with caching & rate limiting."""
    if not query or not query.strip():
        return []

    cleaned_query = re.sub(r"\[.*?\]|\(.*?\)", "", query).strip()
    cleaned_query = re.sub(r"Season \d+|\bS\d+\b", "", cleaned_query, flags=re.IGNORECASE).strip()
    if not cleaned_query:
        cleaned_query = query.strip()

    cache_key = f"{cleaned_query.lower()}::{year or ''}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]

    variables: Dict[str, Any] = {"search": cleaned_query}
    if year:
        variables["year"] = int(year)

    payload = {"query": ANILIST_SEARCH_QUERY, "variables": variables}
    data = await _rate_limited_post(payload)
    if not data:
        _SEARCH_CACHE[cache_key] = []
        return []

    media_list = data.get("data", {}).get("Page", {}).get("media", [])
    results = [_format_media(item) for item in media_list if item]
    _SEARCH_CACHE[cache_key] = results
    return results


async def get_anilist_by_id(anilist_id: int) -> Optional[Dict[str, Any]]:
    """Fetch anime metadata from AniList by AniList ID with caching & rate limiting."""
    if not anilist_id:
        return None

    if anilist_id in _ID_CACHE:
        return _ID_CACHE[anilist_id]

    payload = {"query": ANILIST_GET_BY_ID_QUERY, "variables": {"id": int(anilist_id)}}
    data = await _rate_limited_post(payload)
    if not data:
        return None

    media = data.get("data", {}).get("Media")
    if media:
        res = _format_media(media)
        _ID_CACHE[anilist_id] = res
        return res
    return None


async def get_anilist_relations(anilist_id: int) -> Optional[List[Dict[str, Any]]]:
    """Fetch AniList relations (prequel, sequel, side story, etc.) for an anime.

    Returns a list of dicts like:
        {
            "relation_type": "SEQUEL",   # PREQUEL, SEQUEL, SIDE_STORY, etc.
            "anilist_id": 12345,
            "mal_id": 67890,
            "title": "English title or romaji",
            "title_romaji": "...",
            "title_native": "...",
            "type": "TV",
            "format": "TV Series",
            "status": "Finished Airing",
            "episodes": 24,
            "season_year": 2024,
        }
    Returns None if no relations data or on error.
    Unknown stays unknown: returns None, not an empty list, when AniList has no relations.
    """
    if not anilist_id:
        return None

    cache_key = f"relations::{anilist_id}"
    if cache_key in _ID_CACHE:
        cached = _ID_CACHE[cache_key]
        return cached if cached is not None else None

    payload = {"query": ANILIST_RELATIONS_QUERY, "variables": {"id": int(anilist_id)}}
    data = await _rate_limited_post(payload)
    if not data:
        _ID_CACHE[cache_key] = None
        return None

    media = data.get("data", {}).get("Media")
    if not media:
        _ID_CACHE[cache_key] = None
        return None

    relations = media.get("relations") or {}
    edges = relations.get("edges") or []

    results: List[Dict[str, Any]] = []
    for edge in edges or []:
        node = edge.get("node") or {}
        title_obj = node.get("title") or {}
        results.append({
            "relation_type": edge.get("relationType"),
            "anilist_id": node.get("id"),
            "mal_id": node.get("idMal"),
            "title": title_obj.get("english") or title_obj.get("romaji") or title_obj.get("native") or "",
            "title_romaji": title_obj.get("romaji"),
            "title_native": title_obj.get("native"),
            "type": node.get("type"),
            "format": node.get("format"),
            "status": node.get("status"),
            "episodes": node.get("episodes"),
            "season_year": node.get("seasonYear"),
        })

    _ID_CACHE[cache_key] = results if results else None
    return results
