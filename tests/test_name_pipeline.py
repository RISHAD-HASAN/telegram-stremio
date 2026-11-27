"""Name pipeline tests: clean_filename() -> plan_media().

The captions are typical release names. Nothing here uses the network.
"""
import os

os.environ.setdefault("DATABASE", "mongodb://localhost:27017/db1,mongodb://localhost:27017/db2")

import pytest

from Backend.helper.metadata.common import extract_default_id
from Backend.helper.metadata.parse import quality_hint_from_media, resolution_from_dimensions
from Backend.helper.metadata.plan import SKIP_TITLE_MISSING, plan_media
from Backend.helper.name_cleaning import clean_filename
from Backend.helper.split_files import parse_combined_episodes, parse_split_info

IMDB = "\nhttps://www.imdb.com/title/"


def _plan(caption, dims=None):
    hint = quality_hint_from_media(*dims) if dims else None
    return plan_media(
        clean_filename(caption),
        anime_channel=True,
        quality_hint=hint,
        has_explicit_id=bool(extract_default_id(caption)),
    )


# caption, kind, title, season, episode, absolute, quality
EPISODES = [
    # Year in the name is not the title/episode
    ("Monster.2004.S01E31.In.Broad.Daylight.1080p.BluRay.DUAL.FLAC.2.0.x264-Kitsune.mkv",
     "Monster", 1, 31, False, "1080p"),
    # "S02E01 - 021" is one episode plus its absolute number, not a 1-21 pack
    ("Bleach (2004) - S01E02 - 002 - A Shinigamis Work [BD][FLAC][DUAL][HEVC 10bit]-DBD-Raws (ParanDark-jcraw).mkv",
     "Bleach", 1, 2, False, "HD"),
    # The word "Combined" in an episode title is not a combined pack
    ("Fullmetal Alchemist - Brotherhood - S01E52 - Combined Strength Bluray-1080p Remux.mkv",
     "Fullmetal Alchemist Brotherhood", 1, 52, False, "1080p"),
    ("[DeeVeeDee] Baccano - S00E02 - Episode 15 - OVA 2 [DVD 480p HEVC Opus Dual-Audio] [71E39246].mkv",
     "Baccano", 0, 2, False, "480p"),
    # Malformed season marker "01E01"
    ("[Starbez] Tomo-chan is a Girl - 01E01 [BD 1080p x265 10bit FLAC] [99568175].mkv",
     "Tomo-chan is a Girl", 1, 1, False, "1080p"),
    # Half episodes are specials
    ("Kuroko's Basketball - S01E22.5 - Tip Off (Hi10 1080p BD Dual Audio -KS-).mkv",
     "Kuroko's Basketball", 0, 22, False, "1080p"),
    ("[Dae-P9] Link Click - 05.5 - S00E01 - Marrying by Contesting (BD 1080p HEVC FLAC AAC) [Dual Audio] [5BCD56B8].mkv",
     "Link Click", 0, 1, False, "1080p"),
    ("[INDEX] Blood Blockade Battlefront - Re-Cap [377FFBE2].mkv",
     "Blood Blockade Battlefront", 0, 1, False, "HD"),
    # "- E01" without a season is an absolute episode; "X" stays in the title
    ("[Raze] To Be Hero X - E01 [Dual-Audio].mkv", "To Be Hero X", None, 1, True, "HD"),
    ("[sam] Kusuriya no Hitorigoto - 03v2 [BD 1080p FLAC] [4A303FE1].mkv",
     "Kusuriya no Hitorigoto", None, 3, True, "1080p"),
    ("[Raze] Gachiakuta - 01 x265 10bit 1080p 143 8561fps mkv", "Gachiakuta", None, 1, True, "1080p"),
    ("[Raze] Devil May Cry S2 - 01 (NF) (DDP5.1) (MultiSub) x265 10bit 1080p 144fps.mkv",
     "Devil May Cry", 2, 1, False, "1080p"),
    # Numbers that belong to the title
    ("[LostYears] NieR Automata Ver1.1a - S01E01 v2 (WEB 1080p HEVC AAC E-AC-3) [BCAF0923].mkv",
     "NieR Automata Ver1.1a", 1, 1, False, "1080p"),
    ("2.5.Dimensional.Seduction.2024.S01E01.REPACK2.BDRip-1080p.x265.FLAC.EAC3.Dual.Audio-Freehold.mkv",
     "2.5 Dimensional Seduction", 1, 1, False, "1080p"),
    ("[DB]Fujimoto Tatsuki 17-26_-_01_(Dual Audio_10bit_BD1080p_x265).mkv",
     "Fujimoto Tatsuki 17-26", None, 1, True, "1080p"),
    # Episode-title "Part N" is not a split-file part (see test below) and
    # "Specials" is not part of the show title
    ("[Raze] Lord of Mysteries Specials S00E02 The Marked Hunt Part 1 [Dual-Audio].mkv",
     "Lord of Mysteries", 0, 2, False, "HD"),
    ("WITCH.WATCH.S01E11.Dogs.and.Raindrops.Part.1.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264-VARYG.mkv",
     "WITCH WATCH", 1, 11, False, "1080p"),
    ("[NOTMiNATO] Bleach Thousand-Year Blood War - The Separation - S17E14 - The Last 9 Days [BD 1080p HEVC FLAC) [9658DE8A].mkv",
     "Bleach Thousand-Year Blood War The Separation", 17, 14, False, "1080p"),
    # Frame size stated in the name
    ("[Pookie] Golden Kamuy S1 - 01 [BD 1600x900 x264 FLAC] [F8267C26].mkv",
     "Golden Kamuy", 1, 1, False, "900p"),
]


