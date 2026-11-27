"""Missing-episode finder: gap detection, search matching and copying."""
from types import SimpleNamespace

import pytest

from Backend.helper import gap_filler as G

CHANNEL = -1001234567890
SERIES_IDS = {"imdb_id": "tt0000001", "tmdb_id": 1001}


async def _doc(have, title="Example Quest", first_msg=101, **extra):
    """TV document with one file per held episode: {season: [episodes]}.

    The files are channel posts numbered from first_msg in the order given.
    """
    from Backend.helper.encrypt import encode_string

    seasons, msg_id = [], first_msg
    for season, episodes in have.items():
        entries = []
        for n in episodes:
            stream_id = await encode_string({"chat_id": int(str(CHANNEL)[4:]), "msg_id": msg_id})
            entries.append({
                "episode_number": n,
                "episode_backdrop": f"/thumb/{stream_id}",
                "telegram": [{"id": stream_id, "name": f"[Grp] {title} - S{season:02d}E{n:02d} [1080p].mkv"}],
            })
            msg_id += 1
        seasons.append({"season_number": season, "episodes": entries})
    return {"_id": "doc1", "title": title, "seasons": seasons, **SERIES_IDS, **extra}


def _message(msg_id, name, *, chat_id=555, chat_title="Leech Bot", kind="document", size=1000, caption=None):
    message = SimpleNamespace(
        id=msg_id, chat=SimpleNamespace(id=chat_id, title=chat_title), caption=caption, video=None, document=None,
    )
    setattr(message, kind, SimpleNamespace(file_name=name, file_size=size))
    return message


class FakeUserbot:
    """User session stand-in: search results, plus a channel whose posts can be copied and deleted."""

    def __init__(self, messages, channel_posts=None, fail_copy_of=None):
        self.messages = messages
        self.copied = []
        self.deleted = []
        self.channel = dict(channel_posts or {})
        self.next_id = max(self.channel, default=1000) + 1
        self.fail_copy_of = fail_copy_of

    async def search_global(self, query="", filter=None, limit=0, **_):
        kind = "video" if "VIDEO" in str(filter) else "document"
        for message in self.messages:
            if getattr(message, kind) is not None:
                yield message

    def _source(self, chat_id, message_id):
        if chat_id == CHANNEL:
            return self.channel[message_id]
        return next(m for m in self.messages if m.chat.id == chat_id and m.id == message_id)

    async def copy_message(self, chat_id, from_chat_id, message_id, **_):
        if message_id == self.fail_copy_of:
            raise RuntimeError("copy refused")
        self.copied.append((chat_id, from_chat_id, message_id))
        source = self._source(from_chat_id, message_id)
        media = source.video or source.document
        new = _message(self.next_id, media.file_name, chat_id=chat_id, size=media.file_size)
        self.channel[self.next_id] = new
        self.next_id += 1
        return new

    async def get_messages(self, chat_id, ids, **_):
        return [self.channel.get(i) or SimpleNamespace(id=i, empty=True, video=None, document=None) for i in ids]

    async def delete_messages(self, chat_id, ids, **_):
        for i in ids:
            self.deleted.append(i)
            self.channel.pop(i, None)


class FakeStore:
    """The one MongoDB collection the sorter reads and writes."""

    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, query):
        return self.doc if query.get("_id") == self.doc.get("_id") else None

    async def update_one(self, query, update):
        self.doc.update(update["$set"])


def _library(doc):
    return SimpleNamespace(dbs={"storage_1": {"tv": FakeStore(doc)}})


def _channel_posts(doc):
    """Channel posts behind the document's streams, by message id."""
    posts = {}
    for index, season in enumerate(doc["seasons"]):
        for episode in season["episodes"]:
            msg_id = 101 + len(posts)
            posts[msg_id] = _message(msg_id, episode["telegram"][0]["name"], chat_id=CHANNEL, size=5000 + msg_id)
    return posts


