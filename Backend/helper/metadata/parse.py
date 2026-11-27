"""Filename / caption parsing helpers."""
from __future__ import annotations

import re
from typing import Optional

import PTN
from guessit import guessit as _guessit

from Backend.helper.metadata.common import COMBINED_EPISODE_BASE, COMBINED_SEASON, first
from Backend.helper.split_files import parse_combined_episodes, parse_split_info, strip_part_suffix
from Backend.logger import LOGGER

# CRC32 checksum stripping
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
# With the word "season" in front, even single-letter numerals (i, v, x) are
# unambiguous, so the full roman range is accepted here.
_ROMAN_SEASON_KW_RE = re.compile(
    r"(?i)\bseason\s+(x|viii|vii|vi|v|ix|iv|iii|ii|i)\b(?!\s*x\s*\d)"
)
# Bare numerals (no "season" keyword) are only safe as multi-letter forms.
# Standalone I/V/X are ordinary title letters: "To Be Hero X" is a title, not
# season 10, and "... and I" / "V for Vendetta" are not season markers either.
_ROMAN_SEASON_BARE_RE = re.compile(
    r"(?i)\b(viii|vii|vi|iv|ix|iii|ii)\b(?!\s*x\s*\d)"
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

# Leading group tag with a mistyped bracket: "[Group} Title", "{Group] Title"
_BROKEN_GROUP_RE = re.compile(r"^\s*[\[{]([^\[\]{}()]{1,30})[}\[{]\s*|^\s*\{([^\[\]{}()]{1,30})[\]}]\s*")
# "Show - 01E01": season and episode without the leading "S"
_BARE_SEASON_EP_RE = re.compile(r"(?<![A-Za-z0-9.])(\d{1,2})E(\d{1,3})(?![A-Za-z0-9])")
_HALF_SXXEYY_RE = re.compile(r"(?i)\bS0*(\d{1,2})\s*[-_.]?\s*E0*(\d{1,3})\.5(?![\d.]\d)(?=$|[\s\]\)\-_.])")
_HALF_BARE_RE = re.compile(r"(?<=\s-\s)0*(\d{1,3})\.5(?=\s*(?:$|[-\[\(]|\.[A-Za-z0-9]{2,4}$))")


def _normalize_half_episodes(name: str) -> str:
    """Rewrite ".5" episodes as season 0 episodes.

    "Show - S01E22.5 - Title"         -> "Show - S00E22 - Title"
    "Show - 05.5 - S00E01 - Title"    -> "Show - S00E01 - Title"
    "Show - 11.5 [1080p]"             -> "Show - S00E11 [1080p]"
    """
    name = _HALF_SXXEYY_RE.sub(lambda m: f"S00E{int(m.group(2)):02d}", name)
    half = _HALF_BARE_RE.search(name)
    if half:
        rest = name[:half.start()] + name[half.end():]
        if re.search(r"(?i)\bS\d{1,2}\s*[-_.]?\s*E\d{1,3}\b", rest):
            # explicit SxxEyy present: drop the "05.5 - " prefix
            name = name[:half.start()] + re.sub(r"^\s*-\s*", "", name[half.end():])
        else:
            name = name[:half.start()] + f"S00E{int(half.group(1)):02d}" + name[half.end():]
    return name


def _clean_pre_normalizations(name: str) -> str:
    if not name:
        return ""
    # Strip Telegram hashtags (e.g. #anime #1080p #dual_audio)
    name = re.sub(r"#\w+", " ", name)
    # Strip Telegram channel watermarks and usernames (e.g. @AnimeChannel)
    name = re.sub(r"@[\w_]+", " ", name)
    # Split glued release + resolution tags (e.g. BD1080p -> 1080p, WEBRip720p -> 720p)
    name = re.sub(r"(?i)(?:BD|HDRip|WEBDL|WEBRip|HDTV|DVD|BR|WEB)(\d{3,4}p)", r" \1 ", name)
    # Clean anime dash underscores (e.g. _-_01_ -> - 01 )
    name = re.sub(r"_+[-~]+_+", " - ", name)
    # 1. Doubled extensions (.mkv.mkv, .mp4.mkv)
    while re.search(r"\.(mkv|mp4|avi|webm|ts|m4v)\.(mkv|mp4|avi|webm|ts|m4v)$", name, re.I):
        name = re.sub(r"\.(mkv|mp4|avi|webm|ts|m4v)$", "", name, flags=re.I)
    # 2. Trailing file size tags in parentheses e.g. "(1382MB)"
    name = re.sub(r"\s*\(\s*\d+(?:\.\d+)?\s*[KMGT]?B\s*\)", " ", name, flags=re.I)
    # 3. Space/underscore mangled framerate (e.g. 143_8561fps or 143 8561fps)
    name = re.sub(r"(?i)\b\d+(?:[\s._]\d+)?fps\b", " ", name)
    # 4. Strip leading release group (bracketed or underscore-separated e.g. Raze_Dandadan, [Raze], [SubsPlease])
    # 4a. Group tag with mistyped brackets
    name = _BROKEN_GROUP_RE.sub("", name)
    while re.match(r"^\s*\[[^\]]+\]\s*", name):
        name = re.sub(r"^\s*\[[^\]]+\]\s*", "", name)
    name = _KNOWN_GROUPS_RE.sub(" ", name).strip()
    # 4b. Unwrap leading parenthesized alt titles e.g. (Demon Slayer) Title -> Demon Slayer Title
    while re.match(r"^\s*\((?!(?:19|20)\d{2}\))([^)]+)\)\s*", name):
        name = re.sub(r"^\s*\((?!(?:19|20)\d{2}\))([^)]+)\)\s*", r"\1 ", name)
    # 5. Convert underscores to spaces
    name = name.replace("_", " ")
    # 5a. "01E01" -> "S01E01"
    name = _BARE_SEASON_EP_RE.sub(lambda m: f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}", name)
    # 5b. Half episodes ("S01E22.5", "- 05.5 -") are specials
    name = _normalize_half_episodes(name)
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
    if not _EXPLICIT_SXXEXX_RE.search(name):
        name = re.sub(
            r"(?i)(?:\s+-)?\s+(?:re-?cap|re|summary)\s*(?=\.[a-z0-9]{2,4}$|$|[\[\(])",
            " S00E01 ",
            name,
        )
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

# elierpg port (safety-hardened)
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

# Explicit season/episode anchors
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
# A release's own special counter ("SP1", "OVA 2") as opposed to a provider number ("S00E01")
_SPECIAL_COUNTER_RE = re.compile(r"(?i)\b(?:ova|oad|specials?|sp)\s*[-_.]?\s*0*\d{1,3}\b")
_SEASON_ZERO_TAG_RE = re.compile(r"(?i)\bs0*0e\d")
# Creditless openings and endings that Blu-ray batches ship next to the episodes
_BONUS_EXTRA_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:NC(?:OP|ED)|(?:creditless|textless)[\s._-]+(?:OP|ED|opening|ending)|clean[\s._-]+(?:OP|ED))"
    r"(?:[\s._-]?\d{1,2}[a-z]?)?(?:v\d)?(?![a-z])"
)
# A file that is nothing but an opening or ending ("ED 01.mkv", "OP 2.mkv")
_BARE_OP_ED_RE = re.compile(r"(?i)^(?:OP|ED|opening|ending)(?:[\s._-]*\d{1,2}[a-z]?)?(?:v\d)?$")
# "Show OVA - Episode Title": an OVA named by its title, without a number
_TITLED_SPECIAL_RE = re.compile(r"(?i)^(?P<show>.+?)[\s._]+(?:-\s*)?(?:ova|oad)\s*[-:]\s*(?P<title>\S.*)$")
# "Show Movie OVA": an extra of a film, with nothing that says which one
_UNNAMED_MOVIE_EXTRA_RE = re.compile(r"(?i)\b(?:the\s+)?movie[\s._-]+(?:ova|oad|specials?)$")
_RELEASE_TAG_RE = re.compile(
    r"(?i)\[[^\]]*\]|\((?:[^()]*\b(?:\d{3,4}p|\d{3,4}x\d{3,4}|BD|BDRip|BluRay|WEB|WEB-DL|WEBRip|DVD|"
    r"x26[45]|H\.?26[45]|HEVC|AVC|AV1|AAC|FLAC|Opus|Dual[\s-]?Audio|Multi[\s-]?Subs?|10[\s-]?bit)\b[^()]*)\)"
)
# "(SNS OVA)", "[OAD]", "(Special)" after an episode number
_BUNDLED_SPECIAL_RE = re.compile(r"(?i)[\(\[]([^\(\)\[\]]*\b(?:OVA|OAD|specials?)\b[^\(\)\[\]]*)[\)\]]")
_BUNDLED_SPECIAL_WORD_RE = re.compile(r"(?i)\b(?:OVA|OAD|specials?)\b|\b\d+\b")
# Movie keyword indicators
_MOVIE_KEYWORD_RE = re.compile(r"(?i)\b(?:the\s+movie|movie\s*\d*|film|gekijouban|gekijou-ban)\b")
# Latino Spanish wording: "temporada N" / "temp N" (season) and
# "capitulo N" / "capítulo N" / "cap N" (episode). Tolerates "_" / "." as the
# separator (uploaders write "Soy_Tu_Duena_Capitulo_5.mkv").
_EXPLICIT_CAPITULO_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:temporada|temp|cap[íi]tulo|cap)[._\s-]*0*\d{1,3}"
)
_EXPLICIT_SEASON_WORD_RE = re.compile(r"(?i)\b(?:season|series)\s*0*\d{1,2}\b")


