"""Find episodes missing from the indexed shows and look for them in the owner's Telegram.

Three steps, each usable on its own:

  1. find_gaps:    compare every indexed show with its public episode list
  2. search_gaps:  search the user session's chats (private, groups, channels)
                   for files that would be indexed into a missing slot
  3. fill_gaps:    copy the best file of every found slot into the channel
                   that already holds the show, where it is indexed as usual
     fill_gaps_sorted: the same, and the show's later posts are posted again
                   after the new file so the channel stays in episode order

A file counts as a match only when the normal metadata pipeline resolves its
name to the same show and to a missing season and episode.

Sorting never deletes first. The later posts are copied, the copies are checked,
the library is pointed at the copies, and only then are the old posts removed.
If any step fails the copies are removed again and the old posts stay.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from Backend.logger import LOGGER

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv", ".webm", ".flv")
# Search results read per query and message type
SEARCH_LIMIT = 600
# Seconds between two search queries and between two copies
SEARCH_PAUSE = 1.0
COPY_PAUSE = 2.5
# Share of a show name's words a candidate title must contain
MIN_TITLE_OVERLAP = 0.6
MAX_QUERIES_PER_SHOW = 4
# A show is not sorted when more posts than this would have to be posted again
MAX_REPOSTS_PER_SHOW = 200
# Seconds a reposted file stays known to the receiver after it was registered
REPOST_IGNORE_SECONDS = 3600

_WORD_RE = re.compile(r"[a-z0-9]+")
_QUERY_STRIP_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_GROUP_LEAD_RE = re.compile(r"^\s*\[([^\]]{2,30})\]")
_GROUP_TAIL_RE = re.compile(r"(?:-([A-Za-z0-9]{2,20})|\[([^\]]{2,30})\])\s*(?:\.\w{2,4})?\s*$")


@dataclass
class IndexedFile:
    """One indexed stream of a show and the channel post it points to."""
    chat_id: int
    msg_id: int
    stream_id: str
    season: int
    episode: int
    name: str
    # False for split files and archives, which span several posts
    single_post: bool = True


@dataclass
class Show:
    title: str
    ids: Dict[str, object]
    names: List[str]
    channel: Optional[int]
    release_tags: Set[str]
    have: Dict[int, Set[int]]
    missing: Dict[int, List[int]] = field(default_factory=dict)
    files: List[IndexedFile] = field(default_factory=list)
    doc_id: object = None
    db_key: Optional[str] = None
    # False when no public episode list was found: only holes below the
    # highest indexed episode are known
    complete_list: bool = True

    @property
    def missing_count(self) -> int:
        return sum(len(v) for v in self.missing.values())


@dataclass
class Candidate:
    chat_id: int
    chat_title: str
    msg_id: int
    name: str
    size: int
    season: int = 0
    episode: int = 0
    same_release: bool = False
    score: int = 0


def _words(text: str) -> List[str]:
    return _WORD_RE.findall((text or "").lower().replace("'", "").replace("\u2019", ""))


def title_overlap(candidate_title: str, show_name: str) -> float:
    """Share of the show name's words found in the candidate title."""
    wanted = set(_words(show_name))
    if not wanted:
        return 0.0
    return len(wanted & set(_words(candidate_title))) / len(wanted)


def release_tag(name: str) -> str:
    """Release group of a file name: leading "[Group]" or trailing "-Group" / "[Group]"."""
    name = (name or "").split("\n")[0]
    lead = _GROUP_LEAD_RE.search(name)
    if lead:
        return lead.group(1).strip().lower()
    tail = _GROUP_TAIL_RE.search(name)
    if tail:
        return (tail.group(1) or tail.group(2) or "").strip().lower()
    return ""


def compress_ranges(numbers: Iterable[int]) -> str:
    """[1, 2, 3, 5] -> "1-3, 5"."""
    ordered = sorted(set(int(n) for n in numbers))
    parts, i = [], 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return ", ".join(parts)


