"""Rescan purge: a wiped channel must lose all of its files, including
multi-part ones, which store their messages under "parts"."""
import os

os.environ.setdefault("DATABASE", "mongodb://localhost:27017/db1,mongodb://localhost:27017/db2")

import copy

from Backend.helper.database import Database
from Backend.helper.encrypt import decode_string, encode_string
from Backend.helper.metadata.providers import cinemeta, kitsu
from Backend.helper.scan_manager import ScanManager

CHANNEL = 1234567890
OTHER = 1987654321


class _Cursor:
    def __init__(self, docs):
        self._docs = [copy.deepcopy(d) for d in docs]

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self, docs=()):
        self.docs = list(docs)

    def find(self, _query):
        return _Cursor(self.docs)

    async def replace_one(self, query, doc):
        self.docs = [doc if d["_id"] == query["_id"] else d for d in self.docs]

    async def delete_one(self, query):
        self.docs = [d for d in self.docs if d["_id"] != query["_id"]]

    async def delete_many(self, query):
        self.docs = [d for d in self.docs if any(d.get(k) != v for k, v in query.items())]


class _Db:
    current_db_index = 1

    def __init__(self, movies, shows, subtitles=()):
        self.dbs = {
            "tracking": {"subtitles": _Collection(subtitles)},
            "storage_1": {"movie": _Collection(movies), "tv": _Collection(shows)},
        }

    async def _build_part_id_and_size(self, parts, archive=None):
        return await Database._build_part_id_and_size(self, parts, archive)


async def _single(channel, msg_id, name="a.mkv"):
    return {
        "quality": "1080p",
        "id": await encode_string({"chat_id": channel, "msg_id": msg_id}),
        "name": name,
        "size": "1GB",
    }


async def _multi(parts, name="big.mkv"):
    rows = [
        {"part_number": n, "chat_id": ch, "msg_id": mid, "size_bytes": 1000}
        for n, (ch, mid) in enumerate(parts, start=1)
    ]
    return {
        "quality": "1080p",
        "id": await encode_string({"parts": [{"chat_id": ch, "msg_id": mid} for ch, mid in parts]}),
        "name": name,
        "size": "2GB",
        "group_key": f"{parts[0][0]}:1080p:{name}",
        "parts": rows,
    }


def _manager(db):
    manager = ScanManager()
    manager._db = db
    return manager


async def test_wipe_removes_multi_part_movie():
    db = _Db(
        movies=[
            {"_id": 1, "tmdb_id": 10, "telegram": [await _multi([(CHANNEL, 5), (CHANNEL, 6)])]},
            {"_id": 2, "tmdb_id": 11, "telegram": [await _single(CHANNEL, 5)]},
        ],
        shows=[],
    )
    purged = await _manager(db)._purge_channel_entries(CHANNEL)
    assert purged == 2
    assert db.dbs["storage_1"]["movie"].docs == []


async def test_wipe_removes_multi_part_episode_and_empty_show():
    episode = {"episode_number": 1, "telegram": [await _multi([(CHANNEL, 30), (CHANNEL, 31)])]}
    db = _Db(
        movies=[],
        shows=[{"_id": 1, "tmdb_id": 20, "seasons": [{"season_number": 1, "episodes": [episode]}]}],
    )
    assert await _manager(db)._purge_channel_entries(CHANNEL) == 1
    assert db.dbs["storage_1"]["tv"].docs == []


async def test_wipe_keeps_files_of_other_channels():
    other_single = await _single(OTHER, 7, "other.mkv")
    other_multi = await _multi([(OTHER, 8), (OTHER, 9)], "other-big.mkv")
    db = _Db(
        movies=[{
            "_id": 1,
            "tmdb_id": 10,
            "telegram": [other_single, await _single(CHANNEL, 5), other_multi,
                         await _multi([(CHANNEL, 5), (CHANNEL, 6)])],
        }],
        shows=[],
    )
    assert await _manager(db)._purge_channel_entries(CHANNEL) == 2
    (movie,) = db.dbs["storage_1"]["movie"].docs
    assert [q["name"] for q in movie["telegram"]] == ["other.mkv", "other-big.mkv"]
    assert movie["telegram"][1]["id"] == other_multi["id"]


