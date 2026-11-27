"""Episode resolution tests.

  * uploads without a show title, resolved through the episode title
  * "N.5" specials
  * later seasons that Kitsu keeps as separate entries

Provider data is trimmed from ani.zip / Kitsu responses; no network access.
"""
import os

os.environ.setdefault("DATABASE", "mongodb://localhost:27017/db1,mongodb://localhost:27017/db2")

import pytest

from Backend.helper.metadata import entry, resolvers
from Backend.helper.metadata.parse import (
    episode_titles_match,
    extract_episode_title,
    half_episode_position,
    special_title_score,
    titled_special,
)
from Backend.helper.metadata import episode_maps
from Backend.helper.metadata.episode_maps import AnimeListEntry
from Backend.helper.metadata.plan import SKIP_EXTRA, SKIP_TITLE_MISSING, SKIP_UNNAMED_EXTRA, plan_media
from Backend.helper.metadata.providers import cinemeta, episode_lookup, kitsu
from Backend.helper.name_cleaning import clean_filename


def _plan(caption, **kw):
    return plan_media(clean_filename(caption), anime_channel=True, **kw)


# --------------------------------------------------------------------------
# Episode title written in the file name
# --------------------------------------------------------------------------

@pytest.mark.parametrize("caption, expected", [
    ("S01E01 - The Green Seat.mkv", "The Green Seat"),
    ("S01E25 - Eye of Fortune, Eye of Misfortune.mkv", "Eye of Fortune, Eye of Misfortune"),
    ("S01E12 - One-Eyed Fish.mkv", "One-Eyed Fish"),
    ("Kuroko's Basketball - S01E22.5 - Tip Off (Hi10 1080p BD Dual Audio -KS-).mkv", "Tip Off"),
    ("[Dae-P9] Link Click - 05.5 - S00E01 - Marrying by Contesting (BD 1080p HEVC FLAC AAC) [Dual Audio] [5BCD56B8].mkv",
     "Marrying by Contesting"),
    ("[Raze] Hells Paradise S02E05 Humans and Sages [Dual-Audio].mkv", "Humans and Sages"),
    ("Charlotte.S00E02.Strong.People.1080p.BluRay.Opus2.0.x265-smol.mkv", "Strong People"),
    ("Bleach (2004) - S14E01 - 266 - Ichigo vs. Ulquiorra, Resume! [DVD][DUAL][480i MPEG2].mkv",
     "Ichigo vs. Ulquiorra, Resume!"),
    ("[Raze] Lord of Mysteries Specials S00E02 The Marked Hunt Part 1 [Dual-Audio].mkv", "The Marked Hunt Part 1"),
    ("Clannad.2007.S00E02.Episode.24.OVA.Another.World.Tomoyo.Chapter.1080p.BluRay.x265.mkv",
     "Another World Tomoyo Chapter"),
    # No episode title in the name: release tags must not be mistaken for one
    ("Mob.Psycho.100.S00E08.1080p.BluRay.Dual-Audio.Opus.x265-BlackRose.mkv", None),
    ("[Judas] Solo Leveling - S02E03.mkv", None),
    ("[DeeVeeDee] Baccano - S00E02 - Episode 15 - OVA 2 [DVD 480p HEVC Opus].mkv", None),
])
def test_extract_episode_title(caption, expected):
    assert extract_episode_title(clean_filename(caption)) == expected


def test_episode_titles_match():
    assert episode_titles_match("Tip Off", "Tip-off") == 1.0
    assert episode_titles_match("I'm Harvesting Breasts!", "I`m Harvesting Breasts!") == 1.0
    assert episode_titles_match("The Marked Hunt Part 1", "The Marked Hunt I") >= 0.85
    # Two parts of one story are different episodes
    assert episode_titles_match("The Marked Hunt Part 1", "The Marked Hunt Part 2") < 0.85
    assert episode_titles_match("The Green Seat", "The Sleeping Mountain") < 0.5
    assert episode_titles_match("The Green Seat", "") == 0.0


# --------------------------------------------------------------------------
# "N.5" episodes: an OVA / special placed after episode N
# --------------------------------------------------------------------------

def test_half_episode_position():
    kuroko = clean_filename("Kuroko's Basketball - S01E22.5 - Tip Off (Hi10 1080p BD Dual Audio -KS-).mkv")
    assert half_episode_position(kuroko) == {"season": 1, "episode": 22, "numbered": False}
    link = clean_filename("[Dae-P9] Link Click - 05.5 - S00E01 - Marrying by Contesting (BD 1080p) [5BCD56B8].mkv")
    assert half_episode_position(link) == {"season": None, "episode": 5, "numbered": True}
    assert half_episode_position(clean_filename("[Judas] Solo Leveling - S02E03.mkv")) is None
    # A decimal inside a title is not a half episode
    assert half_episode_position(clean_filename("Evangelion 3.0+1.0 Thrice Upon a Time [1080p].mkv")) is None


def test_half_episode_plan_is_a_special_with_watch_after():
    plan = _plan("Kuroko's Basketball - S01E22.5 - Tip Off (Hi10 1080p BD Dual Audio -KS-).mkv")
    assert (plan["title"], plan["season"], plan["kind"]) == ("Kuroko's Basketball", 0, "tv")
    assert plan["episode_title"] == "Tip Off"
    assert plan["watch_after"] == {"season": 1, "episode": 22, "numbered": False}

    plan = _plan("[Dae-P9] Link Click - 05.5 - S00E01 - Marrying by Contesting (BD 1080p HEVC) [5BCD56B8].mkv")
    assert (plan["title"], plan["season"], plan["episode"]) == ("Link Click", 0, 1)
    assert plan["watch_after"]["episode"] == 5


# ani.zip data for a 25-episode show with specials. The OVA "Tip-off" is
# TVDB S00E10 and aired after episode 22.
KUROKO = {
    "22": {"seasonNumber": 1, "episodeNumber": 22, "absoluteEpisodeNumber": 22,
           "title": {"en": "I'll Win Even If It Kills Me"}},
    "S1": {"seasonNumber": 0, "episodeNumber": 10, "absoluteEpisodeNumber": 23, "title": {"en": "Tip-off"}},
    "S2": {"seasonNumber": 0, "episodeNumber": 1, "absoluteEpisodeNumber": None, "title": {"en": "NG-shuu 1"}},
    "S3": {"seasonNumber": 0, "episodeNumber": 22, "absoluteEpisodeNumber": None, "title": {"en": "NG-shuu 9"}},
}


