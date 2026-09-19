"""Filename / caption parsing helpers."""
from __future__ import annotations

import re
import traceback

import PTN
from guessit import guessit as _guessit

from Backend.helper.metadata.common import COMBINED_EPISODE_BASE, COMBINED_SEASON, first
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix
from Backend.logger import LOGGER

# --- CRC32 checksum stripping ------------------------------------------
# Anime fansub releases often include an 8-character hex CRC32 checksum in
# brackets, e.g. "[ADF621E6]" or "(31F3BD74)". When the hash ends in "E" + digits
# (like "E6" or "E01"), PTN's episode regex misidentifies the hash as an episode
# number (e.g. reading episode 6 from "[ADF621E6]"). Stripping CRC32 hashes before
# PTN parsing prevents this collision completely.
_CRC32_RE = re.compile(r"(?i)[\[\(][0-9a-f]{8}[\]\)]")

def _strip_crc32(name: str) -> str:
    if not name:
        return name
    return _CRC32_RE.sub(" ", name)

ROMAN_NUMS = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
}
_ROMAN_SEASON_RE = re.compile(
    r"(?i)\b(?:season\s+)?(ii|iii|iv|v|vi|vii|viii|ix|x)\b(?!\s*x\s*\d)"
)
_ORDINAL_SEASON_RE = re.compile(
    r"(?i)\b(\d{1,2})(?:st|nd|rd|th)\s+season\b"
)
_PART_SEASON_RE = re.compile(
    r"(?i)\b(?:part|cour)\s*0*(\d{1,2})\b"
)
_R_SEASON_RE = re.compile(
    r"(?i)\bR(\d{1,2})\b"
)

_KNOWN_GROUPS_RE = re.compile(
    r"^(?:\[[^\]]+\]|\((?!(?:19|20)\d{2}\))[^)]+\)|(?:raze|subsplease|erai-raws|judas|ember|asw|horriblesubs|smallsize|commie|coalgirls|kametsu|bakedfish|doki|neogirls|rarbg|nyaa|animebytes|hdkai|hdbits|telly|encore|fle|ozr|jysze|animetime|anime\s+time|psa|pahe|yts|yify|galaxyrg|tgx|bonkai|cleo)[_\s-]+)",
    re.I
)

