import re
from typing import Optional, Tuple

_VIDEO_EXTENSIONS = r'mkv|mp4|avi|ts|m4v|mov|wmv|webm|flv|m2ts|mpg|mpeg'
_ARCHIVE_EXTENSIONS = r'zip|rar|7z'
_ALL_EXTS = rf'{_VIDEO_EXTENSIONS}|{_ARCHIVE_EXTENSIONS}'

# Pattern 1: name.mkv.001 / name.zip.001 / name.001
_TRAILING_NUMERIC_RE = re.compile(rf'(?i)\.(?:({_ALL_EXTS})\.)?0*(\d{{1,4}})$')
# Pattern 2: name.part001.mkv / name.part1.mkv / [Raze] Movie Part 1 [1080p].mkv / name.cd1.avi / name.disc1.mkv / Pt. 1
_GENERAL_PART_RE = re.compile(
    r'(?i)[\s._\-\(\[]+(?:part|pt|cd|disc|disk)[s._\-\s]*0*(\d{1,4})(?:\s*(?:of|/)\s*0*\d{1,4})?(?:[\]\)\s._\-]|$)'
)
_EXT_SEARCH_RE = re.compile(rf'(?i)\.({_ALL_EXTS})(?:\.0*\d{{1,4}})?$')
_NORMALIZE_RE = re.compile(r'[\.\-_ ]+')
_ARCHIVE_SET = {'zip'}


# "Part N" closing an episode title ("S00E02 The Marked Hunt Part 1") is a
# two-part story, not a split upload. ".part001.mkv", ".mkv.001" and
# "S01E01 Part 1" are split parts.
_EPISODE_MARKER_BEFORE_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:s\d{1,2}[ ._-]*e\d{1,4}|\d{1,2}x\d{2,3}|e(?:p|pisode)?[ ._]*\d{1,4})(?![a-z0-9])"
    r"|\s-\s\d{1,4}(?:v\d)?\s"
)
_HUMAN_PART_RE = re.compile(
    r"(?i)^[\s._\-\(\[]+(?:part|pt)\.?[\s._]+[1-9]\d?(?:\s*(?:of|/)\s*\d{1,2})?(?:[\]\)\s\-._]|$)"
)


def _is_episode_title_part(name: str, match: "re.Match") -> bool:
    if not _HUMAN_PART_RE.match(match.group(0)):
        return False
    markers = list(_EPISODE_MARKER_BEFORE_RE.finditer(name[:match.start()]))
    if not markers:
        return False
    # Text between the marker and "Part N" makes it an episode title.
    between = name[markers[-1].end():match.start()]
    return bool(re.search(r"[A-Za-z]{2,}", between))


# Return the archive extension ('zip') if the name is a split archive part, else None
def split_archive_ext(filename: str) -> Optional[str]:
    if not filename:
        return None
    match = _find_split_match(filename.strip())
    if match and match[3] and match[3].lower() in _ARCHIVE_SET:
        return match[3].lower()
    return None


# Collapse separators into dots for stable grouping keys
def _normalize(base: str) -> str:
    return _NORMALIZE_RE.sub('.', base).strip('.').lower()


# Locate a split-part marker in a name (infix, trailing numeric, or trailing keyword)
def _find_split_match(name: str) -> Optional[Tuple[int, int, int, Optional[str]]]:
    if not name:
        return None
    m1 = _TRAILING_NUMERIC_RE.search(name)
    if m1:
        return m1.start(), m1.end(), int(m1.group(2)), m1.group(1)
    m2 = _GENERAL_PART_RE.search(name)
    if m2 and _is_episode_title_part(name, m2):
        m2 = None
    if m2:
        part_num = int(m2.group(1))
        ext_m = _EXT_SEARCH_RE.search(name)
        ext = ext_m.group(1) if ext_m else None
        return m2.start(), m2.end(), part_num, ext
    return None


# Remove a trailing or infix split-part suffix from a filename
def strip_part_suffix(filename: str) -> str:
    if not filename:
        return filename

    name = filename.strip()
    match = _find_split_match(name)
    if not match:
        return filename

    start, end, _part_num, ext = match
    before = name[:start].rstrip(' ._-')
    after = name[end:].lstrip(' ._-')
    if ext:
        if after.lower().endswith(f'.{ext.lower()}'):
            after_inner = after[:-len(ext)-1].strip(' ._-')
            if after_inner:
                return f'{before}.{after_inner}.{ext}'
            return f'{before}.{ext}'
        if after.lower() == ext.lower():
            return f'{before}.{ext}'
        return f'{before}.{after}.{ext}' if after else f'{before}.{ext}'
    return f'{before}.{after}' if after else before