def test_special_is_found_by_its_title_not_by_the_file_number():
    # "S01E22.5" is the special, not regular episode 22
    ep, how = kitsu._find_anizip_special(KUROKO, 22, "Tip Off", {"season": 1, "episode": 22, "numbered": False})
    assert (ep["title"]["en"], ep["episodeNumber"], how) == ("Tip-off", 10, "title")


def test_special_is_found_by_watch_position_without_a_title():
    ep, how = kitsu._find_anizip_special(KUROKO, 22, None, {"season": 1, "episode": 22, "numbered": False})
    assert ep["title"]["en"] == "Tip-off"
    assert how != "title"


def test_special_by_number_and_never_a_regular_episode():
    ep, how = kitsu._find_anizip_special(KUROKO, 1, None, None)
    assert (ep["title"]["en"], how) == ("NG-shuu 1", "number")
    # unknown season 0 number: no entry rather than episode 22
    ep, how = kitsu._find_anizip_special({"22": KUROKO["22"]}, 22, "Tip Off", None)
    assert ep == {}


def test_special_with_same_number_but_a_different_title_is_rejected():
    # The provider's S00E02 is a different special than the file's S00E02.
    specials = {
        "S9": {"seasonNumber": 0, "episodeNumber": 2, "title": {"en": "Chibi Theater: Zhou Mingrui`s Unlucky Day"}},
        "S19": {"seasonNumber": 0, "episodeNumber": 19, "absoluteEpisodeNumber": 15, "title": {"en": "The Marked Hunt I"}},
    }
    ep, how = kitsu._find_anizip_special(specials, 2, "The Marked Hunt Part 1", None)
    assert (ep["episodeNumber"], how) == (19, "title")
    ep, how = kitsu._find_anizip_special(specials, 2, "Some Other Story Entirely", None)
    assert ep == {}


# --------------------------------------------------------------------------
# Season >= 2: the right episode, not episode N of season 1
# --------------------------------------------------------------------------

def _row(kid, title, subtype="TV", start="2023-04-01", **titles):
    return {"id": str(kid), "attributes": {
        "canonicalTitle": title, "subtype": subtype, "startDate": start,
        "titles": titles or {"en": title}, "abbreviatedTitles": [],
    }}


def _doc(tvdb, episodes, tmdb=117465):
    return {"mappings": {"thetvdb_id": tvdb, "themoviedb_id": tmdb, "imdb_id": f"tt{tmdb}"}, "episodes": episodes}


HELL_S1 = _doc(402474, {
    "5": {"seasonNumber": 1, "episodeNumber": 5, "absoluteEpisodeNumber": 5, "title": {"en": "The Samurai and the Woman"}},
    "1": {"seasonNumber": 1, "episodeNumber": 1, "absoluteEpisodeNumber": 1, "title": {"en": "The Death Row Convict and the Executioner"}},
})
HELL_S2 = _doc(402474, {
    "1": {"seasonNumber": 2, "episodeNumber": 1, "absoluteEpisodeNumber": 14, "title": {"en": "Dawn and Confusion"}},
    "5": {"seasonNumber": 2, "episodeNumber": 5, "absoluteEpisodeNumber": 18, "title": {"en": "Humans and Sages"}},
})
OTHER_SHOW_S2 = _doc(999999, {
    "5": {"seasonNumber": 2, "episodeNumber": 5, "title": {"en": "An Unrelated Episode"}},
}, tmdb=888888)


@pytest.fixture
def kitsu_world(monkeypatch):
    """Fake Kitsu, ani.zip and Anime-Lists; episode/series enrichment is passed through."""
    world = {"search": {}, "docs": {}, "episode_meta_calls": []}

    async def fake_search(query, subtype=None, year=None):
        if year:
            return world.get("by_year", {}).get((query, year), [])
        return world["search"].get(query, [])

    async def fake_mappings(kitsu_id=None, anidb_id=None):
        if kitsu_id is None:
            return world.get("anidb_docs", {}).get(int(anidb_id))
        return world["docs"].get(int(kitsu_id))

    async def fake_anime(kitsu_id):
        rows = [r for found in world["search"].values() for r in found]
        return next((r for r in rows if r["id"] == str(kitsu_id)), None)

    async def lists_loaded():
        return episode_maps._anime_lists

    async def no_anibridge():
        return {}

    async def passthrough(payload, *a, **kw):
        ep = kw.get("anizip_ep") or {}
        title = (ep.get("title") or {}).get("en")
        payload["episode_title"] = title or f"S{kw['season_number']:02d}E{kw['episode_number']:02d}" \
            if "season_number" in kw else payload.get("episode_title")
        world["episode_meta_calls"].append(kw)
        return payload

    async def same(payload, *a, **kw):
        return payload

    monkeypatch.setattr(kitsu, "_kitsu_search", fake_search)
    monkeypatch.setattr(kitsu, "get_anizip_mappings", fake_mappings)
    monkeypatch.setattr(kitsu, "_kitsu_anime", fake_anime)
    monkeypatch.setattr(episode_maps, "_anime_lists", {})
    monkeypatch.setattr(episode_maps, "ensure_anime_lists", lists_loaded)
    monkeypatch.setattr(episode_maps, "ensure_anibridge", no_anibridge)
    monkeypatch.setattr(kitsu, "_resolve_episode_meta", passthrough)
    monkeypatch.setattr(kitsu, "_resolve_series_description", same)
    monkeypatch.setattr(kitsu, "_resolve_series_art", same)
    kitsu.KITSU_CACHE.clear()
    yield world
    kitsu.KITSU_CACHE.clear()