# Name starting with the episode marker: "S01E01 - Title", "E05 Title"
_LEADING_EPISODE_RE = re.compile(
    r"(?i)^\s*(?:S\d{1,2}\s*[-_.]?\s*E\d{1,4}|E(?:P|PISODE)?\s*\d{1,4}|\d{1,2}x\d{1,3})(?![A-Za-z0-9])"
)


# Episode title in the name: "Show - S01E05 - Title (1080p)" -> "Title".
# Used to identify specials and to look up the show for untitled uploads.
_EPISODE_MARKER_FOR_TITLE_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:S\d{1,2}\s*[-_. ]?\s*E\d{1,4}|\d{1,2}x\d{2,3})(?:\.5)?(?:v\d)?(?![a-z0-9])"
)
_EPISODE_TITLE_STOP_RE = re.compile(
    r"(?i)[\[\(\{]|(?<![a-z0-9])(?:\d{3,4}[pi]|\d{3,4}x\d{3,4}|4k|uhd|bd(?:rip|remux|mv)?|blu-?ray(?:-\d{3,4}p)?|"
    r"web(?:-?dl|-?rip)?|hdtv|dvd(?:rip)?|remux|repack\d?|proper|x26[45]|h\.?26[45]|hevc|avc|av1|"
    r"10-?bit|hi10p?|flac|aac\d?|opus\d?|ddp?\d|e-?ac-?3|ac3|truehd|dts|dual[-. ]?audio|"
    r"multi[-. ]?(?:audio|subs?))(?![a-z0-9])"
)
_GENERIC_EPISODE_TITLE_RE = re.compile(
    r"(?i)^(?:(?:episode|ep|ova|oad|ona|special|sp|part|pt|nced|ncop|pv|recap|picture drama|extra|bonus|"
    r"pilot|finale|the|and|a)\b[\s\d.,&-]*)*$"
)
_EPISODE_TITLE_LEAD_NOISE_RE = re.compile(
r"(?i)^(?:\d{1,4}(?:\.5)?\s+-\s+|(?:episode|ep)\s*\d{1,4}\b[\s-]*|(?:ova|oad|ona)\b\s*\d{0,3}\s*(?:-\s+)?)+"
)


