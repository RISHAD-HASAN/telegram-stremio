"""Title matching tests: unrelated titles, remakes with a year tag, films
that Kitsu types as specials, and shortened release names."""
import os

os.environ.setdefault("DATABASE", "mongodb://localhost:27017/db1,mongodb://localhost:27017/db2")

import pytest

from Backend.helper.metadata import common
from Backend.helper.metadata.common import score_candidate_aliases, title_similarity
from Backend.helper.metadata.providers import kitsu, tmdb, tvdb


# Title similarity

@pytest.mark.parametrize(
    "query, candidate",
    [
        ("Watari-kun", "Awatar"),
        ("Watari-kun", "Avatar"),
        ("Ocean Waves", "The Wave of Rage"),
    ],
)
def test_titles_without_a_common_word_never_reach_a_threshold(query, candidate):
    lowest = min(common.KITSU_THRESHOLD, common.TVDB_THRESHOLD, common.TMDB_THRESHOLD, common.CINEMETA_THRESHOLD)
    assert title_similarity(query, candidate) < lowest


@pytest.mark.parametrize(
    "query, candidate",
    [
        ("Mushishi", "Mushi-Shi"),
        ("Re:Zero", "ReZero"),
        ("Jujutsu Kaisen", "Jujutsukaisen"),
        ("Freiren", "Frieren"),
        ("Steins Gate", "Steins;Gate"),
    ],
)
def test_spelling_variants_of_one_title_still_match(query, candidate):
    assert title_similarity(query, candidate) >= 0.80


# TVDB search

TVDB_ROWS = {
    "Watari-kun": [
        {"name": "Avatar: The Last Airbender", "tvdb_id": "74852", "year": "2005",
         "slug": "avatar-the-last-airbender",
         "translations": {"eng": "Avatar: The Last Airbender", "pol": "Awatar: Legenda Aanga"}},
    ],
    "Devil May Cry": [
        {"name": "Devil May Cry (2025)", "tvdb_id": "440193", "year": "2025",
         "slug": "devil-may-cry-2025", "translations": {"eng": "Devil May Cry"}},
        {"name": "デビルメイクライ", "tvdb_id": "80224", "year": "2007",
         "slug": "devil-may-cry", "translations": {"eng": "Devil May Cry", "jpn": "デビルメイクライ"}},
    ],
}


@pytest.fixture
def tvdb_world(monkeypatch):
    async def fake_get(path, params=None):
        return {"data": TVDB_ROWS.get((params or {}).get("query"), [])}

    async def no_extended(tvdb_id):
        return None

    monkeypatch.setattr(tvdb, "_get", fake_get)
    monkeypatch.setattr(tvdb, "tvdb_api_key", lambda: "key")
    monkeypatch.setattr(tvdb, "series_extended", no_extended)
    tvdb.TVDB_CACHE.clear()
    yield
    tvdb.TVDB_CACHE.clear()


async def test_tvdb_rejects_a_show_that_only_shares_letters(tvdb_world):
    assert await tvdb.search("Watari-kun", None, "series") is None


async def test_tvdb_year_in_the_file_name_picks_the_remake(tvdb_world):
    hit = await tvdb.search("Devil May Cry", 2025, "series")
    assert hit["tvdb_id"] == "440193"


def test_every_alias_is_scored_not_only_the_first_strong_one():
    remake = score_candidate_aliases(
        "Devil May Cry", 2025, "Devil May Cry (2025)", 2025,
        aliases=["Devil May Cry"], year_reliable=False, year_lower_bound=True,
    )
    older = score_candidate_aliases(
        "Devil May Cry", 2025, "デビルメイクライ", 2007,
        aliases=["Devil May Cry"], year_reliable=False, year_lower_bound=True,
    )
    assert remake > older


def test_year_tag_stays_part_of_the_name_when_the_file_has_no_year():
    plain = common.score_candidate("Fruits Basket", None, "Fruits Basket", 0, year_reliable=False, year_lower_bound=True)
    tagged = common.score_candidate("Fruits Basket", None, "Fruits Basket (2019)", 0, year_reliable=False, year_lower_bound=True)
    assert plain > tagged


# Kitsu search

def _row(kitsu_id, canonical, subtype, titles, abbreviated=(), start="2012-01-01"):
    return {"id": str(kitsu_id), "attributes": {
        "canonicalTitle": canonical, "subtype": subtype, "titles": titles,
        "abbreviatedTitles": list(abbreviated), "startDate": start,
        "slug": canonical.lower().replace(" ", "-"),
    }}


SOLITON = _row(6315, "Soliton no Akuma", "movie", {"en_jp": "Soliton no Akuma"},
               ["Soriton no Akuma", "The Wave of Rage"], "1997-01-01")
OCEAN_WAVES = _row(666, "Umi ga Kikoeru", "special", {"en": "The Ocean Waves", "en_jp": "Umi ga Kikoeru"},
                   ["I Can Hear the Sea", "Ocean Waves"], "1993-05-05")
