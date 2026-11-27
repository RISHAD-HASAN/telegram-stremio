"""
Resolve batches of episodes from Telegram message ranges or Nyaa.si torrent
manifests and index them.

A batch can be given as a Telegram message link range plus TMDb/IMDb IDs, or as
a Nyaa torrent manifest. Either way the flow is the same: work out the show
title, pull canonical episode names from TMDb / TVMaze / Kitsu / AniList, line
the Telegram messages up with them, drop bonus tracks (NCED/NCOP), caption the
files and write the results to MongoDB.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any, Dict, Optional, Tuple

import aiohttp
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait

from Backend.logger import LOGGER
from Backend.helper.manual_add import extract_tg_channel_and_msg
from Backend.helper.pyro import is_media, clean_filename, get_readable_file_size
from Backend.helper.metadata import metadata
from Backend.helper.metadata.entry import search_tv_candidates, search_movie_candidates


_NYAA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CLEAN_TAGS_RE = re.compile(
    r"\[(?:EMBER|SubsPlease|Erai-raws|HorribleSubs|Judas|ASW|Golumpa|Nep|DameDesuYo|Kametsu|Vivid|Tsundere|Bandi|SSA|Beatrice-Raws|NanDesuKa|Yameii|SmugCat|Edge|Kitsune|Anime Time|CTR|DB|Moozzi2|KH|o7|Raizel)[^\]]*\]|"
    r"\[(?:1080p|720p|2160p|480p|4K|BDRip|BDRemux|WEBRip|WEB-DL|HDTV|DVDRip|BluRay)[^\]]*\]|"
    r"\[(?:Dual[\s-]Audio|Multi[\s-]Audio|HEVC|x265|x264|AVC|10[\s-]?bits?|8[\s-]?bits?|AAC|FLAC|DDP|AC3|Opus|EAC3)[^\]]*\]|"
    r"\((?:Batch|Complete|Season\s*\d+|S\d+|1080p|720p|2160p|\d{4})\)",
    re.IGNORECASE,
)

ANIME_SEQUEL_MAP = {
    "aragoto": 2,
    "shippuden": 2,
    "brotherhood": 1,
    "root a": 2,
    "re:": 3,
    "diamond is unbreakable": 3,
    "golden wind": 4,
    "stone ocean": 5,
    "stardust crusaders": 2,
    "after story": 2,
    "alicia": 2,
    "alicization": 3,
    "war of underworld": 4,
    "entertainment district": 2,
    "swordsmith village": 3,
    "hashira training": 4,
    "shibuya incident": 2,
    "final season": 4,
}

_EXTRA_KEYWORDS_RE = re.compile(
    r"(?i)\b(?:NCED|NCOP|Clean[\s_-]*OP|Clean[\s_-]*ED|Creditless|OP[\s_-]*Credits|ED[\s_-]*Credits|Menu|Special[\s_-]*Preview|PV|Teaser|Bonus|OST|Soundtrack|Theme\s*Song|Scans?|Booklet)\b"
)


def extract_nyaa_id(url_or_id: str) -> Optional[str]:
    if not url_or_id:
        return None
    s = str(url_or_id).strip()
    if s.isdigit():
        return s
    m = re.search(r"nyaa\.si/view/(\d+)", s)
    if m:
        return m.group(1)
    m2 = re.search(r"/view/(\d+)", s)
    if m2:
        return m2.group(1)
    return None


def clean_nyaa_title_parts(raw_title: str) -> Tuple[str, str, str, int, Optional[int], str]:
    """
    Parses a raw Nyaa torrent title into:
    (group, clean_title, alt_title, season, year, quality_str)
    """
    group_m = re.search(r"^\s*\[([^\]]+)\]", raw_title)
    group = group_m.group(1).strip() if group_m else ""

    all_brackets = re.findall(r"\[([^\]]+)\]", raw_title)
    quality_tags = [b.strip() for b in all_brackets if b.strip().lower() != group.lower()]
    quality_str = " ".join(quality_tags) if quality_tags else "1080p"

    text = re.sub(r"\[[^\]]*\]", " ", raw_title)

    year_m = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    year = int(year_m.group(1)) if year_m else None
    if year:
        text = re.sub(r"\(?\b" + str(year) + r"\b\)?", " ", text)

    multi_s_m = re.search(r"\b(?:Seasons?|S)\s*0?(\d{1,2})\s*(?:[-+~&]|to|and)\s*(?:Seasons?|S)?\s*0?(\d{1,2})\b", text, re.IGNORECASE)
    if multi_s_m:
        season = int(multi_s_m.group(1))
        text = re.sub(r"\b(?:Seasons?|S)\s*0?\d{1,2}\s*(?:[-+~&]|to|and)\s*(?:Seasons?|S)?\s*0?\d{1,2}\b", " ", text, flags=re.IGNORECASE)
    else:
        season_m = re.search(r"\b(?:Season|S)\s*0?(\d{1,2})\b", text, re.IGNORECASE)
        season = int(season_m.group(1)) if season_m else 1
        text = re.sub(r"\b(?:Season|S)\s*0?\d{1,2}\b", " ", text, flags=re.IGNORECASE)

    alt_m = re.search(r"\(([^)]+)\)", text)
    alt_title = alt_m.group(1).strip() if alt_m else ""
    if alt_title.lower() in ["batch", "complete", "uncensored", "remastered", "dual audio", "dual-audio", "hevc", "bdrip", "webrip"]:
        alt_title = ""

    text = re.sub(r"\([^)]*\)", " ", text)
    clean_title = re.sub(r"[\s\-_+&~]+", " ", text).strip(" -_+&~")

    return group, clean_title, alt_title, season, year, quality_str


_TITLE_UNITS_RE = re.compile(
    r"(?i)\b\d+\s*(?:centimeters?|cm|days?|toubun|nin|hours?|minutes?|percent|%|leaf|clover|bullets?|princes?|kingdoms?|wars?|deadly\s+sins?|tails?|witches|kanojo|gatsu|round|seconds?)\b"
)
_CRC32_RE = re.compile(r"(?i)[\[\(][0-9a-f]{8}[\]\)]|\b[0-9a-f]{8}\b")
_TRUNCATED_HASH_RE = re.compile(r"(?i)[\[\(][0-9a-f]{1,8}(?:\.[a-z0-9]{2,4})?$|[\[\(][0-9a-f]{1,8}$")


def parse_episode_from_filename(fn_clean: str, default_season: int = 1) -> Tuple[int, Optional[Any], str]:
    """
    Extracts (season, episode, ep_title) from a torrent or telegram filename.
    Properly filters out NCED/NCOP bonus tracks, cleans up titles, and handles decimal specials.
    """
    # 0. NCED / NCOP / OP / ED / Extras -> Extras (Season 0, None episode)
    if _EXTRA_KEYWORDS_RE.search(fn_clean):
        return 0, None, "Extra / Bonus"

    detected_season = default_season

    # 1. Explicit SxxExx
    ep_m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3}(?:\.\d+)?)", fn_clean)
    if ep_m:
        s_num = int(ep_m.group(1))
        e_raw = ep_m.group(2)
        e_num = float(e_raw) if "." in e_raw else int(e_raw)
        title_part = re.sub(r"^[Ss]\d{1,2}[Ee]\d{1,3}(?:\.\d+)?\s*[-_]?\s*", "", fn_clean)
        title_part = re.sub(r"\[[0-9A-Fa-f]{8}\]|\.[a-zA-Z0-9]{2,4}$", "", title_part).strip(" -_")
        return s_num, e_num, _clean_episode_title_string(title_part)

    # 2. Check for explicit Season tag in filename (e.g. S2, Season 2, Part 2)
    s_tag = re.search(r"(?i)\b(?:Season|S|Part|Cour)\s*0?(\d{1,2})\b", fn_clean)
    if s_tag:
        try:
            detected_season = int(s_tag.group(1))
        except (ValueError, TypeError):
            pass

    # 3. Check for ordinal season keywords (2nd Season, Second Season, 3rd Season...)
    ord_m = re.search(r"(?i)\b(2nd|3rd|4th|5th|6th|second|third|fourth|fifth)\s*season\b", fn_clean)
    if ord_m:
        ord_map = {"2nd": 2, "second": 2, "3rd": 3, "third": 3, "4th": 4, "fourth": 4, "5th": 5, "fifth": 5}
        matched_ord = ord_m.group(1).lower()
        if matched_ord in ord_map:
            detected_season = max(detected_season, ord_map[matched_ord])

    # 4. Check known anime subtitle sequel keywords (Aragoto -> Season 2, etc.)
    fn_lower = fn_clean.lower()
    for kw, season_val in ANIME_SEQUEL_MAP.items():
        if kw in fn_lower:
            detected_season = max(detected_season, season_val)
            break

    # Extract clean text from filename
    cleaned = fn_clean
    cleaned = re.sub(r"\.[a-zA-Z0-9]{2,4}$", " ", cleaned)
    cleaned = _CRC32_RE.sub(" ", cleaned)
    cleaned = _TRUNCATED_HASH_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\(\[][^\)\]]*$", " ", cleaned)
    cleaned = re.sub(r"^\s*\[[^\]]+\]|\s*\([^\)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[[^\]]*\]", " ", cleaned)
    cleaned = re.sub(r"\([^\)]*\)", " ", cleaned)
    cleaned = re.sub(r"(?i)\b(?:1080p|720p|2160p|480p|x264|x265|hevc|av1|10bit|8bit|flac|aac|ddp|bluray|bdrip|webrip|dual[_\s]audio|multi[_\s]subs?|\d+(?:\.\d+)?fps)\b", " ", cleaned)
    cleaned = re.sub(r"(?:^|[\s._\-(])((?:19|20)\d{2})(?:[\s._\-)]|$)", " ", cleaned)
    cleaned = re.sub(r"(?i)(?<=\d)v\d*(?=[_\s\.\-\)\]]|$)", " ", cleaned)
    cleaned = re.sub(r"(?i)\b3-gatsu\b", "gatsu", cleaned)
    cleaned = _TITLE_UNITS_RE.sub(" ", cleaned)

    cleaned = re.sub(r"(?<=\d)\.(?=\d)", "DOT", cleaned)
    cleaned = cleaned.replace("_", " ").replace(".", " ")
    cleaned = cleaned.replace("DOT", ".")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # 5. Check for 'S<season> - <ep>' or 'S<season> <ep>'
    s_ep_m = re.search(r"(?i)\bS0*(\d{1,2})\s*[-_.]?\s*0*(\d+(?:\.\d+)?)\b", cleaned)
    if s_ep_m:
        val = s_ep_m.group(2)
        ep = float(val) if "." in val else int(val)
        return int(s_ep_m.group(1)), ep, ""

    # 6. Check for decimal numbers e.g. 11.5, 12.5 after dash or separator
    dec_m = re.findall(r"[-–—~]\s*0*(\d+\.\d+)\b", cleaned)
    if dec_m:
        return detected_season, float(dec_m[-1]), "Special / Recap"

    # 7. Explicit E/EP/Episode prefix
    prefixed = re.findall(r"(?i)\b(?:e|ep|episode)\s*0*(\d+(?:\.\d+)?)\b", cleaned)
    if prefixed:
        val = prefixed[-1]
        ep = float(val) if "." in val else int(val)
        return detected_season, ep, ""

    # 8. Check for numbers after a dash or separator (e.g. 'Show - 12' or 'Show - 04 - Title')
    dash_eps = re.findall(r"[-–—~]\s*0*(\d+(?:\.\d+)?)\b", cleaned)
    if dash_eps:
        val = dash_eps[-1]
        ep = float(val) if "." in val else int(val)
        parts = re.split(r"[-–—~]\s*0*" + re.escape(str(val)) + r"\b", cleaned, maxsplit=1)
        ep_title = parts[1].strip(" -_") if len(parts) > 1 else ""
        return detected_season, ep, _clean_episode_title_string(ep_title)

    # 9. Trailing number at the end of the clean title
    end_m = re.search(r"\b0*(\d+(?:\.\d+)?)\s*$", cleaned)
    if end_m:
        val = end_m.group(1)
        ep = float(val) if "." in val else int(val)
        return detected_season, ep, ""

    # 10. Fallback: last number in cleaned string
    any_nums = re.findall(r"\b0*(\d+(?:\.\d+)?)\b", cleaned)
    if any_nums:
        val = any_nums[-1]
        ep = float(val) if "." in val else int(val)
        return detected_season, ep, ""

    return detected_season, None, ""


def _clean_episode_title_string(raw: str) -> str:
    """Clean up raw episode title strings, stripping extensions, hashes, and extra tags."""
    if not raw:
        return ""
    s = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", str(raw)).strip()
    s = _CRC32_RE.sub("", s)
    s = _TRUNCATED_HASH_RE.sub("", s)
    s = re.sub(r"\[[^\]]*\]|\([^\)]*\)", "", s)
    s = re.sub(r"(?i)\b(?:NCED|NCOP|Dual[\s-]Audio|1080p|720p|x264|x265|HEVC)\b", "", s)
    s = re.sub(r"[\s\-_]+", " ", s).strip(" -_")
    # If the clean title is just a number or empty, discard
    if not s or s.isdigit() or len(s) < 2:
        return ""
    return s.title() if s.isupper() else s


async def fetch_canonical_episodes(
    title: Optional[str] = None,
    tmdb_id: Optional[Any] = None,
    imdb_id: Optional[str] = None,
    season: int = 1,
) -> Dict[int, str]:
    """
    Fetch canonical episode titles from TMDb / TVMaze / Kitsu / AniList for a given show and season.
    Returns: {episode_number: episode_title}
    """
    episodes: Dict[int, str] = {}

    # 1. Try TMDb season details if client is available
    if tmdb_id and str(tmdb_id).isdigit():
        try:
            from Backend.helper.metadata.providers.tmdb import get_tmdb_client
            client = get_tmdb_client()
            s_det = await client.season(int(tmdb_id), season).details()
            if s_det and hasattr(s_det, "episodes"):
                for ep in s_det.episodes:
                    ep_n = getattr(ep, "episode_number", None)
                    ep_name = getattr(ep, "name", None)
                    if ep_n is not None and ep_name:
                        episodes[int(ep_n)] = str(ep_name).strip()
                if episodes:
                    return episodes
        except Exception as e:
            LOGGER.debug(f"[NyaaResolver] TMDb season fetch for {tmdb_id} s{season}: {e}")

    # 2. Try TVMaze (IMDb or Show Title)
    search_q = title or ""
    tvmaze_url = None
    if imdb_id and str(imdb_id).startswith("tt"):
        tvmaze_url = f"https://api.tvmaze.com/lookup/shows?imdb={imdb_id}"
    elif search_q:
        tvmaze_url = f"https://api.tvmaze.com/singlesearch/shows?q={search_q}&embed=episodes"

    if tvmaze_url:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(tvmaze_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json()
                        eps = data.get("_embedded", {}).get("episodes", [])
                        if not eps and "id" in data:
                            async with s.get(f"https://api.tvmaze.com/shows/{data['id']}/episodes") as er:
                                if er.status == 200:
                                    eps = await er.json()
                        for ep in eps:
                            s_n = ep.get("season") if ep.get("season") is not None else 1
                            e_n = ep.get("number")
                            e_name = ep.get("name")
                            if s_n == season and e_n and e_name:
                                episodes[int(e_n)] = str(e_name).strip()
                        if episodes:
                            return episodes
        except Exception as e:
            LOGGER.debug(f"[NyaaResolver] TVMaze fetch error: {e}")

    # 3. Try Kitsu API for Anime
    if search_q:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://kitsu.io/api/edge/anime?filter[text]={search_q}&page[limit]=1", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        kdata = await r.json()
                        if kdata.get("data"):
                            aid = kdata["data"][0]["id"]
                            async with s.get(f"https://kitsu.io/api/edge/anime/{aid}/episodes?page[limit]=100") as er:
                                if er.status == 200:
                                    edata = await er.json()
                                    for ep in edata.get("data", []):
                                        attrs = ep.get("attributes", {})
                                        num = attrs.get("number")
                                        name = attrs.get("canonicalTitle") or attrs.get("titles", {}).get("en_us") or attrs.get("titles", {}).get("en_jp")
                                        if num and name:
                                            episodes[int(num)] = str(name).strip()
        except Exception as e:
            LOGGER.debug(f"[NyaaResolver] Kitsu fetch error: {e}")

    return episodes


async def fetch_nyaa_manifest(url_or_id: str) -> Dict[str, Any]:
    nyaa_id = extract_nyaa_id(url_or_id)
    if not nyaa_id:
        raise ValueError(f"Invalid Nyaa URL or ID: '{url_or_id}'")

    url = f"https://nyaa.si/view/{nyaa_id}"
    async with aiohttp.ClientSession(headers=_NYAA_HEADERS) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise ValueError(f"Nyaa returned HTTP {resp.status} for URL {url}")
            page_html = await resp.text()

    title_match = re.search(r'<h3 class="panel-title">\s*([^<]+)\s*</h3>', page_html)
    raw_title = html.unescape(title_match.group(1).strip()) if title_match else f"Torrent {nyaa_id}"

    group, cleaned, alt_title, base_season, year, quality_str = clean_nyaa_title_parts(raw_title)

    file_matches = re.findall(
        r'<i class="fa fa-file[^"]*"></i>\s*([^<]+)<span class="file-size">\(([^)]+)\)</span>',
        page_html
    )
    if not file_matches:
        size_m = re.search(r'<th>File size:</th>\s*<td>([^<]+)</td>', page_html)
        file_size = size_m.group(1).strip() if size_m else "Unknown"
        file_matches = [(raw_title, file_size)]

    files = []
    current_season = base_season
    prev_ep = None

    for fn, sz in file_matches:
        fn_clean = html.unescape(fn.strip())
        sz_clean = sz.strip()

        # Check if file is a bonus/extra track
        if _EXTRA_KEYWORDS_RE.search(fn_clean):
            continue

        s_num, e_num, ep_title = parse_episode_from_filename(fn_clean, default_season=current_season)

        if e_num is not None and prev_ep is not None and e_num < prev_ep and e_num <= 3:
            if s_num <= current_season:
                s_num = current_season + 1

        if s_num > current_season:
            current_season = s_num

        prev_ep = e_num

        files.append({
            "file_name": fn_clean,
            "size": sz_clean,
            "season": s_num,
            "episode": e_num,
            "episode_title": ep_title,
        })

    return {
        "nyaa_id": nyaa_id,
        "nyaa_url": url,
        "raw_title": raw_title,
        "clean_title": cleaned,
        "alt_title": alt_title,
        "group": group,
        "season": base_season,
        "year": year,
        "quality_tags": quality_str,
        "file_count": len(files),
        "files": files,
    }


async def auto_resolve_metadata(title: str, alt_title: str = "", year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    from Backend.helper.metadata.providers.anilist import search_anilist

    candidates = []
    if title:
        candidates = await search_tv_candidates(title)
        if not candidates:
            candidates = await search_movie_candidates(title, year)

    if not candidates and alt_title:
        candidates = await search_tv_candidates(alt_title)
        if not candidates:
            candidates = await search_movie_candidates(alt_title, year)

    anilist_match = None
    try:
        al_res = await search_anilist(title or alt_title, year=year)
        if al_res:
            anilist_match = al_res[0]
    except Exception as e:
        LOGGER.debug(f"[NyaaResolver] AniList search error: {e}")

    if not candidates:
        if anilist_match:
            return {
                "tmdb_id": None,
                "imdb_id": None,
                "anilist_id": anilist_match.get("anilist_id"),
                "mal_id": anilist_match.get("mal_id"),
                "title": anilist_match.get("english_title") or anilist_match.get("title") or title,
                "year": anilist_match.get("season_year") or year,
                "media_type": "tv",
                "poster": anilist_match.get("poster", ""),
                "overview": anilist_match.get("description", ""),
            }
        return None

    best = candidates[0]
    tmdb_id = best.get("id") or best.get("tmdb_id")
    imdb_id = best.get("imdb_id")
    resolved_title = best.get("title") or best.get("name") or (anilist_match.get("english_title") if anilist_match else None) or title
    resolved_year = best.get("year") or (anilist_match.get("season_year") if anilist_match else None) or year
    poster = best.get("poster") or (anilist_match.get("poster") if anilist_match else "")

    return {
        "tmdb_id": tmdb_id,
        "imdb_id": imdb_id,
        "anilist_id": anilist_match.get("anilist_id") if anilist_match else None,
        "mal_id": anilist_match.get("mal_id") if anilist_match else None,
        "title": resolved_title,
        "year": resolved_year,
        "media_type": best.get("media_type") or "tv",
        "poster": poster,
        "overview": best.get("overview", ""),
    }


def _format_ep_tag(season: int, ep: Any) -> str:
    if ep is None:
        return f"S{season:02d}"
    if isinstance(ep, float) and ep % 1 != 0:
        int_part = int(ep)
        dec_part = str(ep).split(".")[1]
        return f"S{season:02d}E{int_part:02d}.{dec_part}"
    return f"S{season:02d}E{int(ep):02d}"


async def inspect_topic_range(
    client,
    channel_id: Any,
    start_msg_id: Any,
    end_msg_id: Any,
    nyaa_url_or_id: Optional[str] = None,
    override_tmdb_id: Optional[Any] = None,
    season_override: Optional[int] = None
) -> Dict[str, Any]:
    """
    Align Telegram message range with TMDb / AniList metadata and canonical episode titles.
    Nyaa URL is optional: if omitted, inspects Telegram files directly and pairs with TMDb.
    """
    ch_from_s, s_id = extract_tg_channel_and_msg(start_msg_id)
    ch_from_e, e_id = extract_tg_channel_and_msg(end_msg_id)
    start_msg_id = s_id or 0
    end_msg_id = e_id or start_msg_id
    if not channel_id:
        channel_id = ch_from_s or ch_from_e

    manifest = None
    if nyaa_url_or_id and str(nyaa_url_or_id).strip():
        try:
            manifest = await fetch_nyaa_manifest(str(nyaa_url_or_id).strip())
        except Exception as e:
            LOGGER.warning(f"[NyaaResolver] Nyaa fetch failed: {e}. Falling back to direct Telegram metadata resolution.")

    base_season = season_override if season_override is not None else (manifest.get("season") if manifest else 1) if manifest else 1

    # Fetch messages from Telegram channel first so we have sample filenames
    chat_ref = int(f"-100{str(channel_id).replace('-100', '')}") if str(channel_id).replace("-", "").isdigit() else channel_id
    msg_ids = list(range(min(start_msg_id, end_msg_id), max(start_msg_id, end_msg_id) + 1))

    messages = []
    chunk_size = 100
    for i in range(0, len(msg_ids), chunk_size):
        sub_ids = msg_ids[i:i + chunk_size]
        try:
            res = await client.get_messages(chat_ref, sub_ids)
            if isinstance(res, list):
                messages.extend(res)
            elif res:
                messages.append(res)
        except Exception as e:
            LOGGER.warning(f"[NyaaResolver] get_messages error for {sub_ids}: {e}")

    media_messages = []
    for m in messages:
        if not m or getattr(m, "empty", False):
            continue
        med = is_media(m)
        if med:
            media_messages.append(m)

    media_messages.sort(key=lambda m: m.id)

    if not media_messages:
        raise ValueError(f"No media messages found in Telegram channel {channel_id} between ID {start_msg_id} and {end_msg_id}.")

    # Extract sample show title and quality info from first Telegram file
    first_med = is_media(media_messages[0])
    first_fn = getattr(first_med, "file_name", "") or getattr(media_messages[0], "caption", "") or ""
    _, sample_clean_title, _, detected_s, sample_year, sample_quality = clean_nyaa_title_parts(first_fn)
    if detected_s and not season_override:
        base_season = detected_s

    # Resolve metadata
    search_title = (manifest.get("clean_title") if manifest else None) or sample_clean_title
    search_alt = (manifest.get("alt_title") if manifest else "")
    search_year = (manifest.get("year") if manifest else None) or sample_year

    meta = None
    if override_tmdb_id:
        val = str(override_tmdb_id).strip()
        if val.startswith("tt"):
            meta = {"imdb_id": val, "tmdb_id": None, "title": search_title, "media_type": "tv"}
        elif val.isdigit():
            meta = {"tmdb_id": int(val), "imdb_id": None, "title": search_title, "media_type": "tv"}
        else:
            meta = await auto_resolve_metadata(val, year=search_year)
            if not meta:
                meta = {"tmdb_id": None, "imdb_id": None, "title": val, "media_type": "tv"}

    if not meta:
        meta = await auto_resolve_metadata(search_title, search_alt, search_year)

    effective_id = (meta.get("tmdb_id") if meta else None) or (meta.get("imdb_id") if meta else None) or override_tmdb_id
    if not effective_id and not meta:
        raise ValueError(
            f"Could not automatically resolve metadata for '{search_title}'. "
            "Please enter a TMDb ID (e.g. 64710) in the TMDb field."
        )

    tmdb_id = meta.get("tmdb_id") if meta else (int(override_tmdb_id) if str(override_tmdb_id).isdigit() else None)
    imdb_id = meta.get("imdb_id") if meta else (str(override_tmdb_id) if str(override_tmdb_id).startswith("tt") else None)
    show_title = (meta.get("title") if meta else None) or search_title or "Anime"

    # Fetch canonical episode titles from TMDb / TVMaze / Kitsu
    canonical_episodes = await fetch_canonical_episodes(
        title=show_title,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        season=base_season,
    )

    id_tag = f"{{tmdb-{tmdb_id}}}" if tmdb_id else f"{{imdb-{imdb_id}}}"

    # Check if batch is Season 2+ using continuous absolute numbers (e.g. 23..44)
    raw_eps = []
    for m in media_messages:
        med = is_media(m)
        raw_name = getattr(med, "file_name", "") or getattr(m, "caption", "") or f"msg_{m.id}.mkv"
        _, ep_val, _ = parse_episode_from_filename(raw_name, default_season=base_season)
        if ep_val is not None:
            raw_eps.append(ep_val)

    abs_offset = 0
    if base_season > 1 and raw_eps and min(raw_eps) > 15:
        abs_offset = int(min(raw_eps)) - 1
        LOGGER.info(f"[NyaaResolver] Detected absolute episode offset {abs_offset} for Season {base_season} batch.")

    aligned_items = []
    current_season = base_season
    prev_ep = None
    nyaa_files = manifest.get("files", []) if manifest else []
    ep_map = {(f.get("season", 1), f.get("episode")): f for f in nyaa_files if f.get("episode") is not None}

    # Quality tags to display
    quality_tags = (manifest.get("quality_tags") if manifest else None) or sample_quality or "1080p"

    for idx, msg in enumerate(media_messages):
        med = is_media(msg)
        raw_name = getattr(med, "file_name", "") or getattr(msg, "caption", "") or f"msg_{msg.id}.mkv"
        raw_size = getattr(med, "file_size", 0)

        # Detect season & episode number from Telegram message filename / caption
        msg_season, ep_num, parsed_ep_title = parse_episode_from_filename(raw_name, default_season=current_season)

        # Extract message-specific quality tags if available (e.g. Dual-Audio, 1080p)
        # nyaa.si is authoritative: its quality takes priority over the Telegram
        # filename, which may carry a lower-quality tag from a suboptimal upload.
        msg_group, _, _, _, _, msg_quality = clean_nyaa_title_parts(raw_name)
        active_quality = quality_tags or msg_quality or "1080p"

        # Apply absolute numbering offset if applicable (e.g. 23 -> 1 for Season 2)
        if ep_num is not None and abs_offset > 0 and ep_num > abs_offset:
            ep_num = round(ep_num - abs_offset, 2) if isinstance(ep_num, float) else ep_num - abs_offset

        # Sequential reset detection across message list (e.g. Ep 12 -> Ep 01 indicates next season)
        if ep_num is not None and prev_ep is not None and ep_num < prev_ep and ep_num <= 3:
            if msg_season <= current_season:
                msg_season = current_season + 1

        if msg_season > current_season:
            current_season = msg_season

        # Fallback to sequential index if filename didn't have an episode number
        if ep_num is None:
            if idx < len(nyaa_files):
                ep_num = nyaa_files[idx].get("episode") or (idx + 1)
                msg_season = nyaa_files[idx].get("season") or current_season
            else:
                ep_num = idx + 1

        prev_ep = ep_num

        # Match official episode title (priority: Canonical TMDb/TVMaze/Kitsu -> Clean parsed title -> Nyaa)
        ep_title = ""
        if ep_num in canonical_episodes:
            ep_title = canonical_episodes[ep_num]
        elif parsed_ep_title and not _EXTRA_KEYWORDS_RE.search(parsed_ep_title):
            ep_title = parsed_ep_title
        elif ep_map.get((msg_season, ep_num)):
            nyaa_ep_t = ep_map[(msg_season, ep_num)].get("episode_title", "")
            if not _EXTRA_KEYWORDS_RE.search(nyaa_ep_t):
                ep_title = _clean_episode_title_string(nyaa_ep_t)

        ep_str = _format_ep_tag(msg_season, ep_num)

        # Build clean proposed caption
        tag_parts = [id_tag, show_title, ep_str]
        if ep_title:
            tag_parts.append(f"- {ep_title}")
        if active_quality:
            tag_parts.append(f"[{active_quality}]")

        proposed_caption = " ".join(tag_parts)

        aligned_items.append({
            "msg_id": msg.id,
            "original_filename": raw_name,
            "raw_size": raw_size,
            "readable_size": get_readable_file_size(raw_size),
            "season": msg_season,
            "episode": ep_num,
            "episode_title": ep_title,
            "quality": active_quality,
            "proposed_caption": proposed_caption,
            "matched_nyaa_file": ep_map.get((msg_season, ep_num), {}).get("file_name") if manifest else None,
            "status": "ready" if ep_num is not None else "ambiguous",
        })

    return {
        "status": "success",
        "manifest": manifest,
        "metadata": meta,
        "show_title": show_title,
        "season": base_season,
        "total_messages_scanned": len(messages),
        "media_found_count": len(media_messages),
        "aligned_count": len([x for x in aligned_items if x["status"] == "ready"]),
        "items": aligned_items,
    }


async def execute_topic_resolution(
    client,
    channel_id: Any,
    inspection_plan: Dict[str, Any],
    update_captions: bool = True,
    direct_index: bool = True,
) -> Dict[str, Any]:
    from Backend.pyrofork.plugins.receiver import file_queue

    chat_ref = int(f"-100{str(channel_id).replace('-100', '')}") if str(channel_id).replace("-", "").isdigit() else channel_id
    items = inspection_plan.get("items", [])
    meta = inspection_plan.get("metadata", {})
    override_id = meta.get("tmdb_id") or meta.get("imdb_id") or inspection_plan.get("tmdb_id")

    results = {
        "total": len(items),
        "captioned": 0,
        "indexed": 0,
        "errors": 0,
        "details": [],
    }

    for item in items:
        msg_id = item["msg_id"]
        caption = item["proposed_caption"]
        season = item.get("season", 1)
        raw_size = item.get("raw_size", 0)
        readable_size = item.get("readable_size", "")

        err = None
        # 1. Update Telegram message caption
        if update_captions:
            try:
                await client.edit_message_caption(
                    chat_id=chat_ref,
                    message_id=msg_id,
                    caption=caption,
                    parse_mode=ParseMode.DEFAULT
                )
                results["captioned"] += 1
                await asyncio.sleep(0.5)
            except FloodWait as fw:
                LOGGER.warning(f"[NyaaResolver] FloodWait: sleeping {fw.value}s")
                await asyncio.sleep(fw.value)
                try:
                    await client.edit_message_caption(
                        chat_id=chat_ref,
                        message_id=msg_id,
                        caption=caption,
                        parse_mode=ParseMode.DEFAULT
                    )
                    results["captioned"] += 1
                except Exception as ex:
                    err = str(ex)
                    results["errors"] += 1
            except Exception as e:
                LOGGER.warning(f"[NyaaResolver] Failed to edit caption for {msg_id}: {e}")
                err = str(e)
                results["errors"] += 1

        # 2. Direct Index into MongoDB
        if direct_index and override_id:
            try:
                meta_info = await metadata(
                    clean_filename(caption),
                    int(str(channel_id).replace("-100", "")),
                    msg_id,
                    override_id=str(override_id),
                    season_hint=season,
                    quality_hint=item.get("quality"),
                )
                if meta_info:
                    await file_queue.put((
                        meta_info,
                        int(str(channel_id).replace("-100", "")),
                        msg_id,
                        readable_size,
                        raw_size,
                        caption
                    ))
                    results["indexed"] += 1
                else:
                    results["errors"] += 1
                    err = err or "Metadata resolution failed"
            except Exception as e:
                LOGGER.error(f"[NyaaResolver] Index error for {msg_id}: {e}")
                results["errors"] += 1
                err = str(e)

        results["details"].append({
            "msg_id": msg_id,
            "caption": caption,
            "status": "success" if not err else "error",
            "error": err,
        })

    if results["indexed"] > 0:
        try:
            from Backend.helper.auto_sync import schedule_auto_sync
            schedule_auto_sync(delay_seconds=2.0)
        except Exception as e:
            LOGGER.debug(f"[NyaaResolver] Auto-sync trigger error: {e}")

    return results