def _clean_pre_normalizations(name: str) -> str:
    if not name:
        return ""
    # Strip Telegram hashtags (e.g. #anime #1080p #dual_audio)
    name = re.sub(r"#\w+", " ", name)
    # 1. Doubled extensions (.mkv.mkv, .mp4.mkv)
    while re.search(r"\.(mkv|mp4|avi|webm|ts|m4v)\.(mkv|mp4|avi|webm|ts|m4v)$", name, re.I):
        name = re.sub(r"\.(mkv|mp4|avi|webm|ts|m4v)$", "", name, flags=re.I)
    # 2. Trailing file size tags in parentheses e.g. "(1382MB)"
    name = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*[KMGT]?B\s*\)", " ", name, flags=re.I)
    # 3. Space/underscore mangled framerate (e.g. 143_8561fps or 143 8561fps)
    name = re.sub(r"(?i)\b\d+(?:[\s._]\d+)?fps\b", " ", name)
    # 4. Strip leading release group (bracketed or underscore-separated e.g. Raze_Dandadan)
    name = _KNOWN_GROUPS_RE.sub(" ", name).strip()
    # 5. Convert underscores to spaces
    name = name.replace("_", " ")
    # 6. Normalize Ep / Episode wording (e.g. "Ep. 05" -> "E05", "Episode 12" -> "E12")
    name = re.sub(r"(?i)\b(?:ep|ep\.|episode)\s*0*(\d{1,4})\b", r"E\1", name)
    # 7. Bare S E<n> -> S00E<n> (specials)
    name = re.sub(r"(?i)(?:^|\s)S\s+E0*(\d{1,3})\b", r" S00E\1", name)
    # 8. S<n> / R<n> / Season <n> followed by episode e.g. "R2 01" -> "S02E01", "S2 07" -> "S02E07", "Season 2 08" -> "S02E08"
    name = re.sub(
        r"(?i)(?:^|\s)(?:S|R|Season\s*)0*(\d{1,2})\s*[-_.]?\s*(?:E0*(\d{1,3})|0*(\d{1,3}))\b",
        lambda m: f" S{int(m.group(1)):02d}E{int(m.group(2) or m.group(3)):02d}",
        name
    )
    # 9. Strip collection prefix like "Studio Ghibli Movie 05" or leading "Movie 05" so exact movie title is parsed
    name = re.sub(r"(?i)(?:^|(?:\b(?:Studio\s+Ghibli|Makoto\s+Shinkai|Anime\s+Time)\s*[-_.]?\s*))\bMovie\s*\d+\s*[-_.]?\s*", " ", name)
    # 10. Normalize trailing recap/summary to special S00E01
    name = re.sub(r"(?i)\b(?:re|recap|summary)\s*(\.[a-z0-9]{2,4}|$)", r" S00E01\1", name)
    # 11. Versioned episode patterns e.g. "S01E01v2" -> "S01E01", "Naruto.001.v4..." -> "Naruto E01...", "91 Days 01v2" -> "91 Days E01"
    name = re.sub(
        r"(?i)\bS0*(\d{1,2})\s*[-_.]?\s*E0*(\d{1,3})v\d*\b",
        lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}",
        name
    )
    name = re.sub(
        r"(?i)\bE0*(\d{1,3})v\d*\b",
        lambda m: f"E{int(m.group(1)):02d}",
        name
    )
    name = re.sub(r"(?i)[._\s]+0*(\d{1,4})[._\s]*v\d+(?=[._\s])", lambda m: f" E{int(m.group(1)):02d} ", name)
    name = re.sub(r"(?i)\b\d{1,3}\s*v\d*\s+E0*(\d{1,3})\b", r"E\1", name)
    return re.sub(r"\s+", " ", name).strip()

def _sanitize_dual_episodes(name: str) -> str:
    """Strip secondary/absolute parenthesized episode (e.g. 'S02 - 01 (25)' -> 'S02 - 01') so PTN doesn't parse as season range."""
    if not name:
        return name
    return re.sub(
        r"(?i)([-_.\s]\d{1,3})\s*[\(\[]0*\d{2,4}[\)\]]",
        r"\1",
        name,
    )

# --- elierpg port (safety-hardened) -------------------------------------
# SxE notation ("1x1", "5X14") -> "S01E01" before PTN, so PTN extracts both
# title and episode instead of treating the bare number as a year/extra.
# Hardened vs upstream-fork version: requires a non-alphanumeric char on both
# sides, so "1920x1080" (resolution) is never mangled into "S19E20".
_SXE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})[xX](\d{1,3})(?![A-Za-z0-9])")

def _normalize_sxe(name: str) -> str:
    if not name:
        return name
    def _replacer(m):
        return "S{0:02d}E{1:02d}".format(int(m.group(1)), int(m.group(2)))
    return _SXE_RE.sub(_replacer, name)

# --- Explicit season/episode anchors -----------------------------------
# GuessIt will happily invent a season/episode by splitting any bare 3-4
# digit number in half (e.g. "One Piece - 1172" -> season 11, episode 72;
# or read a "(2011)" reboot-year tag as season 2011). PTN already catches
# real SxxExx / NxNN patterns on its own, so GuessIt's season/episode is
# only ever trusted when the filename has independent, explicit textual
# evidence for it (i.e. spelled-out "Season N"/"Series N" wording, which
# PTN does NOT parse by itself). Anything else falls through to the
# absolute-episode logic below instead of guessing.
_EXPLICIT_SXXEXX_RE = re.compile(r"(?i)\bs\d{1,2}[._\s-]*e\d{1,3}\b")
# Episode may be 1-3 digits so "4x2" / "1x1" / "12x3" are caught (upstream
# \d{2,3} dropped single-digit episodes like "4x2" and forced episode 1).
_EXPLICIT_NXNN_RE = re.compile(r"(?i)\b\d{1,2}x\d{1,3}\b")
# Special episode tags: S00E01, OVA 01, OAD 01, Special 01, SP01, NCED 01, NCOP 01
_SPECIAL_EP_RE = re.compile(r"(?i)\b(?:s0*0e|ova|oad|special|sp|nced|ncop)\s*[-_.]?\s*0*(\d{1,3})\b")
_BARE_SPECIAL_RE = re.compile(r"(?i)\b(?:ova|oad)\b")
# Movie keyword indicators
_MOVIE_KEYWORD_RE = re.compile(r"(?i)\b(?:the\s+movie|movie\s*\d*|film|gekijouban|gekijou-ban)\b")
# Latino Spanish wording: "temporada N" / "temp N" (season) and
# "capitulo N" / "capítulo N" / "cap N" (episode). Tolerates "_" / "." as the
# separator (uploaders write "Soy_Tu_Duena_Capitulo_5.mkv").
_EXPLICIT_CAPITULO_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:temporada|temp|cap[íi]tulo|cap)[._\s-]*0*\d{1,3}"
)
_EXPLICIT_SEASON_WORD_RE = re.compile(r"(?i)\b(?:season|series)\s*0*\d{1,2}\b")


