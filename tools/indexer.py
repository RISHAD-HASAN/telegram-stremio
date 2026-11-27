#!/usr/bin/env python3
"""
indexer.py - offline indexer for Telegram Stremio.

Accepts any filename/naming pattern, resolves it against TMDB / TVDB / Kitsu /
Cinemeta (in priority order), and produces a database-ready payload for every
stream. It only reports: nothing is written to MongoDB. --apply is switched
off, see APPLY_UNAVAILABLE below.

Handles naming patterns such as:

  [Group] Show Name - S01E02 [1080p HEVC FLAC].mkv          standard anime
  Show.Name.S02E03.1080p.BluRay.x264-Group.mkv              scene style
  Show Name - 02x03 - Episode Title.mkv                     SxE variant
  One Piece 1223 720.mkv                                    absolute episode
  Show Name TOO! - 08 [BD 1080p...].mkv                     implied season 2
  Show Name Climax - 01 [1080p].mkv                         implied season 3
  Movie Name (2024) 1080p.mkv                               movie with year
  Show Name S00E01.mkv                                      special / OVA (season 0)
  Show Name S01 E01-E05.mkv                                 combined episodes
  {tmdb-61223} Show Name S01E01.mkv                         explicit TMDB id
  {imdb-tt0111161} Show Name S01E01.mkv                     explicit IMDB id
  Show Name - S01E01E02.mkv                                 split / multi-ep

Provider priority
-----------------
  Anime   : Kitsu → TVDB → TMDB → Cinemeta
  Movies  : TMDB → Cinemeta
  Series  : TVDB → Cinemeta → TMDB

Usage
-----
    # Dry run (default): parse + resolve, write report only
    python tools/indexer.py

    # From a filenames_report.txt dump
    python tools/indexer.py --dump filenames_report.txt

    # From a directory scan (recursive)
    python tools/indexer.py --scan /path/to/media --ext .mkv .mp4 .ass

    # Custom API keys
    TMDB_API="..." TVDB_API="..." python tools/indexer.py --dump filenames_report.txt

Rules
-----
* No MongoDB writes.
* S00 (specials/OVA) is a distinct bucket - never conflated with S01.
* Low-confidence matches are listed separately for review.
* Explicit {tmdb-...} / {imdb-...} tags in filenames are honored first.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

import httpx

# The tools live in <project>/tools. Python puts only that folder on the import
# path when a tool is run as a script, so the project root is added here for
# the Backend package, and config.env is read from the project root.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Why --apply is refused. apply_to_mongodb() below is kept as the starting
# point for a working version, but must not be reachable until these are fixed.
APPLY_UNAVAILABLE = """--apply is switched off in this release. Nothing was written.

The offline write path is not safe for a real library:
  * it stores each stream under the text "<channel>:<message>", where the server
    expects its own encoded message reference, so the added streams cannot play;
  * it adds the new entry without removing the wrongly filed one;
  * it writes to every storage database, not to the one that holds the title;
  * its backup file is written after the changes and lists what was added,
    not the documents as they were.

Run without --apply to get the report, then correct the entries on the server:
see "When the match is wrong" in docs/usage.md."""

# ---------------------------------------------------------------------------
# Load environment from config.env if present (no hard failure if missing)
# ---------------------------------------------------------------------------

def _load_env_file() -> None:
    path = os.path.join(_PROJECT_ROOT, "config.env")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass


_load_env_file()

# ---------------------------------------------------------------------------
# Optional: import the app's own metadata stack when available (shared logic)
# ---------------------------------------------------------------------------

try:
    from Backend.helper.metadata.common import (
        ensure_media_ids,
        split_default_id,
        title_similarity,
        STRONG_MATCH,
        TMDB_THRESHOLD,
        TVDB_THRESHOLD,
        KITSU_THRESHOLD,
        CINEMETA_THRESHOLD,
        parse_year_range,
        extract_default_id,
        normalize_title,
        collect_title_aliases,
        score_candidate_aliases,
        build_query_variants,
    )
    from Backend.helper.metadata.providers import tmdb as _tmdb_mod, tvdb as _tvdb_mod
    from Backend.helper.metadata.providers import cinemeta as _cinemeta_mod
    from Backend.helper.metadata.providers import kitsu as _kitsu_mod
    from Backend.helper.metadata.providers import jikan as _jikan_mod

    _HAS_APP_STACK = True
except Exception:  # pragma: no cover - standalone fallback
    _HAS_APP_STACK = False

# Name cleaning and routing shared with the bot, so an offline run parses
# names the same way. The built-in parser below is the fallback when the
# Backend package or its dependencies are not installed.
try:
    from Backend.helper.name_cleaning import clean_filename as _shared_clean_filename
    from Backend.helper.metadata.plan import plan_media as _shared_plan_media
    from Backend.helper.metadata.plan import SKIP_TITLE_MISSING as _SKIP_TITLE_MISSING
    from Backend.helper.metadata.parse import quality_hint_from_media as _shared_quality_hint
    from Backend.helper.metadata.common import extract_default_id as _shared_extract_default_id
    _HAS_SHARED_PARSER = True
except Exception:  # pragma: no cover - standalone fallback
    _HAS_SHARED_PARSER = False
    _SKIP_TITLE_MISSING = "title_missing"

# Provider resolution shared with the bot (Kitsu / ani.zip -> TVDB -> TMDB
# -> Cinemeta). ProviderResolver below is the standalone fallback.
try:
    from Backend.helper.metadata.entry import resolve_name as _shared_resolve_name
    _HAS_SHARED_RESOLVER = _HAS_SHARED_PARSER
except Exception:  # pragma: no cover - standalone fallback
    _shared_resolve_name = None
    _HAS_SHARED_RESOLVER = False
# Set by --legacy-resolver
USE_LEGACY_RESOLVER = False

# Watch-order module is self-contained; import independently
try:
    from Backend.helper import watch_order as _watch_order_mod
    _WATCH_ORDER_AVAILABLE = True
except Exception:
    _watch_order_mod = None
    _WATCH_ORDER_AVAILABLE = False

# Jikan provider is self-contained; import independently for filler enrichment
try:
    from Backend.helper.metadata.providers import jikan as _jikan_mod
    _JAKAN_AVAILABLE = True
except Exception:
    _jikan_mod = None
    _JAKAN_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TMDB_API_KEY = os.getenv("TMDB_API", "").strip()
TVDB_API_KEY = os.getenv("TVDB_API", "").strip()
JIKAN_ENABLED = os.getenv("JIKAN_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")

MAX_FILENAME_LEN = 400
MAX_TITLE_LEN = 300
REQUEST_TIMEOUT = 25.0
MAX_CONCURRENT = 6
REQUEST_DELAY = 0.5  # polite delay between API calls

# ---------------------------------------------------------------------------
# Path arguments
# ---------------------------------------------------------------------------


def _safe_path(value: str, label: str, must_exist: bool = False) -> str:
    if not value or not isinstance(value, str):
        raise SystemExit(f"Error: {label} must be a non-empty string.")
    if "\x00" in value:
        raise SystemExit(f"Error: {label} contains null byte.")
    abs_path = os.path.abspath(os.path.expanduser(value))
    if must_exist and not os.path.isfile(abs_path):
        raise SystemExit(f"Error: {label} not found: {abs_path}")
    return abs_path


def _safe_dir(value: str, label: str, must_exist: bool = False) -> str:
    if not value or not isinstance(value, str):
        raise SystemExit(f"Error: {label} must be a non-empty string.")
    if "\x00" in value:
        raise SystemExit(f"Error: {label} contains null byte.")
    abs_path = os.path.abspath(os.path.expanduser(value))
    if must_exist and not os.path.isdir(abs_path):
        raise SystemExit(f"Error: {label} not found: {abs_path}")
    return abs_path


# ---------------------------------------------------------------------------
# Security: credential masking
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"(?i)(mongodb\+srv|mongodb)://[^:]+:[^@]+@")


def _mask_uri(uri: str) -> str:
    return _URI_RE.sub(lambda m: m.group(1) + "://****:****@", uri or "")


def _mask_key(key: str) -> str:
    if not key:
        return "(unset)"
    return key[:4] + "*" * (len(key) - 8) + key[-4:] if len(key) > 8 else "****"


# ---------------------------------------------------------------------------
# Title alias map: nyaa / scene titles that differ from the main show
# ---------------------------------------------------------------------------

TITLE_ALIASES: dict[str, dict[int, str]] = {
    "my teen romantic comedy snafu": {
        2: "my teen romantic comedy snafu too",
        3: "my teen romantic comedy snafu climax",
    },
    "ore no imouto ga konna ni kawaii wake ga nai": {
        2: "ore no imouto ga konna ni kawaii wake ga nai 2",
    },
    "high school dxd": {
        2: "high school dxd born",
        3: "high school dxd new",
        4: "high school dxd hero",
    },
    "food wars": {
        2: "food wars the second plate",
        3: "food wars the third plate",
        4: "food wars the fourth plate",
        5: "food wars the fifth plate",
    },
    "shingeki no kyojin": {
        2: "shingeki no kyojin season 2",
        3: "shingeki no kyojin season 3",
        4: "shingeki no kyojin the final season",
    },
    "attack on titan": {
        2: "attack on titan season 2",
        3: "attack on titan season 3",
        4: "attack on titan the final season",
    },
}


def _alias_title(title: str, season: Optional[int]) -> str:
    if season is None:
        return title
    key = title.strip().lower()
    aliases = TITLE_ALIASES.get(key)
    if not aliases:
        return title
    return aliases.get(int(season), title)


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Pre-compiled regexes for performance
_EXTENSION_RE = re.compile(r"\.(mkv|mp4|avi|webm|ts|m4v|mov|wmv|flv|ass|srt|sub)$", re.I)
_CRC32_RE = re.compile(r"(?i)[\[\(][0-9a-f]{8}[\]\)]")
_BRACKET_GROUP_RE = re.compile(r"^\[([^\]]+)\]\s*")
# Mismatched brackets: [YURI} instead of [YURI]
_BRACKET_ID_RE = re.compile(
    r"(?i)\{\s*(tmdb|imdb|tvdb)\s*[-:]\s*([^\}]+)\s*\}"
)
_QUALITY_P_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{3,4})p(?![A-Za-z0-9])")
_QUALITY_I_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{3,4})i(?![A-Za-z0-9])")
_QUALITY_DIM_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{3,4})[xX](\d{3,4})(?![A-Za-z0-9])")
_QUALITY_4K_WORD_RE = re.compile(r"(?i)\b(?:4k|uhd|2160p?)\b")
_QUALITY_HD_WORD_RE = re.compile(r"(?i)\bhd\b")
_QUALITY_GLUED_RE = re.compile(r"(?i)(?:BD|HDRip|WEBDL|WEBRip|HDTV|DVD|BR|WEB)(\d{3,4})p")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# "S01E22" regular episode; "S01E22.5" decimal/OVA episode (e.g. Kuroko's Basketball S01E22.5)
# Group 1 = season, Group 2 = episode, Group 3 = optional decimal part (e.g. "5" for .5)
_SXXEXX_RE = re.compile(r"(?i)\bS(\d{1,2})\s*[._\s-]*\s*E(\d{1,3})(?:\.(\d+))?\b")
# "S3 01" style: season space episode without an "E" (e.g. "Enen no Shouboutai S3 01")
# Exclude common resolutions so "S3 1080p" doesn't false-match.
_RES_NUMBERS = {"480", "576", "720", "1080", "1440", "2160", "4320"}
_SXX_SPACE_EP_RE = re.compile(
    r"(?i)\bS(\d{1,2})\s+(?![\d]{3,4}p\b)(\d{1,3})\b"
)
_NXNN_RE = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b")
_SEASON_WORD_RE = re.compile(r"(?i)\bseason\s*(\d{1,2})\b")
_EP_WORD_RE = re.compile(r"(?i)\b(?:ep|episode)\s*0*(\d{1,3})\b")
_ABS_EP_RE = re.compile(r"(?i)(?:^|[\s._\-])(\d{2,4})(?=[\s._\-]|$)")
_SPECIAL_RE = re.compile(r"(?i)\b(?:s0*0e|ova|oad|special|sp|nced|ncop)\s*[-_.]?\s*0*(\d{1,3})\b")
_COMBINED_RE = re.compile(
    r"(?i)\b(?:e|ep|episode)\s*0*(\d{1,3})\s*[-‐‑–—]\s*(?:e|ep|episode)?\s*0*(\d{1,3})\b"
)
_SPLIT_PART_RE = re.compile(r"(?i)\bpart\s*0*(\d+)\b", re.I)
_RELEASE_TAGS_RE = re.compile(
    r"(?i)\b(?:x264|x265|h264|h265|hevc|avc|av1|aac|flac|ac3|ddp|truehd|atmos|"
    r"10bit|8bit|hi10p|dual.audio|multi.audio|esub|softsubs|hardsubs|"
    r"bluray|blu.ray|bdrip|brrip|webrip|web.dl|web-dl|hdtv|hdrip|dvdrip|"
    r"remux|encode|v2|v3|v4|proper|repack|internal|limited)\b"
)
_AUDIO_CHANNEL_RE = re.compile(r"(?i)\b\d\.\d\b")
_FPS_RE = re.compile(r"(?i)\b\d+(?:\.\d+)?fps\b")
# Frame rate pairs: "143 8561fps" - both numbers are frame rate, not episode
_FPS_PAIR_RE = re.compile(r"(?i)\b\d+\s+\d+\.?\d*fps\b")
_SIZE_TAG_RE = re.compile(r"\(\s*\d+(?:\.\d+)?\s*[KMGT]?B\s*\)")
_BRACKET_CONTENT_RE = re.compile(r"\[[^\]]{1,40}\]")
_PAREN_CONTENT_RE = re.compile(r"\([^)]{1,40}\)")


def _strip_noise(name: str) -> str:
    """Remove known noise tokens from a filename to expose the core title."""
    n = name
    n = _CRC32_RE.sub(" ", n)
    n = re.sub(r"#\w+", " ", n)  # hashtags
    n = re.sub(r"@[\w_]+", " ", n)  # mentions
    n = _GLUED_SRC_RE.sub(r" \1p ", n) if (_GLUED_SRC_RE := re.compile(r"(?i)(?:BD|HDRip|WEBDL|WEBRip|HDTV|DVD|BR|WEB)(\d{3,4})p")) else n
    n = _QUALITY_GLUED_RE.sub(r" \1p ", n)
    n = _FPS_RE.sub(" ", n)
    n = _SIZE_TAG_RE.sub(" ", n)
    n = _AUDIO_CHANNEL_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


_LEADING_GROUP_RE = re.compile(r"^\s*[\[{]([^\[\]{}]{1,40})[\]}\[]")


def parse_filename(filename: str, quality_hint: Optional[str] = None) -> dict:
    """
    Parse a filename / caption into components: title, season, episode,
    quality, year, group, absolute flag, combined range, split part, explicit IDs.

    Uses the bot's clean_filename() and plan_media() when the Backend package
    can be imported, and the built-in parser otherwise.

    quality_hint: resolution from media attributes, used when the name has none.
    """
    if not filename:
        return _empty_parse()
    if _HAS_SHARED_PARSER:
        try:
            return _parse_filename_shared(filename, quality_hint)
        except Exception as e:  # never let one odd name abort a whole run
            print(f"    [parse] shared parser failed for '{filename[:60]}': {e}; using fallback")
    result = _parse_filename_legacy(filename)
    if not result.get("quality") and quality_hint:
        result["quality"] = quality_hint
    return result


def _parse_filename_shared(filename: str, quality_hint: Optional[str] = None) -> dict:
    """Map the shared routing plan onto the indexer's result dict."""
    result = _empty_parse()
    result["raw_filename"] = filename
    name = filename

    # Explicit ID tags: {tmdb-61223}, {imdb-tt0111161}, {tvdb-12345}
    for source, val in _BRACKET_ID_RE.findall(name):
        val = val.strip()
        if source.lower() == "tmdb":
            result["explicit_tmdb_id"] = int(val) if val.isdigit() else None
        elif source.lower() == "imdb":
            result["explicit_imdb_id"] = val if val.startswith("tt") else f"tt{val}" if val.isdigit() else None
        elif source.lower() == "tvdb":
            result["explicit_tvdb_id"] = int(val) if val.isdigit() else None
    name = _BRACKET_ID_RE.sub(" ", name)

    # IMDb / TMDB link or bare tt-id in the caption (the bot stamps these itself)
    linked = _shared_extract_default_id(name)
    if linked:
        if str(linked).startswith("tt"):
            result["explicit_imdb_id"] = result["explicit_imdb_id"] or linked
        elif str(linked).isdigit():
            result["explicit_tmdb_id"] = result["explicit_tmdb_id"] or int(linked)

    gm = _LEADING_GROUP_RE.match(name)
    if gm:
        result["group"] = gm.group(1).strip()

    has_id = bool(result["explicit_tmdb_id"] or result["explicit_imdb_id"] or result["explicit_tvdb_id"])
    plan = _shared_plan_media(
        _shared_clean_filename(name),
        anime_channel=True,
        quality_hint=quality_hint,
        has_explicit_id=has_id,
    )
    parsed = plan.get("parsed") or {}
    result["year"] = plan.get("year")
    result["is_split"] = plan.get("part_number") is not None
    result["split_part"] = plan.get("part_number")
    # Episode title written in the name, and for "N.5" specials the episode
    # they follow (only present when the name has them).
    if plan.get("episode_title"):
        result["episode_title"] = plan["episode_title"]
    if plan.get("watch_after"):
        result["watch_after"] = plan["watch_after"]

    if plan.get("skip"):
        # Cannot be indexed as named; keep the parsed fields for the report.
        result["skip_reason"] = plan["skip"]
        season, episode = parsed.get("season"), parsed.get("episode")
        result["season"] = season if isinstance(season, int) else None
        result["episode"] = episode if isinstance(episode, int) else None
        result["special"] = result["season"] == 0
        result["quality"] = parsed.get("quality") or quality_hint
        return result

    result["title"] = plan.get("title")
    result["season"] = plan.get("season")
    result["episode"] = plan.get("episode")
    result["absolute"] = bool(plan.get("absolute"))
    result["special"] = plan.get("season") == 0 and plan.get("kind") == "tv"
    result["is_movie"] = plan.get("kind") == "movie"
    # "HD" is the pipeline's "unknown" label; the indexer reports unknown as
    # None unless the name itself says "HD".
    quality = plan.get("quality")
    if quality == "HD" and not parsed.get("quality") and not quality_hint and not _QUALITY_HD_WORD_RE.search(name):
        quality = None
    result["quality"] = quality

    # Title-based season inference ("SNAFU TOO!" -> season 2, "Climax" -> season 3)
    if result["season"] is None and not result["is_movie"]:
        raw_lower = name.lower()
        for key, aliases in TITLE_ALIASES.items():
            if key not in raw_lower:
                continue
            for season_num, alias_title in aliases.items():
                if alias_title.lower() in raw_lower:
                    result["season"] = season_num
                    result["absolute"] = False
                    result["title"] = alias_title
                    result["title_inferred_from_alias"] = True
                    break
            if result["season"] is not None:
                break

    combined = plan.get("combined")
    if combined:
        result["combined"] = True
        result["combined_start"] = combined.get("start")
        result["combined_end"] = combined.get("end")
    return result