async def test_wipe_trims_a_file_split_across_two_channels():
    db = _Db(
        movies=[{"_id": 1, "tmdb_id": 10, "telegram": [await _multi([(OTHER, 8), (CHANNEL, 6)])]}],
        shows=[],
    )
    assert await _manager(db)._purge_channel_entries(CHANNEL) == 1
    (movie,) = db.dbs["storage_1"]["movie"].docs
    (quality,) = movie["telegram"]
    assert [(p["chat_id"], p["msg_id"]) for p in quality["parts"]] == [(OTHER, 8)]
    assert await decode_string(quality["id"]) == {"parts": [{"chat_id": OTHER, "msg_id": 8}]}


async def test_wipe_accepts_prefixed_channel_ids_in_stored_parts():
    stored = await _multi([(int(f"-100{CHANNEL}"), 1), (int(f"-100{CHANNEL}"), 2)])
    db = _Db(movies=[{"_id": 1, "tmdb_id": 10, "telegram": [stored]}], shows=[])
    assert await _manager(db)._purge_channel_entries(CHANNEL) == 1
    assert db.dbs["storage_1"]["movie"].docs == []


async def test_wipe_clears_subtitles_of_the_channel_only():
    db = _Db(movies=[], shows=[], subtitles=[{"chat_id": CHANNEL, "msg_id": 1}, {"chat_id": OTHER, "msg_id": 2}])
    await _manager(db)._purge_channel_entries(CHANNEL)
    assert db.dbs["tracking"]["subtitles"].docs == [{"chat_id": OTHER, "msg_id": 2}]


async def test_wipe_leaves_undecodable_entries_alone():
    broken = {"quality": "720p", "id": "not-a-valid-id", "name": "x.mkv", "size": "1GB"}
    db = _Db(movies=[{"_id": 1, "tmdb_id": 10, "telegram": [broken]}], shows=[])
    assert await _manager(db)._purge_channel_entries(CHANNEL) == 0
    assert db.dbs["storage_1"]["movie"].docs[0]["telegram"] == [broken]


# Films keep their own TMDB id

async def test_film_gets_its_own_tmdb_id_not_the_series_id(monkeypatch):
    async def fake_detail(imdb_id, media_type):
        assert (imdb_id, media_type) == ("tt0275277", "movie")
        return {"moviedb_id": 11299}

    monkeypatch.setattr(cinemeta, "cached_detail", fake_detail)
    payload = await kitsu._confirm_movie_tmdb_id({"imdb_id": "tt0275277", "tmdb_id": 30991})
    assert payload["tmdb_id"] == 11299


async def test_film_tmdb_id_is_kept_when_cinemeta_has_no_answer(monkeypatch):
    async def no_detail(imdb_id, media_type):
        return None

    async def failing(imdb_id, media_type):
        raise RuntimeError("offline")

    monkeypatch.setattr(cinemeta, "cached_detail", no_detail)
    assert (await kitsu._confirm_movie_tmdb_id({"imdb_id": "tt1", "tmdb_id": 5}))["tmdb_id"] == 5
    monkeypatch.setattr(cinemeta, "cached_detail", failing)
    assert (await kitsu._confirm_movie_tmdb_id({"imdb_id": "tt1", "tmdb_id": 5}))["tmdb_id"] == 5


async def test_film_without_imdb_id_is_not_looked_up(monkeypatch):
    async def boom(imdb_id, media_type):
        raise AssertionError("must not be called")

    monkeypatch.setattr(cinemeta, "cached_detail", boom)
    assert (await kitsu._confirm_movie_tmdb_id({"imdb_id": None, "tmdb_id": 5}))["tmdb_id"] == 5