async def test_later_season_uses_that_seasons_own_entry(kitsu_world):
    kitsu_world["search"] = {
        "Hell's Paradise": [_row(44012, "Jigokuraku", en="Hell's Paradise")],
        "Hell's Paradise Season 2": [_row(47679, "Jigokuraku 2nd Season", en="Hell's Paradise Season 2")],
    }
    kitsu_world["docs"] = {44012: HELL_S1, 47679: HELL_S2}

    s2 = await kitsu.fetch_anime_tv("Hell's Paradise", 2, 5, "enc")
    # not season 1 episode 5 ("The Samurai and the Woman")
    assert s2["episode_title"] == "Humans and Sages"
    assert (s2["season_number"], s2["episode_number"]) == (2, 5)
    # same show as season 1
    s1 = await kitsu.fetch_anime_tv("Hell's Paradise", 1, 5, "enc")
    assert s1["episode_title"] == "The Samurai and the Woman"
    assert s1["kitsu_id"] == s2["kitsu_id"] and s1["tvdb_id"] == s2["tvdb_id"]
    assert "weak_match" not in s2


async def test_later_season_never_takes_episodes_from_a_different_show(kitsu_world):
    kitsu_world["search"] = {
        "Hell's Paradise": [_row(44012, "Jigokuraku", en="Hell's Paradise")],
        "Hell's Paradise Season 2": [_row(5, "Hell's Paradise Season 2")],
    }
    kitsu_world["docs"] = {44012: HELL_S1, 5: OTHER_SHOW_S2}
    s2 = await kitsu.fetch_anime_tv("Hell's Paradise", 2, 5, "enc")
    assert s2["episode_title"] == "S02E05"
    assert s2["weak_match"]


async def test_one_entry_with_many_seasons_matches_season_and_episode(kitsu_world):
    # one 366-episode entry: S14E01 is absolute 266
    bleach = _doc(74796, {
        "1": {"seasonNumber": 1, "episodeNumber": 1, "absoluteEpisodeNumber": 1, "title": {"en": "A Shinigami is Born!"}},
        "266": {"seasonNumber": 14, "episodeNumber": 1, "absoluteEpisodeNumber": 266,
                "title": {"en": "Ichigo Vs Ulquiorra, Restart!"}},
    })
    kitsu_world["search"] = {"Bleach": [_row(244, "Bleach", start="2004-10-05")]}
    kitsu_world["docs"] = {244: bleach}
    result = await kitsu.fetch_anime_tv("Bleach", 14, 1, "enc")
    assert result["episode_title"] == "Ichigo Vs Ulquiorra, Restart!"
    assert (result["season_number"], result["episode_number"]) == (14, 1)


async def test_year_in_the_name_picks_the_remake(kitsu_world):
    kitsu_world["search"] = {"Fruits Basket": [
        _row(99, "Fruits Basket", start="2001-07-05"),
        _row(41995, "Fruits Basket (2019)", start="2019-04-06"),
    ]}
    assert (await kitsu.search_anime("Fruits Basket"))["id"] == "99"
    assert (await kitsu.search_anime("Fruits Basket", year=2019))["id"] == "41995"
    assert (await kitsu.search_anime("Fruits Basket", year=2001))["id"] == "99"


async def test_year_in_the_name_finds_an_entry_missing_from_the_first_page(kitsu_world):
    old_series = _row(429, "Ghost in the Shell: Stand Alone Complex", start="2002-10-01")
    new_series = _row(48899, "Koukaku Kidoutai: THE GHOST IN THE SHELL", start="2026-07-07",
                      en="THE GHOST IN THE SHELL")
    kitsu_world["search"] = {"THE GHOST IN THE SHELL": [old_series]}
    kitsu_world["by_year"] = {("THE GHOST IN THE SHELL", 2026): [new_series]}
    assert (await kitsu.search_anime("THE GHOST IN THE SHELL"))["id"] == "429"
    assert (await kitsu.search_anime("THE GHOST IN THE SHELL", year=2026))["id"] == "48899"
    # a year no entry starts in changes nothing
    assert (await kitsu.search_anime("THE GHOST IN THE SHELL", year=2010))["id"] == "429"


# --------------------------------------------------------------------------
# Disc extras: creditless openings, and an OVA numbered after the season
# --------------------------------------------------------------------------

@pytest.mark.parametrize("caption", [
    "[Grp] Tonari no Example - NCED [BD 1080p FLAC] [60053D0E].mkv",
    "[Grp] Tonari no Example - NCOP1 [BD 1080p].mkv",
    "[Grp] Tonari no Example - NCED 02v2 (BD 1080p).mkv",
    "Tonari.no.Example.NCOP.01a.1080p.mkv",
    "[Grp] Tonari no Example - Creditless Opening 2 [1080p].mkv",
    "ED 01.mkv",
    "OP 2.mkv",
    "[Grp] OP [BD 1080p].mkv",
])
def test_creditless_openings_and_endings_are_not_indexed(caption):
    assert _plan(caption)["skip"] == SKIP_EXTRA


@pytest.mark.parametrize("caption", [
    "[Grp] Tonari no Example - 01 [BD 1080p].mkv",
    "Tonari no Example S01E05 - A Clean Ending [1080p].mkv",
    "[Grp] Enced - 03 [1080p].mkv",
    "[Grp] Eden - 01 [1080p].mkv",
    "Opus - 02.mkv",
    "[Grp] Tonari no Example Movie 01 - Two Examples.mkv",
    "[Grp] Tonari no Example the Movie [1080p].mkv",
])
def test_ordinary_episodes_are_not_taken_for_extras(caption):
    assert _plan(caption)["skip"] is None


def test_unnamed_extra_of_a_film_is_not_indexed_as_the_film():
    assert _plan("[Grp] Tonari no Example Movie OVA.mkv")["skip"] == SKIP_UNNAMED_EXTRA
    assert _plan("[Grp] Tonari no Example the Movie Special [BD 1080p].mkv")["skip"] == SKIP_UNNAMED_EXTRA


# --------------------------------------------------------------------------
# "Show OVA - Episode Title": found by title in the show's season 0 list
# --------------------------------------------------------------------------