def resolution_from_dimensions(width: int = 0, height: int = 0) -> Optional[str]:
    """Accurately maps video pixel dimensions to standard resolution tags across 16:9, 4:3, and cinemascope."""
    if not width and not height:
        return None
    w = width or 0
    h = height or 0

    # 4K / UHD
    if w >= 3800 or h >= 2100:
        return "4K"
    # 1440p / 2K
    if w >= 2500 or h >= 1400:
        return "1440p"
    # 1080p FHD (including widescreen 1920x800, 1920x1040)
    if w >= 1800 or h >= 1000 or (w >= 1900 and h >= 720):
        return "1080p"
    # 720p HD (including widescreen 1280x534)
    if (w >= 1200 and h >= 500) or (700 <= h < 1000):
        return "720p"
    # 576p PAL SD
    if h >= 540 or (w >= 720 and h >= 540):
        return "576p"
    # 480p NTSC SD (640x480, 720x480, 854x480)
    if h >= 440 or (w >= 640 and h >= 400):
        return "480p"
    # 360p
    if h >= 320 or (w >= 480 and h >= 300):
        return "360p"
    if h > 0:
        return f"{h}p"
    return None


def parse_media_name(name: str) -> dict:
    name = _strip_crc32(name)
    name = _clean_pre_normalizations(name)
    name = _normalize_sxe(name)
    sanitized_name = _sanitize_dual_episodes(name)
    try:
        ptn = PTN.parse(sanitized_name) or {}
    except Exception as e:
        LOGGER.warning(f"PTN parsing failed for {name}: {e}")
        ptn = {}

    parsed = {
        "title": ptn.get("title"),
        "year": ptn.get("year"),
        "season": ptn.get("season"),
        "episode": ptn.get("episode"),
        "quality": ptn.get("resolution"),
        "excess": ptn.get("excess"),
    }

    # Recover resolution from dimension tokens if missing (e.g. 1920x1080 -> 1080p, 1280x720 -> 720p, 720x576 -> 576p, 720x480 -> 480p)
    if not parsed.get("quality"):
        dim_m = re.search(r"\b(\d{3,4})[xX](\d{3,4})\b", name)
        if dim_m:
            w_val, h_val = int(dim_m.group(1)), int(dim_m.group(2))
            res_val = resolution_from_dimensions(w_val, h_val)
            if res_val:
                parsed["quality"] = res_val

    # Recover ordinal / roman / part / cour / R seasons in anime titles
    if parsed.get("season") is None or (isinstance(parsed.get("season"), list) and parsed.get("episode") is not None):
        if isinstance(parsed.get("season"), list):
            parsed["season"] = parsed["season"][0]
        m = _ORDINAL_SEASON_RE.search(name)
        if m:
            parsed["season"] = int(m.group(1))
        elif _R_SEASON_RE.search(name):
            parsed["season"] = int(_R_SEASON_RE.search(name).group(1))
        elif _PART_SEASON_RE.search(name):
            # Only treat Part N as season if it does NOT occur after an episode number token
            part_m = _PART_SEASON_RE.search(name)
            is_after_ep = False
            if parsed.get("episode") is not None:
                ep_val = parsed["episode"]
                for pat in (rf"\bE0*{ep_val}\b", rf"\b0*{ep_val}\b"):
                    ep_m = re.search(pat, name, re.IGNORECASE)
                    if ep_m and part_m.start() > ep_m.end():
                        is_after_ep = True
                        break
            if not is_after_ep:
                parsed["season"] = int(part_m.group(1))
        elif _ROMAN_SEASON_RE.search(name):
            val = _ROMAN_SEASON_RE.search(name).group(1).lower()
            if val in ROMAN_NUMS:
                parsed["season"] = ROMAN_NUMS[val]

    if _guessit:
        try:
            g = _guessit(name)
            parsed["title"] = parsed["title"] or first(g.get("title"))
            parsed["year"] = parsed["year"] or first(g.get("year"))

            # Only pull season/episode from GuessIt when PTN found neither
            # AND the filename has explicit textual evidence for a season
            # (spelled-out "Season"/"Series" wording, or a raw SxxExx/NxNN
            # PTN somehow missed). Never accept GuessIt's pure numeric
            # digit-splitting guess.
            if parsed["season"] is None and parsed["episode"] is None:
                has_anchor = bool(
                    _EXPLICIT_SXXEXX_RE.search(name)
                    or _EXPLICIT_NXNN_RE.search(name)
                    or _EXPLICIT_SEASON_WORD_RE.search(name)
                    or _EXPLICIT_CAPITULO_RE.search(name)
                    or _SPECIAL_EP_RE.search(name)
                )
                if has_anchor:
                    g_season = first(g.get("season"))
                    if g_season is not None:
                        try:
                            g_season_int = int(g_season)
                        except (TypeError, ValueError):
                            g_season_int = None
                        if g_season_int is not None and g_season_int >= 0:
                            parsed["season"] = g_season_int
                    g_episode = first(g.get("episode"))
                    if g_episode is not None:
                        try:
                            g_episode_int = int(g_episode)
                        except (TypeError, ValueError):
                            g_episode_int = None
                        if g_episode_int is not None and g_episode_int > 0:
                            parsed["episode"] = g_episode_int

            parsed["quality"] = parsed["quality"] or first(g.get("screen_size"))
        except Exception as e:
            LOGGER.warning(f"GuessIt parsing failed for {name}: {e}")

    # Specials / OVAs / OADs / SP detection
    # If no season/episode found yet, check for explicit OVA/Special/SP/NCED/NCOP
    if parsed.get("season") is None or parsed.get("episode") is None:
        sp_m = _SPECIAL_EP_RE.search(name)
        if sp_m is not None:
            try:
                sp_ep = int(sp_m.group(1))
            except (TypeError, ValueError):
                sp_ep = 1
            parsed["season"] = 0
            parsed["episode"] = sp_ep
        elif _BARE_SPECIAL_RE.search(name) and not _MOVIE_KEYWORD_RE.search(name) and parsed.get("season") is None and parsed.get("episode") is None:
            parsed["season"] = 0
            parsed["episode"] = 1

    # Direct Latino Spanish extraction for "temporada/capitulo" wording.
    # GuessIt is unreliable for Spanish "capitulo", so parse it ourselves and
    # only fill what PTN/GuessIt left empty. "temporada 4 capitulo 2" ->
    # season 4, episode 2; bare "capitulo 2" -> season 1, episode 2.
    if parsed.get("episode") is None or parsed.get("season") is None:
        _season_m = re.search(
            r"(?i)\b(?:temporada|temp)\s*0*(\d{1,3})\b", name
        )
        _ep_m = re.search(
            r"(?i)(?<![a-z0-9])(?:cap[íi]tulo|cap)[._\s-]*0*(\d{1,3})", name
        )
        if _ep_m is not None:
            try:
                _ep_int = int(_ep_m.group(1))
            except (TypeError, ValueError):
                _ep_int = None
            if _ep_int is not None and _ep_int > 0 and parsed.get("episode") is None:
                parsed["episode"] = _ep_int
        if _season_m is not None:
            try:
                _se_int = int(_season_m.group(1))
            except (TypeError, ValueError):
                _se_int = None
            if _se_int is not None and _se_int >= 0 and parsed.get("season") is None:
                parsed["season"] = _se_int
        # If we found a Spanish episode but no season anywhere, default to 1
        # so "capitulo 2" routes as S01E02 instead of an absolute/anime episode.
        if _ep_m is not None and parsed.get("episode") is not None and parsed.get("season") is None:
            parsed["season"] = 1

    # Recover the episode from a raw NxNN token ("4x2", "12x3") when PTN read
    # the trailing digit as a release "group" instead of the episode (it does
    # this for single-digit episodes). The anchor regex already widened to
    # 1-3 digits; pull the episode straight from the match.
    if parsed.get("season") is not None and parsed.get("episode") is None:
        _nxnn_m = _EXPLICIT_NXNN_RE.search(name)
        if _nxnn_m is not None:
            _nums = _nxnn_m.group(0).lower().split("x")
            if len(_nums) == 2:
                try:
                    _ep_int = int(_nums[1])
                except (TypeError, ValueError):
                    _ep_int = None
                if _ep_int is not None and _ep_int > 0:
                    parsed["episode"] = _ep_int

    return parsed