@pytest.mark.parametrize("caption,title,season,episode,absolute,quality", EPISODES)
def test_dump_episode_names(caption, title, season, episode, absolute, quality):
    plan = _plan(caption)
    assert plan["skip"] is None
    assert plan["kind"] == "tv"
    assert plan["title"] == title
    assert plan["season"] == season
    assert plan["episode"] == episode
    assert plan["absolute"] is absolute
    assert plan["quality"] == quality
    assert plan["part_number"] is None
    assert plan["combined"] is None


# caption, title, year, quality, split part
MOVIES = [
    ("[DB]Summer Wars_-_(Dual Audio_10bit_BD1080p_x265).mkv", "Summer Wars", None, "1080p", None),
    # A title that starts with a number is not episode 100
    ("100.Meters.2025.1080p.BluRay.DUAL.DDP7.1.x264-ZoroSenpai.part001.mkv" + IMDB + "tt32600395/",
     "100 Meters", 2025, "1080p", 1),
    ("Angels.Egg.1985.1080p.BluRay.FLAC2.0.Hi10P.x264-ZoroSenpai.part001.mkv" + IMDB + "tt0208502/",
     "Angels Egg", 1985, "1080p", 1),
    ("[DB]Cowboy Bebop Knockin' on Heaven's Door _-_(Dual Audio_10bit_BD1080p_x265).mkv" + IMDB + "tt0275277/",
     "Cowboy Bebop Knockin' on Heaven's Door", None, "1080p", None),
    ("[Raze] (Demon Slayer) Kimetsu no Yaiba - Infinity Castle (2025) (BD) (Dual-Audio) (Multi-Sub) x265 10bit 1080p "
     "143.8561fps v2.part001.mkv" + IMDB + "tt32820897/",
     "Kimetsu no Yaiba Infinity Castle", 2025, "1080p", 1),
]


@pytest.mark.parametrize("caption,title,year,quality,part", MOVIES)
def test_dump_movie_names(caption, title, year, quality, part):
    plan = _plan(caption)
    assert plan["skip"] is None
    assert plan["kind"] == "movie"
    assert plan["title"] == title
    assert plan["year"] == year
    assert plan["quality"] == quality
    assert plan["part_number"] == part
    assert "imdb" not in plan["title"].lower() and "http" not in plan["title"].lower()