def extract_episode_title(name: str) -> Optional[str]:
    """Return the episode title that follows the SxxEyy marker, or None.

    "S01E01 - The Green Seat.mkv"            -> "The Green Seat"
    "Show.S00E02.Episode.24.OVA.Title.1080p" -> "Title"
    "Show.S00E08.1080p.BluRay"               -> None
    Generic text ("Episode 15", "OVA 2", "Part 1") does not count as a title.
    """
    if not name:
        return None
    text = _strip_crc32(str(name).splitlines()[0])
    # extension, also when typed as "... Title mkv"
    text = re.sub(r"[.\s](?:mkv|mp4|avi|webm|m4v|mov|ts|wmv|flv)$", "", text.strip(), flags=re.I)
    markers = list(_EPISODE_MARKER_FOR_TITLE_RE.finditer(text))
    if not markers:
        return None
    rest = text[markers[-1].end():]
    stop = _EPISODE_TITLE_STOP_RE.search(rest)
    if stop:
        rest = rest[:stop.start()]
    if " " not in rest.strip(" ._-"):
        # dot / underscore separated release name
        rest = re.sub(r"(?<!\d)[._]|[._](?!\d)", " ", rest)
    rest = rest.replace("_", " ")
    rest = re.sub(r"\s+", " ", rest).strip(" -–—.,:")
    rest = _EPISODE_TITLE_LEAD_NOISE_RE.sub("", rest).strip(" -–—.,:")
    if len(re.sub(r"[^A-Za-z]", "", rest)) < 3 or _GENERIC_EPISODE_TITLE_RE.match(rest):
        return None
    return rest