def _parse_filename_legacy(filename: str) -> dict:
    """
    Built-in parser, used when the Backend package cannot be imported.
    Returns title, season, episode, quality, year, group, absolute flag,
    combined range, split part and explicit IDs.
    """
    if not filename:
        return _empty_parse()

    raw = filename
    result = _empty_parse()
    result["raw_filename"] = raw

    # Strip extension
    name = _EXTENSION_RE.sub(" ", raw)

    # Extract explicit ID tags first: {tmdb-61223}, {imdb-tt0111161}, {tvdb-12345}
    id_matches = _BRACKET_ID_RE.findall(name)
    for source, val in id_matches:
        if source.lower() == "tmdb":
            result["explicit_tmdb_id"] = int(val) if val.isdigit() else None
        elif source.lower() == "imdb":
            result["explicit_imdb_id"] = val if val.startswith("tt") else f"tt{val}" if val.isdigit() else None
        elif source.lower() == "tvdb":
            result["explicit_tvdb_id"] = int(val) if val.isdigit() else None
        name = _BRACKET_ID_RE.sub(" ", name, count=1)

    # Strip CRC32 checksums
    name = _CRC32_RE.sub(" ", name)

    # Strip mismatched brackets like [YURI} instead of [YURI]
    mm = re.match(r"^\[([^\]]+)\}\s*", name.strip())
    if mm:
        result["group"] = mm.group(1).strip()
        name = name[mm.end():].strip()

    # Extract release group: [Group] at start
    gm = _BRACKET_GROUP_RE.match(name.strip())
    if gm:
        result["group"] = gm.group(1).strip()
        name = name[gm.end():].strip()

    # Keep a copy with quality tokens for quality detection
    quality_name = name

    # --- Quality detection (from quality_name, before stripping) ---
    quality = None

    # Explicit p/i tokens (e.g. 1080p, 480i)
    p_heights = [int(m.group(1)) for m in _QUALITY_P_TOKEN_RE.finditer(quality_name)]
    if p_heights:
        quality = f"{max(p_heights)}p"
    else:
        i_heights = [int(m.group(1)) for m in _QUALITY_I_TOKEN_RE.finditer(quality_name)]
        if i_heights:
            # Interlaced labels map to the progressive tier
            quality = f"{max(i_heights)}p"
        else:
            dims = _QUALITY_DIM_RE.findall(quality_name)
            if dims:
                h = max(min(int(w), int(hh)) for w, hh in dims)
                for threshold, label in (
                    (2100, "4K"), (1400, "1440p"), (1000, "1080p"),
                    (700, "720p"), (540, "576p"), (440, "480p"), (320, "360p"),
                ):
                    if h >= threshold:
                        quality = label
                        break
                else:
                    quality = f"{h}p"
            elif _QUALITY_4K_WORD_RE.search(quality_name):
                quality = "4K"
            elif _QUALITY_HD_WORD_RE.search(quality_name):
                quality = "HD"

    result["quality"] = quality

    # --- Year detection ---
    # Exclude years that are part of dimensions (e.g. 1920x1080 -> don't treat 1920 as year)
    dim_positions = set()
    for dm in _QUALITY_DIM_RE.finditer(quality_name):
        dim_positions.add(dm.start(1))
        dim_positions.add(dm.start(2))

    year_matches = []
    for ym in _YEAR_RE.finditer(quality_name):
        if ym.start() not in dim_positions:
            year_matches.append(int(ym.group(0)))

    if year_matches:
        # Prefer years in parentheses (likely release year)
        paren_years = []
        for pm in re.finditer(r"\((\d{4})\)", quality_name):
            paren_years.append(int(pm.group(1)))
        if paren_years:
            result["year"] = paren_years[-1]
        else:
            result["year"] = year_matches[-1]

    # --- Season / Episode detection ---
    name_for_se = name

    # SxxExx pattern (supports decimal/OVA episodes like S01E22.5)
    sxem = _SXXEXX_RE.search(name_for_se)
    if sxem:
        season_num = int(sxem.group(1))
        ep_num = int(sxem.group(2))
        dec_part = sxem.group(3)  # optional decimal, e.g. "5" for .5
        if dec_part:
            result["episode"] = float(f"{ep_num}.{dec_part}")
            result["special"] = True  # decimal episodes are treated as OVA/special
        else:
            result["episode"] = ep_num
            result["special"] = season_num == 0
        result["season"] = season_num
        result["absolute"] = False
    else:
        # "S3 01" style: season space episode without an "E"
        sxsp = _SXX_SPACE_EP_RE.search(name_for_se)
        if sxsp:
            result["season"] = int(sxsp.group(1))
            result["episode"] = int(sxsp.group(2))
            result["absolute"] = False
            result["special"] = result["season"] == 0
        else:
            # NxNN pattern (e.g. 2x03)
            nxnn = _NXNN_RE.search(name_for_se)
            if nxnn:
                result["season"] = int(nxnn.group(1))
                result["episode"] = int(nxnn.group(2))
                result["absolute"] = False
                result["special"] = result["season"] == 0
            else:
                # "Season N" word
                swm = _SEASON_WORD_RE.search(name_for_se)
                if swm:
                    result["season"] = int(swm.group(1))
                    result["special"] = result["season"] == 0
                    # Look for episode after season
                    epm = _EP_WORD_RE.search(name_for_se, swm.end())
                    if epm:
                        result["episode"] = int(epm.group(1))
                        result["absolute"] = False
                else:
                    # No season: could be absolute episode or movie
                    abs_ep = None
                    # Explicit special/OVA tags
                    spm = _SPECIAL_RE.search(name_for_se)
                    if spm:
                        result["season"] = 0
                        result["episode"] = int(spm.group(1))
                        result["special"] = True
                        result["absolute"] = False
                    else:
                        # Bare number that looks like an episode
                        # Exclude quality-like numbers (e.g. 720 from "720p")
                        quality_numbers = set()
                        for m in _QUALITY_P_TOKEN_RE.finditer(name_for_se):
                            quality_numbers.add(int(m.group(1)))
                        for m in _QUALITY_I_TOKEN_RE.finditer(name_for_se):
                            quality_numbers.add(int(m.group(1)))
                        # Also exclude common quality values even without 'p' suffix
                        quality_numbers.update({240, 360, 480, 576, 720, 1080, 1440, 2160, 4320})
                        # Exclude known codec/tech patterns (e.g. H.264 -> 264 is not an episode)
                        codec_numbers = set()
                        for m in re.finditer(r"(?i)\b(?:h|x)\.?26[45]\b", name_for_se):
                            try:
                                codec_numbers.add(int(m.group(0)[-3:]))
                            except ValueError:
                                pass
                        quality_numbers.update(codec_numbers)

                        # Strip frame-rate pairs first (e.g. "143 8561fps")
                        name_for_abs = _FPS_PAIR_RE.sub(" ", name_for_se)
                        for m in _ABS_EP_RE.finditer(name_for_abs):
                            val = int(m.group(1))
                            if (1 <= val <= 9999
                                    and not _YEAR_RE.fullmatch(m.group(1))
                                    and val not in quality_numbers):
                                abs_ep = val
                                break
                        if abs_ep:
                            result["episode"] = abs_ep
                            result["absolute"] = True
                            result["season"] = None
                        else:
                            result["season"] = None
                            result["episode"] = None
                            result["absolute"] = False

    # --- Title-based season inference ---
    # e.g. "SNAFU TOO!" -> season 2, "Climax" -> season 3
    if result.get("season") is None and result.get("title") is None:
        title_lower = raw.lower()
        for key, aliases in TITLE_ALIASES.items():
            if key in title_lower:
                for season_num, alias_title in aliases.items():
                    if alias_title.lower() in title_lower:
                        result["season"] = season_num
                        result["title_inferred_from_alias"] = True
                        break
                if result.get("season"):
                    break

    # --- Combined episode range (E01-E05) ---
    cm = _COMBINED_RE.search(name_for_se)
    # With an episode already parsed only an explicit "E01-E05" is a range;
    # "S02E01 - 021" is an episode and its absolute number.
    if cm and result.get("episode") and not (
        re.search(r"(?i)[-‐‑–—]\s*(?:e|ep|episode)\s*\d", cm.group(0)) and int(cm.group(2)) > int(cm.group(1))
    ):
        cm = None
    if cm:
        result["combined_start"] = int(cm.group(1))
        result["combined_end"] = int(cm.group(2))
        result["combined"] = True
        if not result.get("season"):
            result["season"] = 1
        result["episode"] = result["combined_start"]

    # --- Split file part ---
    spm = _SPLIT_PART_RE.search(name_for_se)
    if spm:
        result["split_part"] = int(spm.group(1))
        result["is_split"] = True

    # --- Title extraction ---
    # Remove quality tokens, release tags, season/episode patterns, split tags
    title_raw = quality_name
    title_raw = _QUALITY_P_TOKEN_RE.sub(" ", title_raw)
    title_raw = _QUALITY_I_TOKEN_RE.sub(" ", title_raw)
    title_raw = _QUALITY_DIM_RE.sub(" ", title_raw)
    title_raw = _QUALITY_4K_WORD_RE.sub(" ", title_raw)
    title_raw = _SXXEXX_RE.sub(" ", title_raw)
    title_raw = _SXX_SPACE_EP_RE.sub(" ", title_raw)
    title_raw = _NXNN_RE.sub(" ", title_raw)
    title_raw = _SEASON_WORD_RE.sub(" ", title_raw)
    title_raw = _EP_WORD_RE.sub(" ", title_raw)
    title_raw = _SPECIAL_RE.sub(" ", title_raw)
    title_raw = _COMBINED_RE.sub(" ", title_raw)
    title_raw = _SPLIT_PART_RE.sub(" ", title_raw)
    title_raw = _FPS_PAIR_RE.sub(" ", title_raw)
    title_raw = _RELEASE_TAGS_RE.sub(" ", title_raw)
    title_raw = _YEAR_RE.sub(" ", title_raw)
    title_raw = _BRACKET_ID_RE.sub(" ", title_raw)
    title_raw = _BRACKET_GROUP_RE.sub(" ", title_raw)
    title_raw = _BRACKET_CONTENT_RE.sub(" ", title_raw)
    title_raw = _PAREN_CONTENT_RE.sub(" ", title_raw)  # parenthetical notes (not years)
    # Strip "Studio Ghibli Movie N" prefix (e.g. "Studio Ghibli Movie 06 Porco Rosso" -> "Porco Rosso")
    title_raw = re.sub(r"(?i)\bstudio\s+ghibli\s+movie\s+\d+\s*", " ", title_raw)
    title_raw = re.sub(r"[._\-]+", " ", title_raw)
    title_raw = re.sub(r"\s+", " ", title_raw).strip()

    # Remove leading/trailing connector words
    title_raw = re.sub(r"^(?:and|the|a|an)\s+", "", title_raw, flags=re.I)
    title_raw = re.sub(r"\s+(and|the|a|an)$", "", title_raw, flags=re.I)

    # Common English words that can legitimately appear at the end of a title —
    # protected so the release-group heuristic doesn't strip them (e.g. FAMILY,
    # EIGHTY, SIX, BOY, BEAST).
    _COMMON_TITLE_WORDS = {
        "the", "and", "for", "with", "from", "into", "during", "before", "after",
        "above", "below", "between", "under", "again", "further", "then", "once",
        "here", "there", "when", "where", "why", "how", "all", "each", "few",
        "more", "most", "other", "some", "such", "no", "nor", "not", "only",
        "own", "same", "so", "than", "too", "very", "just", "also", "even",
        "well", "back", "any", "around", "small", "big", "large", "little",
        "high", "low", "new", "old", "young", "long", "short", "dark", "light",
        "red", "blue", "green", "black", "white", "good", "bad", "great", "best",
        "right", "wrong", "hard", "easy", "soft", "hot", "cold", "warm", "cool",
        "fast", "slow", "quick", "quiet", "loud", "silent", "clean", "dirty",
        "open", "close", "full", "empty", "free", "busy", "safe", "dead", "alive",
        "true", "false", "real", "fake", "love", "life", "death", "war", "peace",
        "time", "day", "night", "story", "tale", "chronicle", "saga", "legend",
        "myth", "dream", "season", "part", "episode", "movie", "film", "ova",
        "special", "final", "last", "first", "second", "third", "next", "new",
        "old", "complete", "collection", "series", "family", "spy", "boy", "beast",
        "girl", "man", "woman", "war", "peace",
        # Number words commonly in titles
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "twenty", "thirty", "forty", "fifty",
        "sixty", "seventy", "eighty", "ninety", "hundred", "thousand",
    }

    # Remove trailing release-group-like / tech words from title (iterative, combined:
    # strips runs like "SPY x FAMILY Dual Audio Opus 5 1 YURASUKA" -> "SPY x FAMILY").
    # Stops at the first non-noise word from the end.
    title_words = title_raw.split()
    tech_tail = {"group", "raws", "subs", "sub", "dual", "audio", "multi", "x264", "x265",
                 "hevc", "avc", "aac", "flac", "bluray", "mkv", "mp4", "web", "bd",
                 "dvd", "encode", "proper", "repack", "internal", "limited", "v2", "v3",
                 "v4", "10bit", "8bit", "hi10p", "opus", "ddp", "dolby", "truehd", "atmos",
                 "ac3", "eac3", "hd", "hdma", "true", "10", "bit", "hi10", "webrip",
                 "web-dl", "bdrip", "brrip", "remux", "proper", "hmax", "msubs",
                 "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}
    while title_words:
        last_word = title_words[-1]
        lw = last_word.lower().strip(".,;:[](){}")
        if lw in tech_tail:
            title_words.pop()
            continue
        # Never strip down to a single word: that last word is almost certainly the title.
        if len(title_words) <= 1:
            break
        # Release-group heuristic 1: short all-caps or alphanumeric (YURASUKA, QM, LYS1TH3A)
        if (2 <= len(last_word) <= 10
                and (last_word.isupper() or any(c.isdigit() for c in last_word))
                and lw not in _COMMON_TITLE_WORDS
                and lw not in tech_tail):
            title_words.pop()
            continue
        # Release-group heuristic 2: short lowercase non-common word (koala, derpie)
        # Check original case so mixed-case words like "Porco" or "Reze" aren't stripped.
        if (3 <= len(lw) <= 8
                and last_word.islower()
                and lw not in _COMMON_TITLE_WORDS
                and lw not in tech_tail
                and not any(c.isdigit() for c in lw)):
            title_words.pop()
            continue
        # Release-group heuristic 3: CamelCase mixed-case tags (ToonsHub, MSubs, ZoroSenpai)
        # A capital letter after position 0 signals a group tag, not a regular title word.
        if (3 <= len(last_word) <= 12
                and any(c.isupper() for c in last_word[1:])
                and last_word[0].isalpha()
                and lw not in _COMMON_TITLE_WORDS
                and lw not in tech_tail):
            title_words.pop()
            continue
        # Release-group heuristic 4: short title-case word after tech/digit noise
        # (e.g. "DUAL 2 0 Kitsune" -> strip "Kitsune" because "0" is noise).
        if (3 <= len(lw) <= 8
                and last_word[0].isupper()
                and last_word[1:].islower()
                and lw not in _COMMON_TITLE_WORDS
                and lw not in tech_tail):
            prev_word = title_words[-2].lower().strip(".,;:[](){}") if len(title_words) >= 2 else ""
            if prev_word in tech_tail or (prev_word.isdigit() and len(prev_word) <= 3):
                title_words.pop()
                continue
        # Single trailing alphanumeric char (noise from codecs like H.264 -> "H", 1080p -> "p").
        # Protect "x" which is common in titles (e.g. "SPY x FAMILY").
        if len(last_word) == 1 and last_word.isalnum() and last_word.lower() != "x":
            title_words.pop()
            continue
        break
    title_raw = " ".join(title_words).strip()

    # If we inferred title from alias, use the alias title
    if result.get("title_inferred_from_alias"):
        for key, aliases in TITLE_ALIASES.items():
            if key in raw.lower():
                for season_num, alias_title in aliases.items():
                    if result.get("season") == season_num and alias_title.lower() in raw.lower():
                        title_raw = alias_title
                        break

    result["title"] = title_raw[:MAX_TITLE_LEN] if title_raw else None

    # --- Movie detection ---
    if re.search(r"(?i)\b(?:the\s+movie|movie\s*\d*|film|gekijouban|gekijou-ban)\b", raw):
        result["is_movie"] = True
    # Year present, no season/episode, no absolute -> likely a movie
    elif result.get("year") and not result.get("season") and not result.get("episode") and not result.get("absolute") and not result.get("special"):
        result["is_movie"] = True

    return result



def _empty_parse() -> dict:
    return {
        "raw_filename": "",
        "title": None,
        "season": None,
        "episode": None,
        "absolute": False,
        "special": False,
        "combined": False,
        "combined_start": None,
        "combined_end": None,
        "split_part": None,
        "is_split": False,
        "quality": None,
        "year": None,
        "group": None,
        "is_movie": False,
        "explicit_tmdb_id": None,
        "explicit_imdb_id": None,
        "explicit_tvdb_id": None,
    }


# ---------------------------------------------------------------------------
# Provider resolution (standalone, using httpx directly)
# ---------------------------------------------------------------------------

class ProviderResolver:
    """Resolves titles against TMDB, TVDB, Kitsu, Cinemeta using httpx directly."""

    def __init__(self, tmdb_key: str = "", tvdb_key: str = "", client: Optional[httpx.AsyncClient] = None):
        self.tmdb_key = tmdb_key
        self.tvdb_key = tvdb_key
        self.client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True)
        self._tvdb_token: Optional[str] = None
        self._tvdb_token_expires: float = 0.0
        self._cache: dict = {}

    async def _tmdb_search(self, title: str, media_type: str, year: Optional[int] = None):
        if not self.tmdb_key:
            return None
        cache_key = f"tmdb::{media_type}::{title}::{year}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            params = {"api_key": self.tmdb_key, "query": title, "language": "en-US"}
            if media_type == "movie" and year:
                params["year"] = year
            endpoint = "search/movie" if media_type == "movie" else "search/tv"
            resp = await self.client.get(f"https://api.themoviedb.org/3/{endpoint}", params=params)
            if resp.status_code != 200:
                return None
            results = resp.json().get("results", [])
            if not results:
                return None
            # Score and pick best
            best = None
            best_score = 0.0
            for r in results[:10]:
                r_title = r.get("title") if media_type == "movie" else r.get("name", "")
                r_date = r.get("release_date") if media_type == "movie" else r.get("first_air_date", "")
                r_year = int(r_date[:4]) if r_date else 0
                score = title_similarity(title, r_title) if _HAS_APP_STACK else self._simple_sim(title, r_title)
                if year and r_year and abs(year - r_year) <= 2:
                    score += 0.1
                if score > best_score:
                    best_score = score
                    best = r
            if best and best_score >= 0.5:
                self._cache[cache_key] = best
                return best
        except Exception:
            pass
        return None

    async def _tmdb_details(self, media_type: str, item_id: int):
        if not self.tmdb_key:
            return None
        cache_key = f"tmdb_det::{media_type}::{item_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            resp = await self.client.get(
                f"https://api.themoviedb.org/3/{media_type}/{item_id}",
                params={"api_key": self.tmdb_key, "language": "en-US", "append_to_response": "external_ids,credits,images"},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._cache[cache_key] = data
                return data
        except Exception:
            pass
        return None

    async def _tmdb_episode(self, tv_id: int, season: int, episode: int):
        if not self.tmdb_key:
            return None
        cache_key = f"tmdb_ep::{tv_id}::{season}::{episode}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            resp = await self.client.get(
                f"https://api.themoviedb.org/3/tv/{tv_id}/season/{season}/episode/{episode}",
                params={"api_key": self.tmdb_key, "language": "en-US"},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._cache[cache_key] = data
                return data
        except Exception:
            pass
        return None

    async def _tmdb_season0_specials(self, tv_id: int) -> list[dict]:
        """Fetch TMDB season 0 (specials/OVAs) for a show."""
        if not self.tmdb_key:
            return []
        cache_key = f"tmdb_s0::{tv_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            resp = await self.client.get(
                f"https://api.themoviedb.org/3/tv/{tv_id}/season/0",
                params={"api_key": self.tmdb_key, "language": "en-US"},
            )
            if resp.status_code == 200:
                data = resp.json()
                eps = data.get("episodes", []) or []
                self._cache[cache_key] = eps
                return eps
        except Exception:
            pass
        return []

    @staticmethod
    def _match_special_by_title(specials: list[dict], title: str) -> Optional[dict]:
        """Match a TMDB season-0 special by title against the filename-derived title."""
        if not specials or not title:
            return None
        # Normalize the search title
        t_norm = re.sub(r"[^a-z0-9]", "", title.lower())
        # Try exact match first
        for ep in specials:
            ep_name = ep.get("name", "")
            ep_norm = re.sub(r"[^a-z0-9]", "", ep_name.lower())
            if ep_norm == t_norm:
                return ep
        # Try contains match (title is contained in special name or vice versa)
        for ep in specials:
            ep_name = ep.get("name", "")
            ep_norm = re.sub(r"[^a-z0-9]", "", ep_name.lower())
            if t_norm and (t_norm in ep_norm or ep_norm in t_norm):
                return ep
        # Try bigram similarity
        best = None
        best_score = 0.0
        for ep in specials:
            ep_name = ep.get("name", "")
            score = ProviderResolver._simple_sim(title, ep_name)
            if score > best_score and score > 0.5:
                best_score = score
                best = ep
        return best

    async def _tvdb_login(self):
        if not self.tvdb_key:
            return None
        now = time.time()
        if self._tvdb_token and now < self._tvdb_token_expires - 60:
            return self._tvdb_token
        try:
            resp = await self.client.post(
                "https://api4.thetvdb.com/v4/login",
                json={"apikey": self.tvdb_key},
            )
            if resp.status_code == 200:
                data = resp.json()
                self._tvdb_token = (data.get("data") or {}).get("token")
                self._tvdb_token_expires = now + 25 * 24 * 3600
                return self._tvdb_token
        except Exception:
            pass
        return None

    async def _tvdb_get(self, path: str, params: Optional[dict] = None):
        token = await self._tvdb_login()
        if not token:
            return None
        try:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            resp = await self.client.get(f"https://api4.thetvdb.com/v4{path}", params=params or {}, headers=headers)
            if resp.status_code == 401:
                self._tvdb_token = None
                self._tvdb_token_expires = 0.0
                token = await self._tvdb_login()
                if not token:
                    return None
                headers["Authorization"] = f"Bearer {token}"
                resp = await self.client.get(f"https://api4.thetvdb.com/v4{path}", params=params or {}, headers=headers)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    async def _tvdb_search(self, title: str, entity: str = "series", year: Optional[int] = None):
        if not self.tvdb_key:
            return None
        cache_key = f"tvdb::{entity}::{title}::{year}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            data = await self._tvdb_get("/search", {"query": title, "type": entity, "limit": 10})
            if not data:
                return None
            results = data.get("data", [])
            if not results:
                return None
            best = None
            best_score = 0.0
            for r in results:
                r_title = r.get("name", "")
                r_year = self._tvdb_year(r)
                score = title_similarity(title, r_title) if _HAS_APP_STACK else self._simple_sim(title, r_title)
                if year and r_year and abs(year - r_year) <= 2:
                    score += 0.1
                if score > best_score:
                    best_score = score
                    best = r
            if best and best_score >= 0.5:
                self._cache[cache_key] = best
                return best
        except Exception:
            pass
        return None

    def _tvdb_year(self, r: dict) -> int:
        for field in ("year", "first_air_time", "release_date", "firstAired"):
            v = r.get(field)
            if v:
                m = re.search(r"(\d{4})", str(v))
                if m:
                    return int(m.group(1))
        return 0

    async def _tvdb_series_extended(self, tvdb_id: int):
        cache_key = f"tvdb_series::{tvdb_id}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            data = await self._tvdb_get(f"/series/{tvdb_id}/extended", {"meta": "translations"})
            if data:
                self._cache[cache_key] = data.get("data")
                return self._cache[cache_key]
        except Exception:
            pass
        return None

    async def _tvdb_episode(self, tvdb_id: int, season: int, episode: int):
        cache_key = f"tvdb_ep::{tvdb_id}::{season}::{episode}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            data = await self._tvdb_get(f"/series/{tvdb_id}/episodes/default", {"page": 0})
            if not data:
                return None
            block = data.get("data", {})
            eps = block.get("episodes", []) if isinstance(block, dict) else block
            for ep in eps or []:
                try:
                    if int(ep.get("seasonNumber", -1)) == season and int(ep.get("number", -1)) == episode:
                        self._cache[cache_key] = ep
                        return ep
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        return None

    async def _tvdb_episode_absolute(self, tvdb_id: int, absolute: int):
        cache_key = f"tvdb_abs::{tvdb_id}::{absolute}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            # Try absolute order
            data = await self._tvdb_get(f"/series/{tvdb_id}/episodes/absolute", {"page": 0})
            if data:
                block = data.get("data", {})
                eps = block.get("episodes", []) if isinstance(block, dict) else block
                for ep in eps or []:
                    try:
                        if int(ep.get("number", -1)) == absolute:
                            self._cache[cache_key] = ep
                            return ep
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        return None

    async def _kitsu_search(self, title: str):
        """Kitsu search for anime."""
        cache_key = f"kitsu::{title}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            resp = await self.client.get(
                "https://kitsu.io/api/edge/anime",
                params={"filter[text]": title, "page[limit]": 10},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("data", [])
                if results:
                    self._cache[cache_key] = results[0]
                    return results[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _simple_sim(a: str, b: str) -> float:
        """Simple similarity fallback when app stack unavailable."""
        if not a or not b:
            return 0.0
        a_n = re.sub(r"[^a-z0-9]", "", a.lower())
        b_n = re.sub(r"[^a-z0-9]", "", b.lower())
        if not a_n or not b_n:
            return 0.0
        # Jaccard on character bigrams
        def bigrams(s):
            return {s[i:i+2] for i in range(len(s)-1)}
        sa, sb = bigrams(a_n), bigrams(b_n)
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    async def resolve_tv(
        self,
        title: str,
        season: int,
        episode: int,
        year: Optional[int] = None,
        quality: Optional[str] = None,
        encoded_string: Optional[str] = None,
        is_anime: bool = False,
    ) -> Optional[dict]:
        """Resolve a TV episode using the provider priority chain."""
        # Anime: Kitsu first
        if is_anime:
            try:
                result = await self._resolve_anime_kitsu(title, season, episode, year, quality, encoded_string)
                if result:
                    return result
            except Exception as e:
                print(f"    [Kitsu] error for '{title}': {e}")

        # TVDB
        try:
            result = await self._resolve_tvdb_series(title, season, episode, year, quality, encoded_string)
            if result:
                return result
        except Exception as e:
            print(f"    [TVDB] error for '{title}': {e}")

        # TMDB
        try:
            result = await self._resolve_tmdb_series(title, season, episode, year, quality, encoded_string)
            if result:
                return result
        except Exception as e:
            print(f"    [TMDB] error for '{title}': {e}")

        # Cinemeta (if available)
        try:
            result = await self._resolve_cinemeta_series(title, season, episode, year, quality, encoded_string)
            if result:
                return result
        except Exception as e:
            print(f"    [Cinemeta] error for '{title}': {e}")

        return None

    async def _resolve_anime_kitsu(
        self, title: str, season: Optional[int], episode: int,
        year: Optional[int], quality: Optional[str], encoded_string: Optional[str],
    ) -> Optional[dict]:
        hit = await self._kitsu_search(title)
        if not hit:
            return None
        attrs = hit.get("attributes", {})
        kitsu_id = hit.get("id")
        # Kitsu doesn't have season/episode structure in the same way; use
        # the anime-level metadata and construct a payload.
        return {
            "tmdb_id": None,
            "imdb_id": None,
            "kitsu_id": int(kitsu_id) if kitsu_id and kitsu_id.isdigit() else None,
            "title": attrs.get("canonicalTitle") or attrs.get("title") or title,
            "title_english": attrs.get("canonicalTitle") or title,
            "original_title": attrs.get("originalTitle") or "",
            "year": int(attrs.get("startDate", "")[:4]) if attrs.get("startDate") else 0,
            "rate": float(attrs.get("averageRating", "0")) / 10 if attrs.get("averageRating") else 0,
            "description": attrs.get("synopsis") or "",
            "poster": (attrs.get("posterImage") or {}).get("medium", ""),
            "backdrop": (attrs.get("coverImage") or {}).get("original", ""),
            "logo": "",
            "genres": [],
            "media_type": "tv",
            "cast": [],
            "runtime": str(attrs.get("episodeLength", "")) if attrs.get("episodeLength") else "",
            "original_language": attrs.get("originalLanguage"),
            "origin_country": [],
            "season_number": season if season is not None else 1,
            "episode_number": episode,
            "episode_title": f"Episode {episode}",
            "episode_backdrop": "",
            "episode_overview": "",
            "episode_released": "",
            "quality": quality or "HD",
            "encoded_string": encoded_string or "",
            "source": "kitsu",
        }

    async def _resolve_tvdb_series(
        self, title: str, season: int, episode: int,
        year: Optional[int], quality: Optional[str], encoded_string: Optional[str],
    ) -> Optional[dict]:
        hit = await self._tvdb_search(title, entity="series", year=year)
        if not hit:
            return None
        tvdb_id = hit.get("tvdb_id") or hit.get("id")
        try:
            tvdb_id = int(tvdb_id)
        except (TypeError, ValueError):
            return None

        series = await self._tvdb_series_extended(tvdb_id)
        if not series:
            series = {
                "id": tvdb_id,
                "name": hit.get("name"),
                "overview": hit.get("overview"),
                "year": hit.get("year"),
                "image": hit.get("image_url") or hit.get("image"),
            }

        ep = await self._tvdb_episode(tvdb_id, season, episode)
        if ep:
            ep_title = ep.get("name") or f"S{season:02d}E{episode:02d}"
        else:
            ep_title = f"S{season:02d}E{episode:02d}"

        return {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": tvdb_id,
            "title": series.get("name") or title,
            "title_english": series.get("name") or title,
            "original_title": series.get("name") or "",
            "year": self._tvdb_year(series),
            "rate": 0.0,
            "description": series.get("overview") or "",
            "poster": "",
            "backdrop": "",
            "logo": "",
            "genres": series.get("genres", []) if isinstance(series.get("genres"), list) else [],
            "media_type": "tv",
            "cast": [],
            "runtime": "",
            "original_language": None,
            "origin_country": [],
            "season_number": season,
            "episode_number": episode,
            "episode_title": ep_title,
            "episode_backdrop": "",
            "episode_overview": ep.get("overview", "") if ep else "",
            "episode_released": str(ep.get("firstAired", "")) if ep else "",
            "quality": quality or "HD",
            "encoded_string": encoded_string or "",
            "source": "tvdb",
        }

    async def _resolve_tmdb_series(
        self, title: str, season: int, episode: int,
        year: Optional[int], quality: Optional[str], encoded_string: Optional[str],
    ) -> Optional[dict]:
        hit = await self._tmdb_search(title, "tv", year)
        if not hit:
            return None
        tmdb_id = hit.get("id")
        details = await self._tmdb_details("tv", tmdb_id)

        # Decimal/OVA episode (e.g. S01E22.5): look up TMDB season 0 specials
        if isinstance(episode, float):
            ep = None
            specials = await self._tmdb_season0_specials(tmdb_id)
            if specials:
                # Try to match the OVA by title (from filename) against special names
                ep = self._match_special_by_title(specials, title)
                if not ep and specials:
                    # Fall back to first special if only one exists
                    if len(specials) == 1:
                        ep = specials[0]
        else:
            ep = await self._tmdb_episode(tmdb_id, season, episode) if details else None

        first_air = (details or {}).get("first_air_date", "")
        year_val = int(first_air[:4]) if first_air else 0

        return {
            "tmdb_id": tmdb_id,
            "imdb_id": (details or {}).get("external_ids", {}).get("imdb_id") if details else None,
            "title": (details or {}).get("name") or title,
            "title_english": (details or {}).get("name") or title,
            "original_title": (details or {}).get("original_name") or "",
            "year": year_val,
            "rate": float((details or {}).get("vote_average", 0) or 0),
            "description": (details or {}).get("overview") or "",
            "poster": f"https://image.tmdb.org/t/p/w500{(details or {}).get('poster_path')}" if (details or {}).get("poster_path") else "",
            "backdrop": f"https://image.tmdb.org/t/p/original{(details or {}).get('backdrop_path')}" if (details or {}).get("backdrop_path") else "",
            "logo": "",
            "genres": [g.get("name", "") for g in (details or {}).get("genres", []) or []],
            "media_type": "tv",
            "cast": [c.get("name", "") for c in (details or {}).get("credits", {}).get("cast", []) or []][:10],
            "runtime": "",
            "original_language": (details or {}).get("original_language"),
            "origin_country": (details or {}).get("origin_country", []) or [],
            "season_number": season,
            "episode_number": episode,
            "episode_title": ep.get("name", f"S{season:02d}E{episode:02d}") if ep else f"S{season:02d}E{episode:02d}",
            "episode_backdrop": f"https://image.tmdb.org/t/p/original{ep.get('still_path')}" if ep and ep.get("still_path") else "",
            "episode_overview": ep.get("overview", "") if ep else "",
            "episode_released": ep.get("air_date", "") if ep else "",
            "quality": quality or "HD",
            "encoded_string": encoded_string or "",
            "source": "tmdb",
        }

    async def _resolve_cinemeta_series(
        self, title: str, season: int, episode: int,
        year: Optional[int], quality: Optional[str], encoded_string: Optional[str],
    ) -> Optional[dict]:
        """Fallback via Cinemeta (Stremio addon API)."""
        try:
            resp = await self.client.get(
                f"https://v3-cinemeta.strem.io/catalog/tv/top/{quote(title)}.json",
                timeout=15.0,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            metas = data.get("metas", [])
            if not metas:
                return None
            best = metas[0]
            imdb_id = best.get("id", "")
            # Fetch details
            det_resp = await self.client.get(
                f"https://v3-cinemeta.strem.io/meta/tv_detail/{imdb_id}.json",
                timeout=15.0,
            )
            if det_resp.status_code == 200:
                det = det_resp.json().get("meta", {})
                return {
                    "tmdb_id": None,
                    "imdb_id": imdb_id if imdb_id.startswith("tt") else None,
                    "title": det.get("name") or title,
                    "title_english": det.get("name") or title,
                    "original_title": det.get("originalTitle") or "",
                    "year": year_from_str(det.get("year")) if (year_from_str := __import__("Backend.helper.metadata.common", fromlist=["year_from_str"]).year_from_str) else 0,
                    "rate": float(det.get("rating", 0) or 0),
                    "description": det.get("description") or "",
                    "poster": det.get("poster", ""),
                    "backdrop": det.get("background", ""),
                    "logo": det.get("logo", ""),
                    "genres": det.get("genres", []) if isinstance(det.get("genres"), list) else [],
                    "media_type": "tv",
                    "cast": det.get("cast", []) if isinstance(det.get("cast"), list) else [],
                    "runtime": "",
                    "original_language": None,
                    "origin_country": [],
                    "season_number": season,
                    "episode_number": episode,
                    "episode_title": f"S{season:02d}E{episode:02d}",
                    "episode_backdrop": "",
                    "episode_overview": "",
                    "episode_released": "",
                    "quality": quality or "HD",
                    "encoded_string": encoded_string or "",
                    "source": "cinemeta",
                }
        except Exception:
            pass
        return None

    async def resolve_movie(
        self,
        title: str,
        year: Optional[int] = None,
        quality: Optional[str] = None,
        encoded_string: Optional[str] = None,
    ) -> Optional[dict]:
        """Resolve a movie using TMDB → Cinemeta."""
        # TMDB first
        hit = await self._tmdb_search(title, "movie", year)
        if hit:
            tmdb_id = hit.get("id")
            details = await self._tmdb_details("movie", tmdb_id)
            release = (details or {}).get("release_date", "")
            year_val = int(release[:4]) if release else 0
            return {
                "tmdb_id": tmdb_id,
                "imdb_id": (details or {}).get("external_ids", {}).get("imdb_id") if details else None,
                "title": (details or {}).get("title") or title,
                "title_english": (details or {}).get("title") or title,
                "original_title": (details or {}).get("original_title") or "",
                "year": year_val,
                "rate": float((details or {}).get("vote_average", 0) or 0),
                "description": (details or {}).get("overview") or "",
                "poster": f"https://image.tmdb.org/t/p/w500{(details or {}).get('poster_path')}" if (details or {}).get("poster_path") else "",
                "backdrop": f"https://image.tmdb.org/t/p/original{(details or {}).get('backdrop_path')}" if (details or {}).get("backdrop_path") else "",
                "logo": "",
                "genres": [g.get("name", "") for g in (details or {}).get("genres", []) or []],
                "media_type": "movie",
                "cast": [c.get("name", "") for c in (details or {}).get("credits", {}).get("cast", []) or []][:10],
                "runtime": str((details or {}).get("runtime", "")) if (details or {}).get("runtime") else "",
                "original_language": (details or {}).get("original_language"),
                "origin_country": (details or {}).get("origin_country", []) or [],
                "quality": quality or "HD",
                "encoded_string": encoded_string or "",
                "source": "tmdb",
            }

        # Cinemeta fallback
        try:
            resp = await self.client.get(
                f"https://v3-cinemeta.strem.io/catalog/movie/top/{quote(title)}.json",
                timeout=15.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                metas = data.get("metas", [])
                if metas:
                    best = metas[0]
                    imdb_id = best.get("id", "")
                    return {
                        "tmdb_id": None,
                        "imdb_id": imdb_id if imdb_id.startswith("tt") else None,
                        "title": best.get("name") or title,
                        "title_english": best.get("name") or title,
                        "original_title": best.get("originalTitle") or "",
                        "year": year_from_str(best.get("year")) if (year_from_str := __import__("Backend.helper.metadata.common", fromlist=["year_from_str"]).year_from_str) else 0,
                        "rate": float(best.get("rating", 0) or 0),
                        "description": best.get("description") or "",
                        "poster": best.get("poster", ""),
                        "backdrop": best.get("background", ""),
                        "logo": best.get("logo", ""),
                        "genres": best.get("genres", []) if isinstance(best.get("genres"), list) else [],
                        "media_type": "movie",
                        "cast": best.get("cast", []) if isinstance(best.get("cast"), list) else [],
                        "runtime": "",
                        "original_language": None,
                        "origin_country": [],
                        "quality": quality or "HD",
                        "encoded_string": encoded_string or "",
                        "source": "cinemeta",
                    }
        except Exception:
            pass
        return None

    async def close(self):
        try:
            await self.client.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Payload builder: converts resolution + parse into DB-ready document
# ---------------------------------------------------------------------------

def build_payload(parsed: dict, resolved: Optional[dict], channel_id: int, msg_id: int) -> dict:
    """Build a MongoDB-ready media document from parsed filename + resolved metadata."""
    resolved = resolved or {}
    quality = parsed.get("quality") or resolved.get("quality") or "HD"
    encoded_string = f"{channel_id}:{msg_id}"

    base = {
        "tmdb_id": resolved.get("tmdb_id"),
        "imdb_id": resolved.get("imdb_id"),
        "kitsu_id": resolved.get("kitsu_id"),
        "tvdb_id": resolved.get("tvdb_id"),
        "anilist_id": resolved.get("anilist_id"),
        "title": resolved.get("title") or parsed.get("title") or "Unknown",
        "title_english": resolved.get("title_english") or parsed.get("title") or "",
        "original_title": resolved.get("original_title") or "",
        "year": resolved.get("year") or parsed.get("year") or 0,
        "rate": resolved.get("rate") or 0,
        "description": resolved.get("description") or "",
        "poster": resolved.get("poster") or "",
        "backdrop": resolved.get("backdrop") or "",
        "logo": resolved.get("logo") or "",
        "genres": resolved.get("genres") or [],
        "cast": resolved.get("cast") or [],
        "runtime": resolved.get("runtime") or "",
        "original_language": resolved.get("original_language"),
        "origin_country": resolved.get("origin_country") or [],
        "media_type": "movie" if parsed.get("is_movie") or resolved.get("media_type") == "movie" else "tv",
        "quality": quality,
        "encoded_string": encoded_string,
        "source": resolved.get("source") or "unknown",
        "group": parsed.get("group"),
        "is_split": parsed.get("is_split", False),
        "split_part": parsed.get("split_part"),
        "absolute_episode": parsed.get("episode") if parsed.get("absolute") else None,
    }

    if base["media_type"] == "tv":
        season = parsed.get("season")
        if parsed.get("special"):
            season = 0
        base["season_number"] = season if season is not None else 1
        base["episode_number"] = parsed.get("episode") or 1
        base["episode_title"] = resolved.get("episode_title") or f"S{base['season_number']:02d}E{base['episode_number']:02d}"
        base["episode_backdrop"] = resolved.get("episode_backdrop") or ""
        base["episode_overview"] = resolved.get("episode_overview") or ""
        base["episode_released"] = resolved.get("episode_released") or ""

        # Combined episodes
        if parsed.get("combined"):
            base["combined"] = True
            base["combined_start"] = parsed.get("combined_start")
            base["combined_end"] = parsed.get("combined_end")
            base["episode_title"] = f"Season {base['season_number']} Combined"
    else:
        base["season_number"] = None
        base["episode_number"] = None

    # The shared pipeline already mapped absolute numbers, specials and
    # movies; take its numbers as they are.
    if resolved.get("resolved_by") == "live":
        base["media_type"] = "movie" if resolved.get("media_type") == "movie" else "tv"
        if base["media_type"] == "tv":
            if resolved.get("season_number") is not None:
                base["season_number"] = int(resolved["season_number"])
            if resolved.get("episode_number") is not None:
                base["episode_number"] = int(resolved["episode_number"])
            base["absolute_episode"] = resolved.get("absolute_episode")
            if resolved.get("episode_title") and not parsed.get("combined"):
                base["episode_title"] = resolved["episode_title"]
            base.setdefault("episode_backdrop", resolved.get("episode_backdrop") or "")
            base.setdefault("episode_overview", resolved.get("episode_overview") or "")
            base.setdefault("episode_released", resolved.get("episode_released") or "")
            if resolved.get("watch_after"):
                base["watch_after"] = resolved["watch_after"]
        else:
            base["season_number"] = None
            base["episode_number"] = None
            base["absolute_episode"] = None
            for key in ("episode_title", "episode_backdrop", "episode_overview", "episode_released"):
                base.pop(key, None)

    # An id in the name or caption ({tmdb-1234}, an IMDb link, tt1234567)
    # overrides the title search.
    explicit_imdb = parsed.get("explicit_imdb_id")
    explicit_tmdb = parsed.get("explicit_tmdb_id")
    explicit_tvdb = parsed.get("explicit_tvdb_id")
    if explicit_imdb and base.get("imdb_id") != explicit_imdb:
        base["imdb_id"] = explicit_imdb
        if not explicit_tmdb:
            base["tmdb_id"] = None  # belonged to a different title
    if explicit_tmdb:
        if base.get("tmdb_id") != explicit_tmdb and not explicit_imdb:
            base["imdb_id"] = None
        base["tmdb_id"] = explicit_tmdb
    if explicit_tvdb:
        base["tvdb_id"] = explicit_tvdb

    # Ensure stable IDs
    if _HAS_APP_STACK:
        base = ensure_media_ids(base, seed=f"{base['title']}:{base['year']}:{base['media_type']}")
    else:
        # Minimal ID generation
        if not base.get("tmdb_id"):
            digest = int(hashlib.md5(f"{base['title']}:{base['year']}".encode()).hexdigest()[:8], 16)
            base["tmdb_id"] = -(digest % 1_000_000_000 + 1)
        if not base.get("imdb_id"):
            base["imdb_id"] = f"tg{abs(int(base['tmdb_id']))}"

    return base


# ---------------------------------------------------------------------------
# Filler / recap enrichment (Jikan v4): additive, does not alter resolution
# ---------------------------------------------------------------------------

# Filler lookups are optional: each call has a deadline, and after a few
# consecutive failures Jikan is skipped for the rest of the run.
JIKAN_CALL_TIMEOUT = 15.0
JIKAN_MAX_FAILURES = 3
_jikan_failures = 0
_jikan_mal_ids: dict = {}


async def _jikan_call(coro):
    """Await a Jikan call under a deadline; trips the breaker after repeated failures."""
    global _jikan_failures
    try:
        value = await asyncio.wait_for(coro, JIKAN_CALL_TIMEOUT)
    except Exception as e:
        _jikan_failures += 1
        if _jikan_failures == JIKAN_MAX_FAILURES:
            print(f"    [filler] Jikan failed {JIKAN_MAX_FAILURES} times in a row ({type(e).__name__}); "
                  f"filler flags are skipped for the rest of this run")
        raise
    _jikan_failures = 0
    return value


async def enrich_with_filler(payload: dict, resolved: Optional[dict]) -> dict:
    """Enrich a payload with Jikan filler/recap data for anime episodes.

    This is purely additive: it never overrides existing fields, never
    interferes with the resolver chain, and unknown stays unknown.

    Returns the payload with is_filler / filler_type added (None if unknown
    or if Jikan is disabled / unavailable).
    """
    if not JIKAN_ENABLED or not _JAKAN_AVAILABLE or not _jikan_mod:
        return payload
    if _jikan_failures >= JIKAN_MAX_FAILURES:
        return payload

    # Only enrich TV entries that look like anime
    if payload.get("media_type") != "tv":
        return payload

    # Need a season + episode to look up
    season = payload.get("season_number")
    episode = payload.get("episode_number")
    if season is None or episode is None:
        return payload
    # Jikan numbers episodes per MyAnimeList entry and this is the show's
    # first entry, so later seasons and specials are left without flags.
    if payload.get("absolute_episode"):
        episode = payload["absolute_episode"]
    elif int(season) != 1:
        return payload

    # Jikan needs a MyAnimeList id; Kitsu and AniList ids are not
    # interchangeable with it.
    mal_id = (resolved or {}).get("mal_id")

    # Otherwise search Jikan by title (once per title)
    title = payload.get("title") or (resolved or {}).get("title")
    if not mal_id and title:
        if title in _jikan_mal_ids:
            mal_id = _jikan_mal_ids[title]
        else:
            try:
                search_results = await _jikan_call(_jikan_mod.search_anime(title))
                for result in search_results or []:
                    if result.get("mal_id"):
                        mal_id = result["mal_id"]
                        break
                _jikan_mal_ids[title] = mal_id
            except Exception:
                mal_id = None

    if not mal_id:
        # Unknown stays unknown, but the fields are always present.
        payload.setdefault("is_filler", None)
        payload.setdefault("filler_type", None)
        return payload

    try:
        filler_info = await _jikan_call(_jikan_mod.get_episode_filler(int(mal_id), int(episode)))
        payload["is_filler"] = filler_info.get("filler") if filler_info else None
        payload["filler_type"] = _jikan_mod.classify_filler(filler_info)
    except Exception:
        payload["is_filler"] = None
        payload["filler_type"] = None

    return payload


# ---------------------------------------------------------------------------
# Input loaders: TSV dump, directory scan, stdin
# ---------------------------------------------------------------------------

def _looks_like_jsonl(dump_path: str) -> bool:
    try:
        with open(dump_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    return line.lstrip().startswith("{")
    except OSError:
        pass
    return False


def load_records_from_jsonl(dump_path: str) -> list[dict]:
    """Load a Telegram channel dump: one JSON object per line.

    Keys: chat_id, msg_id, kind, file_name, caption, size, width, height,
    duration. The caption is used as the name, as the bot does (file_name is
    often truncated). chat_id and msg_id are kept for the stream links.
    """
    records = []
    skipped = 0
    with open(dump_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            caption = (obj.get("caption") or "").strip()
            tg_name = (obj.get("file_name") or "").strip()
            name = caption or tg_name
            if not name:
                skipped += 1
                continue
            records.append({
                "media_type": "",
                "storage": "telegram",
                "title": "",
                "season": None,
                "episode": None,
                "stored_label": "",
                "size": obj.get("size") or "",
                "file_name": name,
                "tg_file_name": tg_name,
                "is_split": False,
                "chat_id": obj.get("chat_id"),
                "msg_id": obj.get("msg_id"),
                "kind": obj.get("kind"),
                "width": obj.get("width") or 0,
                "height": obj.get("height") or 0,
                "duration": obj.get("duration") or 0,
                "raw_size": obj.get("size") or 0,
            })
    print(f"Loaded {len(records)} records from {dump_path} (JSONL, skipped {skipped} bad lines).")
    return records


def _record_quality_hint(rec: dict) -> Optional[str]:
    """Resolution fallback from a dump record's media attributes (video only)."""
    if not _HAS_SHARED_PARSER or rec.get("kind", "video") != "video":
        return None
    if not (rec.get("width") or rec.get("height")):
        return None
    return _shared_quality_hint(
        rec.get("width") or 0, rec.get("height") or 0,
        rec.get("raw_size") or 0, rec.get("duration") or 0,
    )


def write_parse_preview(records: list[dict], out_base: str) -> str:
    """Parse every name without network or database access.

    Writes <out_base>_parse.tsv with one row per file and prints a summary
    per title.
    """
    tsv_path = _safe_path(f"{out_base}_parse.tsv", "report output path")
    by_title: dict[tuple, list] = {}
    spellings: dict[tuple, dict] = {}
    skipped = 0
    lookup_online = 0

    def _title_key(title) -> str:
        # Group spelling variants ("Hell's Paradise" / "Hells Paradise")
        text = re.sub(r"['\u2018\u2019`\u00B4]", "", str(title or "")).lower()
        return re.sub(r"[^a-z0-9]+", " ", text).strip() or str(title)
    rows = ["msg_id\ttype\ttitle\tseason\tepisode\tabsolute\tquality\tyear\tsplit_part\tcombined\tnote\tname"]
    for rec in records:
        name = rec.get("file_name", "")
        p = parse_filename(name, quality_hint=_record_quality_hint(rec))
        if p.get("skip_reason"):
            kind, skipped = "skip", skipped + 1
        elif p.get("is_movie"):
            kind = "movie"
        else:
            kind = "tv"
        combined = f"{p.get('combined_start')}-{p.get('combined_end')}" if p.get("combined") else ""
        note = p.get("skip_reason")
        if p.get("skip_reason") == _SKIP_TITLE_MISSING and p.get("episode_title"):
            lookup_online += 1
            note = (f"no show name: an online run looks the show up from the episode title "
                    f"'{p['episode_title']}' and indexes the file only if a provider confirms it")
        elif p.get("watch_after"):
            wa = p["watch_after"]
            after = f"S{int(wa['season']):02d}E{int(wa['episode']):02d}" if wa.get("season") else f"episode {wa['episode']}"
            note = f"special, watch after {after}; an online run uses the provider's season-0 number"
        rows.append("\t".join(str(v) if v is not None else "" for v in (
            rec.get("msg_id"), kind, p.get("title"), p.get("season"), p.get("episode"),
            "abs" if p.get("absolute") else "", p.get("quality"), p.get("year"),
            p.get("split_part"), combined, note,
            " | ".join(name.splitlines()),
        )))
        if kind != "skip":
            key = (kind, _title_key(p.get("title")))
            by_title.setdefault(key, []).append(p)
            seen = spellings.setdefault(key, {})
            seen[p.get("title")] = seen.get(p.get("title"), 0) + 1

    with open(tsv_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(rows) + "\n")

    print(f"\nParse preview: {len(records)} names -> {len(by_title)} titles, {skipped} skipped")
    if lookup_online:
        print(f"  ({lookup_online} of the skipped names have no show name but do have an episode title; "
              f"an online run looks those shows up)")
    for key, items in sorted(by_title.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        kind = key[0]
        variants = sorted(spellings[key].items(), key=lambda kv: (-kv[1], str(kv[0])))
        title = variants[0][0]
        if len(variants) > 1:
            title = f"{title}  (also written: {', '.join(str(v) for v, _ in variants[1:])})"
        if kind == "movie":
            parts = sorted(i["split_part"] for i in items if i.get("split_part"))
            extra = f" parts {parts}" if parts else ""
            print(f"  movie  {title}{extra} [{items[0].get('quality')}]")
            continue
        groups: dict[str, list[int]] = {}
        for i in items:
            label = "abs" if i.get("absolute") else f"S{i.get('season')}"
            if isinstance(i.get("episode"), int):
                groups.setdefault(label, []).append(i["episode"])
        desc = "; ".join(f"{label}: {_compact_ranges(eps)}" for label, eps in sorted(groups.items()))
        print(f"  tv     {title} -> {desc}")
    print(f"\nWrote {tsv_path}")
    return tsv_path


def _compact_ranges(numbers: list[int]) -> str:
    """[1,2,3,5,6,9] -> '1-3,5-6,9' (duplicates collapsed)."""
    nums = sorted(set(numbers))
    if not nums:
        return "-"
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = n
    out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


def load_records_from_tsv(dump_path: str) -> list[dict]:
    """Load records from dump_all_filenames.py TSV output (or a JSONL Telegram dump)."""
    dump_path = _safe_path(dump_path, "--dump", must_exist=True)
    if _looks_like_jsonl(dump_path):
        return load_records_from_jsonl(dump_path)
    records = []
    skipped = 0
    with open(dump_path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#") or line.startswith("==") or line.startswith("---"):
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                skipped += 1
                continue
            media_type = parts[0].strip()
            storage = parts[1].strip()
            title = parts[2].strip()
            sxe = parts[3].strip()
            stored_label = parts[4].strip()
            size = parts[5].strip()
            split_flag = parts[6].strip()
            file_name = parts[7].strip()
            if not title or not file_name:
                skipped += 1
                continue
            # Parse SxE
            season = episode = None
            m = re.match(r"(?i)^S(\d{1,2})E(\d{1,3})$", sxe.strip())
            if m:
                season, episode = int(m.group(1)), int(m.group(2))
            records.append({
                "media_type": media_type,
                "storage": storage,
                "title": title,
                "season": season,
                "episode": episode,
                "stored_label": stored_label,
                "size": size,
                "file_name": file_name,
                "is_split": split_flag not in ("-", ""),
            })
    print(f"Loaded {len(records)} records from {dump_path} (skipped {skipped} non-data lines).")
    return records


def scan_directory(scan_dir: str, exts: list[str]) -> list[dict]:
    """Recursively scan a directory for media files."""
    scan_dir = _safe_dir(scan_dir, "--scan", must_exist=True)
    ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts}
    records = []
    for root, _dirs, files in os.walk(scan_dir):
        for fname in files:
            if _EXTENSION_RE.search(fname):
                ext = os.path.splitext(fname)[1].lower()
                if ext in ext_set:
                    full_path = os.path.join(root, fname)
                    records.append({
                        "media_type": "tv",  # default; resolved later
                        "storage": "scan",
                        "title": "",
                        "season": None,
                        "episode": None,
                        "stored_label": "",
                        "size": "",
                        "file_name": full_path,
                        "is_split": False,
                        "_from_scan": True,
                    })
    print(f"Scanned {scan_dir}: found {len(records)} files matching {sorted(ext_set)}.")
    return records


# ---------------------------------------------------------------------------
# Main indexing flow
# ---------------------------------------------------------------------------

async def _resolve_with_live_pipeline(
    file_name: str, parsed: dict, rec: dict, channel_id: int, msg_id: int, anime: bool,
) -> Optional[dict]:
    """Resolve through the bot's own pipeline (see _HAS_SHARED_RESOLVER)."""
    explicit = parsed.get("explicit_imdb_id") or parsed.get("explicit_tmdb_id")
    name = _BRACKET_ID_RE.sub(" ", file_name)
    result = await _shared_resolve_name(
        _shared_clean_filename(name),
        channel_id,
        msg_id,
        anime_channel=anime,
        override_id=str(explicit) if explicit else None,
        quality_hint=_record_quality_hint(rec),
        encoded_string=f"{channel_id}:{msg_id}",
    )
    if not result:
        return None
    result = dict(result)
    result["resolved_by"] = "live"
    if not result.get("source"):
        tmdb_id = result.get("tmdb_id")
        imdb_id = str(result.get("imdb_id") or "")
        if result.get("kitsu_id"):
            result["source"] = "kitsu"
        elif result.get("tvdb_id"):
            result["source"] = "tvdb"
        elif isinstance(tmdb_id, int) and tmdb_id > 0:
            result["source"] = "tmdb"
        elif imdb_id.startswith("tt"):
            result["source"] = "cinemeta"
    return result


async def process_record(
    resolver: ProviderResolver,
    rec: dict,
    channel_id: int = 0,
    msg_id: int = 0,
    is_anime_channel: bool = False,
) -> dict:
    """Process a single record: parse filename, resolve metadata, build payload."""
    file_name = rec.get("file_name", "")

    # Telegram dump records carry their own ids and media attributes.
    if rec.get("msg_id") is not None:
        msg_id = rec["msg_id"]
    if rec.get("chat_id") is not None and not channel_id:
        channel_id = int(str(rec["chat_id"]).replace("-100", ""))
    parsed = parse_filename(file_name, quality_hint=_record_quality_hint(rec))
    anime = is_anime_channel or bool(rec.get("is_anime"))
    use_live = _HAS_SHARED_RESOLVER and not USE_LEGACY_RESOLVER and rec.get("media_type") != "movie"

    live_resolved = None
    if use_live:
        try:
            live_resolved = await _resolve_with_live_pipeline(file_name, parsed, rec, channel_id, msg_id, anime)
        except Exception as e:
            print(f"    [resolve] error for '{file_name[:60]}': {e}")
        if live_resolved and parsed.get("skip_reason") and live_resolved.get("title_from_episode_lookup"):
            # Show found through the episode title lookup
            parsed.pop("skip_reason", None)
            parsed["title"] = live_resolved.get("title")
            parsed["title_from_episode_lookup"] = True

    if parsed.get("skip_reason"):
        # Nothing to search for, e.g. a name without a show title
        return {
            "raw_filename": file_name,
            "parsed": parsed,
            "resolved": None,
            "payload": None,
            "confidence": "skipped",
            "action": "skip",
            "skip_reason": parsed["skip_reason"],
            "is_movie": False,
            "anime": is_anime_channel or bool(rec.get("is_anime")),
            "is_filler": None,
            "filler_type": None,
        }

    # Determine media type
    is_movie = parsed.get("is_movie", False)
    if rec.get("media_type") == "movie":
        is_movie = True
    if live_resolved:
        is_movie = live_resolved.get("media_type") == "movie"

    # Apply title alias for season-specific nyaa/scene titles
    title = parsed.get("title") or ""
    season = parsed.get("season")
    if season and not is_movie:
        aliased = _alias_title(title, season)
        if aliased != title:
            parsed["title"] = aliased
            title = aliased

    # Build encoded string
    encoded = f"{channel_id}:{msg_id}"

    # Resolve
    resolved = live_resolved
    try:
        if use_live:
            pass  # already resolved by the shared pipeline
        elif is_movie:
            resolved = await resolver.resolve_movie(
                title=title,
                year=parsed.get("year"),
                quality=parsed.get("quality"),
                encoded_string=encoded,
            )
        else:
            use_season = season if season is not None else 1
            use_episode = parsed.get("episode") or 1
            resolved = await resolver.resolve_tv(
                title=title,
                season=use_season,
                episode=use_episode,
                year=parsed.get("year"),
                quality=parsed.get("quality"),
                encoded_string=encoded,
                is_anime=anime,
            )
    except Exception as e:
        print(f"    [resolve] error for '{file_name[:60]}': {e}")

    payload = build_payload(parsed, resolved, channel_id, msg_id)

    # Filler / recap enrichment (anime only, additive, unknown stays unknown)
    try:
        payload = await enrich_with_filler(payload, resolved)
    except Exception as e:
        print(f"    [filler] enrichment skipped for '{file_name[:60]}': {e}")

    # Confidence scoring for the report
    confidence = "high" if resolved and resolved.get("source") in ("tmdb", "tvdb", "kitsu") else ("medium" if resolved else "low")
    if parsed.get("explicit_tmdb_id") or parsed.get("explicit_imdb_id"):
        confidence = "high"  # explicit ID overrides

    return {
        "raw_filename": file_name,
        "parsed": parsed,
        "resolved": resolved,
        "payload": payload,
        "confidence": confidence,
        "action": "add",  # always 'add' for new indexing; could be 'update' if matching existing
        "is_movie": is_movie,
        "anime": anime,
        "is_filler": payload.get("is_filler"),
        "filler_type": payload.get("filler_type"),
    }


async def run_index(
    records: list[dict],
    channel_id: int,
    apply: bool,
    dry_run: bool,
    is_anime_channel: bool = False,
    max_concurrent: int = MAX_CONCURRENT,
) -> list[dict]:
    """Process all records through the resolver."""
    results = []
    sem = asyncio.Semaphore(max_concurrent)

    async def _worker(rec):
        async with sem:
            await asyncio.sleep(REQUEST_DELAY * 0.1)  # micro-delay to spread load
            return await process_record(resolver, rec, channel_id, len(results), is_anime_channel)

    resolver = ProviderResolver(tmdb_key=TMDB_API_KEY, tvdb_key=TVDB_API_KEY)
    try:
        for i, rec in enumerate(records):
            res = await _worker(rec)
            results.append(res)
            if (i + 1) % 10 == 0:
                print(f"  Processed {i + 1}/{len(records)}...")
    finally:
        await resolver.close()

    # Watch-order post-processing: group anime by franchise and assign order
    if _WATCH_ORDER_AVAILABLE and _watch_order_mod:
        anime_entries = [
            r["payload"] for r in results
            if r.get("anime") and r.get("payload") and r["payload"].get("media_type") == "tv"
        ]
        if anime_entries:
            try:
                assignments = await _watch_order_mod.assign_watch_order(anime_entries)
                for r in results:
                    payload = r.get("payload")
                    if not payload:
                        continue
                    key = _watch_order_mod._anime_key(payload)
                    if key and key in assignments:
                        a = assignments[key]
                        if payload.get("watch_order_group") is None:
                            payload["watch_order_group"] = a.get("watch_order_group")
                        if payload.get("watch_order_index") is None:
                            payload["watch_order_index"] = a.get("watch_order_index")
                        r["watch_order_group"] = payload.get("watch_order_group")
                        r["watch_order_index"] = payload.get("watch_order_index")
            except Exception as e:
                print(f"    [watch-order] assignment skipped: {e}")

    return results


def _one_line(text: str) -> str:
    """Captions can span several lines (name + IMDb link); keep report rows single-line."""
    return " | ".join(part.strip() for part in str(text or "").splitlines() if part.strip())


def write_report(results: list[dict], out_base: str):
    """Write human-readable and JSON reports."""
    txt_path = _safe_path(f"{out_base}.txt", "report output path")
    json_path = _safe_path(f"{out_base}.json", "report output path")

    high = [r for r in results if r["confidence"] == "high"]
    med = [r for r in results if r["confidence"] == "medium"]
    low = [r for r in results if r["confidence"] == "low"]

    lines = [
        "INDEXER REPORT (read-only unless --apply)",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Total processed: {len(results)}",
        f"  High confidence: {len(high)}",
        f"  Medium confidence: {len(med)}",
        f"  Low confidence: {len(low)}",
        "",
    ]

    lines.append("== HIGH CONFIDENCE (will be indexed) ==")
    for r in high:
        p = r["parsed"]
        pay = r["payload"]
        where = f"{pay['title']} S{p['season'] or 0:02d}E{p['episode'] or 0:02d}" if not r["is_movie"] else pay["title"]
        lines.append(f"  {where} | {pay['quality']} | {pay['source']}")
        lines.append(f"      file : {_one_line(r['raw_filename'])[:100]}")
        lines.append(f"      tmdb : {pay.get('tmdb_id')} | imdb : {pay.get('imdb_id')}")
        lines.append("")

    lines.append("== MEDIUM CONFIDENCE (review recommended) ==")
    for r in med:
        lines.append(f"  {_one_line(r['raw_filename'])[:100]} | {r['payload'].get('title')}")
        lines.append("")

    lines.append("== LOW CONFIDENCE (unresolved - manual action needed) ==")
    for r in low:
        lines.append(f"  {_one_line(r['raw_filename'])[:100]} | parsed title: {r['parsed'].get('title')}")
        lines.append("")

    skipped = [r for r in results if r.get("action") == "skip"]
    if skipped:
        lines.append("== SKIPPED (name cannot be indexed as written - rename / re-caption) ==")
        for r in skipped:
            lines.append(f"  {_one_line(r['raw_filename'])[:100]} | reason: {r.get('skip_reason')}")
        lines.append("")

    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False, default=str)

    print(f"\nReports: {txt_path} / {json_path}")
    print(f"  High: {len(high)} | Medium: {len(med)} | Low: {len(low)}")


async def apply_to_mongodb(results: list[dict], uris: list[str]):
    """Add high-confidence results to MongoDB. Not reachable: see APPLY_UNAVAILABLE."""
    try:
        from pymongo import MongoClient
    except ImportError:
        raise SystemExit("pymongo is required for --apply. Install with: pip install pymongo")

    changes = [r for r in results if r["confidence"] == "high"]
    if not changes:
        print("No high-confidence changes to apply.")
        return

    clients = [MongoClient(uri, serverSelectionTimeoutMS=5000) for uri in uris[1:]]
    try:
        # Phase 1: backup
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rand = secrets.token_hex(4)
        backup_path = f"nyaa_fix_backup_{stamp}_{rand}.json"
        _safe_path(backup_path, "backup output path")

        backup_docs = []
        applied = 0
        for client in clients:
            db = client[os.getenv("DB_NAME", "dbFyvio")]
            for coll_name in ("tv", "movie"):
                # Insert new documents (indexer adds new streams)
                for r in changes:
                    pay = r["payload"]
                    if pay["media_type"] != coll_name:
                        continue
                    # Check if exists by encoded_string or title+season+episode
                    existing = None
                    if coll_name == "movie":
                        existing = db[coll_name].find_one({"telegram.id": pay["encoded_string"]})
                    else:
                        for season in (db[coll_name].find({}) or []):
                            for ep in season.get("episodes", []):
                                for entry in ep.get("telegram", []):
                                    if entry.get("id") == pay["encoded_string"]:
                                        existing = season
                                        break
                    if existing:
                        continue  # skip duplicates

                    # Build telegram entry
                    tel_entry = {
                        "id": pay["encoded_string"],
                        "name": r["raw_filename"],
                        "quality": pay["quality"],
                        "size": pay.get("size", ""),
                        "group_key": f"{pay['media_type']}:{pay['quality']}:{pay['encoded_string']}",
                    }

                    if coll_name == "movie":
                        doc = {
                            "title": pay["title"],
                            "tmdb_id": pay.get("tmdb_id"),
                            "imdb_id": pay.get("imdb_id"),
                            "media_type": "movie",
                            "db_index": 1,
                            "telegram": [tel_entry],
                            "updated_on": datetime.utcnow(),
                        }
                        for k, v in pay.items():
                            if k not in doc and v not in (None, "", [], {}):
                                doc[k] = v
                        db[coll_name].insert_one(doc)
                    else:
                        # TV: find or create show document
                        show = db[coll_name].find_one({"title": pay["title"]})
                        if not show:
                            show = {
                                "title": pay["title"],
                                "tmdb_id": pay.get("tmdb_id"),
                                "imdb_id": pay.get("imdb_id"),
                                "media_type": "tv",
                                "db_index": 1,
                                "seasons": [],
                                "updated_on": datetime.utcnow(),
                            }
                            db[coll_name].insert_one(show)
                            show = db[coll_name].find_one({"title": pay["title"]})

                        # Find or create season
                        season_num = pay.get("season_number") if pay.get("season_number") is not None else 1
                        season_doc = None
                        for s in show.get("seasons", []):
                            if s.get("season_number") == season_num:
                                season_doc = s
                                break
                        if not season_doc:
                            season_doc = {"season_number": season_num, "episodes": []}
                            show["seasons"].append(season_doc)

                        # Find or create episode
                        ep_num = pay.get("episode_number") if pay.get("episode_number") is not None else 1
                        ep_doc = None
                        for e in season_doc.get("episodes", []):
                            if e.get("episode_number") == ep_num:
                                ep_doc = e
                                break
                        if not ep_doc:
                            ep_doc = {"episode_number": ep_num, "telegram": []}
                            season_doc["episodes"].append(ep_doc)

                        ep_doc["telegram"].append(tel_entry)

                        # Update the show document
                        db[coll_name].replace_one({"_id": show["_id"]}, show)

                    applied += 1
                    backup_docs.append({"collection": coll_name, "doc": pay})

        # Write backup
        with open(backup_path, "w", encoding="utf-8") as fh:
            json.dump(backup_docs, fh, indent=2, ensure_ascii=False, default=str)
        print(f"\nBackup of {len(backup_docs)} documents: {backup_path}")
        print(f"Applied {applied} new streams to MongoDB.")
    finally:
        for client in clients:
            client.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline indexer for Telegram Stremio (TMDB / TVDB / Kitsu / Cinemeta)."
    )
    parser.add_argument(
        "--dump", default=None,
        help="Path to filenames_report.txt TSV dump from dump_all_filenames.py, "
             "or a Telegram channel dump in JSONL (one JSON object per line with "
             "msg_id / file_name / caption / size / width / height / duration).",
    )
    parser.add_argument(
        "--parse-only", action="store_true", default=False,
        help="Offline preview: only parse the names (no provider lookups, no DB) and "
             "write <out>_parse.tsv showing how every file would be indexed.",
    )
    parser.add_argument(
        "--scan", default=None,
        help="Recursively scan a directory for media files",
    )
    parser.add_argument(
        "--ext", nargs="*", default=[".mkv", ".mp4", ".ass", ".srt"],
        help="Extensions to include when scanning (default: .mkv .mp4 .ass .srt)",
    )
    parser.add_argument(
        "--channel", type=int, default=0,
        help="Telegram channel ID (for encoded_string) (default: 0)",
    )
    parser.add_argument(
        "--anime-channel", action="store_true", default=False,
        help="Treat input as an anime channel (Kitsu-first resolution)",
    )
    parser.add_argument(
        "--legacy-resolver", action="store_true", default=False,
        help="Resolve with the indexer's built-in provider code instead of the live bot's pipeline",
    )
    parser.add_argument(
        "--out", default="indexer_report",
        help="Base path for output reports (default: indexer_report)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Switched off in this release; prints the reason and exits.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Dry run only (default).",
    )
    parser.add_argument(
        "--delay", type=float, default=0.5,
        help="Delay between API requests in seconds (default: 0.5)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=MAX_CONCURRENT,
        help="Max concurrent API requests (default: 6)",
    )
    parser.add_argument(
        "--resume", action="store_true", default=False,
        help="Skip records already resolved as high-confidence in a previous indexer_report.json (faster re-runs).",
    )
    args = parser.parse_args()
    global USE_LEGACY_RESOLVER
    USE_LEGACY_RESOLVER = bool(args.legacy_resolver)

    if args.apply:
        print(APPLY_UNAVAILABLE)
        return 2

    # Load records
    records = []
    if args.dump:
        records = load_records_from_tsv(args.dump)
    elif args.scan:
        records = scan_directory(args.scan, args.ext)
    else:
        # Default: look for filenames_report.txt in cwd
        default_dump = os.path.join(os.getcwd(), "filenames_report.txt")
        if os.path.exists(default_dump):
            records = load_records_from_tsv(default_dump)
        else:
            raise SystemExit(
                "No input specified. Use --dump <file> or --scan <dir>.\n"
                "Example: python tools/indexer.py --dump filenames_report.txt"
            )

    # --- Resume: skip already-high-confidence records from a previous run ---
    previous_high = {}  # raw_filename -> previous result dict
    if args.resume:
        prev_json = _safe_path(f"{args.out}.json", "report input path", must_exist=False)
        if prev_json and os.path.exists(prev_json):
            try:
                with open(prev_json, "r", encoding="utf-8") as fh:
                    prev_results = json.load(fh)
                for r in prev_results:
                    if r.get("confidence") == "high":
                        previous_high[r["raw_filename"]] = r
                skipped = len(records)
                records = [r for r in records if r.get("file_name") not in previous_high]
                skipped -= len(records)
                print(f"\nResume: skipping {skipped} already-resolved high-confidence records. "
                      f"{len(records)} records remain to process.\n")
            except Exception as e:
                print(f"\nResume: could not load previous report ({e}), processing all records.\n")
        else:
            print(f"\nResume: no previous {args.out}.json found, processing all records.\n")

    if not records:
        print("No records to process.")
        return 0

    if args.parse_only:
        write_parse_preview(records, args.out)
        return 0

    print(f"\nTMDB API key: {_mask_key(TMDB_API_KEY)}")
    print(f"TVDB API key: {_mask_key(TVDB_API_KEY)}")
    print(f"Anime channel: {args.anime_channel}")
    print(f"Channel ID: {args.channel}")
    print(f"Concurrency: {args.concurrency} | Delay: {args.delay}s\n")

    # Run indexing
    results = asyncio.run(run_index(
        records=records,
        channel_id=args.channel,
        apply=args.apply,
        dry_run=args.dry_run,
        is_anime_channel=args.anime_channel,
        max_concurrent=args.concurrency,
    ))

    # Merge previous high-confidence results with new results for the full report
    if previous_high:
        prev_by_name = {r["raw_filename"]: r for r in previous_high.values()}
        merged = list(results)
        for name, prev_r in prev_by_name.items():
            # Only add back if not already in new results (shouldn't happen, but safe)
            if name not in {r["raw_filename"] for r in results}:
                merged.append(prev_r)
        # Sort so high confidence comes first for cleaner report
        merged.sort(key=lambda r: (0 if r.get("confidence") == "high" else 1, r.get("raw_filename", "")))
        results = merged
        print(f"\nMerged {len(previous_high)} previous high-confidence records with {len(results) - len(previous_high)} new results.\n")

    # Write report
    write_report(results, args.out)

    # Apply if requested
    if args.apply:
        uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
        if len(uris) < 2:
            raise SystemExit(
                "DATABASE env needs at least 2 comma-separated MongoDB URIs "
                "(set in config.env or environment)."
            )
        print(f"\nConnecting to {len(uris)} database URI(s): "
              + ", ".join(_mask_uri(u) for u in uris))
        asyncio.run(apply_to_mongodb(results, uris))
    else:
        print("\nDry run only. Review the report, then re-run with --apply to write changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