def search_queries(names: Iterable[str]) -> List[str]:
    """Search strings for a show: every name without punctuation, plus a short form.

    Telegram only returns messages that contain every word of the query and
    matches words by their beginning. A word with an apostrophe is cut there
    ("Kuroko's" is searched as "Kuroko"), because files spell it in several
    ways, and a long title is also tried by its first words.
    """
    queries: List[str] = []

    def add(words: List[str]) -> None:
        text = " ".join(words)
        if len(text) >= 3 and text.lower() not in (q.lower() for q in queries):
            queries.append(text)

    for name in names:
        words = []
        for word in re.split(r"\s+", name or ""):
            word = re.split(r"['\u2019`]", word)[0]
            word = _QUERY_STRIP_RE.sub(" ", word).strip()
            words.extend(w for w in word.split() if w)
        add(words)
        long_words = [w for w in words if len(w) >= 3]
        if len(long_words) > 3:
            add(long_words[:3])
    return queries[:MAX_QUERIES_PER_SHOW]


async def _expected_from_cinemeta(imdb_id: str, today: str) -> Dict[int, Set[int]]:
    from Backend.helper.metadata.providers import cinemeta

    data = await cinemeta._fetch_json(f"{cinemeta.BASE_URL}/meta/series/{imdb_id}.json")
    expected: Dict[int, Set[int]] = defaultdict(set)
    for video in ((data or {}).get("meta") or {}).get("videos") or []:
        try:
            season = int(video.get("season"))
            episode = int(video.get("episode") or video.get("number"))
        except (TypeError, ValueError):
            continue
        released = str(video.get("released") or video.get("firstAired") or "")[:10]
        if season < 1 or episode < 1 or not released or released > today:
            continue
        expected[season].add(episode)
    return dict(expected)


async def _expected_from_anizip(kitsu_id, today: str) -> Dict[int, Set[int]]:
    from Backend.helper.metadata.providers.kitsu import get_anizip_mappings

    doc = await get_anizip_mappings(kitsu_id)
    expected: Dict[int, Set[int]] = defaultdict(set)
    for key, entry in ((doc or {}).get("episodes") or {}).items():
        if not str(key).isdigit() or not entry:
            continue
        aired = str(entry.get("airdate") or entry.get("airDate") or "")[:10]
        if aired and aired > today:
            continue
        try:
            season = int(entry.get("seasonNumber") or 1)
            episode = int(entry.get("episodeNumber") or key)
        except (TypeError, ValueError):
            continue
        if season >= 1 and episode >= 1:
            expected[season].add(episode)
    return dict(expected)


async def expected_episodes(doc: dict, today: Optional[str] = None) -> Dict[int, Set[int]]:
    """Aired regular episodes per season: Cinemeta by IMDb id, else ani.zip by Kitsu id."""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    imdb_id = str(doc.get("imdb_id") or "")
    expected: Dict[int, Set[int]] = {}
    if imdb_id.startswith("tt"):
        try:
            expected = await _expected_from_cinemeta(imdb_id, today)
        except Exception as e:
            LOGGER.warning(f"[GAPS] Cinemeta episode list failed for {imdb_id}: {e}")
    if not expected and doc.get("kitsu_id"):
        try:
            expected = await _expected_from_anizip(doc.get("kitsu_id"), today)
        except Exception as e:
            LOGGER.warning(f"[GAPS] ani.zip episode list failed for {doc.get('kitsu_id')}: {e}")
    return expected


def _full_chat_id(chat_id) -> int:
    text = str(chat_id)
    return int(text if text.startswith("-100") else f"-100{text.lstrip('-')}")


