"""Caption and file name clean-up.

Standard library only, so the bot and the offline tools can share it.
Cleaning removes decoration (emoji, watermarks, channel tags) and leaves
everything the parser needs: title words, episode markers, hyphens and the
release group.
"""
from __future__ import annotations

import re
import unicodedata

from Backend.helper.split_files import strip_part_suffix

_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F300-\U0001F5FF"   # symbols & pictographs
    "\U0001F680-\U0001F6FF"   # transport & map
    "\U0001F700-\U0001FAFF"   # extended symbols (geometric, supplemental, etc.)
    "\U00002702-\U000027B0"   # dingbats
    "\U000024C2-\U0001F251"   # enclosed chars
    "\u2600-\u26FF"           # misc symbols (☆, ★, ☀, ©, ®, ™)
    "\u2700-\u27BF"           # dingbats block
    "\uFE00-\uFE0F"           # variation selectors
    "\U0001F1E0-\U0001F1FF"   # regional indicator / flag sequences
    "]+",
    re.UNICODE,
)

# Decorative symbols. Single hyphens are punctuation ("Show - 01", "x265-GROUP")
# and stay; only rules such as "-----" or "=====" are removed.
_DECORATION_PATTERN = re.compile(
    r"[•·‣⁃◦⦿⦾❖✦✧✪✫✬✭✮★☆♦♣♠♥►◄\u2192\u2190\u2191\u2193»«|¦‖⟨⟩⌨⌚⌛⏰⏱⏲⏳✔✖✗✘✅❌❎⚠⛔🚫]+"
    r"|-{3,}|={3,}|~{3,}",
    re.UNICODE,
)

# Channel tags and links: @Channel, [@Channel], _ChannelName_, t.me/xxx.
# The underscore form has to be a token of its own, otherwise names like
# "My_Neighbors_the_Yamadas_(1999)" would lose their title.
_CHANNEL_TAG_PATTERN = re.compile(
    r"(?:join\s*:?\s*)?@[\w_]+"
    r"|(?:(?<=\s)|^)_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_(?=\s|$|\.[A-Za-z0-9]{2,4}$)"
    r"|[\[({【][\s@]*[^\])}】]*@[\w_.]+[\])}】]"
    r"|(?:https?:\/\/)?(?:t\.me|telegram\.me|telegram\.dog)[/_][A-Za-z0-9_+]+",
    re.IGNORECASE,
)

_DOMAIN_TLDS = (
    "com|net|org|in|vip|me|to|is|cc|mov|tv|site|xyz|online|club|top|tech|info|co|biz|live|pro|"
    "click|download|baby|bar|wtf|fun|space|monster|pw|icu|best|buzz|uno|art|run|mx|lol|kim|"
    "skin|pics|ink|nl|ws|fyi|day|quest|cyou|rest|cam|mom|boo"
)
# Hosts with an explicit www. prefix.
_WWW_DOMAIN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*?"
    r"\.(?!(?:mkv|mp4|avi|m4v|webm|wmv|flv|mpg|mpeg|m2ts|zip|rar|7z)\b)[A-Za-z]{2,10}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# Bare hosts, matched only as a whole token. Dotted release names contain
# plenty of "word.tld" pairs ("S01E31.In.Broad.Daylight").
_DOMAIN_TAG_PATTERN = re.compile(
    rf"(?<![\w.\-])(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)?)\.(?P<tld>{_DOMAIN_TLDS})(?![\w.\-])",
    re.IGNORECASE,
)
_MEDIA_EXT_TLDS = {"mov", "ts"}
_NOT_A_HOST_RE = re.compile(r"(?i)^(?:s\d{1,2}e\d{1,4}|e\d{1,4}|ep\d{1,4}|\d+|v\d+|vol|no|mr|mrs|dr|st|vs|ft|feat|pt)$")

_CODEC_WORD_PATTERN = re.compile(
    r"(?<=\W)(org|AMZN|DDP|DD|NF|AAC|TVDL|~|\b\w+kbps\b)(?=\W)",
    re.IGNORECASE,
)
# Audio channel layouts, stripped only after the first technical marker so
# that titles containing "2.0" or "5.1" are kept ("Evangelion 2.0 ...").
_CHANNEL_LAYOUT_PATTERN = re.compile(r"(?<=\W)(5\.1|2\.1|2\.0|7\.0|7\.1|5\.0)(?=\W)")
_TECH_ANCHOR_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:\d{3,4}[pi]|\d{3,4}x\d{3,4}|s\d{1,2}[ ._-]?e\d{1,4}|(?:19|20)\d{2}"
    r"|blu-?ray|b[dr]rip|bd|web[-. ]?dl|web-?rip|web|hdtv|dvd(?:rip)?|remux"
    r"|x26[45]|h\.?26[45]|hevc|avc|av1|flac|opus|aac|e?ac-?3|ddp?\+?|dts|truehd)(?![a-z])"
)