async def _stream_posts(doc):
    """{(season, episode): message id} as the library now points."""
    from Backend.helper.encrypt import decode_string

    out = {}
    for season in doc["seasons"]:
        for episode in season["episodes"]:
            out[(season["season_number"], episode["episode_number"])] = (await decode_string(episode["telegram"][0]["id"]))["msg_id"]
    return out


@pytest.fixture
def no_pauses(monkeypatch):
    monkeypatch.setattr(G, "SEARCH_PAUSE", 0)
    monkeypatch.setattr(G, "COPY_PAUSE", 0)


@pytest.fixture
def episode_list(monkeypatch):
    """Public episode list: season 1 has 12 episodes, season 2 has 6."""
    async def fake(doc, today=None):
        return {1: set(range(1, 13)), 2: set(range(1, 7))}

    monkeypatch.setattr(G, "expected_episodes", fake)


@pytest.fixture
def pipeline(monkeypatch):
    """Metadata pipeline stand-in: reads SxxEyy from the name; "Other" names are another show."""
    import re

    import Backend.helper.metadata as metadata_pkg

    async def fake_metadata(name, channel, msg_id, override_id=None, **_):
        found = re.search(r"S(\d+)E(\d+)", name)
        if not found:
            return None
        ids = {"imdb_id": "tt0000009", "tmdb_id": 9009} if "Other" in name else dict(SERIES_IDS)
        return {"media_type": "tv", "season_number": int(found.group(1)), "episode_number": int(found.group(2)), **ids}

    monkeypatch.setattr(metadata_pkg, "metadata", fake_metadata)


def test_compress_ranges():
    assert G.compress_ranges([5, 1, 2, 3, 9, 10]) == "1-3, 5, 9-10"


def test_search_queries_cut_apostrophes_and_shorten_long_titles():
    assert G.search_queries(["Kuroko's Basketball"]) == ["Kuroko Basketball"]
    queries = G.search_queries(["The World's Finest Assassin Gets Reincarnated"])
    assert queries == ["The World Finest Assassin Gets Reincarnated", "The World Finest"]
    assert G.search_queries(["Example Quest", "example quest", "Re:Example"]) == ["Example Quest", "Re Example"]


def test_release_tag():
    assert G.release_tag("[Grp] Example Quest - 01 [1080p].mkv") == "grp"
    assert G.release_tag("Example.Quest.S01E01.1080p.BluRay.x265-Team.mkv") == "team"
    assert G.release_tag("Example Quest 01.mkv") == ""


async def test_gaps_are_holes_and_the_remaining_episodes_of_held_seasons(episode_list):
    show = await G.show_gaps(await _doc({0: [1], 1: [2, 3, 5, 6]}))
    # season 2 has no file, so it is not reported; season 0 is never reported
    assert show.missing == {1: [1, 4, 7, 8, 9, 10, 11, 12]}
    assert show.channel == CHANNEL and show.complete_list
    assert "Example Quest" in show.names


async def test_complete_show_has_no_gaps(episode_list):
    assert await G.show_gaps(await _doc({1: list(range(1, 13))})) is None


async def test_without_an_episode_list_only_holes_are_known(monkeypatch):
    async def nothing(doc, today=None):
        return {}

    monkeypatch.setattr(G, "expected_episodes", nothing)
    show = await G.show_gaps(await _doc({1: [1, 2, 4]}))
    assert show.missing == {1: [3]} and not show.complete_list