@pytest.mark.parametrize("caption, expected", [
    ("[Grp] Hero Example OVA - Rescue! Rescue Training!.mkv", ("Hero Example", "Rescue! Rescue Training!")),
    ("[Grp] Hero Example OVA - HLB (Hero League Baseball) [BD 1080p].mkv", ("Hero Example", "HLB (Hero League Baseball)")),
    ("[Grp] Hero Example - OVA - Survival Training, Part 2 (BD 1080p).mkv", ("Hero Example", "Survival Training, Part 2")),
    ("Hero.Example.OAD: The Lost Day.mkv", ("Hero.Example", "The Lost Day")),
    ("[Grp] Hero Example OVA - 02 [1080p].mkv", None),
    ("[Grp] Hero Example OVA 2 - The Lost Day [1080p].mkv", None),
    ("[Grp] Hero Example - 05 [1080p].mkv", None),
    ("[Grp] Hero Example OVA [1080p].mkv", None),
])
def test_titled_special_is_split_into_show_and_title(caption, expected):
    assert titled_special(caption) == expected


def test_titled_special_plan_keeps_a_part_number_in_the_title():
    plan = _plan("[Grp] Hero Example OVA - Survival Training, Part 2.mkv")
    assert (plan["kind"], plan["season"]) == ("tv", 0)
    assert (plan["special_show"], plan["special_title"]) == ("Hero Example", "Survival Training, Part 2")
    # a numbered special is not looked up by title
    assert _plan("[Grp] Hero Example - OVA 2 [1080p].mkv")["special_title"] is None


@pytest.mark.parametrize("file_title, provider_title, fits", [
    ("Rescue! Rescue Training!", "Save! Rescue Training!", True),
    ("All Might Rising The Animation", "All Might: Rising", True),
    ("HLB (Hero League Baseball)", "HLB - Hero League of Baseball", True),
    ("Laugh! As If You Are In Hell", "Laugh! As If You Are in Hell", True),
    ("Two Examples", "Hero Example: The Two Examples", True),
    ("Ilse's Notebook", "Ilse's Notebook: Memoirs of a Scout Regiment Member", True),
    ("Survival Training, Part 1", "Survival Training, Part 1", True),
    ("Survival Training, Part 2", "Survival Training, Part 1", False),
    ("Training of the Dead", "Save! Rescue Training!", False),
    ("Rescue! Rescue Training!", "Hero Notebook", False),
])
def test_special_title_fits_the_provider_name(file_title, provider_title, fits):
    score = special_title_score(file_title, provider_title, "Hero Example")
    assert (score >= resolvers.SPECIAL_TITLE_MIN) is fits


SEASON_ZERO = [
    (1, "Save! Rescue Training!"),
    (2, "Hero Notebook"),
    (3, "Training of the Dead"),
    (5, "All Might: Rising"),
    (7, "Hero Example: Heroes Rising"),
    (8, "Survival Training, Part 1"),
    (9, "Survival Training, Part 2"),
    (10, "Heroes Rising: Epilogue Plus Picture Drama"),
]


@pytest.fixture
def show_with_named_specials(monkeypatch):
    """One show whose season 0 list is known; counts the lookups made."""
    calls = []

    async def fake_tv(title, season, episode, encoded_string, **kw):
        calls.append((title, season, episode))
        if title != "Hero Example":
            return None
        return {"title": "Hero Example", "media_type": "tv", "imdb_id": "tt5600000", "season_number": season,
                "episode_number": episode, "episode_title": "An Interview", "quality": kw.get("quality")}

    async def fake_specials(imdb_id):
        return SEASON_ZERO if imdb_id == "tt5600000" else []

    async def fake_encode(_data):
        return "enc"

    monkeypatch.setattr(kitsu, "fetch_anime_tv", fake_tv)
    monkeypatch.setattr(cinemeta, "season_zero_titles", fake_specials)
    monkeypatch.setattr(entry, "encode_string", fake_encode)
    monkeypatch.setattr(entry, "_is_anime_channel", lambda channel: True)
    return calls


@pytest.mark.parametrize("title, slot, name", [
    ("Rescue! Rescue Training!", 1, "Save! Rescue Training!"),
    ("Training of the Dead!", 3, "Training of the Dead"),
    ("All Might Rising The Animation", 5, "All Might: Rising"),
    ("Survival Training, Part 2", 9, "Survival Training, Part 2"),
])
async def test_titled_special_takes_the_slot_of_the_matching_name(show_with_named_specials, title, slot, name):
    result = await resolvers.resolve_titled_special("Hero Example", title, "enc")
    assert (result["season_number"], result["episode_number"], result["episode_title"]) == (0, slot, name)


async def test_titled_special_is_left_alone_when_no_single_name_fits(show_with_named_specials):
    film = await resolvers.resolve_titled_special("Hero Example", "Heroes Rising", "enc")
    assert film["episode_number"] == 7
    drama = await resolvers.resolve_titled_special("Hero Example", "Heroes Rising Epilogue Plus", "enc")
    assert drama["episode_number"] == 10
    # one word says too little
    assert await resolvers.resolve_titled_special("Hero Example", "Heroes", "enc") is None
    assert await resolvers.resolve_titled_special("Hero Example", "A Day at the Beach", "enc") is None
    assert await resolvers.resolve_titled_special("Unknown Show", "Training of the Dead", "enc") is None


async def test_two_part_special_is_two_episodes_not_one_split_file(show_with_named_specials):
    first = await entry.metadata("[Grp] Hero Example OVA - Survival Training, Part 1.mkv", 1, 10)
    second = await entry.metadata("[Grp] Hero Example OVA - Survival Training, Part 2.mkv", 1, 11)
    assert (first["season_number"], first["episode_number"]) == (0, 8)
    assert (second["season_number"], second["episode_number"]) == (0, 9)
    assert first["group_key"] is None and second["group_key"] is None
    assert first["part_number"] is None and second["part_number"] is None


def test_ova_numbered_as_an_episode_keeps_its_label():
    plan = _plan("[Grp] Tonari no Example - 13 (SNS OVA) [BD 1080p FLAC] [2099E8BB].mkv")
    assert (plan["title"], plan["episode"], plan["absolute"]) == ("Tonari no Example", 13, True)
    assert plan["special_label"] == "SNS" and plan["episode_title"] == "SNS"
    assert _plan("[Grp] Tonari no Example - 25 (OAD) [BD 1080p].mkv")["special_label"] == ""
    assert _plan("[Grp] Tonari no Example - 13 [BD 1080p FLAC].mkv")["special_label"] is None
    assert _plan("[Grp] Tonari no Example - 07 (BD 1080p HEVC).mkv")["special_label"] is None


