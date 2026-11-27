"""Season 0 handling: specials are stored in season 0, special ranges in the
Anime-Lists data do not renumber regular episodes, and Replace Mode only
replaces a file with the same episode or film."""
import os

os.environ.setdefault("DATABASE", "mongodb://localhost:27017/db1,mongodb://localhost:27017/db2")

import pytest

from Backend.helper import database as database_module
from Backend.helper.database import Database
from Backend.helper.metadata import episode_maps
from Backend.helper.metadata.episode_maps import AnimeListEntry
from Backend.helper.release_identity import same_release_target


# Season numbers

@pytest.mark.parametrize(
    "value, expected",
    [(0, 0), ("0", 0), (1, 1), ("2", 2), (None, 1), ("", 1), ("x", 1)],
)
def test_season_number_keeps_specials_in_season_zero(value, expected):
    assert Database._season_number(value) == expected


# Anime-Lists special ranges

@pytest.fixture
def anime_lists(monkeypatch):
    entries = {
        # specials 2-16 -> TVDB S00E11-25
        12665: AnimeListEntry(anidb_id=12665, tvdb_id=331753, default_tvdb_season="1",
                              mappings=[(0, 0, 2, 16, 9)]),
        # specials 2-11 -> TVDB S00E01-10
        8148: AnimeListEntry(anidb_id=8148, tvdb_id=248035, default_tvdb_season="1",
                             mappings=[(0, 0, 2, 11, -1)]),
        # long-running show split into TVDB seasons by episode ranges
        69: AnimeListEntry(anidb_id=69, tvdb_id=81797, default_tvdb_season="a",
                           mappings=[(1, 2, 62, 77, -61), (0, 0, 1, 5, 3)]),
        # 25-episode first season; season 4 aired in two cours
        500: AnimeListEntry(anidb_id=500, tvdb_id=7000, tmdb_tv_id=7100, default_tvdb_season="1", tmdb_season="1"),
        501: AnimeListEntry(anidb_id=501, tvdb_id=7000, tmdb_tv_id=7100, default_tvdb_season="4", tmdb_season="4"),
        502: AnimeListEntry(anidb_id=502, tvdb_id=7000, tmdb_tv_id=7100, default_tvdb_season="4", tmdb_season="4",
                            episode_offset=13, tmdb_offset=13),
        # second season that TMDB lists as episodes 26+ of season 1
        600: AnimeListEntry(anidb_id=600, tvdb_id=8000, tmdb_tv_id=8100, default_tvdb_season="1", tmdb_season="1"),
        601: AnimeListEntry(anidb_id=601, tvdb_id=8000, tmdb_tv_id=8100, default_tvdb_season="2", tmdb_season="1",
                            tmdb_offset=25),
    }
    monkeypatch.setattr(episode_maps, "_anime_lists", entries)
    return entries


@pytest.mark.parametrize("anidb_id, absolute", [(12665, 2), (12665, 8), (12665, 16), (8148, 2), (8148, 11)])
def test_special_ranges_do_not_move_regular_episodes(anime_lists, anidb_id, absolute):
    hit = episode_maps.resolve_via_anime_lists(anidb_id, absolute)
    assert (hit["season_number"], hit["episode_number"]) == (1, absolute)


def test_regular_ranges_still_apply(anime_lists):
    hit = episode_maps.resolve_via_anime_lists(69, 70)
    assert (hit["season_number"], hit["episode_number"]) == (2, 9)
    # outside the regular ranges: TVDB absolute numbering
    hit = episode_maps.resolve_via_anime_lists(69, 3)
    assert hit.get("tvdb_absolute") is True and hit["season_number"] is None


def test_second_cour_of_a_later_season_does_not_split_season_one(anime_lists):
    for absolute in (13, 14, 25):
        hit = episode_maps.resolve_via_anime_lists(500, absolute)
        assert (hit["season_number"], hit["episode_number"]) == (1, absolute)


def test_absolute_numbers_continue_into_the_next_season(anime_lists):
    hit = episode_maps.resolve_via_anime_lists(600, 25)
    assert (hit["season_number"], hit["episode_number"]) == (1, 25)
    hit = episode_maps.resolve_via_anime_lists(600, 26)
    assert (hit["season_number"], hit["episode_number"]) == (2, 1)


def test_reverse_lookup_ignores_special_ranges(anime_lists):
    # S00E17 is a special
    assert episode_maps._absolute_from_entry(anime_lists[12665], 0, 17) is None
    assert episode_maps._absolute_from_entry(anime_lists[12665], 1, 8) == 8
    assert episode_maps._absolute_from_entry(anime_lists[69], 2, 9) == 70


# Release identity

@pytest.mark.parametrize(
    "existing, incoming",
    [
        ("Kuroko's Basketball - S01E10 (Hi10 1080p BD Dual Audio -KS-).mkv",
         "Kuroko's Basketball - S01E22.5 - Tip Off (Hi10 1080p BD Dual Audio -KS-).mkv"),
        ("[Datte13] Grimgar of Fantasy and Ash - S01E01 - Whisper, Chant, Prayer, Awaken [A].mkv",
         "[Datte13] Grimgar of Fantasy and Ash - S00E01 - Staking Our Youths On The Bath Wall [B].mkv"),
        ("[CloverKnights] Black Clover 008 (BD 1080p HEVC Opus) [Dual Audio] [0C39B91D].mkv",
         "[CloverKnights] Black Clover 017 (BD 1080p HEVC Opus) [Dual Audio] [BC757F44].mkv"),
        ("[sam] Kuroshitsuji - 02 [BD 1080p FLAC] [DD91C1B1].mkv",
         "[sam] Kuroshitsuji - OVA (S00E02) [BD 1080p FLAC] [442919EC].mkv"),
        ("[INDEX] Blood Blockade Battlefront - S01E01 [TV Ver.] [F3CE2CD1].mkv",
         "[INDEX] Blood Blockade Battlefront - Re-Cap [377FFBE2].mkv"),
        ("Show.S01E05.1080p.WEB.mkv", "Show.S02E05.1080p.WEB.mkv"),
        ("Show - S01E22 (1080p WEB).mkv", "Show - S01E22.5 - Extra (1080p WEB).mkv"),
    ],
)
def test_different_episodes_are_not_the_same_target(existing, incoming):
    assert same_release_target(existing, incoming, "tv") is False
    assert same_release_target(incoming, existing, "tv") is False


