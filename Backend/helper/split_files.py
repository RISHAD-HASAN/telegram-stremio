import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg'
_ARCHIVE_EXTENSIONS = r'zip|rar|7z'
_ALL_EXTS = rf'{_VIDEO_EXTENSIONS}|{_ARCHIVE_EXTENSIONS}'

# Pattern 1: name.mkv.001 / name.zip.001 / name.001
_TRAILING_NUMERIC_RE = re.compile(rf'(?i)\.(?:({_ALL_EXTS})\.)?0*(\d{{1,4}})$')
# Pattern 2: name.part001.mkv / name.part01.rar / name.cd1.avi / name.disc1.mkv
_INFIX_PART_RE = re.compile(rf'(?i)[\s._-]*(?:part|cd|disc|disk)[s._-]*0*(\d{{1,4}})(?=[\s._-]*\.({_ALL_EXTS})$)')
# Pattern 3: name.part001 / name.part1 (no trailing ext)
_TRAILING_PART_RE = re.compile(r'(?i)[\s._-]*(?:part|cd|disc|disk)[s._-]*0*(\d{1,4})$')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')
_ARCHIVE_SET = {'zip'}


#----- Return the archive extension ('zip') if the name is a split archive part, else None
def split_archive_ext(filename: str) -> Optional[str]:
    if not filename:
        return None
    match = _find_split_match(filename.strip())
    if match and match[3] and match[3].lower() in _ARCHIVE_SET:
        return match[3].lower()
    return None


#----- Collapse separators into dots for stable grouping keys
def _normalize(base: str) -> str:
    return _NORMALIZE_RE.sub('.', base).strip('.').lower()


#----- Locate a split-part marker in a name (infix, trailing numeric, or trailing keyword)
def _find_split_match(name: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    if not name:
        return None
    m = _INFIX_PART_RE.search(name)
    if m:
        return m.start(), m.end(), int(m.group(1)), m.group(2)
    m = _TRAILING_NUMERIC_RE.search(name)
    if m:
        return m.start(), m.end(), int(m.group(2)), m.group(1)
    m = _TRAILING_PART_RE.search(name)
    if m:
        return m.start(), m.end(), int(m.group(1)), None
    return None


#----- Remove a trailing or infix split-part suffix from a filename
def strip_part_suffix(filename: str) -> str:
    if not filename:
        return filename

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return filename

    start, end, _part_num, ext = match
    if ext and name.lower().endswith(f".{ext.lower()}"):
        return name[:start] + f".{ext}"
    return (name[:start] + '.' + ext) if ext else (name[:start] + name[end:])


#----- Parse a split filename into (group_key, part_number), or None
def parse_split_info(filename: str) -> Optional[Tuple[str, int]]:
    if not filename:
        return None

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return None

    start, end, part_num, ext = match
    remainder = strip_part_suffix(name)
    return _normalize(remainder), part_num


_COMBINED_EPISODES_RE = re.compile(
    r"(?i)(?:[-–—\(\[\s]|S0*\d{1,2}|E(?:P|PISODE)?)\s*0*(\d{1,4})\s*(?:-|–|~|\+|&|,|to)\s*(?:E(?:P|PISODE)?\s*)?0*(\d{1,4})(?=\s*[\)\]\.\s_-]|\D|$)"
)
_COMBINED_SEASON_RE = re.compile(r"S(?:EASON)?[\s._-]*0*(\d{1,3})", re.IGNORECASE)
_COMBINED_KEYWORD_RE = re.compile(r"\bcombined\b", re.IGNORECASE)


#----- Extract a season number from a combined-episode filename
def _combined_season(name: str) -> Optional[int]:
    match = _COMBINED_SEASON_RE.search(name)
    return int(match.group(1)) if match else None


#----- Detect combined-episode ranges (e.g. E01-E04, 001-366), or None
def parse_combined_episodes(filename: str) -> Optional[dict]:
    if not filename:
        return None

    match = _COMBINED_EPISODES_RE.search(filename)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if 1 <= start < end <= 9999:
            return {"season": _combined_season(filename) or 1, "start": start, "end": end}

    if _COMBINED_KEYWORD_RE.search(filename):
        season = _combined_season(filename)
        if season is not None:
            return {"season": season, "start": None, "end": None}

    return None


#----- Stable grouping key for combined files (episode range/keyword removed)
def combined_name_key(filename: str) -> str:
    if not filename:
        return ""
    name = _COMBINED_EPISODES_RE.sub("", filename)
    name = _COMBINED_KEYWORD_RE.sub("", name)
    return re.sub(r"[\s._-]+", " ", name).strip().lower()