def apply_combined_override(payload: dict, combined: dict) -> None:
    season, start, end = combined["season"], combined["start"], combined["end"]
    payload["season_number"] = COMBINED_SEASON
    payload["episode_number"] = COMBINED_EPISODE_BASE + season
    payload["episode_title"] = f"Season {season} Combined"
    label = "Full" if start is None else f"E{start:02d}-E{end:02d}"
    payload["quality"] = f"{payload.get('quality') or 'HD'} {label}"
    if not payload.get("episode_backdrop"):
        payload["episode_backdrop"] = payload.get("backdrop") or payload.get("poster") or ""


# Absolute / orphan episode patterns (no SxxExx), e.g. "One Piece 1223 720.mkv"
_SEASON_EP_RE = _EXPLICIT_SXXEXX_RE
# Resolution with an explicit trailing 'p' (unambiguous quality marker)
_RES_WITH_P_RE = re.compile(r"(?i)(?<![\w])(?:240|360|480|576|720|1080|1440|2160|4320)p(?![\w])")
# Bare resolution value with NO trailing 'p' (e.g. "One Piece 1223 720.mkv") is
# ambiguous with an absolute episode number that happens to equal a common
# resolution (episode 240, 480, 720...). Only treat it as quality when it's the
# trailing token right before the extension/end of string - conventionally
# where quality sits - and it's applied only as a fallback when no proper
# "NNNp" resolution was already found elsewhere in the name.
_RES_BARE_TRAILING_RE = re.compile(
    r"(?i)(?<![\w])(?:240|360|480|576|720|1080|1440|2160|4320)(?![\w])"
    r"(?=(?:[\s._-]*(?:\.[a-z0-9]{2,4})?)$)"
)
# Quality / codec / audio tokens
_QUALITY_TOKEN_RE = re.compile(
    r"(?i)(?:\d{3,4}x\d{3,4}|web-?dl|blu-?ray|bluray|hdtv|hdrip|webrip|bdrip|brrip|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|aac|"
    r"(?:ddp|dd\+?|e?ac-?3|dts(?:-?hd)?|truehd|atmos)\s*\d?(?:[\s.]\d)?|"
    r"(?<!\d)\d[\s.]\d(?!\d)|10bit|8bit|"
    r"multi(?:\s*audio)?|dual(?:\s*audio)?|esub|subs?|softsubs?|hardsubs?|"
    r"(?<![\w])(?:bd|remux|encode)(?![\w]))"
)
# Bracketed release-group tags: [Judas], [SubsPlease], etc.
_RELEASE_GROUP_RE = re.compile(r"\[[^\]]{1,40}\]")
# Trailing absolute ep after title: "One Piece - 1172" / "One Piece 1172"
_TITLE_ABS_EP_RE = re.compile(
    r"(?i)^(?P<title>.+?)\s*[-–—]?\s*0*(?P<ep>\d{2,4})\s*$"
)
_YEAR_RE = re.compile(r"(?:^|[\s._\-(])((?:19|20)\d{2})(?:[\s._\-)]|$)")
# Numbered title units where leading number is part of show name (e.g. "5 Centimeters per Second", "91 Days", "3-gatsu no Lion")
_TITLE_UNITS_RE = re.compile(
    r"(?i)\b\d+\s*(?:centimeters?|cm|days?|toubun|nin|hours?|minutes?|percent|%|leaf|clover|bullets?|princes?|kingdoms?|wars?|deadly\s+sins?|tails?|witches|kanojo|gatsu|round|seconds?)\b"
)