def is_bonus_extra(name: str) -> bool:
    """True for a creditless opening or ending ("Show - NCOP1 [BD 1080p]").

    These are extras of a disc release, not episodes, and no provider lists them.
    """
    if not name:
        return False
    first_line = str(name).splitlines()[0]
    if _BONUS_EXTRA_RE.search(first_line):
        return True
    return bool(_BARE_OP_ED_RE.fullmatch(_bare_name(first_line)))


def _bare_name(name: str) -> str:
    """The name without extension and release tags ("[Group] Show - 01 [1080p].mkv" -> "Show - 01")."""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", str(name or "").strip())
    base = _RELEASE_TAG_RE.sub(" ", base)
    return re.sub(r"\s+", " ", base).strip(" -_.")


def titled_special(name: str) -> Optional[tuple]:
    """("Show", "Episode Title") for "Show OVA - Episode Title.mkv", else None.

    A number right after the marker ("OVA - 02") is a counter, not a title.
    """
    if not name:
        return None
    match = _TITLED_SPECIAL_RE.match(_bare_name(str(name).splitlines()[0]))
    if not match:
        return None
    show, title = match.group("show").strip(" -_."), match.group("title").strip(" -_.")
    if not show or re.match(r"(?i)^(?:ep(?:isode)?\.?\s*)?\d", title) or not re.search(r"[A-Za-z]{3}", title):
        return None
    return show, title


def is_unnamed_movie_extra(name: str) -> bool:
    """True for "Show Movie OVA.mkv": an extra of some film of the show, not the film."""
    return bool(name and _UNNAMED_MOVIE_EXTRA_RE.search(_bare_name(str(name).splitlines()[0])))