@pytest.mark.parametrize(
    "existing, incoming",
    [
        ("[SCY] Death Parade - 01 (BD 1080p Hi10 FLAC) [AA368FD2].mkv",
         "[SubsPlease] Death Parade - 01 (1080p) [0A1B2C3D].mkv"),
        ("[SCY] Death Parade - 01 (BD 1080p Hi10 FLAC) [AA368FD2].mkv",
         "Death.Parade.S01E01.1080p.BluRay.x265.mkv"),
        ("Show.S02E05.1080p.WEB.x264.mkv", "Show.S02E05.1080p.BluRay.x265.mkv"),
        ("Show - S00E03 - Extra (1080p).mkv", "Show.S00E03.1080p.BluRay.mkv"),
    ],
)
def test_same_episode_from_another_release_is_the_same_target(existing, incoming):
    assert same_release_target(existing, incoming, "tv") is True


def test_film_and_its_extra_are_not_the_same_target():
    film = "[DB]Cowboy Bebop Knockin' on Heaven's Door _-_(Dual Audio_10bit_BD1080p_x265).mkv"
    extra = "[DB]Cowboy Bebop Ein's Summer Vacation_-_SP_(10bit_BD1080p_x265).mkv"
    assert same_release_target(film, extra, "movie") is False
    assert same_release_target(extra, film, "movie") is False


def test_two_releases_of_one_film_are_the_same_target():
    assert same_release_target(
        "Spirited.Away.2001.1080p.BluRay.x265.mkv",
        "Spirited Away (2001) [1080p x265 HEVC 10bit BluRay Dual Audio AAC] [Prof].mkv",
        "movie",
    ) is True


def test_unreadable_names_are_never_the_same_target():
    assert same_release_target("", "Show.S01E01.mkv", "tv") is False
    assert same_release_target("random file.mkv", "Show.S01E01.mkv", "tv") is False


# Replace Mode

class _Settings:
    replace_mode = True
    duplicate_protection = True


@pytest.fixture
def replace_db(monkeypatch):
    monkeypatch.setattr(database_module.SettingsManager, "current", staticmethod(lambda: _Settings()))
    db = Database.__new__(Database)
    deleted = []

    async def record(quality):
        deleted.append(quality["name"])

    db._queue_quality_deletion = record
    return db, deleted


def _quality(name, size, quality="1080p"):
    return {"quality": quality, "id": f"id-{name}", "name": name, "size": size}


async def test_replace_mode_keeps_a_different_episode(replace_db):
    db, deleted = replace_db
    episode_ten = _quality("Kuroko's Basketball - S01E10 (Hi10 1080p BD Dual Audio -KS-).mkv", "1.10GB")
    ova = _quality("Kuroko's Basketball - S01E22.5 - Tip Off (Hi10 1080p BD Dual Audio -KS-).mkv", "1015.57MB")
    result = await db._apply_quality_update([episode_ten], ova, media_type="tv")
    assert deleted == []
    assert [q["name"] for q in result] == [episode_ten["name"], ova["name"]]


async def test_replace_mode_keeps_a_film_when_its_extra_arrives(replace_db):
    db, deleted = replace_db
    film = _quality("[DB]Cowboy Bebop Knockin' on Heaven's Door _-_(Dual Audio_10bit_BD1080p_x265).mkv", "3.15GB")
    extra = _quality("[DB]Cowboy Bebop Ein's Summer Vacation_-_SP_(10bit_BD1080p_x265).mkv", "18.48MB")
    result = await db._apply_quality_update([film], extra, media_type="movie")
    assert deleted == []
    assert len(result) == 2


async def test_replace_mode_still_replaces_the_same_episode(replace_db):
    db, deleted = replace_db
    old = _quality("Show.S01E05.1080p.BluRay.x265.FLAC.mkv", "1.50GB")
    new = _quality("Show.S01E05.1080p.BluRay.x265.FLAC.REPACK.mkv", "1.20GB")
    result = await db._apply_quality_update([old], new, media_type="tv")
    assert deleted == [old["name"]]
    assert [q["name"] for q in result] == [new["name"]]


async def test_replace_mode_leaves_other_qualities_alone(replace_db):
    db, deleted = replace_db
    low = _quality("Show.S01E05.720p.WEB.x264.mkv", "500MB", quality="720p")
    old = _quality("Show.S01E05.1080p.BluRay.x265.FLAC.mkv", "1.50GB")
    new = _quality("Show.S01E05.1080p.BluRay.x265.FLAC.REPACK.mkv", "1.20GB")
    result = await db._apply_quality_update([low, old], new, media_type="tv")
    assert deleted == [old["name"]]
    assert [q["name"] for q in result] == [low["name"], new["name"]]