def extract_absolute_episode(filename: str, parsed: dict | None = None) -> int | None:
    """Return absolute episode number when no season is present.

    Handles styles like:
      [UDF] 91 Days 01v2 (BDRip 1080p x264 FLACx2) [5AEB36B4].mkv -> Ep 1
      [Raze] Sakamoto Days 11 ... 143.8561fps.mkv -> Ep 11
      [Raze] Gachiakuta 19 ... 143.8561fps.mkv -> Ep 19
      Bleach 001 The Day I Became A Shinigami.mkv -> Ep 1
      Bleach 024 Assemble! The 13 Divisions.mkv -> Ep 24
      One Piece 1223 720.mkv -> Ep 1223
      [Judas] One Piece - 1172.mkv -> Ep 1172
    """
    parsed = parsed or {}
    try:
        if parsed.get("season") is not None and int(parsed.get("season")) >= 0:
            return None
    except (TypeError, ValueError):
        if parsed.get("season") is not None:
            return None
    if _SEASON_EP_RE.search(filename or ""):
        return None

    # Movies (e.g. "Bleach the Movie 1 Memories of Nobody", "Chainsaw Man - Movie")
    # should NOT extract "1" as an absolute TV episode
    if _MOVIE_KEYWORD_RE.search(filename or "") and not re.search(r"(?i)\b(?:e|ep|episode)\s*0*\d+", filename or ""):
        return None

    ep = parsed.get("episode")
    if isinstance(ep, list):
        return None
    if ep is not None:
        try:
            return int(ep)
        except (TypeError, ValueError):
            pass

    name = filename or ""
    # Strip extension, release-group brackets, quality/codec tokens
    cleaned = re.sub(r"\.[a-z0-9]{2,4}$", " ", name, flags=re.I)
    cleaned = _RELEASE_GROUP_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\([^\)]{1,40}\)", " ", cleaned)
    # Strip fps (e.g. 143.8561fps, 60fps, 24fps)
    cleaned = re.sub(r"(?i)\b\d+(?:\.\d+)?fps\b", " ", cleaned)
    cleaned = _RES_WITH_P_RE.sub(" ", cleaned)
    if not _RES_WITH_P_RE.search(name):
        cleaned = _RES_BARE_TRAILING_RE.sub(" ", cleaned)
    cleaned = _QUALITY_TOKEN_RE.sub(" ", cleaned)
    # Strip years so 2021 is not treated as an episode
    cleaned = _YEAR_RE.sub(" ", cleaned)
    # Strip v2, v3 suffixes (e.g. 01v2 -> 01)
    cleaned = re.sub(r"(?i)(?<=\d)v\d+\b", " ", cleaned)
    # Strip audio channel notations (5.1, 7.1, 2.0)
    cleaned = re.sub(r"(?i)\b\d\.\d\b", " ", cleaned)
    # Strip numbered title units so "5 Centimeters", "91 Days" don't misread show name as episode
    cleaned = _TITLE_UNITS_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\s._-]+", " ", cleaned).strip()

    # 1. Explicit E/EP/Episode prefix wins
    prefixed = re.findall(r"(?i)(?:^|\s)(?:e|ep|episode)\s*0*(\d{1,4})(?:\s|$)", cleaned)
    if prefixed:
        return int(prefixed[-1])

    # 2. Leading-zero number (e.g. 01, 02, 024, 001, 016) is almost always an episode tag
    # (beats non-leading zero numbers in titles like '91' in '91 Days')
    leading_zero = re.findall(r"(?:^|\s)0+(\d{1,4})(?:\s|$)", cleaned)
    if leading_zero:
        return int(leading_zero[0])

    # 3. Number immediately following a dash or separator: "Bleach - 24 - Assemble!"
    sep_m = re.search(r"(?i)[-–—]\s*0*(\d{1,4})\b", filename or "")
    if sep_m:
        return int(sep_m.group(1))

    # 4. Trailing numbers or first number left after stripping title
    nums = re.findall(r"(?:^|\s)(\d{1,4})(?:\s|$)", cleaned)
    candidates = []
    for x in nums:
        try:
            n = int(x)
        except ValueError:
            continue
        if n < 1:
            continue
        candidates.append(n)
    if not candidates:
        return None

    # Prefer 3–4 digit token (typical anime absolute e.g. 110, 190, 366); else last remaining
    three_digit = [n for n in candidates if n >= 100]
    if three_digit:
        return three_digit[-1]
    return candidates[-1]