def test_split_parts_of_one_movie_share_a_group():
    keys = set()
    for n in (1, 2, 3):
        name = clean_filename(f"100.Meters.2025.1080p.BluRay.DUAL.DDP7.1.x264-ZoroSenpai.part00{n}.mkv")
        key, part = parse_split_info(name)
        assert part == n
        keys.add(key)
    assert len(keys) == 1


def test_episode_title_part_is_not_a_split_part():
    for name in (
        "[Raze] Lord of Mysteries Specials S00E02 The Marked Hunt Part 1 [Dual-Audio].mkv",
        "WITCH.WATCH.S01E12.Dogs.and.Raindrops.Part.2.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264-VARYG.mkv",
        "WITCH.WATCH.S01E14.The.Phasey.Hunt.Part.4-Diary.of.My.Favorite.Artist.1080p.CR.WEB-DL.DUAL.AAC2.0.H.264-VARYG.mkv",
    ):
        assert parse_split_info(clean_filename(name)) is None, name
    # ...while genuine split uploads still are
    for name, part in (
        ("Darling in the FranXX - S01E21v3 - (BD 1080p HEVC 10-bit Opus) [Dual Audio] [BlackRose].part002.mkv", 2),
        ("[Group] Noragami - S01E01 Part 1 [1080p].mkv", 1),
        ("Demon Slayer Infinity Castle Part 2.mkv", 2),
        ("Movie.2020.Part.1.1080p.mkv", 1),
    ):
        info = parse_split_info(clean_filename(name))
        assert info is not None and info[1] == part, name


def test_single_episode_is_not_a_combined_range():
    for name in (
        "Bleach (2004) - S02E01 - 021 - Enter! The World of the Shinigami [BD].mkv",
        "[DeeVeeDee] Baccano - S00E02 - Episode 15 - OVA 2 [DVD 480p HEVC Opus Dual-Audio] [71E39246].mkv",
        "Fullmetal Alchemist - Brotherhood - S01E52 - Combined Strength Bluray-1080p Remux.mkv",
        "[DB]Fujimoto Tatsuki 17-26_-_01_(Dual Audio_10bit_BD1080p_x265).mkv",
    ):
        assert parse_combined_episodes(clean_filename(name)) is None, name
    # ...while real packs still are
    for name, start, end in (
        ("Show Name S01E01-E04 1080p.mkv", 1, 4),
        ("Show Name S01 E01-E05.mkv", 1, 5),
    ):
        combined = parse_combined_episodes(clean_filename(name))
        assert combined and (combined["start"], combined["end"]) == (start, end), name


@pytest.mark.parametrize("caption, episode, counter", [
    ("Azure.Example.SP1.1080p.Blu-Ray.10-Bit.Dual-Audio.FLAC.x264-GRP.mkv", 1, True),
    ("[Grp] Azure Example - OVA 2 [BD 1080p].mkv", 2, True),
    ("Azure.Example.S00E01.1080p.BluRay.x265-GRP.mkv", 1, False),
    ("[Grp] Azure Example Specials S00E03 Some Title [Dual-Audio].mkv", 3, False),
])
def test_release_special_counter_is_told_apart_from_a_season_zero_number(caption, episode, counter):
    plan = _plan(caption)
    assert (plan["title"], plan["season"], plan["episode"]) == ("Azure Example", 0, episode)
    assert plan["special_counter"] is counter


def test_regular_episode_has_no_special_counter():
    assert _plan("Azure.Example.S01E01.1080p.BluRay.x265-GRP.mkv")["special_counter"] is False
    assert _plan("[Grp] Azure Example - 13 (OVA) [BD 1080p].mkv")["special_counter"] is False