async def test_search_matches_only_files_that_fill_a_gap(episode_list, pipeline, no_pauses):
    show = await G.show_gaps(await _doc({1: [1, 2, 3, 5]}))
    userbot = FakeUserbot([
        _message(1, "[Grp] Example Quest - S01E04 [1080p].mkv"),
        _message(2, "[Grp] Example Quest - S01E03 [1080p].mkv"),                       # already indexed
        _message(3, "[Grp] Example Quest - S00E04 [1080p].mkv"),                       # special
        _message(4, "[Grp] Other Example Quest - S01E06 [1080p].mkv"),                 # another show
        _message(5, "[Grp] Example Quest - S01E07 [1080p].mkv.001"),                   # split part
        _message(6, "[Grp] Example Quest - S01E08 [1080p].srt"),                       # not a video
        _message(7, "[Grp] Example Quest - S01E09 [1080p].mkv", chat_id=CHANNEL),      # already in the channel
        _message(8, "Something Else - S01E10 [1080p].mkv"),                            # title does not fit
        _message(9, "video.mp4", kind="video", caption="[Grp] Example Quest - S01E11 [1080p].mkv"),
        # split part whose shortened file name lost the part marker
        _message(10, "Example.Quest.S01E06.1080p.BluRay.Dual-Audio.Op.mkv", kind="video",
                 caption="Example.Quest.S01E06.1080p.BluRay.Dual-Audio.Opus-Grp.part001.mkv"),
    ])
    found = await G.search_show(userbot, show, {CHANNEL})
    assert {slot: [c.msg_id for c in cands] for slot, cands in found.items()} == {(1, 4): [1], (1, 11): [9]}


async def test_best_candidate_is_same_release_then_quality_then_size(episode_list, pipeline, no_pauses):
    show = await G.show_gaps(await _doc({1: [1, 2, 3, 5]}))
    userbot = FakeUserbot([
        _message(1, "[Else] Example Quest - S01E04 [BD 1080p].mkv", size=9000),
        _message(2, "[Grp] Example Quest - S01E04 [720p].mkv", size=100),
        _message(3, "[Grp] Example Quest - S01E04 [1080p].mkv", size=500),
    ])
    found = await G.search_show(userbot, show, set())
    assert [c.msg_id for c in found[(1, 4)]] == [3, 2, 1]


async def test_fill_copies_one_file_per_slot_into_the_shows_channel(episode_list, pipeline, no_pauses):
    show = await G.show_gaps(await _doc({1: [1, 2, 3, 5]}))
    userbot = FakeUserbot([
        _message(1, "[Grp] Example Quest - S01E04 [1080p].mkv"),
        _message(2, "[Grp] Example Quest - S01E04 [720p].mkv"),
        _message(3, "[Grp] Example Quest - S01E06 [1080p].mkv", chat_id=777, chat_title="Group"),
    ])
    found = await G.search_show(userbot, show, set())
    copied, errors = await G.fill_gaps(userbot, [(show, found)])
    assert (copied, errors) == (2, [])
    assert userbot.copied == [(CHANNEL, 555, 1), (CHANNEL, 777, 3)]
    report = G.search_report([(show, found)], copied, errors)
    assert "S01E04  FOUND" in report and "Copied to the channel: 2" in report
    assert "not found 7-12" in report


async def _sorted_case(have, leech, monkeypatch, **userbot_options):
    import asyncio

    doc = await _doc(have)
    show = await G.show_gaps(doc, db_key="storage_1")
    userbot = FakeUserbot(leech, _channel_posts(doc), **userbot_options)
    found = await G.search_show(userbot, show, {CHANNEL})
    result = await G._fill_show_sorted(userbot, show, found, _library(doc), asyncio.Lock())
    return doc, show, userbot, result


async def test_sorted_fill_posts_the_later_episodes_again_after_the_new_one(episode_list, pipeline, no_pauses, monkeypatch):
    # posts 101-105 hold S01E01, E02, E03, E05, E06
    doc, show, userbot, (copied, errors, note) = await _sorted_case(
        {1: [1, 2, 3, 5, 6]}, [_message(1, "[Grp] Example Quest - S01E04 [1080p].mkv")], monkeypatch,
    )
    assert (copied, errors) == (1, [])
    # E04 from the leech chat, then E05 and E06 from the channel itself
    assert userbot.copied == [(CHANNEL, 555, 1), (CHANNEL, CHANNEL, 104), (CHANNEL, CHANNEL, 105)]
    assert userbot.deleted == [104, 105]
    assert sorted(userbot.channel) == [101, 102, 103, 106, 107, 108]
    # the library follows the moved posts; untouched episodes keep theirs
    assert await _stream_posts(doc) == {(1, 1): 101, (1, 2): 102, (1, 3): 103, (1, 5): 107, (1, 6): 108}
    moved = doc["seasons"][0]["episodes"][3]
    assert moved["episode_backdrop"] == f"/thumb/{moved['telegram'][0]['id']}"
    assert "posted 2 later file(s) again" in note


