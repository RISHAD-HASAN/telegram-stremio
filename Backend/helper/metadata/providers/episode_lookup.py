"""Find the show an episode belongs to from the episode title.

For uploads named only "S01E01 - The Green Seat.mkv". The metadata providers
cannot search by episode title, but Wikipedia's "List of <show> episodes"
pages can be searched without a key:

    intitle:episodes "The Green Seat"  ->  "List of Mushishi episodes"

The result is a list of candidates. The caller resolves each one through the
provider chain and keeps it only if that provider's title for the same season
and episode matches the file name.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import List, Optional

import httpx

from Backend.helper.metadata.common import _LRUDict, cached_call
from Backend.logger import LOGGER

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
_HEADERS = {"User-Agent": "Telegram-Stremio (+https://github.com/RISHAD-HASAN/telegram-stremio) episode-title lookup"}
# Stay well inside Wikipedia's rate limit.
_MIN_INTERVAL = 1.0

_CACHE: dict = _LRUDict(maxsize=1000)
_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_rate_lock = asyncio.Lock()
_last_request = 0.0


async def _get_client() -> httpx.AsyncClient:
    global _client
    async with _client_lock:
        if _client is None or _client.is_closed:
            _client = httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=_HEADERS)
        return _client


def show_name_from_page_title(page_title: str) -> Optional[str]:
    """Show name from a page title: "List of Mushishi episodes" -> "Mushishi"."""
    name = (page_title or "").strip()
    if not name:
        return None
    name = re.sub(r"(?i)^list of\s+", "", name)
    name = re.sub(r"(?i)\s+episodes?\b.*$", "", name)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    name = re.sub(r"(?i)[\s:,-]+(?:season|series|part|cour)\s+\d+\s*$", "", name)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip(" -:,")
    return name or None


async def _search(query: str, limit: int) -> List[str]:
    global _last_request
    client = await _get_client()
    for attempt in range(3):
        async with _rate_lock:
            wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_request = time.monotonic()
            resp = await client.get(WIKIPEDIA_API, params={
                "action": "query", "list": "search", "srsearch": query,
                "srlimit": limit, "srprop": "", "format": "json",
            })
        if resp.status_code == 200:
            hits = ((resp.json() or {}).get("query") or {}).get("search") or []
            return [h.get("title") for h in hits if h.get("title")]
        if resp.status_code in (429, 503):
            await asyncio.sleep(2.0 * (attempt + 1))
            continue
        break
    return []


async def show_candidates_for_episode_title(episode_title: str, limit: int = 4) -> List[str]:
    """Candidate show names for an episode title, best first."""
    title = re.sub(r'["\s]+', " ", episode_title or "").strip()
    if len(title) < 4:
        return []
    cache_key = f"wiki-episode::{title.lower()}::{limit}"

    async def _produce():
        try:
            pages = await _search(f'intitle:episodes "{title}"', limit + 2)
        except Exception as e:
            LOGGER.warning(f"[EPISODE-LOOKUP] Wikipedia search failed for '{title}': {e}")
            return []
        names: List[str] = []
        # Episode list pages first
        ordered = [p for p in pages if re.search(r"(?i)\bepisodes?\b", p)] + \
                  [p for p in pages if not re.search(r"(?i)\bepisodes?\b", p)]
        for page in ordered:
            name = show_name_from_page_title(page)
            if name and name.lower() not in (n.lower() for n in names):
                names.append(name)
        return names[:limit]

    return await cached_call(_CACHE, cache_key, "wiki-episode", _produce)
