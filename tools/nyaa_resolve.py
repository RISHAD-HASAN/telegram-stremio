#!/usr/bin/env python3
"""Resolve correct quality labels (and spot metadata errors) using nyaa.si.

Meant for libraries whose files come from nyaa releases. The release names
on nyaa carry the real resolution ("[Group] Show Name - 24 ... 1080p"), and
the RSS feed reports torrent sizes, so a matching size shows that a Telegram
file is the same encode and not a downscaled re-upload.

Workflow
--------
1. Export the library, then run the resolver:
     python tools/dump_all_filenames.py
     python tools/nyaa_resolve.py
   It reads filenames_report.json, groups streams into (release group, title)
   units, and runs one cached nyaa search per unit (with a delay between
   requests; an interrupted run resumes from nyaa_cache.json).
2. Each stream is scored against the returned torrents on group, title
   similarity, season/episode, and file-size proximity. The winner's canonical
   resolution becomes the proposed label, with confidence + evidence.
3. Outputs:
     nyaa_resolution_report.txt   review sheet
     nyaa_resolution_report.json machine-readable proposals
4. Nothing touches MongoDB until you add --apply, which writes only
   high-confidence label changes, backs up every touched document first, and
   rewrites split-file group_keys consistently. Dry-run is the default.

Safety rules baked in
---------------------
* Literal filename tokens win: a "540p" tag is 540p, never bucketed to 576p.
* 480i/480p and 4K/2160p/UHD are treated as equivalents, never churned.
* S00 (specials/OVA) is a distinct bucket - never conflated with S01.
* A file much smaller than the nyaa source is flagged as a suspected
  re-encode: the stored dimension-derived label is kept and the case is
  reported instead of blindly overwritten.
* Low-match-score and zero-result units go to the review list, never changed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from typing import Optional

import requests
from rapidfuzz import fuzz

DB_NAME = os.getenv("DB_NAME", "dbFyvio")
NYAA_RSS = "https://nyaa.si/?page=rss"
NS = "{https://nyaa.si/xmlns/nyaa}"
USER_AGENT = "Mozilla/5.0 (compatible; label-resolver/1.0)"

# Equivalent labels that must never be "corrected" into each other.
EQUIVALENT_LABELS = [
    {"4k", "2160p", "2160", "uhd", "uhd-4k", "4k uhd"},
    {"480i", "480p"},
]
GLUED_SOURCE_RE = re.compile(r"(?i)(?:BD|HDRip|WEBDL|WEBRip|HDTV|DVD|BR|WEB)(\d{3,4})p")
P_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{3,4})p(?![A-Za-z0-9])")
# Interlaced DVD tags ("480i", "576i", "1080i") carry the same height semantics.
I_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{3,4})i(?![A-Za-z0-9])")
DIM_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(\d{3,4})[xX](\d{3,4})(?![A-Za-z0-9])")
WORD_4K_RE = re.compile(r"(?i)\b(?:4k|uhd|2160p?)\b")
YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
SXXEXX_RE = re.compile(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b")
BARE_EP_RE = re.compile(r"(?i)(?:^|[\s._\-])E?0*(\d{1,3})(?=[\s._\-]|$)")
STOPWORDS = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "no", "na",
             "wa", "ga", "season", "series", "movie", "film"}

# ---------------------------------------------------------------------------
# Security: XML bomb protection
# nyaa.si RSS is parsed with ElementTree. Limit total element count to prevent
# billion-laughs / quadratic blowup from a malicious (or compromised) feed.
# ---------------------------------------------------------------------------
_MAX_XML_ELEMENTS = 5000


def _safe_fromstring(xml_bytes: bytes):
    """Parse XML with element-count limits to resist XML bombs."""
    import xml.etree.ElementTree as ET

    class _CountingParser(ET.XMLParser):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._count = 0

        def _start(self, *a, **k):
            self._count += 1
            if self._count > _MAX_XML_ELEMENTS:
                raise ET.ParseError("XML element count exceeded limit")
            return super()._start(*a, **k)

    parser = _CountingParser()
    return ET.fromstring(xml_bytes, parser=parser)


# ---------------------------------------------------------------------------
# Path arguments
# ---------------------------------------------------------------------------

def _safe_path(value: str, label: str, must_exist: bool = False) -> str:
    """Absolute form of a path argument; exits with a message when it is unusable."""
    if not value or not isinstance(value, str):
        raise SystemExit(f"Error: {label} must be a non-empty string.")
    if "\x00" in value:
        raise SystemExit(f"Error: {label} contains null byte.")
    abs_path = os.path.abspath(os.path.expanduser(value))
    if must_exist and not os.path.isfile(abs_path):
        raise SystemExit(f"Error: {label} not found: {abs_path}")
    return abs_path


# ---------------------------------------------------------------------------
# Security: credential masking
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"(?i)(mongodb\+srv|mongodb)://[^:]+:[^@]+@")


def _mask_uri(uri: str) -> str:
    """Hide credentials in a MongoDB URI for safe logging."""
    return _URI_RE.sub(lambda m: m.group(1) + "://****:****@", uri or "")


# ---------------------------------------------------------------------------
# Security: cache integrity
# ---------------------------------------------------------------------------

_CACHE_VERSION = 1
_CACHE_MAX_ENTRIES = 2000


def _load_cache(cache_path: str) -> dict:
    """Load nyaa cache with integrity validation."""
    if not os.path.exists(cache_path):
        return {"_version": _CACHE_VERSION, "_entries": {}}
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: cache {cache_path} unreadable ({exc}); starting fresh.")
        return {"_version": _CACHE_VERSION, "_entries": {}}
    if not isinstance(data, dict):
        return {"_version": _CACHE_VERSION, "_entries": {}}
    version = data.get("_version", 0)
    entries = data.get("_entries", data)  # backward compat: old flat format
    if not isinstance(entries, dict):
        entries = {}
    if version != _CACHE_VERSION:
        print(f"Warning: cache version mismatch ({version} != {_CACHE_VERSION}); starting fresh.")
        return {"_version": _CACHE_VERSION, "_entries": {}}
    if len(entries) > _CACHE_MAX_ENTRIES:
        keys = sorted(entries.keys())[: _CACHE_MAX_ENTRIES // 2]
        entries = {k: entries[k] for k in keys}
        print(f"Warning: cache trimmed to {_CACHE_MAX_ENTRIES // 2} entries.")
    return {"_version": _CACHE_VERSION, "_entries": entries}


def _save_cache(cache_path: str, cache: dict) -> None:
    """Persist nyaa cache atomically."""
    tmp_path = f"{cache_path}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1)
        os.replace(tmp_path, cache_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Small parsing helpers (pure, unit-tested)
# ---------------------------------------------------------------------------

def labels_equivalent(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if a == b:
        return True
    return any(a in group and b in group for group in EQUIVALENT_LABELS)


def literal_name_quality(name: str) -> Optional[str]:
    """Resolution explicitly claimed by a file name, taken literally."""
    name = GLUED_SOURCE_RE.sub(r" \1p ", name or "")
    heights = [int(m.group(1)) for m in P_TOKEN_RE.finditer(name)]
    if heights:
        return f"{max(heights)}p"
    interlaced = [int(m.group(1)) for m in I_TOKEN_RE.finditer(name)]
    if interlaced:
        return f"{max(interlaced)}i"
    dims = DIM_RE.findall(name)
    if dims:
        h = max(min(int(w), int(h)) for w, h in dims)
        for threshold, label in ((2100, "4K"), (1400, "1440p"), (1000, "1080p"),
                                 (700, "720p"), (540, "576p"), (440, "480p"), (320, "360p")):
            if h >= threshold:
                return label
        return f"{h}p"
    if WORD_4K_RE.search(name):
        return "4K"
    return None


def parse_size_mb(text: str) -> Optional[float]:
    """Parse both nyaa ('1.1 GiB', '765.3 MiB') and DB ('1.17GB') sizes to MB."""
    if not text:
        return None
    m = re.match(r"\s*([\d.]+)\s*(KiB|MiB|GiB|TiB|KB|MB|GB|TB)", text, re.IGNORECASE)
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    factors = {"kib": 1 / 1024, "kb": 1 / 1000,
               "mib": 1, "mb": 1,
               "gib": 1024, "gb": 1000,
               "tib": 1024 ** 2, "tb": 1000 ** 2}
    return value * factors[unit]


_TECH_TAIL_WORDS = {"x264", "x265", "h264", "h265", "hevc", "avc", "av1", "aac",
                    "flac", "ac3", "ddp", "mkv", "mp4", "avi", "web", "bd", "dvd"}


def extract_group(name: str) -> Optional[str]:
    m = re.match(r"^\[([^\]]+)\]", name or "")
    if m:
        return m.group(1).strip()
    # Scene-style suffix: "... H.264 VARYG" / "... x264-Tsundere-Raws (VF)".
    base = re.sub(r"\.(mkv|mp4|avi|ts|m4v|mov|webm)$", "", name or "", flags=re.IGNORECASE)
    base = re.sub(r"(\s*\([^)]*\))+$", "", base)  # trailing notes block the end-anchor
    m = re.search(r"[-–]\s*([A-Za-z0-9][A-Za-z0-9\s.'&]*?)(?:([-– ])(raws|subs|sub))?$",
                  base, flags=re.IGNORECASE)
    if m:
        if m.group(3):
            name = m.group(1).strip()
            return f"{name} {m.group(3)}" if m.group(2) == " " else name
        for token in reversed(m.group(1).split()):
            if token and token.lower() not in _TECH_TAIL_WORDS:
                return token.strip()
    for keyword in ("VARYG", "Okay Raws", "DBD Raws", "ParanDark"):
        if keyword.lower() in (name or "").lower():
            return keyword
    return None


def normalize_words(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text or "")
    text = re.sub(
        r"(?i)\b(s\d{1,2}\s*e\d{1,3}|\d{3,4}p|x26[45]|hevc|avc|aac|flac|ddp?|"
        r"web[ ._-]?dl|webrip|bdrip|bluray|bd|dvd|dual[ ._-]?audio|multi[ ._-]?sub?|"
        r"\d{1,3}bit|hi10p?|mkv|mp4)\b",
        " ",
        text,
    )
    text = re.sub(r"[’']", "", text)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text.lower()) if w and w not in STOPWORDS]
    return " ".join(dict.fromkeys(words))


def episode_tokens(name: str) -> set[int]:
    tokens = set()
    for m in SXXEXX_RE.finditer(name or ""):
        tokens.add(int(m.group(2)))
    # Bare trailing episode markers ("- 13", "E01", " 07 ")
    for m in re.finditer(r"(?i)(?:^|[\s._\-])(?:e|-)?0*(\d{1,3})(?=[\s._\-]|$)", name or ""):
        tokens.add(int(m.group(1)))
    return {t for t in tokens if 0 < t < 1000}


def season_token(name: str) -> Optional[int]:
    m = SXXEXX_RE.search(name or "")
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)\bS(\d{1,2})(?![A-Za-z0-9])", name or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# nyaa fetching with disk cache and polite retries
# ---------------------------------------------------------------------------

def fetch_nyaa(query: str, cache: dict, delay: float, retries: int = 3) -> list[dict]:
    """Fetch nyaa.si RSS with cache, retry, and rate-limit respect."""
    if query in cache:
        return cache[query]
    url = f"{NYAA_RSS}&q={urllib.parse.quote(query)}"
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=25, headers={"User-Agent": USER_AGENT})
            # Respect HTTP 429 / Retry-After
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else delay * (attempt + 5)
                print(f"    HTTP 429 for '{query}', waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code == 200:
                break
            wait = delay * (attempt + 2)
            print(f"    HTTP {resp.status_code} for '{query}', retrying in {wait:.0f}s")
            time.sleep(wait)
        except requests.RequestException as exc:
            if attempt == retries - 1:
                print(f"    giving up on '{query}': {exc}")
                cache[query] = []
                return []
            time.sleep(delay * (attempt + 2))
    else:
        cache[query] = []
        return []

    torrents = []
    try:
        root = _safe_fromstring(resp.content)
        for item in root.findall(".//item"):
            torrents.append({
                "id": (item.findtext("guid") or "").rsplit("/", 1)[-1],
                "title": item.findtext("title") or "",
                "size_mb": parse_size_mb(item.findtext(NS + "size") or ""),
                "size_text": item.findtext(NS + "size") or "",
                "pub_date": item.findtext("pubDate") or "",
            })
    except ET.ParseError as exc:
        print(f"    RSS parse failure for '{query}': {exc}")
    cache[query] = torrents
    time.sleep(delay)
    return torrents


BATCH_WORD_RE = re.compile(
    r"(?i)\b(collection|batch|anthology|complete|season \d+\s*-\s*\d+)\b"
)


def build_queries(group: Optional[str], title: str, sample_name: str) -> list[str]:
    title_words = [w for w in normalize_words(title).split() if len(w) > 1][:6]
    group_words = set(re.findall(r"[a-z0-9]+", (group or "").lower()))
    title_word_set = set(title_words)
    tech_stop = {"dual", "audio", "multi", "sub", "subs", "x264", "x265", "hevc",
                 "avc", "aac", "flac", "bluray", "mkv", "mp4", "bit", "hi10",
                 "av1", "opus", "dts", "web", "bdrip", "dvdrip", "hdr"}
    raw = re.findall(r"[A-Za-z]{3,}", sample_name.lower())
    file_words = [w for w in dict.fromkeys(raw)
                  if w not in group_words and w not in title_word_set
                  and w not in tech_stop][:4]
    queries = []
    if group and group not in ("VARYG",):
        queries.append(" ".join([group] + title_words[:5]))
        if file_words:
            queries.append(" ".join([group] + file_words[:3]))
    queries.append(" ".join(title_words))
    if len(title_words) > 3:
        queries.append(" ".join(title_words[:3]))
    seen, ordered = set(), []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            ordered.append(q)
    return ordered


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def score_torrent(record: dict, torrent: dict) -> tuple[int, list[str]]:
    score, reasons = 0, []
    t_name = torrent["title"]
    db_group = extract_group(record["file_name"])
    t_group = extract_group(t_name)
    if db_group and t_group and db_group.lower() == t_group.lower():
        score += 40
        reasons.append("group")
    elif db_group and t_group:
        score -= 25

    title_sim = fuzz.token_set_ratio(normalize_words(record["title"]),
                                     normalize_words(t_name))
    score += int(title_sim / 4)  # up to +25
    if title_sim >= 85:
        reasons.append(f"title~{title_sim}")

    if record["media_type"] == "tv":
        db_eps = {int(record["episode"])} if str(record["episode"]).isdigit() else set()
        t_eps = episode_tokens(t_name)
        if db_eps and t_eps and db_eps & t_eps:
            score += 30
            reasons.append("episode")
        elif db_eps and t_eps:
            # Both encode episode numbers but disagree: different episode or
            # a different numbering scheme (absolute vs seasonal). Reject hard.
            score -= 35
        # A batch torrent (no episode token) stays neutral on episode.
        t_season = season_token(t_name)
        # S00 (specials) is a distinct bucket: never match S00 against S01+.
        if t_season is not None and str(record["season"]) not in ("?", "0"):
            if t_season == int(record["season"]):
                score += 10
                reasons.append("season")
            else:
                score -= 20
                reasons.append(f"season-mismatch S{t_season}")
        elif t_season is not None and str(record["season"]) == "0":
            # Explicitly penalize S01 torrent matched against S00 special.
            if t_season != 0:
                score -= 20
                reasons.append(f"season-mismatch S{t_season} vs S00 special")

    # Release-year agreement stops same-name different-series collisions.
    year_range = bool(re.search(r"(?:19|20)\d{2}\s*[-–]\s*(?:19|20)\d{2}", t_name))
    db_years, t_years = set(YEAR_RE.findall(record["file_name"])), set(YEAR_RE.findall(t_name))
    if db_years and t_years and not year_range and len(t_years) < 3:
        if db_years & t_years:
            score += 8
            reasons.append("year")
        else:
            score -= 30
            reasons.append("year-mismatch")

    db_size = parse_size_mb(record.get("size") or "")
    t_size = torrent.get("size_mb")
    if db_size and t_size:
        ratio = min(db_size, t_size) / max(db_size, t_size)
        if ratio >= 0.85:
            score += 20
            reasons.append(f"size {ratio:.2f}")
        elif ratio >= 0.6:
            score += 5
        else:
            score -= 10
            reasons.append(f"size-gap {ratio:.2f}")
    return score, reasons


def resolve_unit(records: list[dict], cache: dict, delay: float) -> dict:
    sample = records[0]
    group = extract_group(sample["file_name"])
    queries = build_queries(group, sample["title"], sample["file_name"])
    candidates = []
    for query in queries:
        hits = fetch_nyaa(query, cache, delay)
        candidates.extend(hits)
        if any(score_torrent(sample, t)[0] >= 75 for t in hits):
            break
    unique = {t["id"]: t for t in candidates if t.get("id")}
    return {"queries": queries, "candidates": list(unique.values())}


def _evidence(torrent: dict, score: int, reasons: list) -> dict:
    return {
        "nyaa_id": torrent["id"], "nyaa_title": torrent["title"],
        "nyaa_size": torrent["size_text"], "score": score, "why": reasons,
        "nyaa_url": f"https://nyaa.si/view/{torrent['id']}",
    }


def propose_for_record(record: dict, candidates: list[dict], min_score: int) -> dict:
    stored = (record["stored_label"] or "").strip()
    name_says = literal_name_quality(record["file_name"])

    # Tier 1: the file name itself carries an explicit resolution token.
    if name_says:
        if labels_equivalent(stored, name_says):
            return _verdict(record, "confirm", stored, name_says, None,
                            "filename token confirms stored label")
        return _verdict(record, "change", name_says, name_says, None,
                        "filename explicitly disagrees with stored label")

    # Tier 2: silent file name - resolve via the nyaa candidates.
    if not candidates:
        return _verdict(record, "review", stored, None, None,
                        "no resolution token in filename and no nyaa results")

    # Unit-level batch rule: a same-group collection/batch torrent that names
    # the franchise is authoritative for every episode the unit contains.
    db_group = extract_group(record["file_name"])
    if db_group:
        batch_hits = []
        for t in candidates:
            t_group = extract_group(t["title"])
            if not t_group or t_group.lower() != db_group.lower():
                continue
            t_label = literal_name_quality(t["title"])
            if not t_label:
                continue
            db_size = parse_size_mb(record.get("size") or "")
            oversized = bool(db_size and t.get("size_mb") and t["size_mb"] > 8 * db_size)
            is_explicit_batch = bool(BATCH_WORD_RE.search(t["title"]))
            if not (is_explicit_batch or oversized):
                continue
            sim = max(fuzz.token_set_ratio(normalize_words(record["title"]),
                                           normalize_words(t["title"])),
                      fuzz.token_set_ratio(normalize_words(record["file_name"]),
                                           normalize_words(t["title"])))
            fn_tokens = [w for w in normalize_words(record["file_name"]).split()
                         if w not in set(normalize_words(db_group).split())]
            shared = {w for w in fn_tokens
                      if w in set(normalize_words(t["title"]).split())}
            ok = (
                (is_explicit_batch and sim >= 55 and len(shared) >= 2)
                or (oversized and sim >= 70 and len(shared) >= 3)
            )
            if ok:
                batch_hits.append((sim, t, t_label))
        if batch_hits:
            sim, batch, batch_label = max(batch_hits, key=lambda x: x[0])
            ev = _evidence(batch, sim, ["group", "batch-release", f"title~{sim}"])
            if labels_equivalent(stored, batch_label):
                return _verdict(record, "confirm", stored, batch_label, ev,
                                "same-group batch release confirms stored label")
            return _verdict(record, "change", batch_label, None, ev,
                            "same-group batch release supplies canonical resolution")

    scored = sorted(((score_torrent(record, t)[0], t, score_torrent(record, t)[1])
                     for t in candidates), key=lambda x: -x[0])
    with_label = [(s, t, why) for s, t, why in scored if literal_name_quality(t["title"])]
    best_score, best, reasons = (with_label or scored)[0]
    nyaa_label = literal_name_quality(best["title"])
    evidence = _evidence(best, best_score, reasons)

    if best_score < min_score:
        return _verdict(record, "review", stored, None, evidence,
                        f"best match score {best_score} below threshold {min_score}")
    t_group = extract_group(best["title"])
    if db_group and t_group and db_group.lower() != t_group.lower():
        return _verdict(record, "review", stored, None, evidence,
                        f"group mismatch: file='{db_group}' torrent='{t_group}'")
    if not nyaa_label:
        return _verdict(record, "review", stored, None, evidence,
                        "matched torrent has no resolution token")

    has_identity = (
        "group" in reasons or "year" in reasons or "season" in reasons
        or any(r.startswith("size ") for r in reasons)
    )

    db_size = parse_size_mb(record.get("size") or "")
    t_size = best.get("size_mb")
    oversized = bool(db_size and t_size and t_size > 8 * db_size)
    if (not oversized and db_size and t_size
            and min(db_size, t_size) / max(db_size, t_size) < 0.6):
        return _verdict(record, "reencode_keep", stored, nyaa_label, evidence,
                        "file much smaller than nyaa source; suspected re-encode, label kept")

    if labels_equivalent(stored, nyaa_label):
        return _verdict(record, "confirm", stored, nyaa_label, evidence,
                        "nyaa canonical release confirms stored label")
    if not has_identity:
        return _verdict(record, "review", stored, None, evidence,
                        "label differs but no group/size/season/year identity evidence")
    if not db_group or db_group == "VARYG":
        if not (db_size and t_size
                and min(db_size, t_size) / max(db_size, t_size) >= 0.90):
            return _verdict(record, "review", stored, None, evidence,
                            "no release group and size not near-identical; cannot prove same encode")
    return _verdict(record, "change", nyaa_label, None, evidence,
                    "nyaa canonical resolution differs from stored label")


def _verdict(record, action, label, name_says, evidence, reason):
    return {
        "action": action, "title": record["title"],
        "media_type": record["media_type"], "season": record["season"],
        "episode": record["episode"], "stored_label": record["stored_label"],
        "proposed_label": label if action in ("change",) else record["stored_label"],
        "filename_says": name_says, "file_name": record["file_name"],
        "size": record["size"], "is_split": record["is_split"],
        "evidence": evidence or {}, "reason": reason,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_reports(verdicts: list[dict], path_txt: str, path_json: str):
    order = {"change": 0, "reencode_keep": 1, "review": 2, "confirm": 3}
    verdicts = sorted(verdicts, key=lambda v: (order.get(v["action"], 9), v["title"]))
    counts = defaultdict(int)
    for v in verdicts:
        counts[v["action"]] += 1

    lines = [
        "NYAA RESOLUTION REPORT (read-only unless --apply)",
        f"Generated: {datetime.now().isoformat(timespec='seconds')} | DB: {DB_NAME}",
        "Counts: " + ", ".join(f"{k}={counts[k]}" for k in sorted(counts)),
        "",
    ]
    sections = [
        ("change", "PROPOSED LABEL CHANGES (high confidence)"),
        ("reencode_keep", "SUSPECTED RE-ENCODES (stored label kept)"),
        ("review", "REVIEW NEEDED (no confident nyaa match)"),
        ("confirm", "CONFIRMED BY NYAA (no action)"),
    ]
    for action, heading in sections:
        group = [v for v in verdicts if v["action"] == action]
        lines.append(f"== {heading} ({len(group)}) ==")
        for v in group:
            where = (f"{v['title']} S{v['season']}E{v['episode']}" if v["media_type"] == "tv"
                     else v["title"])
            ev = v.get("evidence") or {}
            lines.append(f"  {where} | {v['stored_label']} -> {v['proposed_label']} | {v['size']}")
            lines.append(f"      file : {v['file_name']}")
            if ev:
                lines.append(f"      nyaa : {ev.get('nyaa_title','')} [{ev.get('nyaa_size','')}] score={ev.get('score')}")
                lines.append(f"              {ev.get('nyaa_url','')}")
            lines.append(f"      why  : {v['reason']}")
        lines.append("")

    txt_path = _safe_path(path_txt, "report output path")
    json_path = _safe_path(path_json, "report output path")
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                   "counts": dict(counts), "verdicts": verdicts}, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Apply path (MongoDB, backup-first)
# ---------------------------------------------------------------------------

def rewrite_group_key(group_key: str, old_label: str, new_label: str) -> tuple[str, bool]:
    parts = str(group_key).split(":")
    idx = 2 if parts and parts[0] == "manual" else 1
    if len(parts) > idx and parts[idx].strip().lower() == old_label.strip().lower():
        parts[idx] = new_label
        return ":".join(parts), True
    return group_key, False


def apply_changes(verdicts: list[dict], uris: list[str]):
    from pymongo import MongoClient
    changes = [v for v in verdicts if v["action"] == "change"
               and not labels_equivalent(v["stored_label"], v["proposed_label"])]
    if not changes:
        print("No high-confidence changes to apply.")
        return
    clients = [MongoClient(uri, serverSelectionTimeoutMS=5000) for uri in uris[1:]]
    try:
        # Phase 1: locate every target and keep ONE held copy per document.
        holders, order, missed = {}, [], []
        for v in changes:
            located = _locate_entry(clients, v)
            if located is None:
                missed.append(v)
                continue
            client, coll_name, doc, _entry = located
            key = (id(client), coll_name, doc["_id"])
            if key not in holders:
                holders[key] = {"client": client, "coll": coll_name, "doc": doc, "verdicts": []}
                order.append(key)
            holders[key]["verdicts"].append(v)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rand = secrets.token_hex(4)
        if holders:
            backup = [{"collection": h["coll"], "_id": str(h["doc"]["_id"]), "doc": h["doc"]}
                      for h in (holders[k] for k in order)]
            path = f"nyaa_fix_backup_{stamp}_{rand}.json"
            _safe_path(path, "backup output path")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(backup, fh, indent=2, ensure_ascii=False, default=str)
            print(f"Backup of {len(backup)} documents written before any change: {path}")

        # Phase 2: apply every verdict to its held document copy, then write once.
        applied = 0
        for key in order:
            h = holders[key]
            doc, client, coll_name = h["doc"], h["client"], h["coll"]
            for v in h["verdicts"]:
                entry = _find_entry(doc, v)
                if entry is None:
                    missed.append(v)
                    continue
                old = entry["quality"]
                entry["quality"] = v["proposed_label"]
                if entry.get("group_key"):
                    new_key, ok = rewrite_group_key(entry["group_key"], old, v["proposed_label"])
                    if ok:
                        entry["group_key"] = new_key
                applied += 1
            client[DB_NAME][coll_name].update_one(
                {"_id": doc["_id"]},
                {"$set": {"seasons": doc.get("seasons"), "telegram": doc.get("telegram")}})
    finally:
        for client in clients:
            client.close()

    print(f"Applied {applied} label changes; {len(missed)} could not be located.")
    for v in missed[:10]:
        print("  missed:", v["title"], v["file_name"])


def _find_entry(doc: dict, verdict: dict) -> Optional[dict]:
    """Locate the telegram stream entry inside an already-held document copy."""
    if verdict["media_type"] == "movie":
        for entry in doc.get("telegram") or []:
            if entry.get("name") == verdict["file_name"]:
                return entry
        return None
    for season in doc.get("seasons") or []:
        if str(season.get("season_number")) != str(verdict["season"]):
            continue
        for ep in season.get("episodes") or []:
            if str(ep.get("episode_number")) != str(verdict["episode"]):
                continue
            for entry in ep.get("telegram") or []:
                if entry.get("name") == verdict["file_name"]:
                    return entry
    return None


def _locate_entry(clients, verdict):
    """Find the document and exact stream entry described by a verdict."""
    for client in clients:
        db = client[DB_NAME]
        for coll_name in ("tv", "movie"):
            for doc in db[coll_name].find({}):
                if verdict["media_type"] == "movie":
                    if doc.get("title") != verdict["title"]:
                        continue
                    for entry in doc.get("telegram") or []:
                        if entry.get("name") == verdict["file_name"]:
                            return client, coll_name, doc, entry
                else:
                    if doc.get("title") != verdict["title"]:
                        continue
                    for season in doc.get("seasons") or []:
                        if str(season.get("season_number")) != str(verdict["season"]):
                            continue
                        for ep in season.get("episodes") or []:
                            if str(ep.get("episode_number")) != str(verdict["episode"]):
                                continue
                            for entry in ep.get("telegram") or []:
                                if entry.get("name") == verdict["file_name"]:
                                    return client, coll_name, doc, entry
    return None


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", default="filenames_report.json",
                        help="library export from dump_all_filenames.py")
    parser.add_argument("--delay", type=float, default=1.2,
                        help="seconds between nyaa requests (default: 1.2)")
    parser.add_argument("--min-score", type=int, default=65,
                        help="minimum match score for an automated proposal (default: 65)")
    parser.add_argument("--max-units", type=int, default=0,
                        help="limit lookup units (0 = all); useful for test runs")
    parser.add_argument("--unit-filter", default="",
                        help="only process units whose title contains this text")
    parser.add_argument("--apply", action="store_true",
                        help="write high-confidence changes to MongoDB (default: dry-run report only)")
    args = parser.parse_args()

    report_path = _safe_path(args.report, "--report", must_exist=True)
    cache_path = "nyaa_cache.json"

    with open(report_path, encoding="utf-8") as fh:
        dump = json.load(fh)
    records = dump["records"]

    units = defaultdict(list)
    for record in records:
        units[(extract_group(record["file_name"]), record["title"])].append(record)
    unit_items = sorted(units.items(), key=lambda kv: -len(kv[1]))
    if args.unit_filter:
        needle = args.unit_filter.lower()
        unit_items = [item for item in unit_items if needle in item[0][1].lower()]
    if args.max_units:
        unit_items = unit_items[:args.max_units]

    cache = _load_cache(cache_path)
    entries = cache.get("_entries", {})

    verdicts = []
    queries_used = 0
    for index, ((group, title), recs) in enumerate(unit_items, 1):
        if all(literal_name_quality(r["file_name"]) for r in recs):
            for record in recs:
                verdicts.append(propose_for_record(record, [], args.min_score))
            continue
        print(f"[{index}/{len(unit_items)}] [{group or 'no-group'}] {title} ({len(recs)} streams)")
        resolution = resolve_unit(recs, entries, args.delay)
        queries_used += len(resolution["queries"])
        for record in recs:
            verdicts.append(propose_for_record(record, resolution["candidates"], args.min_score))
        if index % 10 == 0:
            cache["_entries"] = entries
            _save_cache(cache_path, cache)
    print(f"nyaa queries issued (cached): {queries_used}")

    cache["_entries"] = entries
    _save_cache(cache_path, cache)
    write_reports(verdicts, "nyaa_resolution_report.txt", "nyaa_resolution_report.json")

    summary = defaultdict(int)
    for v in verdicts:
        summary[v["action"]] += 1
    print("\nDone.")
    for action in ("change", "reencode_keep", "review", "confirm"):
        print(f"  {action:14} {summary.get(action, 0)}")
    print("Reports: nyaa_resolution_report.txt / .json")

    if args.apply:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.env"))
        uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
        if len(uris) < 2:
            raise SystemExit("DATABASE in config.env needs at least 2 URIs.")
        print(f"\nConnecting to {len(uris)} database URI(s): "
              + ", ".join(_mask_uri(u) for u in uris))
        apply_changes(verdicts, uris)
    else:
        print("\nDry run only. Review the report, then re-run with --apply to write changes.")


if __name__ == "__main__":
    sys.exit(main())