def test_name_without_show_title_is_skipped_unless_id_given():
    plan = _plan("S01E01 - The Green Seat.mkv")
    assert plan["skip"] == SKIP_TITLE_MISSING
    plan = plan_media(clean_filename("S01E01 - The Green Seat.mkv"), anime_channel=True, has_explicit_id=True)
    assert plan["skip"] is None and plan["season"] == 1 and plan["episode"] == 1


def test_urls_and_site_tags_never_reach_the_title():
    for caption in (
        "Angels.Egg.1985.1080p.BluRay.FLAC2.0.Hi10P.x264-ZoroSenpai.part001.mkv\nhttps://www.imdb.com/title/tt0208502/",
        "www.Example.com - Angels Egg (1985) 1080p.mkv",
        "[Example.org] Angels Egg (1985) 1080p.mkv",
    ):
        title = _plan(caption)["title"]
        assert title == "Angels Egg", (caption, title)


# Telegram's width/height attributes are only a fallback.
def test_name_resolution_beats_media_dimensions():
    plan = _plan("[DB]Summer Wars_-_(Dual Audio_10bit_BD1080p_x265).mkv", (3840, 2160, 3800000000, 6800))
    assert plan["quality"] == "1080p"
    plan = _plan("[Raze] Devil May Cry (2025) - 01 (Dual-Audio) (Multi-Sub) x265 10bit 1080p 144fps mkv",
                 (640, 360, 1800000000, 1800))
    assert plan["quality"] == "1080p"


def test_dimension_hint_is_capped_and_thumbnail_sizes_ignored():
    # 3840x2160 without a resolution in the name is not labelled 4K
    plan = _plan("[Raze] Hell's Paradise - S01E01 [Dual-Audio].mkv", (3840, 2160, 800000000, 1500))
    assert plan["quality"] == "1080p"
    # thumbnail-sized dimensions at a high bitrate are ignored
    plan = _plan("[Raze] Lord of Mysteries S01E02-Beyonder mkv", (640, 272, 1100000000, 2200))
    assert plan["quality"] == "HD"
    plan = _plan("[Raze] Gachiakuta - 03 (Dual-Audio) (Multi-Sub) x265 10bit 1 mkv", (640, 360, 950000000, 1400))
    assert plan["quality"] == "HD" and plan["episode"] == 3
    # a small file keeps its label
    assert quality_hint_from_media(640, 360, 60 * 1024 * 1024, 1420) == "360p"


def test_absolute_number_in_name_is_not_a_resolution():
    plan = _plan("Bleach (2004) - S16E18 - 360 - Ichigo vs. Uryu [BD][FLAC][DUAL][HEVC 10bit]-DBD-Raws.mkv")
    assert (plan["season"], plan["episode"], plan["quality"]) == (16, 18, "HD")


def test_resolution_tiers():
    assert resolution_from_dimensions(1920, 1080) == "1080p"
    assert resolution_from_dimensions(1920, 800) == "1080p"
    assert resolution_from_dimensions(1600, 900) == "900p"
    assert resolution_from_dimensions(1280, 720) == "720p"
    assert resolution_from_dimensions(720, 576) == "576p"
    assert resolution_from_dimensions(720, 480) == "480p"


def test_stream_sort_buckets_non_standard_heights():
    from Backend.fastapi.routes.stremio_routes import get_resolution_priority, stream_res_label

    assert get_resolution_priority("Telegram 576p") == 480
    assert get_resolution_priority("Telegram 540p") == 480
    assert get_resolution_priority("Telegram 900p") == 720
    assert get_resolution_priority("Telegram 1440p") == 1080
    assert stream_res_label("Telegram 576p") == "480p"
    assert get_resolution_priority("Telegram 1080p") == 1080
    assert get_resolution_priority("Telegram 4K") == 2160