async def _indexed_files(doc: dict) -> List[IndexedFile]:
    """Every stream of the show with the post it points to."""
    from Backend.helper.encrypt import decode_string

    files: List[IndexedFile] = []
    for season in doc.get("seasons") or []:
        for episode in season.get("episodes") or []:
            for item in episode.get("telegram") or []:
                try:
                    data = await decode_string(item.get("id"))
                    parts = data.get("parts") or item.get("parts") or []
                    chat_id = data.get("chat_id") or (parts[0] if parts else {}).get("chat_id")
                    msg_id = data.get("msg_id") or (parts[0] if parts else {}).get("msg_id")
                    if not chat_id or not msg_id:
                        continue
                    files.append(IndexedFile(
                        chat_id=_full_chat_id(chat_id),
                        msg_id=int(msg_id),
                        stream_id=str(item.get("id")),
                        season=int(season.get("season_number")),
                        episode=int(episode.get("episode_number")),
                        name=str(item.get("name") or ""),
                        single_post=not parts and not item.get("group_key") and not item.get("parts"),
                    ))
                except Exception:
                    continue
    return files


def _main_channel(files: List[IndexedFile]) -> Optional[int]:
    """Channel that holds most of the show's files."""
    seen = Counter(f.chat_id for f in files)
    return seen.most_common(1)[0][0] if seen else None


def _show_names(doc: dict, file_names: List[str]) -> List[str]:
    """Names to search by: the titles files of this show already use, then the stored titles."""
    from Backend.helper.metadata.plan import plan_media
    from Backend.helper.name_cleaning import clean_filename

    used: Counter = Counter()
    for name in file_names[:60]:
        try:
            plan = plan_media(clean_filename(name), anime_channel=True)
        except Exception:
            continue
        if isinstance(plan, dict) and not plan.get("skip") and plan.get("title"):
            used[str(plan["title"]).strip()] += 1
    names = [n for n, _ in used.most_common(2)]
    for key in ("title", "title_english", "original_title"):
        value = str(doc.get(key) or "").strip()
        if value and value.lower() not in (n.lower() for n in names):
            names.append(value)
    return names


async def show_gaps(doc: dict, today: Optional[str] = None, db_key: Optional[str] = None) -> Optional[Show]:
    """Missing regular episodes of one show, only in seasons that have at least one file."""
    have: Dict[int, Set[int]] = defaultdict(set)
    file_names: List[str] = []
    for season in doc.get("seasons") or []:
        for episode in season.get("episodes") or []:
            files = episode.get("telegram") or []
            if not files:
                continue
            try:
                have[int(season.get("season_number"))].add(int(episode.get("episode_number")))
            except (TypeError, ValueError):
                continue
            file_names.extend(str(f.get("name") or "") for f in files[:1])
    regular = {s: eps for s, eps in have.items() if s >= 1 and eps}
    if not regular:
        return None

    expected = await expected_episodes(doc, today)
    missing: Dict[int, List[int]] = {}
    for season, eps in regular.items():
        wanted = set(range(1, max(eps) + 1)) | expected.get(season, set())
        gap = sorted(wanted - eps)
        if gap:
            missing[season] = gap
    if not missing:
        return None

    files = await _indexed_files(doc)
    return Show(
        title=str(doc.get("title") or ""),
        ids={k: doc.get(k) for k in ("imdb_id", "tmdb_id", "kitsu_id") if doc.get(k)},
        names=_show_names(doc, file_names),
        channel=_main_channel(files),
        release_tags={t for t in (release_tag(n) for n in file_names) if t},
        have=dict(regular),
        missing=missing,
        files=files,
        doc_id=doc.get("_id"),
        db_key=db_key,
        complete_list=bool(expected),
    )


async def _tv_docs(database) -> List[Tuple[str, dict]]:
    docs: List[Tuple[str, dict]] = []
    for key, store in database.dbs.items():
        if not str(key).startswith("storage_"):
            continue
        async for doc in store["tv"].find({}):
            docs.append((str(key), doc))
    return docs