@pytest.fixture
def series_with_ova(kitsu_world, monkeypatch):
    """A 12-episode series whose OVA is a separate entry and TVDB S00E14."""
    episodes = {
        str(n): {"seasonNumber": 1, "episodeNumber": n, "absoluteEpisodeNumber": n, "title": {"en": f"Regular {n}"}}
        for n in range(1, 13)
    }
    episodes["S1"] = {"seasonNumber": 0, "episodeNumber": 13, "title": {"en": "Recap"}}
    series = _doc(385000, episodes, tmdb=100000)
    series["mappings"].update(anidb_id=500, kitsu_id=43000)
    ova = {"titles": {"x-jat": "Tonari no Example: SNS"}, "episodes": {
        "1": {"seasonNumber": 0, "episodeNumber": 14, "absoluteEpisodeNumber": 13, "title": {"en": "OVA"}},
    }}
    ids = {"thetvdb_id": 385000, "themoviedb_id": 100000, "imdb_id": "tt100000"}
    web_short = {"mappings": dict(ids, anidb_id=502), "titles": {"x-jat": "Tonari no Example: Seifuku"},
                 "episodes": {"1": {"title": {"en": "Web"}}}}
    school_arc = {"mappings": dict(ids, anidb_id=503), "episodes": {
        str(n): {"seasonNumber": 0, "episodeNumber": 15 + n, "absoluteEpisodeNumber": 26 + n,
                 "title": {"en": f"School {n}"}}
        for n in range(1, 5)
    }}
    kitsu_world["search"] = {
        "Tonari no Example": [_row(43000, "Tonari no Example", start="2020-10-03")],
        "Tonari no Example Seifuku": [_row(45000, "Tonari no Example: Seifuku", subtype="special",
                                           en="Tonari no Example - Uniform")],
        "Tonari no Example Joshikou-hen": [_row(47000, "Tonari no Example: Joshikou-hen", subtype="ONA")],
    }
    kitsu_world["docs"] = {43000: series, 45000: web_short, 47000: school_arc}
    kitsu_world["anidb_docs"] = {500: series, 501: ova, 502: web_short}
    entries = {
        500: AnimeListEntry(anidb_id=500, tvdb_id=385000, tmdb_tv_id=100000, default_tvdb_season="1",
                            tmdb_season="1", name="Tonari no Example"),
        501: AnimeListEntry(anidb_id=501, tvdb_id=385000, tmdb_tv_id=100000, default_tvdb_season="0",
                            tmdb_season="0", episode_offset=13, name="Tonari no Example: SNS"),
        502: AnimeListEntry(anidb_id=502, tvdb_id=385000, tmdb_tv_id=100000, default_tvdb_season="0",
                            tmdb_season="0", episode_offset=14, name="Tonari no Example: Seifuku"),
        503: AnimeListEntry(anidb_id=503, tvdb_id=385000, tmdb_tv_id=100000, default_tvdb_season="0",
                            tmdb_season="0", episode_offset=15, name="Tonari no Example: Joshikou Hen"),
    }

    monkeypatch.setattr(episode_maps, "_anime_lists", entries)
    return kitsu_world


async def test_ova_numbered_after_the_season_is_stored_as_its_special(series_with_ova):
    by_label = await kitsu.fetch_anime_tv("Tonari no Example", None, 13, "enc", absolute=True, special_label="SNS")
    assert (by_label["season_number"], by_label["episode_number"]) == (0, 14)
    assert by_label["absolute_episode"] is None and by_label["tvdb_id"] == 385000
    # no label in the name: TVDB's absolute number decides
    by_number = await kitsu.fetch_anime_tv("Tonari no Example", None, 13, "enc", absolute=True, special_label="")
    assert (by_number["season_number"], by_number["episode_number"]) == (0, 14)


async def test_ova_marker_does_not_move_regular_episodes(series_with_ova):
    result = await kitsu.fetch_anime_tv("Tonari no Example", None, 12, "enc", absolute=True, special_label="SNS")
    assert (result["season_number"], result["episode_number"]) == (1, 12)


async def test_absolute_number_never_takes_a_special_with_the_same_number(series_with_ova):
    result = await kitsu.fetch_anime_tv("Tonari no Example", None, 13, "enc", absolute=True)
    assert (result["season_number"], result["episode_number"]) == (1, 13)
    assert result["episode_title"] != "Recap"


@pytest.fixture
def series_with_numbered_extra(kitsu_world, monkeypatch):
    """A 26-episode series; TVDB lists its extra episode as S00E02 and absolute 27."""
    episodes = {
        str(n): {"seasonNumber": 1, "episodeNumber": n, "absoluteEpisodeNumber": n, "title": {"en": f"Regular {n}"}}
        for n in range(1, 27)
    }
    episodes["S1"] = {"seasonNumber": 0, "episodeNumber": 1, "title": {"en": "Mini Game"}}
    episodes["S2"] = {"seasonNumber": 0, "episodeNumber": 2, "absoluteEpisodeNumber": 27, "title": {"en": "An Extra"}}
    # Clips without any numbering must not count as regular episodes
    for n in (1, 2, 3):
        episodes[f"O{n}"] = {"title": {"en": f"Video Clips {n}"}}
    series = _doc(148000, episodes, tmdb=65000)
    series["mappings"].update(anidb_id=700, kitsu_id=4800)
    kitsu_world["search"] = {"Kaichou no Example": [_row(4800, "Kaichou no Example", start="2010-04-02")]}
    kitsu_world["docs"] = {4800: series}
    entries = {
        700: AnimeListEntry(anidb_id=700, tvdb_id=148000, tmdb_tv_id=65000, default_tvdb_season="1",
                            tmdb_season="1", name="Kaichou no Example"),
    }
    monkeypatch.setattr(episode_maps, "_anime_lists", entries)
    return entries


async def test_extra_that_tvdb_numbers_after_the_season_is_a_special(series_with_numbered_extra):
    result = await kitsu.fetch_anime_tv("Kaichou no Example", None, 27, "enc", absolute=True)
    assert (result["season_number"], result["episode_number"]) == (0, 2)
    assert result["episode_title"] == "An Extra" and result["absolute_episode"] is None
    last = await kitsu.fetch_anime_tv("Kaichou no Example", None, 26, "enc", absolute=True)
    assert (last["season_number"], last["episode_number"]) == (1, 26)