def special_title_score(file_title: str, provider_title: str, show: Optional[str] = None) -> float:
    """How well a special's title in a file name fits a provider's episode name (0..1).

    The provider name may carry the show name in front and either side may
    add a few words ("All Might Rising The Animation" / "All Might: Rising").
    "Part 1" never fits "Part 2".
    """
    def words(text):
        text = re.sub(r"[`'\u2019\"]", "", str(text or "").lower())
        return [w for w in re.sub(r"[^a-z0-9]+", " ", text).split() if w not in ("the", "a", "an", "of")]

    provider = str(provider_title or "")
    if show:
        show_words, provider_words = words(show), words(provider)
        if show_words and provider_words[:len(show_words)] == show_words and len(provider_words) > len(show_words):
            provider = " ".join(provider_words[len(show_words):])
    best = episode_titles_match(file_title, provider)
    a, b = words(file_title), words(provider)
    if not a or not b:
        return best
    if [w for w in a if w.isdigit()] != [w for w in b if w.isdigit()]:
        return min(best, 0.5)
    small, large = (set(a), set(b)) if len(set(a)) <= len(set(b)) else (set(b), set(a))
    # One title holds every word of the other: the closer in length, the better
    if len(small) >= 2 and small <= large:
        best = max(best, 0.8 + 0.2 * len(small) / len(large))
    return best


def is_special_counter(name: str) -> bool:
    """True when a special is numbered by the release itself.

    "Show.SP1.1080p"       -> True   (first special of this release)
    "Show - OVA 2 [BD]"    -> True
    "Show.S00E01.1080p"    -> False  (season 0 number of the provider)
    """
    if not name:
        return False
    text = str(name).splitlines()[0]
    return bool(_SPECIAL_COUNTER_RE.search(text)) and not _SEASON_ZERO_TAG_RE.search(text)


def bundled_special_label(name: str) -> Optional[str]:
    """Label of a special that a release numbers as the next regular episode.

    "Show - 13 (SNS OVA) [BD 1080p]" -> "SNS"
    "Show - 25 (OAD)"                -> ""
    "Show - 13 [BD 1080p]"           -> None
    """
    if not name:
        return None
    m = _BUNDLED_SPECIAL_RE.search(str(name).splitlines()[0])
    if not m:
        return None
    label = _BUNDLED_SPECIAL_WORD_RE.sub(" ", m.group(1))
    return re.sub(r"\s+", " ", label).strip(" -_.,:")


def half_episode_position(name: str) -> Optional[dict]:
    """Return the episode an "N.5" release follows, or None.

    "Show - S01E22.5 - Title"       -> {"season": 1, "episode": 22, "numbered": False}
    "Show - 05.5 - S00E01 - Title"  -> {"season": None, "episode": 5, "numbered": True}
    "numbered" is True when the name also carries the special's own S00Exx.
    """
    if not name:
        return None
    text = _strip_crc32(str(name))
    m = _HALF_SXXEYY_RE.search(text)
    if m:
        season = int(m.group(1))
        # 22 is the position, not the special's own number
        return {"season": season if season > 0 else None, "episode": int(m.group(2)), "numbered": False}
    m = _HALF_BARE_RE.search(text)
    if m:
        rest = text[:m.start()] + text[m.end():]
        numbered = bool(re.search(r"(?i)\bS0*0\s*[-_.]?\s*E\d{1,3}\b", rest))
        return {"season": None, "episode": int(m.group(1)), "numbered": numbered}
    return None


def episode_titles_match(a: str, b: str) -> float:
    """Similarity (0..1) of two episode titles, ignoring case, punctuation and articles."""
    from rapidfuzz import fuzz

    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}

    def norm(t):
        t = re.sub(r"[`’'\"]", "", str(t or "").lower())
        t = re.sub(r"[^a-z0-9]+", " ", t)
        t = re.sub(r"\b(?:the|a|an)\b", " ", t)
        # "Title Part 1" == "Title I"
        t = re.sub(r"\b(?:part|pt)\s+(?=\d|[ivx]+\b)", "", t)
        words = t.split()
        if len(words) > 1 and words[-1] in roman:
            words[-1] = roman[words[-1]]
        return " ".join(words)

    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return 1.0
    score = max(fuzz.ratio(na, nb), fuzz.token_sort_ratio(na, nb)) / 100.0
    # Different numbers mean different episodes ("Part 1" / "Part 2").
    if re.findall(r"\d+", na) != re.findall(r"\d+", nb):
        score = min(score, 0.5)
    return score