async def find_gaps(database, title_filter: str = "", today: Optional[str] = None) -> List[Show]:
    """Every indexed show with missing episodes, sorted by title."""
    wanted = _words(title_filter)
    shows: List[Show] = []
    for db_key, doc in await _tv_docs(database):
        if wanted and not set(wanted) <= set(_words(" ".join(
            str(doc.get(k) or "") for k in ("title", "title_english", "original_title")
        ))):
            continue
        try:
            show = await show_gaps(doc, today, db_key)
        except Exception as e:
            LOGGER.warning(f"[GAPS] Could not check '{doc.get('title')}': {e}")
            continue
        if show:
            shows.append(show)
    shows.sort(key=lambda s: s.title.lower())
    return shows


def _message_names(message) -> Tuple[str, str]:
    """(name used for indexing, raw file name) of a media message."""
    media = getattr(message, "video", None) or getattr(message, "document", None)
    file_name = str(getattr(media, "file_name", "") or "")
    return str(getattr(message, "caption", None) or file_name or ""), file_name


def _is_single_video(message) -> bool:
    from Backend.helper.split_files import parse_split_info

    media = getattr(message, "video", None) or getattr(message, "document", None)
    if not media:
        return False
    title, file_name = _message_names(message)
    caption = title.split("\n")[0]
    # Telegram can shorten the file name, so the part marker may only be in the caption.
    if any(parse_split_info(name) for name in (file_name, caption) if name):
        return False
    if getattr(message, "video", None):
        return True
    return (file_name or caption).lower().rstrip().endswith(VIDEO_EXTS)


def _same_show(meta: dict, show: Show) -> bool:
    for key in ("imdb_id", "tmdb_id", "kitsu_id"):
        mine, other = show.ids.get(key), meta.get(key)
        if mine and other and str(mine) == str(other):
            return True
    return False


async def _resolve_slot(message, show: Show) -> Optional[Tuple[int, int]]:
    """Season and episode the message would be indexed as, when it belongs to the show."""
    from Backend.helper.metadata import extract_default_id, metadata
    from Backend.helper.metadata.plan import plan_media
    from Backend.helper.name_cleaning import clean_filename

    title, file_name = _message_names(message)
    for name in dict.fromkeys(n for n in (title, file_name) if n):
        cleaned = clean_filename(name)
        try:
            plan = plan_media(cleaned, anime_channel=True)
        except Exception:
            continue
        if not isinstance(plan, dict) or plan.get("skip") or plan.get("kind") != "tv":
            continue
        if max((title_overlap(plan.get("title") or "", n) for n in show.names), default=0.0) < MIN_TITLE_OVERLAP:
            continue
        season, episode = plan.get("season"), plan.get("episode")
        if season is not None and int(season) == 0:
            continue
        if (
            season is not None and episode is not None and not plan.get("absolute")
            and int(episode) not in show.missing.get(int(season), [])
        ):
            continue
        meta = await metadata(
            cleaned, show.channel or 0, getattr(message, "id", 0),
            override_id=extract_default_id(getattr(message, "caption", None) or ""),
        )
        if not meta or meta.get("media_type") != "tv" or not _same_show(meta, show):
            continue
        try:
            slot = (int(meta.get("season_number")), int(meta.get("episode_number")))
        except (TypeError, ValueError):
            continue
        if slot[1] in show.missing.get(slot[0], []):
            return slot
    return None


async def _search(userbot, query: str, limit: int):
    """Media messages matching the query in every chat of the user session."""
    from pyrogram import enums
    from pyrogram.errors import FloodWait

    for media_filter in (enums.MessagesFilter.VIDEO, enums.MessagesFilter.DOCUMENT):
        for attempt in (1, 2):
            try:
                async for message in userbot.search_global(query=query, filter=media_filter, limit=limit):
                    yield message
                break
            except FloodWait as e:
                wait = int(getattr(e, "value", 30) or 30)
                if attempt == 2 or wait > 300:
                    LOGGER.warning(f"[GAPS] Search for '{query}' stopped by a {wait}s flood wait")
                    break
                await asyncio.sleep(wait + 1)