# Look-alike characters that file name sanitisers substitute for ":", "?",
# "/" and quotes. Mapping them back keeps titles such as "ID:INVADED" intact.
_TYPOGRAPHY = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u02BC": "'", "\u00B4": "'", "\uFF07": "'",
    "\u201C": '"', "\u201D": '"', "\uFF02": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-", "\uFF0D": "-",
    "\u13D3": ":", "\uA789": ":", "\u2236": ":", "\uFF1A": ":", "\uFE55": ":", "\u02D0": ":",
    "\uFF1F": "?", "\uFF01": "!", "\uFF06": "&", "\uFF0B": "+",
    "\u29F8": "/", "\uFF0F": "/", "\u2044": "/",
    "\u3000": " ", "\u00A0": " ", "\u2026": "...",
})


def remove_urls(text):
    if not text:
        return ""

    # "www." starts a URL only at a token boundary; inside a dotted name it
    # is left to the watermark patterns.
    url_pattern = r'(?:\b(?:https?|ftp):\/\/|(?<![\w.])www\.(?=[^\s]*/))[^\s/$.?#].[^\s]*'
    text_without_urls = re.sub(url_pattern, '', text)
    cleaned_text = re.sub(r'\s+', ' ', text_without_urls).strip()

    return cleaned_text


def _strip_domain(match: re.Match) -> str:
    host = match.group("host")
    tld = match.group("tld").lower()
    last_label = host.rsplit(".", 1)[-1]
    if _NOT_A_HOST_RE.match(last_label) or _NOT_A_HOST_RE.match(host):
        return match.group(0)
    # A trailing ".mov" / ".ts" is a file extension.
    if tld in _MEDIA_EXT_TLDS and match.end() == len(match.string.rstrip()):
        return match.group(0)
    return " "


def fold_to_ascii(text: str) -> str:
    """Map look-alike punctuation and accented letters to ASCII ("Pokémon" -> "Pokemon")."""
    if not text:
        return ""
    text = text.translate(_TYPOGRAPHY)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_filename(filename: str) -> str:
    if not filename:
        return "unknown_file"

    # Has to run before the emoji pattern, whose ranges cover these characters.
    filename = fold_to_ascii(filename)

    # URLs and hashtags. Whole URLs first, so an IMDb link in the caption does
    # not leave fragments behind; callers read ids from the raw caption.
    filename = remove_urls(filename)
    filename = _WWW_DOMAIN_PATTERN.sub(" ", filename)
    filename = re.sub(r"#\w+", " ", filename)

    # Emoji
    filename = _EMOJI_PATTERN.sub(" ", filename)

    # Decorative symbols
    filename = _DECORATION_PATTERN.sub(" ", filename)

    # Anything still outside printable ASCII
    filename = re.sub(r"[^\x20-\x7E]", " ", filename)

    # Channel tags and links
    filename = _CHANNEL_TAG_PATTERN.sub(" ", filename)

    # Domain watermarks (site.com, [yts.mx])
    filename = _DOMAIN_TAG_PATTERN.sub(_strip_domain, filename)

    # Codec and source tags
    filename = _CODEC_WORD_PATTERN.sub(" ", filename)
    anchor = _TECH_ANCHOR_RE.search(filename)
    if anchor:
        head, tail = filename[:anchor.start()], filename[anchor.start():]
        filename = head + _CHANNEL_LAYOUT_PATTERN.sub(" ", tail)

    # Leftovers: empty brackets, doubled dots, a leading separator
    filename = re.sub(r"[\[({]\s*[\])}]", " ", filename)
    filename = re.sub(r"\.\s+\.", ".", filename)
    filename = re.sub(r"^[\s\-_]+(?=\S)", "", filename)

    # Whitespace
    filename = re.sub(r"\s+", " ", filename).strip().replace(" .", ".")

    return filename if filename else "unknown_file"


_MISSING_EXT_DOT_RE = re.compile(r"(?i)[ _](mkv|mp4)$")


def finalize_media_name(title: str, is_split: bool = False) -> str:
    """Display name stored for a media entry, always ending in a video extension."""
    title = _DECORATION_PATTERN.sub(" ", _EMOJI_PATTERN.sub(" ", remove_urls(title)))
    title = re.sub(r"\s+", " ", title).strip().replace(" .", ".")

    ext_match = re.search(
        r"\.(?:mkv|mp4)(?:\.\d{2,3})?(?=$|[^A-Za-z0-9])",
        title,
        flags=re.IGNORECASE,
    )
    if ext_match:
        title = title[: ext_match.end()]
    else:
        # "Show - 01 1080p mkv": the dot before the extension is missing
        title = _MISSING_EXT_DOT_RE.sub(lambda m: f".{m.group(1).lower()}", title)

    if is_split:
        title = strip_part_suffix(title)
    if not title.endswith((".mkv", ".mp4")):
        title += ".mkv"
    return title