_STUDIO_PREFIX_RE = re.compile(
    r"(?i)^(?:studio\s+ghibli|ghibli|makoto\s+shinkai|disney|pixar|kyoto\s+animation|kyoani|"
    r"comix\s+wave(?:[\s.]films)?|ufotable|mappa|madhouse|bones|sunrise|toei(?:\s+animation)?|"
    r"trigger|production\s+i\.?g|a-?1\s+pictures|cloverworks|shaft|wit(?:\s+studio)?|pierrot)\b"
)
_COLLECTION_MOVIE_RE = re.compile(
    r"(?i)^(?P<prefix>.+?)\s*[-–—:]\s*(?:the\s+)?movie\s*0*\d*\s*[-–—:]\s*(?P<title>.+)$"
)
_SEASON_SUFFIX_RE = re.compile(
    r"(?i)\s*[-–—:]?\s*\b(?:"
    r"\d{1,2}(?:st|nd|rd|th)\s+season|"
    r"season\s*\d{1,2}|"
    r"(?:the\s+)?final\s+season(?:\s+part\s*\d*)?|"
    r"part(?:\s*\d+)?|"
    r"cour(?:\s*\d+)?|"
    r"r\d{1,2}|"
    r"(?:season\s+)?(?:ii|iii|iv|v|vi|vii|viii|ix|x)"
    r")\s*$"
)