async def search_show(userbot, show: Show, skip_chats: Set[int], limit: int = SEARCH_LIMIT) -> Dict[Tuple[int, int], List[Candidate]]:
    """Files in the user's chats that would fill a missing slot of the show."""
    from Backend.helper.quality_checker import QualityChecker

    found: Dict[Tuple[int, int], List[Candidate]] = defaultdict(list)
    seen: Set[Tuple[int, int]] = set()
    for query in search_queries(show.names):
        async for message in _search(userbot, query, limit):
            chat = getattr(message, "chat", None)
            if not message or not chat or chat.id in skip_chats:
                continue
            key = (chat.id, message.id)
            if key in seen:
                continue
            seen.add(key)
            if not _is_single_video(message):
                continue
            try:
                slot = await _resolve_slot(message, show)
            except Exception as e:
                LOGGER.warning(f"[GAPS] Could not resolve message {key}: {e}")
                continue
            if not slot:
                continue
            title, file_name = _message_names(message)
            name = (title.split("\n")[0] or file_name).strip()
            media = getattr(message, "video", None) or getattr(message, "document", None)
            try:
                score = int(QualityChecker.calculate_total_score(QualityChecker.parse_filename(name)))
            except Exception:
                score = 0
            found[slot].append(Candidate(
                chat_id=chat.id,
                chat_title=str(
                    getattr(chat, "title", None) or getattr(chat, "first_name", None)
                    or getattr(chat, "username", None) or chat.id
                ),
                msg_id=message.id,
                name=name,
                size=int(getattr(media, "file_size", 0) or 0),
                season=slot[0],
                episode=slot[1],
                same_release=bool(release_tag(name)) and release_tag(name) in show.release_tags,
                score=score,
            ))
        await asyncio.sleep(SEARCH_PAUSE)
    for candidates in found.values():
        candidates.sort(key=lambda c: (c.same_release, c.score, c.size), reverse=True)
    return dict(found)


def _skip_chats() -> Set[int]:
    """Channels that are already indexed, in both id forms."""
    from Backend.helper.settings_manager import SettingsManager

    out: Set[int] = set()
    for raw in SettingsManager.current().auth_channels or []:
        text = str(raw).strip()
        try:
            out.add(int(text))
            out.add(int(text if text.startswith("-100") else f"-100{text.lstrip('-')}"))
        except ValueError:
            continue
    return out


def _default_channel() -> Optional[int]:
    from Backend.helper.settings_manager import SettingsManager

    for raw in SettingsManager.current().auth_channels or []:
        text = str(raw).strip()
        try:
            return int(text if text.startswith("-100") else f"-100{text.lstrip('-')}")
        except ValueError:
            continue
    return None


async def search_gaps(
    userbot,
    shows: List[Show],
    progress: Optional[Callable[[int, int, str], "asyncio.Future"]] = None,
) -> List[Tuple[Show, Dict[Tuple[int, int], List[Candidate]]]]:
    skip = _skip_chats()
    results = []
    for index, show in enumerate(shows, 1):
        if progress:
            try:
                await progress(index, len(shows), show.title)
            except Exception:
                pass
        try:
            found = await search_show(userbot, show, skip)
        except Exception as e:
            LOGGER.warning(f"[GAPS] Search failed for '{show.title}': {e}")
            found = {}
        results.append((show, found))
    return results


async def _copy(userbot, target: int, from_chat: int, msg_id: int):
    """Copy one post, waiting out flood limits. Returns the new message."""
    from pyrogram.errors import FloodWait

    for attempt in range(1, 6):
        try:
            result = await userbot.copy_message(target, from_chat, msg_id)
            if isinstance(result, (list, tuple)):
                result = result[0] if result else None
            if result is None or not getattr(result, "id", None):
                raise RuntimeError("the copy returned no message")
            return result
        except FloodWait as e:
            wait = int(getattr(e, "value", 30) or 30)
            if attempt == 5 or wait > 900:
                raise RuntimeError(f"flood wait {wait}s")
            await asyncio.sleep(wait + 1)