async def test_number_after_the_season_stays_regular_when_a_later_season_exists(series_with_numbered_extra):
    # 27 could be the first episode of season 2 in absolute numbering
    series_with_numbered_extra[701] = AnimeListEntry(
        anidb_id=701, tvdb_id=148000, tmdb_tv_id=65000, default_tvdb_season="2", tmdb_season="2",
        name="Kaichou no Example (2012)")
    result = await kitsu.fetch_anime_tv("Kaichou no Example", None, 27, "enc", absolute=True)
    assert result["season_number"] != 0
    # an OVA marker in the name settles it
    marked = await kitsu.fetch_anime_tv("Kaichou no Example", None, 27, "enc", absolute=True, special_label="")
    assert (marked["season_number"], marked["episode_number"]) == (0, 2)


def _season_doc(season, first_absolute, count, first_episode=1):
    """ani.zip document of one season entry with `count` regular episodes and one extra."""
    episodes = {
        str(n): {"seasonNumber": season, "episodeNumber": first_episode + n - 1,
                 "absoluteEpisodeNumber": first_absolute + n - 1, "title": {"en": f"S{season} Part {n}"}}
        for n in range(1, count + 1)
    }
    episodes["S1"] = {"seasonNumber": 0, "episodeNumber": 9, "title": {"en": "A Bonus"}}
    return _doc(305000, episodes, tmdb=65900)


@pytest.fixture
def series_with_a_season_per_entry(kitsu_world, monkeypatch):
    """Season 1 in two entries (12 + 13), season 2 (12) and season 3 (11), each its own entry."""
    first = _season_doc(1, 1, 12)
    first["mappings"].update(anidb_id=800, kitsu_id=9100)
    kitsu_world["search"] = {"Hero Example": [_row(9100, "Hero Example", start="2016-04-03")]}
    kitsu_world["docs"] = {9100: first}
    kitsu_world["anidb_docs"] = {
        801: _season_doc(1, 13, 13, first_episode=13),
        802: _season_doc(2, 26, 12),
        803: _season_doc(3, 38, 11),
    }

    def entry(anidb_id, season, offset=0):
        return AnimeListEntry(anidb_id=anidb_id, tvdb_id=305000, tmdb_tv_id=65900, default_tvdb_season=season,
                              episode_offset=offset, tmdb_season=season, name="Hero Example")

    entries = {800: entry(800, "1"), 801: entry(801, "1", 12), 802: entry(802, "2"), 803: entry(803, "3"),
               899: entry(899, "0")}
    monkeypatch.setattr(episode_maps, "_anime_lists", entries)
    return kitsu_world


@pytest.mark.parametrize("absolute, expected, title", [
    (12, (1, 12), "S1 Part 12"),
    (26, (2, 1), "S2 Part 1"),
    (37, (2, 12), "S2 Part 12"),
    (38, (3, 1), "S3 Part 1"),
    (48, (3, 11), "S3 Part 11"),
])
async def test_absolute_number_counts_through_the_later_seasons(series_with_a_season_per_entry, absolute, expected, title):
    result = await kitsu.fetch_anime_tv("Hero Example", None, absolute, "enc", absolute=True)
    assert (result["season_number"], result["episode_number"]) == expected
    assert result["episode_title"] == title
    assert result["absolute_episode"] == absolute


async def test_absolute_number_in_the_second_part_of_season_one_stays_there(series_with_a_season_per_entry):
    result = await kitsu.fetch_anime_tv("Hero Example", None, 13, "enc", absolute=True)
    assert (result["season_number"], result["episode_number"]) == (1, 13)
    last = await kitsu.fetch_anime_tv("Hero Example", None, 25, "enc", absolute=True)
    assert (last["season_number"], last["episode_number"]) == (1, 25)


async def test_absolute_number_is_not_guessed_past_an_unknown_season(series_with_a_season_per_entry):
    # past the last known season
    beyond = await kitsu.fetch_anime_tv("Hero Example", None, 49, "enc", absolute=True)
    assert (beyond["season_number"], beyond["episode_number"]) == (1, 49)
    # season 2 has no episode list: season 3 cannot be counted
    del series_with_a_season_per_entry["anidb_docs"][802]
    kitsu.KITSU_CACHE.clear()
    unknown = await kitsu.fetch_anime_tv("Hero Example", None, 38, "enc", absolute=True)
    assert (unknown["season_number"], unknown["episode_number"]) == (1, 38)


@pytest.fixture
def series_with_clips_and_one_special(kitsu_world):
    """12 episodes; season 0 holds three 2-minute clips (1 to 3) and one full episode (4)."""
    episodes = {
        str(n): {"seasonNumber": 1, "episodeNumber": n, "absoluteEpisodeNumber": n, "title": {"en": f"Regular {n}"}}
        for n in range(1, 13)
    }
    episodes["S1"] = {"seasonNumber": 0, "episodeNumber": 4, "runtime": 24, "title": {"en": "The Cat Runs Away"}}
    for n in (1, 2, 3):
        episodes[f"S{n + 1}"] = {"seasonNumber": 0, "episodeNumber": n, "runtime": 2, "title": {"en": f"Bonus Clip {n}"}}
    kitsu_world["search"] = {"Azure Example": [_row(5900, "Azure Example", start="2011-04-17")]}
    kitsu_world["docs"] = {5900: _doc(248000, episodes, tmdb=38000)}


async def test_release_counter_means_the_full_length_special_not_a_clip(series_with_clips_and_one_special):
    # "Azure.Example.SP1": the release ships one special, the full episode
    result = await kitsu.fetch_anime_tv("Azure Example", 0, 1, "enc", release_counter=True)
    assert (result["season_number"], result["episode_number"]) == (0, 4)
    assert result["episode_title"] == "The Cat Runs Away"


async def test_provider_season_zero_number_is_taken_as_written(series_with_clips_and_one_special):
    # "Azure.Example.S00E01" names the provider's own slot, clip or not
    result = await kitsu.fetch_anime_tv("Azure Example", 0, 1, "enc")
    assert (result["season_number"], result["episode_number"]) == (0, 1)
    assert result["episode_title"] == "Bonus Clip 1"