SG_MOVIE = _row(6539, "Steins;Gate Movie: Fuka Ryouiki no Déjà vu", "movie",
                {"en": "Steins;Gate: The Movie − Load Region of Déjà Vu",
                 "en_jp": "Steins;Gate Movie: Fuka Ryouiki no Déjà vu"}, ["Steins Gate Movie"], "2013-04-20")
SG_SPECIAL = _row(6389, "Steins;Gate: Egoistic Poriomania", "special",
                  {"en": "Steins;Gate: Egoistic Poriomania", "en_jp": "Steins;Gate: Oukoubakko no Poriomania"},
                  ["Steins,Gate Special"], "2012-02-22")
WATARI = _row(47963, "Watari-kun no xx ga Houkai Sunzen", "TV",
              {"en": "Watari-kun's ****** Is about to Collapse", "en_jp": "Watari-kun no xx ga Houkai Sunzen"},
              ["Watari-kun's ****** Is about to Collapse"], "2025-07-04")


@pytest.fixture
def kitsu_world(monkeypatch):
    world = {"movie": {}, "any": {}, "docs": {}}

    async def fake_search(query, subtype=None, year=None):
        if year:
            return []
        return world["movie" if subtype == "movie" else "any"].get(query, [])

    async def fake_mappings(kitsu_id):
        return world["docs"].get(int(kitsu_id))

    async def same(payload, *a, **kw):
        return payload

    monkeypatch.setattr(kitsu, "_kitsu_search", fake_search)
    monkeypatch.setattr(kitsu, "get_anizip_mappings", fake_mappings)
    monkeypatch.setattr(kitsu, "_confirm_movie_tmdb_id", same)
    kitsu.KITSU_CACHE.clear()
    yield world
    kitsu.KITSU_CACHE.clear()


async def test_film_search_finds_a_release_kitsu_files_as_a_special(kitsu_world):
    kitsu_world["movie"] = {"Ocean Waves": [SOLITON]}
    kitsu_world["any"] = {"Ocean Waves": [OCEAN_WAVES]}
    kitsu_world["docs"] = {666: {"mappings": {"imdb_id": "tt0108432", "themoviedb_id": "21057"}}}
    payload = await kitsu.fetch_anime_movie("Ocean Waves", "enc", quality="1080p")
    assert payload["kitsu_id"] == 666
    assert payload["imdb_id"] == "tt0108432"


async def test_special_is_not_filed_under_the_franchise_film(kitsu_world):
    name = "Steins Gate Egoistic Poriomania"
    kitsu_world["movie"] = {name: [SG_MOVIE]}
    kitsu_world["any"] = {name: [SG_SPECIAL]}
    payload = await kitsu.fetch_anime_movie(name, "enc", quality="1080p")
    assert payload["kitsu_id"] == 6389


async def test_franchise_name_alone_is_not_a_film_match(kitsu_world):
    name = "Steins Gate Egoistic Poriomania"
    kitsu_world["movie"] = {name: [SG_MOVIE]}
    assert await kitsu.fetch_anime_movie(name, "enc", quality="1080p") is None


async def test_confident_film_match_is_kept(kitsu_world):
    name = "Steins;Gate Load Region of Deja Vu"
    kitsu_world["movie"] = {name: [SG_MOVIE]}
    kitsu_world["any"] = {name: [SG_MOVIE, SG_SPECIAL]}
    payload = await kitsu.fetch_anime_movie(name, "enc", quality="1080p")
    assert payload["kitsu_id"] == 6539


async def test_series_named_by_its_leading_words(kitsu_world):
    kitsu_world["any"] = {"Watari-kun": [WATARI]}
    row = await kitsu.search_anime("Watari-kun")
    assert row["id"] == "47963"


async def test_leading_words_need_a_single_candidate(kitsu_world):
    name = "Watari-kun to Yukai na Nakama-tachi no Daibouken"
    other = _row(1, name, "TV", {"en_jp": name})
    kitsu_world["any"] = {"Watari-kun": [WATARI, other]}
    assert await kitsu.search_anime("Watari-kun") is None


async def test_one_short_word_is_not_a_leading_words_match(kitsu_world):
    fate = _row(2, "Fate/stay night", "TV", {"en_jp": "Fate/stay night"})
    kitsu_world["any"] = {"Fate": [fate]}
    assert await kitsu.search_anime("Fate") is None


# TMDB

async def test_tmdb_is_not_asked_about_made_up_ids(monkeypatch):
    def fail():
        raise AssertionError("TMDB was called")

    monkeypatch.setattr(tmdb, "tmdb_api_key", lambda: "key")
    monkeypatch.setattr(tmdb, "get_tmdb_client", fail)
    assert await tmdb.details("tv", -12345) is None