async def fill_gaps(userbot, results) -> Tuple[int, List[str]]:
    """Copy the best file of every found slot into the show's channel. Returns (copied, errors)."""
    copied, errors = 0, []
    for show, found in results:
        done, problems = await _fill_show(userbot, show, found)
        copied += done
        errors += problems
    return copied, errors


async def _fill_show(userbot, show: Show, found) -> Tuple[int, List[str]]:
    copied, errors = 0, []
    target = show.channel or _default_channel()
    if not target:
        return 0, [f"{show.title}: no channel to copy to"] if found else []
    for slot in sorted(found):
        best = found[slot][0]
        try:
            await _copy(userbot, target, best.chat_id, best.msg_id)
            copied += 1
        except Exception as e:
            errors.append(f"{show.title} S{slot[0]:02d}E{slot[1]:02d}: {e}")
        await asyncio.sleep(COPY_PAUSE)
    return copied, errors


# Files that are being posted again. The receiver leaves them alone: their
# library entries are pointed at the new posts directly.
_reposting: Dict[tuple, float] = {}


def _repost_keys(message) -> List[tuple]:
    media = getattr(message, "video", None) or getattr(message, "document", None)
    if not media:
        return []
    keys: List[tuple] = []
    unique = getattr(media, "file_unique_id", None)
    if unique:
        keys.append(("id", str(unique)))
    # The exact size within the same chat identifies the file as well
    size = getattr(media, "file_size", None)
    if size:
        keys.append(("size", getattr(getattr(message, "chat", None), "id", None), int(size)))
    return keys


def is_repost(message) -> bool:
    """True for a new channel post that is the copy of a file being moved."""
    if not _reposting:
        return False
    now = time.time()
    for key in [k for k, until in _reposting.items() if until < now]:
        _reposting.pop(key, None)
    return any(key in _reposting for key in _repost_keys(message))


def repost_plan(show: Show, found) -> Tuple[Optional[list], str]:
    """Posting order that puts the found files in front of the show's later posts.

    Returns (steps, reason). steps is None when nothing has to move (the reason
    is empty) or when the show cannot be sorted (the reason says why). A step is
    ("old", IndexedFile) for a post to post again or ("new", Candidate).
    Posts keep the order they have now; only the found files are slotted in.
    """
    if not found or not show.channel:
        return None, ""
    first = min(found)
    posts: Dict[int, IndexedFile] = {}
    for item in sorted(show.files, key=lambda f: f.msg_id):
        if item.chat_id == show.channel:
            posts.setdefault(item.msg_id, item)
    later = [f for f in posts.values() if f.season >= 1 and (f.season, f.episode) > first]
    if not later:
        return None, ""
    if any(not f.single_post for f in show.files if f.chat_id == show.channel):
        return None, "the show has split files"
    start = min(f.msg_id for f in later)
    tail = [f for f in posts.values() if f.msg_id >= start]
    if len(tail) > MAX_REPOSTS_PER_SHOW:
        return None, f"{len(tail)} posts would have to be posted again"

    steps: list = [("old", f) for f in tail]

    def slot_of(step):
        kind, item = step
        if kind == "old" and item.season < 1:
            return None
        return (item.season, item.episode)

    for slot in sorted(found):
        index = next(
            (i for i, step in enumerate(steps) if slot_of(step) is not None and slot_of(step) > slot),
            len(steps),
        )
        steps.insert(index, ("new", found[slot][0]))
    return steps, ""