async def test_release_counter_keeps_the_number_when_it_is_a_full_special(kitsu_world):
    episodes = {
        "1": {"seasonNumber": 1, "episodeNumber": 1, "absoluteEpisodeNumber": 1, "title": {"en": "Regular 1"}},
        "S1": {"seasonNumber": 0, "episodeNumber": 2, "runtime": 24, "title": {"en": "Second OVA"}},
        "S2": {"seasonNumber": 0, "episodeNumber": 1, "runtime": 24, "title": {"en": "First OVA"}},
    }
    kitsu_world["search"] = {"Azure Example": [_row(5900, "Azure Example", start="2011-04-17")]}
    kitsu_world["docs"] = {5900: _doc(248000, episodes, tmdb=38000)}
    result = await kitsu.fetch_anime_tv("Azure Example", 0, 1, "enc", release_counter=True)
    assert (result["episode_number"], result["episode_title"]) == (1, "First OVA")


async def test_release_counter_keeps_a_short_when_the_longer_extras_have_no_slot(kitsu_world):
    # six 3-minute shorts are season 0 episodes 1 to 6; the 15-minute web
    # videos are not listed in season 0 at all
    episodes = {str(n): {"seasonNumber": 1, "episodeNumber": n, "absoluteEpisodeNumber": n,
                         "title": {"en": f"Regular {n}"}} for n in range(1, 13)}
    for n in range(1, 7):
        episodes[f"S{n}"] = {"seasonNumber": 0, "episodeNumber": n, "runtime": 3, "title": {"en": f"Short {n}"}}
        episodes[f"S{n + 6}"] = {"runtime": 15, "title": {"en": f"Web Video {n}"}}
    kitsu_world["search"] = {"Game Example": [_row(7800, "Game Example", start="2014-04-09")]}
    kitsu_world["docs"] = {7800: _doc(278000, episodes, tmdb=60800)}
    for n in (1, 6):
        result = await kitsu.fetch_anime_tv("Game Example", 0, n, "enc", release_counter=True)
        assert (result["season_number"], result["episode_number"]) == (0, n)
        assert result["episode_title"] == f"Short {n}"


async def test_ova_with_its_own_entry_takes_the_series_slot_not_the_release_counter(series_with_ova):
    # "Show - Seifuku (OVA2)": the second OVA of the franchise, a one-episode entry
    result = await kitsu.fetch_anime_tv("Tonari no Example Seifuku", 0, 2, "enc")
    assert (result["season_number"], result["episode_number"]) == (0, 15)
    assert result["tmdb_id"] == 100000
    assert result["episode_title"] == "Tonari no Example - Uniform"
    # filed under the series, with the series' name
    assert (result["title"], result["kitsu_id"]) == ("Tonari no Example", 43000)


async def test_numbered_ova_series_is_stored_in_season_zero(series_with_ova):
    result = await kitsu.fetch_anime_tv("Tonari no Example Joshikou-hen", None, 2, "enc", absolute=True)
    assert (result["season_number"], result["episode_number"]) == (0, 17)
    assert result["episode_title"] == "School 2"
    # 2 counts inside the OVA entry; it must not be looked up as episode 2 of the series
    assert result["absolute_episode"] is None
    call = series_with_ova["episode_meta_calls"][-1]
    assert call["absolute"] is False and call["absolute_hint"] is None
    assert (result["title"], result["kitsu_id"]) == ("Tonari no Example", 43000)


async def test_recap_special_does_not_beat_its_series(kitsu_world):
    title = "Bleach Thousand-Year Blood War The Separation"
    kitsu_world["search"] = {title: [
        _row(48914, "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan Recap", subtype="special",
             en="BLEACH: Thousand-Year Blood War - The Separation Recap"),
        _row(46903, "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan",
             en="BLEACH: Thousand-Year Blood War Part 2 - The Separation"),
    ]}
    assert (await kitsu.search_anime(title))["id"] == "46903"


async def test_weak_kitsu_match_lets_other_providers_answer(monkeypatch):
    calls = []

    async def weak_kitsu(*a, **kw):
        return {"title": "Devil May Cry", "kitsu_id": 1547, "media_type": "tv", "weak_match": "no season 2"}

    async def fallbacks(title, season, episode, *a):
        calls.append((title, season, episode))
        return {"title": "Devil May Cry", "imdb_id": "tt15716776", "media_type": "tv"}

    monkeypatch.setattr(resolvers.kitsu, "fetch_anime_tv", weak_kitsu)
    monkeypatch.setattr(resolvers, "_resolve_anime_tv_fallbacks", fallbacks)
    result = await resolvers.resolve_anime_tv("Devil May Cry", 2, 1, "enc")
    assert result["imdb_id"] == "tt15716776" and calls == [("Devil May Cry", 2, 1)]

    async def nothing(*a):
        return None

    monkeypatch.setattr(resolvers, "_resolve_anime_tv_fallbacks", nothing)
    result = await resolvers.resolve_anime_tv("Devil May Cry", 2, 1, "enc")
    assert result["kitsu_id"] == 1547 and "weak_match" not in result


# --------------------------------------------------------------------------
# No show name in the file: "S01E01 - The Green Seat.mkv"
# --------------------------------------------------------------------------

def test_titleless_plan_keeps_what_is_needed_for_a_lookup():
    plan = _plan("S01E01 - The Green Seat.mkv")
    assert plan["skip"] == SKIP_TITLE_MISSING
    assert (plan["season"], plan["episode"], plan["episode_title"]) == (1, 1, "The Green Seat")
    # With the show name supplied the same file plans like any other episode
    plan = _plan("S01E01 - The Green Seat.mkv", show_title="Mushishi")
    assert (plan["skip"], plan["kind"], plan["title"], plan["season"], plan["episode"]) == (None, "tv", "Mushishi", 1, 1)


@pytest.mark.parametrize("page, show", [
    ("List of Mushishi episodes", "Mushishi"),
    ("List of Bleach episodes (season 14)", "Bleach"),
    ("Monster (TV series)", "Monster"),
    ("List of Attack on Titan episodes", "Attack on Titan"),
    ("Fruits Basket season 2", "Fruits Basket"),
])
def test_show_name_from_page_title(page, show):
    assert episode_lookup.show_name_from_page_title(page) == show