def clean_anime_search_title(title: str, absolute_ep: int | None = None) -> str:
    """Strip absolute episode / noise from a title used for provider search.

    "One Piece - 1172" + abs=1172 -> "One Piece"
    "Bleach 001 The Day I Became A Shinigami" + abs=1 -> "Bleach"
    "[Judas] One Piece" -> "One Piece"
    "[SubsPlease] Bleach - OVA 01" -> "Bleach"
    "Studio Ghibli - Movie 04 - Kiki's Delivery Service" -> "Kiki's Delivery Service"
    "Dragon Ball Z - Movie 08 - Broly The Legendary Super Saiyan" -> "Dragon Ball Z Broly The Legendary Super Saiyan"
    "Fire Force Part 2" -> "Fire Force"
    "Mob Psycho 100 II" -> "Mob Psycho 100"
    """
    t = (title or "").strip()
    if not t:
        return t
    t = _RELEASE_GROUP_RE.sub(" ", t)
    t = re.sub(r"\([^\)]{1,40}\)", " ", t)

    # Collection/Studio movie pattern: "Studio Ghibli - Movie 04 - Kiki's Delivery Service"
    col_m = _COLLECTION_MOVIE_RE.match(t)
    if col_m:
        prefix = col_m.group("prefix").strip()
        real_title = col_m.group("title").strip()
        if _STUDIO_PREFIX_RE.match(prefix):
            t = real_title
        else:
            t = f"{prefix} {real_title}"

    # Strip special tags: - OVA 01, - Special 01, - SP01, - S00E01, etc.
    t = re.sub(
        r"(?i)\s*[-–—]?\s*\b(?:s0*0e\s*\d{1,3}|ova\s*\d*|oad\s*\d*|special\s*\d*|sp\s*0*\d+|nced\s*\d*|ncop\s*\d*)\b\s*",
        " ",
        t,
    )
    # Strip standalone movie markers like " - Movie" or " - The Movie"
    t = re.sub(r"(?i)\s*[-–—]\s*(?:the\s+movie|movie|film)\b", " ", t)
    t = re.sub(r"[\s._]+", " ", t).strip()

    # Strip season / part / cour / roman numeral suffixes from show titles
    t = _SEASON_SUFFIX_RE.sub("", t).strip()

    if absolute_ep is not None:
        ep_num = int(absolute_ep)
        # Pattern 1: Title followed by episode in the middle: 'Bleach 001 The Day I Became A Shinigami' or 'Bleach - 001 - Title'
        mid_m = re.match(
            rf"(?i)^(?P<show>.+?)\s*[-–—]?\s*(?:e|ep|episode)?\s*0*{ep_num}\s*(?:[-–—:]\s*|\s+)(?P<ep_title>.+)$",
            t,
        )
        if mid_m:
            show = mid_m.group("show").strip()
            # Ensure show name is reasonable length (not empty) and not an anchor
            if len(show) >= 2:
                t = show

        # Pattern 2: Trailing episode: 'Bleach - 001' or 'One Piece 1172'
        t = re.sub(
            rf"(?i)\s*[-–—]?\s*(?:\b(?:e|ep|episode)\s*)?0*{ep_num}\s*$",
            "",
            t,
        ).strip()

    t = re.sub(r"(?i)\s*[-–—]?\s*\b(?:e|ep|episode|cap[íi]tulo|cap)\s*$", "", t).strip()
    t = re.sub(r"\s*[-–—:]\s*$", "", t).strip()
    return t or (title or "").strip()