def _chunks(items: list, size: int = 100):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _get_posts(userbot, chat_id: int, msg_ids: List[int]) -> Dict[int, object]:
    """Existing media posts by id; missing or empty ones are left out."""
    posts: Dict[int, object] = {}
    for chunk in _chunks(list(msg_ids)):
        got = await userbot.get_messages(chat_id, chunk)
        for message in got if isinstance(got, (list, tuple)) else [got]:
            if not message or getattr(message, "empty", False):
                continue
            if getattr(message, "video", None) or getattr(message, "document", None):
                posts[int(message.id)] = message
    return posts


def _media_size(message) -> int:
    media = getattr(message, "video", None) or getattr(message, "document", None)
    return int(getattr(media, "file_size", 0) or 0)


async def _delete_posts(userbot, chat_id: int, msg_ids: List[int]) -> Optional[str]:
    try:
        for chunk in _chunks(list(msg_ids)):
            await userbot.delete_messages(chat_id, chunk)
        return None
    except Exception as e:
        return str(e)


async def _repoint(database, show: Show, moves: Dict[int, int]) -> Set[int]:
    """Point the show's streams at the new posts. Returns the old post ids that were repointed."""
    from Backend.helper.encrypt import decode_string, encode_string

    store = database.dbs[show.db_key]["tv"]
    doc = await store.find_one({"_id": show.doc_id})
    if not doc:
        return set()
    by_stream = {f.stream_id: f for f in show.files if f.chat_id == show.channel and f.msg_id in moves}
    done: Set[int] = set()
    for season in doc.get("seasons") or []:
        for episode in season.get("episodes") or []:
            for item in episode.get("telegram") or []:
                old_id = item.get("id")
                source = by_stream.get(old_id)
                if not source:
                    continue
                data = await decode_string(old_id)
                data["msg_id"] = moves[source.msg_id]
                new_id = await encode_string(data)
                backdrop = episode.get("episode_backdrop")
                if isinstance(backdrop, str) and backdrop.endswith(f"/thumb/{old_id}"):
                    episode["episode_backdrop"] = backdrop[: -len(old_id)] + new_id
                item["id"] = new_id
                done.add(source.msg_id)
    if done:
        await store.update_one({"_id": show.doc_id}, {"$set": {"seasons": doc["seasons"]}})
    return done


async def _fill_show_sorted(userbot, show: Show, found, database, db_lock, settle=None) -> Tuple[int, List[str], str]:
    """Fill one show and keep its channel posts in order. Returns (copied, errors, note).

    settle, when given, is awaited after the copies were posted and before
    they are checked, so the receiver has finished with the new posts.
    """
    steps, reason = repost_plan(show, found)
    if steps is None:
        copied, errors = await _fill_show(userbot, show, found)
        return copied, errors, (f"channel order not changed: {reason}" if reason else "")

    channel = show.channel
    old_ids = [item.msg_id for kind, item in steps if kind == "old"]
    originals = await _get_posts(userbot, channel, old_ids)
    if len(originals) != len(old_ids):
        copied, errors = await _fill_show(userbot, show, found)
        return copied, errors, "channel order not changed: some indexed posts no longer exist"

    until = time.time() + REPOST_IGNORE_SECONDS
    for message in originals.values():
        for key in _repost_keys(message):
            _reposting[key] = until

    copied, errors = 0, []
    moves: Dict[int, int] = {}
    failure = None
    waiting = dict(found)
    for kind, item in steps:
        try:
            if kind == "old":
                moves[item.msg_id] = int((await _copy(userbot, channel, channel, item.msg_id)).id)
            else:
                await _copy(userbot, channel, item.chat_id, item.msg_id)
                copied += 1
                waiting.pop((item.season, item.episode), None)
        except Exception as e:
            where = f"S{item.season:02d}E{item.episode:02d}"
            failure = f"{show.title} {where}: {e}"
            break
        await asyncio.sleep(COPY_PAUSE)

    if failure is None:
        if settle is not None:
            await settle()
        copies = await _get_posts(userbot, channel, list(moves.values()))
        for old_id, new_id in moves.items():
            if new_id not in copies or _media_size(copies[new_id]) != _media_size(originals[old_id]):
                failure = f"{show.title}: the copy of post {old_id} could not be confirmed"
                break

    if failure is not None:
        # Take the copies of the old posts away again; the old posts were not touched
        problem = await _delete_posts(userbot, channel, list(moves.values()))
        errors.append(failure)
        if problem:
            errors.append(f"{show.title}: could not remove {len(moves)} duplicate copies: {problem}")
        # The found files that were not posted yet are still added, at the end
        done, problems = await _fill_show(userbot, show, waiting)
        return copied + done, errors + problems, "channel order not changed: a copy failed, the old posts were kept"

    async with db_lock:
        repointed = await _repoint(database, show, moves)
    stray = [new_id for old_id, new_id in moves.items() if old_id not in repointed]
    if stray:
        await _delete_posts(userbot, channel, stray)
    problem = await _delete_posts(userbot, channel, sorted(repointed))
    if problem:
        errors.append(f"{show.title}: the old posts could not be removed ({problem}); they are duplicates now")
    note = f"posted {len(repointed)} later file(s) again after the new one(s) and removed the old posts"
    return copied, errors, note