@pytest.fixture
def titleless(monkeypatch):
    """Fake lookup + provider: Mushishi S01E01 is 'The Green Seat'."""
    state = {"candidates": ["The Prisoner", "Mushishi"], "lookups": [], "resolved": []}
    episodes = {
        ("mushishi", 1, 1): "The Green Seat",
        ("mushishi", 1, 2): "The Light of the Eyelid",
        ("mushishi", 1, 25): "Eye of Luck, Eye of Misfortune",
        ("the prisoner", 1, 1): "Arrival",
        ("the prisoner", 1, 2): "The Chimes of Big Ben",
    }

    async def fake_candidates(title, limit=4):
        state["lookups"].append(title)
        return list(state["candidates"])

    async def fake_resolve(plan, filename, channel, msg_id, default_id, anime_channel, encoded_string=None):
        state["resolved"].append(plan["title"])
        name = episodes.get((plan["title"].lower(), plan["season"], plan["episode"]))
        if name is None:
            return None
        return {"title": plan["title"], "media_type": "tv", "season_number": plan["season"],
                "episode_number": plan["episode"], "episode_title": name}

    monkeypatch.setattr(episode_lookup, "show_candidates_for_episode_title", fake_candidates)
    monkeypatch.setattr(entry, "_resolve_plan", fake_resolve)
    entry._TITLELESS_SHOW.clear()
    yield state
    entry._TITLELESS_SHOW.clear()


async def test_titleless_file_is_matched_through_its_episode_title(titleless):
    result = await entry.resolve_name("S01E01 - The Green Seat.mkv", 1, 10, anime_channel=True)
    assert (result["title"], result["episode_title"]) == ("Mushishi", "The Green Seat")
    assert result["title_from_episode_lookup"] is True
    # the first candidate is rejected, its S01E01 has another title
    assert titleless["resolved"] == ["The Prisoner", "Mushishi"]


async def test_titleless_file_is_skipped_when_no_show_confirms_the_title(titleless):
    assert await entry.resolve_name("S01E03 - Totally Made Up Title.mkv", 1, 11, anime_channel=True) is None
    titleless["candidates"] = []
    assert await entry.resolve_name("S01E01 - The Green Seat.mkv", 1, 12, anime_channel=True) is None
    # right title, wrong episode number
    titleless["candidates"] = ["Mushishi"]
    assert await entry.resolve_name("S01E02 - The Green Seat.mkv", 1, 13, anime_channel=True) is None


async def test_titleless_batch_reuses_the_confirmed_show(titleless):
    await entry.resolve_name("S01E01 - The Green Seat.mkv", 1, 10, anime_channel=True)
    titleless["lookups"].clear()
    result = await entry.resolve_name("S01E02 - The Light of the Eyelid.mkv", 1, 11, anime_channel=True)
    assert result["title"] == "Mushishi"
    assert titleless["lookups"] == []
    # slightly different wording is accepted once the show is confirmed
    result = await entry.resolve_name("S01E25 - Eye of Fortune, Eye of Misfortune.mkv", 1, 12, anime_channel=True)
    assert result["title"] == "Mushishi"
    # another channel does its own lookup
    await entry.resolve_name("S01E02 - The Light of the Eyelid.mkv", 2, 11, anime_channel=True)
    assert titleless["lookups"] == ["The Light of the Eyelid"]


async def test_titleless_file_without_an_episode_title_is_skipped(titleless):
    assert await entry.resolve_name("S01E01.mkv", 1, 10, anime_channel=True) is None
    assert titleless["lookups"] == []


def test_watch_after_is_written_into_the_episode_title():
    result = {"media_type": "tv", "episode_title": "Tip-off"}
    entry._apply_watch_after(result, {"season": 1, "episode": 22}, "Tip Off")
    assert result["episode_title"] == "Tip-off (watch after S01E22)"
    assert result["watch_after"] == {"season": 1, "episode": 22}
    # no provider title: the one from the file name is used
    result = {"media_type": "tv", "episode_title": "S00E01"}
    entry._apply_watch_after(result, {"season": None, "episode": 5}, "Marrying by Contesting")
    entry._apply_watch_after(result, {"season": None, "episode": 5}, "Marrying by Contesting")
    assert result["episode_title"] == "Marrying by Contesting (watch after episode 5)"


class _Reply:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code, self._body, self.headers = status_code, body, headers or {}

    def json(self):
        return self._body


def _provider_replies(monkeypatch, replies):
    """ani.zip answers with the given replies in turn; returns the list of calls made."""
    calls = []

    class Client:
        async def get(self, url, params=None):
            calls.append(params)
            reply = replies[min(len(calls), len(replies)) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply

    async def client():
        return Client()

    monkeypatch.setattr(kitsu, "_get_client", client)
    monkeypatch.setattr(kitsu, "RETRY_WAITS", (0, 0))
    for key in [k for k in kitsu.KITSU_CACHE if str(k).startswith("anizip::")]:
        kitsu.KITSU_CACHE.pop(key, None)
    return calls


async def test_busy_provider_is_asked_again(monkeypatch):
    doc = {"mappings": {"thetvdb_id": 1}, "episodes": {}}
    calls = _provider_replies(monkeypatch, [_Reply(429), _Reply(200, doc)])
    assert await kitsu.get_anizip_mappings(77001) == doc
    assert len(calls) == 2


async def test_unreachable_provider_is_not_remembered_as_no_data(monkeypatch):
    doc = {"mappings": {"thetvdb_id": 1}, "episodes": {}}
    replies = [_Reply(503), kitsu.httpx.ConnectError("down"), _Reply(429), _Reply(200, doc)]
    calls = _provider_replies(monkeypatch, replies)
    assert await kitsu.get_anizip_mappings(77002) is None      # three tries, all failed
    assert len(calls) == 3
    assert await kitsu.get_anizip_mappings(77002) == doc       # the next file asks again
    assert len(calls) == 4


async def test_unknown_entry_is_remembered(monkeypatch):
    calls = _provider_replies(monkeypatch, [_Reply(404)])
    assert await kitsu.get_anizip_mappings(77003) is None
    assert await kitsu.get_anizip_mappings(77003) is None
    assert len(calls) == 1