def _restore_title_decimals(name: str, title):
    """Restore a decimal point the tokenizer split ("2 5 Dimensional
    Seduction" -> "2.5 Dimensional Seduction")."""
    if not title or not isinstance(title, str):
        return title

    def _fix(m):
        a, b = m.group(1), m.group(2)
        return f"{a}.{b}" if re.search(rf"(?<![\d.]){re.escape(a)}\.{re.escape(b)}(?!\d)", name or "") else m.group(0)

    return re.sub(r"(?<![\d.])(\d) (\d)(?!\d)", _fix, title)


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
    # 900p (1600x900, 1600x670)
    if w >= 1560 or 880 <= h < 1000:
        return "900p"
    # 720p HD (including widescreen 1280x534)
    if (w >= 1200 and h >= 500) or (700 <= h < 1000):
        return "720p"
    # 576p PAL SD
    if h >= 540 or (w >= 720 and h >= 540):
        return "576p"
    # 480p NTSC SD (640x480, 720x480, 854x480)
    if h >= 440 or (w >= 640 and h >= 400):
        return "480p"
    # 360p (including cinemascope 640x272 / 640x267, which is 360p-class by width)
    if h >= 320 or (w >= 480 and h >= 300) or w >= 600:
        return "360p"
    # 240p (426x240, 426x180)
    if h >= 200 or w >= 400:
        return "240p"
    if h > 0:
        return f"{h}p"
    return None


# Highest tier taken from Telegram's width/height attributes alone. Upload
# clients often report doubled sizes or the thumbnail's, and a real UHD
# release states it in the name.
MAX_DIMENSION_HINT = "1080p"
_HINT_ABOVE_MAX = {"4K", "1440p"}
# Above this average bitrate (Mbit/s) a tiny frame size is the thumbnail's.
_THUMBNAIL_DIM_MAX_MBPS = 1.5


def quality_hint_from_media(width: int = 0, height: int = 0, file_size: int = 0, duration: int = 0) -> Optional[str]:
    """Resolution hint from Telegram media attributes, for names without one.

    Returns None when the attributes cannot be trusted.
    """
    tier = resolution_from_dimensions(width or 0, height or 0)
    if not tier:
        return None
    if tier in _HINT_ABOVE_MAX:
        return MAX_DIMENSION_HINT
    if tier in ("360p", "240p") or re.fullmatch(r"\d{1,3}p", tier) and int(tier[:-1]) < 400:
        try:
            mbps = (float(file_size) * 8 / float(duration)) / 1_000_000 if file_size and duration else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            mbps = 0.0
        if mbps > _THUMBNAIL_DIM_MAX_MBPS:
            return None
    return tier


_NAME_RESOLUTION_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:(?:240|360|480|540|576|720|900|1080|1440|2160|4320)[pi]|\d{3,4}\s?x\s?\d{3,4}|[48]k|uhd|fhd|qhd)(?![a-z0-9])"
)