async def test_sorted_fill_marks_the_reposts_for_the_receiver(episode_list, pipeline, no_pauses, monkeypatch):
    monkeypatch.setattr(G, "_reposting", {})
    doc, show, userbot, _ = await _sorted_case(
        {1: [1, 2, 3, 5, 6]}, [_message(1, "[Grp] Example Quest - S01E04 [1080p].mkv")], monkeypatch,
    )
    # the copies of E05 and E06 are left alone, the new E04 is indexed as usual
    assert G.is_repost(userbot.channel[107]) and G.is_repost(userbot.channel[108])
    assert not G.is_repost(userbot.channel[106])


async def test_sorted_fill_keeps_the_old_posts_when_a_copy_fails(episode_list, pipeline, no_pauses, monkeypatch):
    doc, show, userbot, (copied, errors, note) = await _sorted_case(
        {1: [1, 2, 3, 5, 6]}, [_message(1, "[Grp] Example Quest - S01E04 [1080p].mkv")], monkeypatch,
        fail_copy_of=105,
    )
    before = {(1, 1): 101, (1, 2): 102, (1, 3): 103, (1, 5): 104, (1, 6): 105}
    assert await _stream_posts(doc) == before
    # every old post is still there; only the copy of E05 was taken away again
    assert {101, 102, 103, 104, 105} <= set(userbot.channel)
    assert userbot.deleted == [107]
    assert copied == 1 and any("copy refused" in e for e in errors) and "old posts were kept" in note


async def test_sorted_fill_only_copies_when_nothing_comes_after(episode_list, pipeline, no_pauses, monkeypatch):
    doc, show, userbot, (copied, errors, note) = await _sorted_case(
        {1: [1, 2, 3]}, [_message(1, "[Grp] Example Quest - S01E04 [1080p].mkv")], monkeypatch,
    )
    assert (copied, errors, note) == (1, [], "")
    assert userbot.copied == [(CHANNEL, 555, 1)] and userbot.deleted == []


async def test_repost_plan_slots_several_new_files_between_the_old_posts(episode_list, pipeline, no_pauses):
    # posts: S01E01, E02, E05, E06, then season 2 E01; a special sits between
    doc = await _doc({1: [1, 2, 5, 6], 0: [1], 2: [1]})
    show = await G.show_gaps(doc, db_key="storage_1")
    userbot = FakeUserbot([
        _message(1, "[Grp] Example Quest - S01E04 [1080p].mkv"),
        _message(2, "[Grp] Example Quest - S01E07 [1080p].mkv"),
    ])
    found = await G.search_show(userbot, show, {CHANNEL})
    steps, reason = G.repost_plan(show, found)
    order = [(kind, item.season, item.episode) for kind, item in steps]
    assert reason == "" and order == [
        ("new", 1, 4), ("old", 1, 5), ("old", 1, 6), ("old", 0, 1), ("new", 1, 7), ("old", 2, 1),
    ]


async def test_show_with_split_files_is_not_sorted(episode_list, pipeline, no_pauses):
    doc = await _doc({1: [1, 2, 3, 5, 6]})
    doc["seasons"][0]["episodes"][4]["telegram"][0]["group_key"] = "x:1080p:example"
    show = await G.show_gaps(doc, db_key="storage_1")
    found = await G.search_show(FakeUserbot([_message(1, "[Grp] Example Quest - S01E04 [1080p].mkv")]), show, {CHANNEL})
    assert G.repost_plan(show, found) == (None, "the show has split files")