def test_offline_indexer_matches_live_pipeline():
    import indexer

    assert indexer._HAS_SHARED_PARSER
    for caption, title, season, episode, absolute, quality in EPISODES:
        parsed = indexer.parse_filename(caption)
        assert parsed["title"] == title, caption
        assert parsed["season"] == season, caption
        assert parsed["episode"] == episode, caption
        assert parsed["absolute"] is absolute, caption
        assert parsed["is_movie"] is False, caption
        assert parsed["quality"] == (None if quality == "HD" else quality), caption
    for caption, title, year, quality, part in MOVIES:
        parsed = indexer.parse_filename(caption)
        assert parsed["title"] == title and parsed["is_movie"] is True, caption
        assert parsed["split_part"] == part, caption
    assert indexer.parse_filename("S01E01 - The Green Seat.mkv")["skip_reason"] == SKIP_TITLE_MISSING
    assert indexer.parse_filename(MOVIES[1][0])["explicit_imdb_id"] == "tt32600395"


def test_indexer_explicit_id_overrides_searched_id():
    import indexer

    parsed = indexer.parse_filename(MOVIES[1][0])
    # the title search found some other film
    resolved = {"tmdb_id": 999, "imdb_id": "tt0000001", "title": "Other", "media_type": "movie", "source": "tmdb"}
    payload = indexer.build_payload(parsed, resolved, 123, 42)
    assert payload["imdb_id"] == "tt32600395"
    assert payload["tmdb_id"] != 999
    # nothing found: the id from the caption is still used
    payload = indexer.build_payload(parsed, None, 123, 42)
    assert payload["imdb_id"] == "tt32600395"
    assert payload["media_type"] == "movie" and payload["split_part"] == 1

    parsed = indexer.parse_filename("Show Name {tmdb-61223} S01E02 1080p.mkv")
    payload = indexer.build_payload(parsed, None, 123, 1)
    assert payload["tmdb_id"] == 61223


def test_indexer_reads_telegram_jsonl_dump(tmp_path, monkeypatch):
    import json

    import indexer

    rows = [
        # truncated Telegram file_name, full name in the caption
        {"chat_id": 1234567890, "msg_id": 11, "kind": "video", "file_name": "Monster.2004.S01E31.In.Broad.Da",
         "caption": "Monster.2004.S01E31.In.Broad.Daylight.1080p.BluRay.DUAL.FLAC.2.0.x264-Kitsune.mkv",
         "size": 1700000000, "width": 1920, "height": 1080, "duration": 1440.0},
        {"chat_id": 1234567890, "msg_id": 12, "kind": "video", "file_name": "",
         "caption": "[Raze] Hell's Paradise - S01E01 [Dual-Audio].mkv",
         "size": 800000000, "width": 3840, "height": 2160, "duration": 1500.0},
        {"chat_id": 1234567890, "msg_id": 13, "kind": "video", "file_name": "S01E01 - The Green Seat.mkv",
         "caption": "S01E01 - The Green Seat.mkv", "size": 800000000, "width": 1920, "height": 1080, "duration": 1440.0},
    ]
    monkeypatch.chdir(tmp_path)
    with open("dump.txt", "w", encoding="utf-8", newline="\r\n") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        fh.write("not json\n")

    records = indexer.load_records_from_tsv("dump.txt")
    assert [r["msg_id"] for r in records] == [11, 12, 13]
    assert records[0]["file_name"].endswith("x264-Kitsune.mkv")

    parsed = [indexer.parse_filename(r["file_name"], quality_hint=indexer._record_quality_hint(r)) for r in records]
    assert (parsed[0]["title"], parsed[0]["season"], parsed[0]["episode"], parsed[0]["quality"]) == ("Monster", 1, 31, "1080p")
    assert (parsed[1]["title"], parsed[1]["quality"]) == ("Hell's Paradise", "1080p")
    assert parsed[2]["skip_reason"] == SKIP_TITLE_MISSING

    path = indexer.write_parse_preview(records, "preview")
    lines = open(path, encoding="utf-8").read().splitlines()
    assert len(lines) == 4 and lines[1].split("\t")[:5] == ["11", "tv", "Monster", "1", "31"]