def _normalize_quality(name: str, quality) -> Optional[str]:
    """Keep a parsed resolution only if the name states one.

    The parsers take a bare number for a resolution ("S16E18 - 360 - Title").
    Interlaced labels ("480i", "1080i") map to the progressive tier.
    """
    if not quality:
        return None
    quality = str(quality).strip()
    if not _NAME_RESOLUTION_RE.search(name or ""):
        return None
    m = re.fullmatch(r"(?i)(\d{3,4})i", quality)
    if m:
        quality = f"{m.group(1)}p"
    return quality


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
        "quality": _normalize_quality(name, ptn.get("resolution")),
        "excess": ptn.get("excess"),
    }

    parsed["title"] = _restore_title_decimals(name, parsed.get("title"))

    # Name starts with the episode marker: there is no show title, only an
    # episode title. The caller needs an explicit id or a lookup.
    title_missing = bool(_LEADING_EPISODE_RE.match(name))
    if title_missing:
        parsed["title"] = None

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
        else:
            # Keyword-anchored match wins; otherwise only multi-letter bare
            # numerals count, so a title ending in "X" is not read as season 10.
            roman_m = _ROMAN_SEASON_KW_RE.search(name) or _ROMAN_SEASON_BARE_RE.search(name)
            if roman_m:
                val = roman_m.group(1).lower()
                if val in ROMAN_NUMS:
                    parsed["season"] = ROMAN_NUMS[val]

    if _guessit:
        try:
            g = _guessit(name)
            if not title_missing:
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

            parsed["quality"] = parsed["quality"] or _normalize_quality(name, first(g.get("screen_size")))
        except Exception as e:
            LOGGER.warning(f"GuessIt parsing failed for {name}: {e}")

    if not parsed["quality"] and (m := _RES_GLUED_RE.search(name)):
        parsed["quality"] = m.group(0).lower()

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

    if title_missing:
        parsed["title"] = None
        parsed["title_missing"] = True
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
_RES_GLUED_RE = re.compile(r"(?i)(?<!\d)(?:240|360|480|576|720|1080|1440|2160|4320)p(?!\d)")
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
# Numbered title units where leading number is part of show name (e.g. "5 Centimeters per Second", "100 Meters", "91 Days", "3-gatsu no Lion")
_TITLE_UNITS_RE = re.compile(
    r"(?i)\b\d+\s*(?:centimeters?|cm|meters?|meter|m|days?|toubun|nin|hours?|minutes?|percent|%|leaf|clover|bullets?|princes?|kingdoms?|wars?|deadly\s+sins?|tails?|witches|kanojo|gatsu|round|seconds?)\b"
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

    clean_fn = re.sub(r"#\w+", " ", filename or "")
    clean_fn = re.sub(r"@[\w_]+", " ", clean_fn)

    if _SEASON_EP_RE.search(clean_fn):
        return None

    # Movies (e.g. "Bleach the Movie 1 Memories of Nobody", "Chainsaw Man - Movie")
    # should NOT extract "1" as an absolute TV episode
    if _MOVIE_KEYWORD_RE.search(clean_fn) and not re.search(r"(?i)\b(?:e|ep|episode)\s*0*\d+", clean_fn):
        return None

    ep = parsed.get("episode")
    if isinstance(ep, list):
        return None
    if ep is not None:
        try:
            return int(ep)
        except (TypeError, ValueError):
            pass

    name = clean_fn
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

    # 4. Trailing numbers or first number left after stripping title.
    # A leading number followed by words is part of the title ("100 Meters").
    candidates = []
    for num_m in re.finditer(r"(?:^|\s)(\d{1,4})(?=\s|$)", cleaned):
        try:
            n = int(num_m.group(1))
        except ValueError:
            continue
        if n < 1:
            continue
        if num_m.start() == 0 and re.search(r"[A-Za-z]", cleaned[num_m.end():]):
            continue
        candidates.append(n)
    if not candidates:
        return None

    # Prefer 3-4 digit token (typical anime absolute e.g. 110, 190, 366); else last remaining
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
    # A single-letter numeral counts only after "season" ("To Be Hero X").
    r"season\s+(?:viii|vii|vi|iv|ix|iii|ii|i|v|x)|"
    r"(?:viii|vii|vi|iv|ix|iii|ii)"
    r")\s*$"
)


