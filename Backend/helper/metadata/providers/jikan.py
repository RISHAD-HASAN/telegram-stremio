"""Jikan v4 (MyAnimeList) provider for episode-level filler/recap enrichment.

This provider is intentionally SEPARATE from the metadata resolver chain.
It does NOT participate in title-to-ID resolution. It is purely additive:
after a show has been resolved by Kitsu/TVDB/TMDB/Cinemeta, Jikan is
queried for per-episode filler/recap flags so those can be stored as
optional enrichment fields on the indexed document.

Unknown stays unknown: if Jikan has no data for an episode, is_filler is
None (not False), and filler_type is None (not "canon").
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import httpx

LOGGER = logging.getLogger("TelegramStremio.Jikan")

JIKAN_BASE_URL = "https://api.jikan.moe/v4"

_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_rate_lock = asyncio.Lock()
_last_request_time: float = 0.0
# Jikan unauthenticated limit: 30 req/min, 2 req/sec. Keep 0.6s between calls
# to stay safely under the per-second cap while allowing burst headroom.
_RATE_LIMIT_DELAY = 0.6


class _LRUDict(OrderedDict):
    """Memory-bounded dictionary that evicts oldest entries to prevent RAM creep."""

    def __init__(self, maxsize: int = 1000, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if len(self) > self._maxsize:
            try:
                first_key = next(iter(self))
                self.pop(first_key, None)
            except (StopIteration, RuntimeError):
                pass


_SEARCH_CACHE: dict = _LRUDict(maxsize=1000)
_EPISODE_CACHE: dict = _LRUDict(maxsize=1000)  # mal_id -> {ep_num: {filler, recap}}

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Telegram-Stremio (+https://github.com/RISHAD-HASAN/telegram-stremio)",
}


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=_HEADERS)
        return _client


async def _rate_limited_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """Execute GET with rate limit throttling and 429 backoff."""
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
            resp = await client.get(f"{JIKAN_BASE_URL}{path}", params=params or {})

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after_hdr = resp.headers.get("Retry-After")
                retry_seconds = float(retry_after_hdr) if retry_after_hdr else (2.0 + attempt * 2.0)
                LOGGER.debug(f"[Jikan] Rate limited (429). Pausing for {retry_seconds:.1f}s before retry...")
                await asyncio.sleep(retry_seconds)
                continue

            LOGGER.debug(f"[Jikan] Request returned status {resp.status_code}: {resp.text[:120]}")
            return None

        except httpx.RequestError as e:
            LOGGER.debug(f"[Jikan] Network error on attempt {attempt + 1}: {e}")
            await asyncio.sleep(1.0)

    return None


def _clean_query(query: str) -> str:
    """Strip common release noise so Jikan search matches better."""
    if not query:
        return ""
    q = re.sub(r"\\[.*?\\]|\\(.*?\\)", "", query)          # remove brackets/parens
    q = re.sub(r"\b(?:1080p|720p|480p|2160p|4K|HD|BD|BluRay|WEB|x264|x265|HEVC|AAC|FLAC|Dual|Multi)\b", " ", q, flags=re.I)
    q = re.sub(r"\bS\d{1,2}E\d{1,3}\b", " ", q, flags=re.I)  # drop SxxExx
    q = re.sub(r"\b\d{1,2}x\d{1,3}\b", " ", q, flags=re.I)   # drop NxNN
    q = re.sub(r"\b(?:19|20)\d{2}\b", " ", q)                 # drop years
    q = re.sub(r"\s+", " ", q).strip()
    return q


async def search_anime(query: str) -> List[Dict[str, Any]]:
    """Search Jikan for anime by title. Returns list of {mal_id, title, ...}."""
    if not query or not query.strip():
        return []

    cleaned = _clean_query(query)
    if not cleaned:
        cleaned = query.strip()

    cache_key = f"search::{cleaned.lower()}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]

    data = await _rate_limited_get("/anime", {"q": cleaned, "limit": 10})
    if not data:
        _SEARCH_CACHE[cache_key] = []
        return []

    results: List[Dict[str, Any]] = []
    for item in data.get("data", []) or []:
        results.append({
            "mal_id": item.get("mal_id"),
            "title": item.get("title") or "",
            "title_english": item.get("title_english"),
            "title_japanese": item.get("title_japanese"),
            "type": item.get("type"),          # TV, Movie, OVA, Special, ONA
            "episodes": item.get("episodes"),
            "status": item.get("status"),
            "score": item.get("score"),
            "year": item.get("year"),
            "season": item.get("season"),
            "url": item.get("url"),
            "images": item.get("images") or {},
        })

    _SEARCH_CACHE[cache_key] = results
    return results


async def get_anime_by_id(mal_id: int) -> Optional[Dict[str, Any]]:
    """Fetch anime details by MAL ID."""
    if not mal_id:
        return None

    cache_key = f"anime::{mal_id}"
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]

    data = await _rate_limited_get(f"/anime/{mal_id}")
    if not data:
        return None

    item = data.get("data")
    if not item:
        return None

    res = {
        "mal_id": item.get("mal_id"),
        "title": item.get("title") or "",
        "title_english": item.get("title_english"),
        "title_japanese": item.get("title_japanese"),
        "type": item.get("type"),
        "episodes": item.get("episodes"),
        "status": item.get("status"),
        "score": item.get("score"),
        "year": item.get("year"),
        "season": item.get("season"),
        "synopsis": item.get("synopsis"),
        "url": item.get("url"),
        "images": item.get("images") or {},
    }
    _SEARCH_CACHE[cache_key] = res
    return res


async def get_episode_filler(mal_id: int, episode: int) -> Optional[Dict[str, Any]]:
    """Return filler/recap info for a specific episode, or None if unknown.

    Returns:
        None                      -> Jikan has no data for this episode
        {"filler": False, "recap": False}   -> canon episode
        {"filler": True, "recap": False}    -> filler episode
        {"filler": False, "recap": True}    -> recap episode
        {"filler": True, "recap": True}     -> filler recap (rare)
    """
    if not mal_id or not episode:
        return None

    cache_key = f"ep::{mal_id}::{episode}"
    if cache_key in _EPISODE_CACHE:
        return _EPISODE_CACHE[cache_key]

    # Jikan paginates 30 episodes per page. Fetch pages until we find the
    # target episode or run out of pages.
    page = 1
    found: Optional[Dict[str, Any]] = None
    while page <= 10:  # safety cap; most anime have < 300 episodes
        data = await _rate_limited_get(f"/anime/{mal_id}/episodes", {"page": page, "limit": 30})
        if not data:
            break

        episodes = data.get("data", []) or []
        if not episodes:
            break

        for ep in episodes:
            try:
                if int(ep.get("mal_id", 0)) == episode or int(ep.get("episode", 0)) == episode:
                    found = {
                        "filler": bool(ep.get("filler", False)),
                        "recap": bool(ep.get("recap", False)),
                        "title": ep.get("title"),
                        "title_japanese": ep.get("title_japanese"),
                        "aired": ep.get("aired"),
                        "duration": ep.get("duration"),
                    }
                    break
            except (TypeError, ValueError):
                continue

        if found:
            break

        # Check if there's a next page
        pagination = data.get("pagination", {}) or {}
        if not pagination.get("has_next_page"):
            break
        page += 1

    _EPISODE_CACHE[cache_key] = found
    return found


def classify_filler(filler_info: Optional[Dict[str, Any]]) -> Optional[str]:
    """Classify filler info into a human-readable type.

    Returns:
        None        -> unknown (no data)
        "canon"     -> not filler, not recap
        "filler"    -> filler episode
        "recap"     -> recap episode
        "filler_recap" -> both (rare)
    """
    if filler_info is None:
        return None
    if not filler_info:  # empty dict -> no flags -> canon
        return "canon"
    if filler_info.get("filler") and filler_info.get("recap"):
        return "filler_recap"
    if filler_info.get("filler"):
        return "filler"
    if filler_info.get("recap"):
        return "recap"
    return "canon"