def is_absolute_episode(parsed: dict, filename: str = "") -> bool:
    """True when we have an episode number but no season (orphan/absolute style)."""
    try:
        if parsed.get("season") is not None and int(parsed.get("season")) >= 0:
            return False
    except (TypeError, ValueError):
        if parsed.get("season") is not None:
            return False
    if _SEASON_EP_RE.search(filename or ""):
        return False
    if _MOVIE_KEYWORD_RE.search(filename or "") and not re.search(r"(?i)\b(?:e|ep|episode)\s*0*\d+", filename or ""):
        return False
    if parsed.get("episode") is not None and not isinstance(parsed.get("episode"), list):
        return True
    return extract_absolute_episode(filename, parsed) is not None

def analyze_metadata_failure(filename: str) -> str:
    split_info = parse_split_info(filename or "")
    parse_target = strip_part_suffix(filename) if split_info else (filename or "")

    try:
        parsed = parse_media_name(parse_target)
    except Exception:
        return (
            "The file name / caption could not be parsed. Give it a clear name like "
            "'Movie Name (2021) 1080p'."
        )

    combined = parse_combined_episodes(parse_target)
    excess = parsed.get("excess")
    if not combined and excess and any("combined" in str(item).lower() for item in excess):
        return (
            "The caption says 'combined' but no season number could be read from it "
            "(e.g. name it 'Show S02 Combined')."
        )

    title = parsed.get("title")
    season = parsed.get("season")
    episode = parsed.get("episode")
    quality = parsed.get("quality")

    if not combined and (isinstance(season, list) or isinstance(episode, list)):
        return (
            "The name spans multiple seasons (e.g. S01-S03) that can't be filed as one entry. "
            "Upload one season per file. Combined episode packs within a single season are fine "
            "when named like 'Show S02 E01-E05' or 'Show S02 Combined'."
        )
    if not quality:
        return (
            "No video quality/resolution was found. Add one to the caption "
            "(e.g. 480p, 720p, 1080p or 2160p)."
        )
    if not title:
        return "No title could be detected. Rename or caption the file with a clear title."

    return (
        "Could not match this title on the configured providers. Fix the title/year in the "
        "caption, or add an IMDb link/id (tt...) or a TMDB link/id, then forward it again."
    )