def tidy_title_separators(t: str) -> str:
    """Turn separator dashes into spaces but keep hyphenated words.

    "Fullmetal Alchemist - Brotherhood" -> "Fullmetal Alchemist Brotherhood"
    "Parasyte -the maxim-"              -> "Parasyte the maxim"
    "Kaguya-sama wa Kokurasetai"        -> unchanged
    """
    if not t:
        return t
    t = re.sub(r"\s+[-–—]+\s+", " ", t)          # " - " separators
    t = re.sub(r"(?<![A-Za-z0-9])[-–—]+", " ", t)   # dangling leading dash: "-the"
    t = re.sub(r"[-–—]+(?![A-Za-z0-9])", " ", t)    # dangling trailing dash: "maxim-"
    # Unbalanced bracket left over at either end
    if not re.search(r"[\[\(\{].*[\]\)\}]", t):
        t = re.sub(r"^[\[\(\{]+\s*|\s*[\]\)\}]+$", "", t)
    return re.sub(r"\s+", " ", t).strip()


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
    t = re.sub(r"#\w+", " ", t)
    t = re.sub(r"@[\w_]+", " ", t)
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
        r"(?i)\s*[-–—]?\s*\b(?:s0*0e\s*\d{1,3}|ova\s*\d*|oad\s*\d*|specials?\s*\d*|sp\s*0*\d+|nced\s*\d*|ncop\s*\d*)\b\s*",
        " ",
        t,
    )
    # Strip standalone movie markers like " - Movie" or " - The Movie"
    t = re.sub(r"(?i)\s*[-–—]\s*(?:the\s+movie|movie|film)\b", " ", t)
    # Dots and underscores are separators, decimal points are not ("Ver1.1a").
    t = re.sub(r"(?:[\s_]|\.(?!\d)|(?<!\d)\.)+", " ", t).strip()
    # Trailing "Movie" tag on long titles. Short titles ("Bee Movie") and
    # "... The Movie" keep it.
    if len(t.split()) >= 5:
        t = re.sub(r"(?i)(?<!\bthe)\s+movie$", "", t).strip()
    # Edition tags ("30th Anniversary")
    t = re.sub(r"(?i)\s*[-–—]?\s*\b\d{1,3}(?:st|nd|rd|th)\s+anniversary(?:\s+edition)?\b", " ", t).strip()

    # Strip season / part / cour / roman numeral suffixes from show titles
    t = _SEASON_SUFFIX_RE.sub("", t).strip()

    if absolute_ep is not None:
        ep_num = int(absolute_ep)
        # Pattern 1: Title followed by episode in the middle: 'Bleach 001 The Day I Became A Shinigami' or 'Bleach - 001 - Title'
        mid_m = re.match(
            # \b keeps the optional E/EP from taking the show's last letter.
            rf"(?i)^(?P<show>.+?)\s*[-–—]?\s*(?:\b(?:e|ep|episode))?\s*(?<![0-9])0*{ep_num}\s*(?:[-–—:]\s*|\s+)(?P<ep_title>.+)$",
            t,
        )
        if mid_m:
            show = mid_m.group("show").strip()
            # Ensure show name is reasonable length (not empty) and not an anchor
            if len(show) >= 2:
                t = show

        # Pattern 2: Trailing episode: 'Bleach - 001' or 'One Piece 1172'
        t = re.sub(
            # (?<![0-9]) so episode 6 does not match the 6 in "Title 17-26".
            rf"(?i)\s*[-–—]?\s*(?:\b(?:e|ep|episode)\s*)?(?<![0-9])0*{ep_num}\s*$",
            "",
            t,
        ).strip()

    t = re.sub(r"(?i)\s*[-–—]?\s*\b(?:e|ep|episode|cap[íi]tulo|cap)\s*$", "", t).strip()
    t = re.sub(r"\s*[-–—:]\s*$", "", t).strip()
    # Trailing "SP" tag
    t = re.sub(r"\s+[-–—]?\s*SP$", "", t).strip() if len(t.split()) >= 3 else t
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

    if parsed.get("title_missing"):
        return (
            "The caption starts with the episode number and has no show name "
            "(e.g. 'S01E01 - Episode Title'). Put the show name first, like "
            "'Show Name S01E01 - Episode Title', or add an IMDb/TMDB link."
        )

    combined = parse_combined_episodes(parse_target)
    excess = parsed.get("excess")
    if not combined and parsed.get("episode") is None and excess and any("combined" in str(item).lower() for item in excess):
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