# Parse a split filename into (group_key, part_number), or None
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

# One explicit episode marker ("S01E52")
_SINGLE_EPISODE_RE = re.compile(r"(?i)(?<![a-z0-9])s\d{1,2}[ ._-]*e\d{1,4}(?![a-z])")
# "S02E" right before the first number of a range
_SXXE_PREFIX_RE = re.compile(r"(?i)s\d{1,2}[ ._-]*e(?:p|pisode)?\s*0*$")
# Another episode number directly after the pair
_RANGE_TAIL_EPISODE_RE = re.compile(r"(?i)^\s*(?:[-–—_]\s*)+(?:e(?:p|pisode)?\s*)?0*\d{1,4}(?![\dpx])")
_RESOLUTION_VALUES = {240, 360, 480, 540, 576, 720, 1080, 1440, 2160, 4320}


# Extract a season number from a combined-episode filename
def _combined_season(name: str) -> Optional[int]:
    match = _COMBINED_SEASON_RE.search(name)
    return int(match.group(1)) if match else None


def _is_real_range(name: str, match: "re.Match") -> bool:
    """Tell an episode range from two numbers that only look like one.

    Not ranges:
        S02E01 - 021 - Title     episode followed by its absolute number
        S00E02 - Episode 15      special followed by its broadcast number
        Title 17-26 - 01 (...)   the pair belongs to the title
        Show - 12 - 1080p        episode followed by the resolution
        2019-2021                years
    """
    whole = match.group(0)
    start, end = int(match.group(1)), int(match.group(2))
    sep_m = re.search(r"\d\s*(-|–|~|\+|&|,|to)\s*(?:E(?:P|PISODE)?\s*)?0*\d{1,4}$", whole, re.IGNORECASE)
    # Only " - " is ambiguous; "&", "to" and "~" always join two episodes.
    spaced = bool(re.search(r"\d\s+[-–]\s+", whole))
    second_has_e = bool(re.search(r"(?i)(?:-|–|~|\+|&|,|to)\s*E\s*0*\d{1,4}$", whole))
    second_has_word = bool(re.search(r"(?i)(?:-|–|~|\+|&|,|to)\s*(?:EP|EPISODE)\s*0*\d{1,4}$", whole))

    # Second number is a resolution
    after = name[match.end():]
    if end in _RESOLUTION_VALUES and re.match(r"(?i)\s*[pi]\b", after):
        return False
    if end in _RESOLUTION_VALUES and re.match(r"\s*x\s*\d{3,4}", after):
        return False

    # First number belongs to an SxxEyy marker
    first_num_pos = match.start(1)
    head = name[:first_num_pos]
    if _SXXE_PREFIX_RE.search(head):
        # "S01E01-E04" and "S01E01-04" are ranges, "S02E01 - 021" is not.
        if spaced and not second_has_e:
            return False
        if second_has_word:
            return False

    # Pair inside the title, real episode number after it
    if _RANGE_TAIL_EPISODE_RE.match(after):
        return False

    # Year span
    if 1900 <= start <= 2100 and 1900 <= end <= 2100:
        return False
    return bool(sep_m)


# Detect combined-episode ranges (e.g. E01-E04, 001-366), or None
def parse_combined_episodes(filename: str) -> Optional[dict]:
    if not filename:
        return None

    for match in _COMBINED_EPISODES_RE.finditer(filename):
        start, end = int(match.group(1)), int(match.group(2))
        if not (1 <= start < end <= 9999):
            continue
        if not _is_real_range(filename, match):
            continue
        season = _combined_season(filename)
        return {"season": season if season is not None else 1, "start": start, "end": end}

    # "Combined" marks a season pack unless the name has its own episode
    # number ("S01E52 - Combined Strength").
    if _COMBINED_KEYWORD_RE.search(filename) and not _SINGLE_EPISODE_RE.search(filename):
        season = _combined_season(filename)
        if season is not None:
            return {"season": season, "start": None, "end": None}

    return None


# Stable grouping key for combined files (episode range/keyword removed)
def combined_name_key(filename: str) -> str:
    if not filename:
        return ""
    name = _COMBINED_EPISODES_RE.sub("", filename)
    name = _COMBINED_KEYWORD_RE.sub("", name)
    return re.sub(r"[\s._-]+", " ", name).strip().lower()