async def fill_gaps_sorted(userbot, results, database, db_lock, settle=None) -> Tuple[int, List[str], Dict[str, str]]:
    """fill_gaps that also keeps each show's posts in episode order. Returns (copied, errors, notes by title)."""
    copied, errors, notes = 0, [], {}
    for show, found in results:
        if not found:
            continue
        try:
            done, problems, note = await _fill_show_sorted(userbot, show, found, database, db_lock, settle)
        except Exception as e:
            done, problems, note = 0, [f"{show.title}: {e}"], "channel order not changed: unexpected error"
        copied += done
        errors += problems
        if note:
            notes[show.title] = note
    return copied, errors, notes


def _readable_size(size: int) -> str:
    from Backend.helper.pyro import get_readable_file_size

    return get_readable_file_size(size) if size else "?"


def gaps_report(shows: List[Show]) -> str:
    lines = [f"Shows with missing episodes: {len(shows)}", f"Missing episodes: {sum(s.missing_count for s in shows)}", ""]
    for show in shows:
        note = "" if show.complete_list else "  (no public episode list: only holes are shown)"
        lines.append(f"{show.title}{note}")
        for season in sorted(show.missing):
            lines.append(
                f"  Season {season}: missing {compress_ranges(show.missing[season])}"
                f"   (have {compress_ranges(show.have.get(season, []))})"
            )
    return "\n".join(lines) + "\n"


def search_report(
    results, copied: Optional[int] = None, errors: Optional[List[str]] = None, notes: Optional[Dict[str, str]] = None,
) -> str:
    total = sum(s.missing_count for s, _ in results)
    hit = sum(len(f) for _, f in results)
    lines = [f"Missing episodes: {total}", f"Found in your Telegram: {hit}", f"Not found: {total - hit}"]
    if copied is not None:
        lines.append(f"Copied to the channel: {copied}")
    lines.append("")
    for show, found in results:
        lines.append(show.title)
        if notes and notes.get(show.title):
            lines.append(f"  Channel: {notes[show.title]}")
        for season in sorted(show.missing):
            for episode in show.missing[season]:
                candidates = found.get((season, episode)) or []
                if not candidates:
                    continue
                best = candidates[0]
                more = f"  (+{len(candidates) - 1} more)" if len(candidates) > 1 else ""
                lines.append(
                    f"  S{season:02d}E{episode:02d}  FOUND  {best.name}  [{_readable_size(best.size)}]"
                    f"  in \"{best.chat_title}\" msg {best.msg_id}{more}"
                )
            left = [e for e in show.missing[season] if (season, e) not in found]
            if left:
                lines.append(f"  Season {season}: not found {compress_ranges(left)}")
    if errors:
        lines += ["", "Errors:"] + [f"  {e}" for e in errors]
    return "\n".join(lines) + "\n"
