import pytest
import os
import re
os.environ["DATABASE"] = "mongodb://localhost:27017/db1,mongodb://localhost:27017/db2"

from Backend.helper.metadata.parse import (
    extract_absolute_episode,
    clean_anime_search_title,
    _normalize_sxe,
    parse_media_name,
)
from Backend.helper.quality_checker import QualityChecker

def test_sxe_normalization():
    assert _normalize_sxe("Show.1x05.720p.mkv") == "Show.S01E05.720p.mkv"
    assert _normalize_sxe("Show 4x2 1080p.mkv") == "Show S04E02 1080p.mkv"
    assert "1920x1080" in _normalize_sxe("Movie.1920x1080.mkv")

def test_absolute_episode_extraction():
    cases = [
        ("Naruto.001.v4.480p.DVD.Dual-Audio.FLAC2.0.Hi10P.x264-JySzE.mkv", 1),
        ("Naruto.071.v4.480p.DVD.Dual-Audio.FLAC2.0.Hi10P.x264-JySzE.mkv", 71),
        ("Naruto.220.v4.480p.DVD.Dual-Audio.FLAC2.0.Hi10P.x264-JySzE.mkv", 220),
        ("[UDF] 91 Days 01v2 (BDRip 1080p x264 FLACx2) [5AEB36B4].mkv", 1),
        ("[UDF] 91 Days 02v2 (BDRip 1080p x264 FLACx2) [CA9A8B9C].mkv", 2),
        ("[UDF] 91 Days 12v2 (BDRip 1080p x264 FLACx2) [80CF04C1].mkv", 12),
        ("[Raze] Sakamoto Days 11 (Dual Audio DDP5.1) x265 1080p 143.8561fps.mkv", 11),
        ("[Raze] Gachiakuta 19 (Dual Audio) x265 1080p 143.8561fps.mkv", 19),
        ("Bleach 001 The Day I Became A Shinigami.mkv", 1),
        ("Bleach 024 Assemble! The 13 Divisions.mkv", 24),
        ("[Judas] One Piece - 1172.mkv", 1172),
    ]
    for fn, expected in cases:
        parsed = parse_media_name(fn)
        ep = extract_absolute_episode(fn, parsed)
        assert ep == expected, f"Failed for {fn}: got {ep}, expected {expected}"

def test_movies_not_extracted_as_episodes():
    movie_cases = [
        ("Bleach the Movie 1 Memories of Nobody (2006) 1080p.mkv", "Bleach the Movie 1 Memories of Nobody"),
        ("Bleach the Movie 2 The DiamondDust Rebellion (2007) 1080p.mkv", "Bleach the Movie 2 The DiamondDust Rebellion"),
        ("Bleach the Movie 3 Fade to Black (2008) 1080p.mkv", "Bleach the Movie 3 Fade to Black"),
        ("Bleach the Movie 4 Hell Verse (2010) 1080p.mkv", "Bleach the Movie 4 Hell Verse"),
        ("[Anime Time] Studio Ghibli - Movie 04 - Kiki's Delivery Service [1989].mkv", "Kiki's Delivery Service"),
        ("[Anime Time] Studio Ghibli - Movie 10 - Princess Mononoke [1997].mkv", "Princess Mononoke"),
        ("Makoto Shinkai - Movie 05 - Your Name [2016].mkv", "Your Name"),
        ("Dragon Ball Z - Movie 08 - Broly The Legendary Super Saiyan [1080p].mkv", "Dragon Ball Z Broly The Legendary Super Saiyan"),
        ("[Judas] Kimetsu no Yaiba - Mugen Ressha-hen (Movie) [1080p].mkv", "Kimetsu no Yaiba - Mugen Ressha-hen"),
        ("[SubsPlease] Chainsaw Man - Movie (1080p).mkv", "Chainsaw Man"),
        ("[SubsPlease] Jujutsu Kaisen 0 (Movie) [1080p].mkv", "Jujutsu Kaisen 0"),
        ("5 Centimeters per Second (2007) [1080p].mkv", "5 Centimeters per Second"),
        ("5 Centimeters per Second [1080p].mkv", "5 Centimeters per Second"),
        ("91 Days (2016) [1080p].mkv", "91 Days"),
        ("Oppenheimer.2023.1080p.BluRay.x264.mkv", "Oppenheimer"),
        ("Inception (2010) [1080p].mkv", "Inception"),
    ]
    for fn, exp_clean in movie_cases:
        parsed = parse_media_name(fn)
        ep = extract_absolute_episode(fn, parsed)
        assert ep is None, f"Movie {fn} should not have extracted episode, got {ep}"
        clean = clean_anime_search_title(parsed.get("title") or "")
        assert exp_clean.lower() in clean.lower() or clean.lower() in exp_clean.lower(), f"Clean title mismatch for {fn}: got {clean}, expected {exp_clean}"

def test_specials_and_ovas():
    special_cases = [
        ("[Erai-raws] Bleach - S00E01 [1080p].mkv", 0, 1),
        ("[Erai-raws] Bleach - S00E02 [1080p].mkv", 0, 2),
        ("[SubsPlease] Bleach - OVA 01 [1080p].mkv", 0, 1),
        ("[SubsPlease] Bleach - Special 01 [1080p].mkv", 0, 1),
        ("[SubsPlease] Bleach - SP01 [1080p].mkv", 0, 1),
        ("[SubsPlease] Bleach - SP 02 [1080p].mkv", 0, 2),
        ("[SubsPlease] Shingeki no Kyojin - OAD 01 [1080p].mkv", 0, 1),
    ]
    for fn, exp_s, exp_e in special_cases:
        parsed = parse_media_name(fn)
        assert parsed.get("season") == exp_s, f"Failed season for {fn}: got {parsed.get('season')}, exp {exp_s}"
        assert parsed.get("episode") == exp_e, f"Failed episode for {fn}: got {parsed.get('episode')}, exp {exp_e}"

def test_quality_checker():
    # Higher tier replaces lower tier
    should_rep, reason = QualityChecker.should_replace_quality(
        "1080p", "Movie.1080p.HDCAM.x264.mkv", "1.5GB",
        "1080p", "Movie.1080p.BluRay.x264.mkv", "2.0GB"
    )
    assert should_rep is True
    assert "Better source tier" in reason

    # Lower tier does not replace higher tier
    should_rep, reason = QualityChecker.should_replace_quality(
        "1080p", "Movie.1080p.BluRay.x264.mkv", "2.0GB",
        "1080p", "Movie.1080p.HDCAM.x264.mkv", "1.5GB"
    )
    assert should_rep is False
    assert "Lower source tier" in reason

from Backend.helper.split_files import parse_split_info, strip_part_suffix

def test_spanish_filename_parsing():
    cases = [
        ("Bleach Capitulo 5 [1080p].mkv", 5),
        ("Bleach Temp 1 Cap 08 [1080p].mkv", 8),
        ("Bleach Temporada 2 Capitulo 12 [1080p].mkv", 12),
    ]
    for fn, ep in cases:
        parsed = parse_media_name(fn)
        extracted = extract_absolute_episode(fn, parsed)
        assert extracted == ep or parsed.get("episode") == ep

def test_split_files_parsing():
    split_cases = [
        ("Oppenheimer.2023.1080p.part001.mkv", 1, "oppenheimer.2023.1080p.mkv", "Oppenheimer.2023.1080p.mkv"),
        ("Oppenheimer.2023.1080p.part002.mkv", 2, "oppenheimer.2023.1080p.mkv", "Oppenheimer.2023.1080p.mkv"),
        ("Bleach.S01E01.1080p.mkv.001", 1, "bleach.s01e01.1080p.mkv", "Bleach.S01E01.1080p.mkv"),
        ("Bleach.S01E01.1080p.mkv.002", 2, "bleach.s01e01.1080p.mkv", "Bleach.S01E01.1080p.mkv"),
        ("Inception.2010.1080p.cd1.avi", 1, "inception.2010.1080p.avi", "Inception.2010.1080p.avi"),
        ("Inception.2010.1080p.cd2.avi", 2, "inception.2010.1080p.avi", "Inception.2010.1080p.avi"),
        ("Movie.1080p.part01.rar", 1, "movie.1080p.rar", "Movie.1080p.rar"),
        ("Archive.1080p.zip.001", 1, "archive.1080p.zip", "Archive.1080p.zip"),
    ]
    for fn, exp_part, exp_group, exp_stripped in split_cases:
        info = parse_split_info(fn)
        assert info is not None, f"Failed to parse split info for {fn}"
        assert info[1] == exp_part, f"Part number mismatch for {fn}: got {info[1]}, expected {exp_part}"
        assert info[0] == exp_group, f"Group key mismatch for {fn}: got {info[0]}, expected {exp_group}"
        stripped = strip_part_suffix(fn)
        assert stripped == exp_stripped, f"Stripped name mismatch for {fn}: got {stripped}, expected {exp_stripped}"

def test_colon_prefix_alias_matching():
    from Backend.helper.metadata.common import collect_title_aliases, score_candidate_aliases
    canonical = "Yuusha Kei ni Shosu: Choubatsu Yuusha 9004-tai Keimu Kiroku"
    aliases = collect_title_aliases(canonical, ["Sentenced to Be a Hero"])
    assert "Yuusha Kei ni Shosu" in aliases
    score = score_candidate_aliases("Yuusha kei ni Shosu", None, canonical, 2025, aliases=aliases)
    assert score >= 0.90, f"Expected high match score, got {score}"

def test_anime_season_variations():
    cases = [
        ("Fire Force Season 2 - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force 2nd Season - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force S2 - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force S02 - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force S2 Episode 5 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force S02E05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force Part 2 - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force Cour 2 - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Fire Force II - 05 [1080p].mkv", 2, 5, "Fire Force"),
        ("Raze_Dandadan_S2_07_Dual_Audio_x265_10bit_1080p_143_8561fps.mkv", 2, 7, "Dandadan"),
        ("Raze_Dandadan_S2_08_Dual_Audio_x265_10bit_1080p_143_8561fps.mkv", 2, 8, "Dandadan"),
        ("[Raze] Just Because! S1 E1 (NF) (DDP5.1) (MultiSub) x265 10bit 1080p 144fps.mkv.mkv", 1, 1, "Just Because"),
        ("[Raze] Devil May Cry S2 06 (Dual Audio) (DDP5.1) (NF) (10bit) (1080p) x265 144fps (1382MB).mkv", 2, 6, "Devil May Cry"),
        ("Boku no Hero Academia S E1 [1080p].mkv", 0, 1, "Boku no Hero Academia"),
        ("Darling in the FranXX S E1 [1080p].mkv", 0, 1, "Darling in the FranXX"),
        ("Cyberpunk Edgerunners S01 E2 [1080p].mkv", 1, 2, "Cyberpunk Edgerunners"),
        ("Jujutsu Kaisen Season 2 - 01 (25) [1080p].mkv", 2, 1, "Jujutsu Kaisen"),
        ("Jujutsu Kaisen 2nd Season - 01 [1080p].mkv", 2, 1, "Jujutsu Kaisen"),
        ("Mob Psycho 100 II - 01 [1080p].mkv", 2, 1, "Mob Psycho 100"),
        ("Mob Psycho 100 III - 01 [1080p].mkv", 3, 1, "Mob Psycho 100"),
        ("Overlord IV - 01 [1080p].mkv", 4, 1, "Overlord"),
        ("Kingdom 5th Season - 01 [1080p].mkv", 5, 1, "Kingdom"),
        ("KonoSuba 3 - 01 [1080p].mkv", 3, 1, "KonoSuba"),
        ("Code Geass R2 - 01 [1080p].mkv", 2, 1, "Code Geass"),
        ("[OZR] Code Geass Lelouch of the Rebellion R2 01 (BDrip 1920x1080 x264 FLAC) [Dual Audio].mkv", 2, 1, "Code Geass"),
        ("[OZR] Code Geass Lelouch of the Rebellion R2 02 (BDrip 1920x1080 x264 FLAC) [Dual Audio].mkv", 2, 2, "Code Geass"),
        ("[OZR] Code Geass Lelouch of the Rebellion R2 25 (BDrip 1920x1080 x264 FLAC) [Dual Audio].mkv", 2, 25, "Code Geass"),
        ("Studio Ghibli Movie 05 Only Yesterday (1991).mkv", None, None, "Only Yesterday"),
        ("Studio Ghibli Movie 04 Kiki's Delivery Service (1989).mkv", None, None, "Kiki's Delivery Service"),
        ("Blood Blockade Battlefront Re.mkv", 0, 1, "Blood Blockade Battlefront"),
        ("Boku no Hero Academia 7th Season - 01 [1080p].mkv", 7, 1, "Boku no Hero Academia"),
    ]
    for fn, exp_s, exp_e, exp_t in cases:
        p = parse_media_name(fn)
        assert p.get("season") == exp_s, f"Failed season for {fn}: got {p.get('season')}, exp {exp_s}"
        assert p.get("episode") == exp_e, f"Failed episode for {fn}: got {p.get('episode')}, exp {exp_e}"
        clean_t = clean_anime_search_title(p.get("title") or "")
        assert exp_t.lower() in clean_t.lower(), f"Failed title clean for {fn}: got {clean_t}, exp {exp_t}"

def test_single_letter_roman_numerals_in_titles_are_not_seasons():
    # Standalone X/I/V are ordinary title letters: "To Be Hero X" was being
    # misread as season 10 because the bare roman rule matched the trailing X.
    collision_cases = [
        "[Raze] To Be Hero X E01 [Dual Audio].mkv",
        "[Raze] To Be Hero X E24 [Dual Audio].mkv",
        "Project X E05.mkv",
    ]
    for fn in collision_cases:
        p = parse_media_name(fn)
        assert p.get("season") is None, f"{fn}: title letter parsed as season {p.get('season')}"
    # Episode numbers must still come through.
    assert parse_media_name(collision_cases[0]).get("episode") == 1
    assert parse_media_name(collision_cases[1]).get("episode") == 24
    # The explicit "Season <roman>" wording keeps the full numeral range,
    # including single letters.
    assert parse_media_name("Show Season X - 03.mkv").get("season") == 10
    assert parse_media_name("Show Season V - 02.mkv").get("season") == 5
    # Multi-letter bare numerals still denote sequel seasons.
    assert parse_media_name("Mushoku Tensei II - 01.mkv").get("season") == 2
    assert parse_media_name("Overlord IV - 01.mkv").get("season") == 4

def test_anime_batches_and_combined():
    from Backend.helper.split_files import parse_combined_episodes
    batch_cases = [
        ("Bleach - 001-366.mkv", 1, 1, 366),
        ("One Piece - 01-130.mkv", 1, 1, 130),
        ("Monster (01-74) [1080p].mkv", 1, 1, 74),
        ("Hunter x Hunter [01-148].mkv", 1, 1, 148),
        ("Fire Force S01E01-E24 [1080p].mkv", 1, 1, 24),
        ("Fire Force S02 E01-12 [1080p].mkv", 2, 1, 12),
        ("Show S01 01-12.mkv", 1, 1, 12),
    ]
    for fn, exp_s, exp_start, exp_end in batch_cases:
        comb = parse_combined_episodes(fn)
        assert comb is not None, f"Failed to parse combined batch for {fn}"
        assert comb["season"] == exp_s, f"Season mismatch for {fn}: got {comb['season']}, exp {exp_s}"
        assert comb["start"] == exp_start, f"Start mismatch for {fn}: got {comb['start']}, exp {exp_start}"
        assert comb["end"] == exp_end, f"End mismatch for {fn}: got {comb['end']}, exp {exp_end}"

def test_hashtag_and_caption_cleaning():
    from Backend.helper.pyro import clean_filename, has_video_extension
    raw = "Naruto Shippuden Ep. 05 (1080p) #anime #naruto #action.mkv"
    cleaned = clean_filename(raw)
    assert "#anime" not in cleaned
    assert "#naruto" not in cleaned
    p = parse_media_name(cleaned)
    assert p.get("episode") == 5
    assert "Naruto Shippuden" in p.get("title")

    assert has_video_extension("movie.mkv") is True
    assert has_video_extension("clip.webm") is True
    assert has_video_extension("sample.ts") is True
    assert has_video_extension("image.png") is False

def test_dimension_resolution_detection():
    from Backend.helper.metadata.parse import resolution_from_dimensions, parse_media_name
    # Dimension calculations
    assert resolution_from_dimensions(3840, 2160) == "4K"
    assert resolution_from_dimensions(3840, 1600) == "4K"
    assert resolution_from_dimensions(2560, 1440) == "1440p"
    assert resolution_from_dimensions(1920, 1080) == "1080p"
    assert resolution_from_dimensions(1920, 800) == "1080p"
    assert resolution_from_dimensions(1280, 720) == "720p"
    assert resolution_from_dimensions(1280, 534) == "720p"
    assert resolution_from_dimensions(720, 576) == "576p"
    assert resolution_from_dimensions(720, 480) == "480p"
    assert resolution_from_dimensions(640, 480) == "480p"
    assert resolution_from_dimensions(640, 360) == "360p"

    # Extraction from filename without standard 'p' tag
    p1 = parse_media_name("Show.S01E01.1920x1080.mkv")
    assert p1.get("quality") == "1080p"
    p2 = parse_media_name("Movie.1280x720.BluRay.mkv")
    assert p2.get("quality") == "720p"
    p3 = parse_media_name("Anime.720x576.DVD.mkv")
    assert p3.get("quality") == "576p"
    p4 = parse_media_name("Anime.720x480.DVD.mkv")
    assert p4.get("quality") == "480p"

def test_m3u_export_formatting():
    # Verify M3U structure format
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    show_title = "My Hero Academia"
    s_num, ep_num = 1, 1
    ep_title = "Izuku Midoriya: Origin"
    quality_str = "1080p"
    stream_url = "http://127.0.0.1:8000/dl/TOKEN/STREAM_ID/video.mkv"
    display_name = f"{show_title} - S{s_num:02d}E{ep_num:02d} - {ep_title} [{quality_str}]"
    lines.append(f'#EXTINF:-1 tvg-id="65930" tvg-name="{show_title} S{s_num:02d}E{ep_num:02d}" tvg-season="{s_num}" tvg-episode="{ep_num}" group-title="Season {s_num}",{display_name}')
    lines.append("#EXTVLCOPT:http-user-agent=Stremio")
    lines.append(stream_url)
    m3u_text = "\n".join(lines)
    assert "#EXTM3U" in m3u_text
    assert "S01E01" in m3u_text
    assert stream_url in m3u_text

def test_batch_caption_helper():
    from Backend.helper.caption_helper import generate_batch_captions

    raw_files = """[Erai-raws] Sousou no Frieren - 01 [1080p][HEVC].mkv
[Erai-raws] Sousou no Frieren - 02 [1080p][HEVC].mkv
S01E03-The Land Where Souls Rest [AA0D3FB7].mkv"""

    res = generate_batch_captions(
        title="Sousou no Frieren",
        tmdb_id="209867",
        season=1,
        filenames=raw_files,
        format_style="tmdb_tag"
    )

    assert res["count"] == 3
    assert res["items"][0]["episode"] == 1
    assert "{tmdb-209867} Sousou no Frieren S01E01 [1080p]" in res["items"][0]["caption"]
    assert "{tmdb-209867} Sousou no Frieren S01E02 [1080p]" in res["items"][1]["caption"]
    assert "The Land Where Souls Rest" in res["items"][2]["caption"]

    # Test sequential generation without filenames
    res_seq = generate_batch_captions(
        title="Bleach",
        tmdb_id="30984",
        season=2,
        start_ep=1,
        end_ep=4,
        format_style="tmdb_tag"
    )
    assert res_seq["count"] == 4
    assert "{tmdb-30984} Bleach S02E01 [1080p]" in res_seq["items"][0]["caption"]
    assert "{tmdb-30984} Bleach S02E04 [1080p]" in res_seq["items"][3]["caption"]

def test_duplicate_scoring_and_ranking():
    from Backend.helper.quality_checker import QualityChecker

    qualities = [
        {"id": "id1", "name": "Bleach.S01E01.1080p.HDCAM.x264.mkv", "size": "1.5GB", "quality": "1080p"},
        {"id": "id2", "name": "Bleach.S01E01.1080p.BluRay.10bit.HEVC.Dual-Audio.mkv", "size": "2.2GB", "quality": "1080p"},
    ]

    scored = []
    for q in qualities:
        parsed = QualityChecker.parse_filename(q["name"])
        score = QualityChecker.calculate_total_score(parsed)
        size_mb = QualityChecker.parse_file_size(q["size"])
        scored.append({"id": q["id"], "score": score, "size_mb": size_mb, "parsed": parsed})

    scored.sort(key=lambda x: (x["score"], -x["size_mb"]), reverse=True)
    # The BluRay 10bit HEVC Dual-Audio file should rank 1st with a much higher score
    assert scored[0]["id"] == "id2"
    assert scored[0]["score"] > scored[1]["score"]
    assert scored[0]["parsed"]["source_tier"] > scored[1]["parsed"]["source_tier"]

@pytest.mark.anyio
async def test_anilist_provider():
    from Backend.helper.metadata.providers.anilist import search_anilist

    results = await search_anilist("Sousou no Frieren")
    assert len(results) > 0
    top = results[0]
    assert "Frieren" in top["title"] or "Sousou no Frieren" in top["romaji_title"]
    assert top["anilist_id"] is not None
    assert top["studio"] != ""
    assert top["anilist_score"] is not None

def test_require_tmdb_id_coercion():
    from Backend.fastapi.routes.api_routes import _require_tmdb_id, _coerce_tmdb_id
    from fastapi import HTTPException

    assert _coerce_tmdb_id(12345) == 12345
    assert _coerce_tmdb_id("12345") == 12345
    assert _coerce_tmdb_id("-123") == -123
    assert _coerce_tmdb_id(None) is None
    assert _coerce_tmdb_id("null") is None
    assert _coerce_tmdb_id("undefined") is None
    assert _coerce_tmdb_id("invalid") is None

    assert _require_tmdb_id(209867) == 209867
    assert _require_tmdb_id("209867") == 209867
    with pytest.raises(HTTPException):
        _require_tmdb_id(None)

def test_caption_helper_edge_cases():
    from Backend.helper.caption_helper import generate_batch_captions

    # Edge case: empty string
    res_empty = generate_batch_captions(title="Test Show", filenames="", format_style="standard")
    assert res_empty["count"] >= 1

    # Edge case: movie type
    res_movie = generate_batch_captions(title="Your Name", tmdb_id=372058, media_type="movie", format_style="tmdb_tag")
    assert res_movie["items"][0]["caption"] == "{tmdb-372058} Your Name Movie [1080p]"

    # Edge case: rich telegram markdown
    res_md = generate_batch_captions(title="Bleach", season=1, start_ep=1, end_ep=1, format_style="telegram_markdown")
    assert "🎬 **Bleach**" in res_md["items"][0]["caption"]
    assert "#Bleach" in res_md["items"][0]["caption"]

def test_nyaa_id_and_manifest_extraction():
    from Backend.helper.nyaa_resolver import extract_nyaa_id, _CLEAN_TAGS_RE

    assert extract_nyaa_id("https://nyaa.si/view/1859137") == "1859137"
    assert extract_nyaa_id("http://nyaa.si/view/1859137") == "1859137"
    assert extract_nyaa_id("1859137") == "1859137"
    assert extract_nyaa_id("/view/2094924") == "2094924"
    assert extract_nyaa_id("invalid") is None

    # Test title cleanup for Ember / Nyaa releases
    raw_title = "[EMBER] Frieren: Beyond Journey's End (2023) (Season 1) [BDRip] [1080p Dual Audio HEVC 10 bits DDP] (Sousou no Frieren) (Batch)"
    cleaned = _CLEAN_TAGS_RE.sub("", raw_title)
    assert "EMBER" not in cleaned
    assert "BDRip" not in cleaned
    assert "Frieren: Beyond Journey's End" in cleaned

@pytest.mark.anyio
async def test_nyaa_live_manifest_fetch():
    from Backend.helper.nyaa_resolver import fetch_nyaa_manifest

    manifest = await fetch_nyaa_manifest("https://nyaa.si/view/1859137")
    assert manifest["nyaa_id"] == "1859137"
    assert manifest["group"] == "EMBER"
    assert manifest["season"] == 1
    assert manifest["file_count"] >= 20
    assert "The Journey's End" in manifest["files"][0]["episode_title"] or manifest["files"][0]["episode"] == 1

@pytest.mark.anyio
async def test_anime_season_mapping_noragami():
    from Backend.helper.metadata import metadata

    # Test Noragami Aragoto (Season 2) episodes do NOT get dumped into Season 0 (Specials)
    m_aragoto = await metadata("[KH] Noragami Aragoto 03 False Bond.mkv", 12345, 101)
    assert m_aragoto["season_number"] == 2
    assert m_aragoto["episode_number"] == 3
    assert m_aragoto["episode_title"] == "False Bond"

    # Test Noragami (Season 1) regular episodes
    m_s1 = await metadata("[KH] Noragami 05 Borderline.mkv", 12345, 102)
    assert m_s1["season_number"] == 1
    assert m_s1["episode_number"] == 5
    assert m_s1["episode_title"] == "Borderline"

def test_numbered_release_groups_and_danganronpa():
    from Backend.helper.nyaa_resolver import parse_episode_from_filename

    # Test [o7] group does NOT get falsely identified as episode 7
    s, ep, _ = parse_episode_from_filename("[o7] DANGANRONPA THE ANIMATION - 01 (BD 1080P X264 10-BIT FLAC) [761F64E4].mkv", default_season=1)
    assert ep == 1
    assert s == 1

    s2, ep2, _ = parse_episode_from_filename("[o7] Danganronpa The Animation - 02 (BD 1080p x264 10 bit FLAC) [4D83EB06].mkv", default_season=1)
    assert ep2 == 2
    assert s2 == 1

    s13, ep13, _ = parse_episode_from_filename("[o7] Danganronpa The Animation 13 (BD 1080p x264 10 bit FLAC) [51AFF0B2].mkv", default_season=1)
    assert ep13 == 13
    assert s13 == 1


def test_thumb_target_and_message_has_thumb():
    from Backend.helper.pyro import message_has_thumb, get_thumb_download_target, get_video_cover
    from types import SimpleNamespace

    # Case 1: Video with thumbs list
    thumb1 = SimpleNamespace(file_id="thumb_id_1")
    thumb2 = SimpleNamespace(file_id="thumb_id_2")
    msg_with_thumb = SimpleNamespace(video=SimpleNamespace(thumbs=[thumb1, thumb2], cover=None), document=None)
    assert message_has_thumb(msg_with_thumb) is True
    assert get_thumb_download_target(msg_with_thumb) == thumb2

    # Case 2: Video with cover attribute
    cover_obj = SimpleNamespace(file_id="cover_id_1")
    msg_with_cover = SimpleNamespace(video=SimpleNamespace(thumbs=None, cover=cover_obj), document=None)
    assert message_has_thumb(msg_with_cover) is True
    assert get_video_cover(msg_with_cover) == cover_obj
    assert get_thumb_download_target(msg_with_cover) == cover_obj

    # Case 3: Document without thumbs
    msg_no_thumb = SimpleNamespace(video=None, document=SimpleNamespace(thumbs=None, cover=None))
    assert message_has_thumb(msg_no_thumb) is False
    assert get_thumb_download_target(msg_no_thumb) is None


def test_telegram_message_range_and_topic_link_parsing():
    from Backend.helper.manual_add import (
        parse_telegram_link,
        extract_msg_id,
        extract_tg_channel_and_msg,
        parse_tg_range_spec,
    )

    # 1. Topic URL parsing: https://t.me/c/1234567890/3363/3472
    chat_ref, msg_id = parse_telegram_link("https://t.me/c/1234567890/3363/3472")
    assert chat_ref == -1001234567890
    assert msg_id == 3472

    # 2. Extract msg ID directly from topic link instead of just 3472
    assert extract_msg_id("https://t.me/c/1234567890/3363/3472") == 3472
    assert extract_msg_id("https://t.me/c/1234567890/3472") == 3472
    assert extract_msg_id("https://t.me/c/1234567890/3363/3472?single") == 3472
    assert extract_msg_id("3472") == 3472
    assert extract_msg_id(3472) == 3472

    # 3. Channel & Message extraction
    ch, msg = extract_tg_channel_and_msg("https://t.me/c/1234567890/3363/3472")
    assert ch == "-1001234567890"
    assert msg == 3472

    # 4. Range specification with two full links
    ch_r, s_id, e_id = parse_tg_range_spec(
        "https://t.me/c/1234567890/3363 https://t.me/c/1234567890/3363/3472"
    )
    assert ch_r == "-1001234567890"
    assert s_id == 3363
    assert e_id == 3472

    # 5. Hyphenated range or single link
    ch_h, s_h, e_h = parse_tg_range_spec("https://t.me/c/1234567890/3363-3472")
    assert ch_h == "-1001234567890"
    assert s_h == 3363
    assert e_h == 3472

    ch_single, s_single, e_single = parse_tg_range_spec("https://t.me/c/1234567890/3363/3472")
    assert ch_single == "-1001234567890"
    assert s_single == 3472
    assert e_single == 3472


def test_complex_naming_patterns_and_nyaa_parsing():
    from Backend.helper.nyaa_resolver import parse_episode_from_filename
    from Backend.helper.metadata.parse import parse_media_name, extract_absolute_episode, clean_anime_search_title

    # 1. Danganronpa 3 with number 3 in title
    s, ep, _ = parse_episode_from_filename(
        "Danganronpa 3 - The End of Kibougamine Gakuen - Zetsubou-hen - 04 (1920x1080 HEVC2 10bit AAC).mkv",
        default_season=1
    )
    assert ep == 4
    assert s == 1

    s2, ep2, _ = parse_episode_from_filename(
        "Danganronpa 3 - The End of Kibougamine Gakuen - Mirai-hen - 02 (1080p).mkv",
        default_season=1
    )
    assert ep2 == 2
    assert s2 == 1

    # 2. Numbered show titles like 91 Days and 5 Centimeters
    s3, ep3, _ = parse_episode_from_filename("91 Days 01v2.mkv", default_season=1)
    assert ep3 == 1
    assert s3 == 1

    s4, ep4, _ = parse_episode_from_filename("[UDF] 91 Days - 12.mkv", default_season=1)
    assert ep4 == 12
    assert s4 == 1

    # 3. Year stripping so movie years are not mistaken for episodes in Nyaa manifest parser
    s5, ep5, _ = parse_episode_from_filename("Inception.2010.1080p.BluRay.x264.DTS-WiKi.mkv", default_season=1)
    assert ep5 is None

    # 4. Telegram watermarks & hashtags cleaning
    parsed = parse_media_name("#Movie #Action @AnimeChannel [Ember] Frieren 01 (1080p) #DualAudio.mkv")
    abs_ep = extract_absolute_episode("#Movie #Action @AnimeChannel [Ember] Frieren 01 (1080p) #DualAudio.mkv", parsed)
    assert abs_ep == 1
    clean_title = clean_anime_search_title(parsed.get("title") or "", abs_ep)
    assert clean_title == "Frieren"

    # 5. Raizel Blue Exorcist with underscores, full/truncated CRC32 hashes (from user report)
    raizel_cases = [
        ("[RAIZEL]_BLUE_EXORCIST__01_(1080P_DUAL_AUDIO_FLAC)_(8.MKV", 1),
        ("[RAIZEL]_BLUE_EXORCIST__02_(1080P_DUAL_AUDIO_FLAC)_(0.MKV", 2),
        ("[RAIZEL]_BLUE_EXORCIST__03V2_(1080P_DUAL_AUDIO_FLAC)_.MKV", 3),
        ("[RAIZEL]_BLUE_EXORCIST__04_(1080P_DUAL_AUDIO_FLAC)_(E.MKV", 4),
        ("[Raizel]_Blue_Exorcist_16_(1080p_Dual_Audio_FLAC)_(37CDD627).mkv", 16),
        ("[Raizel]_Blue_Exorcist_01_(1080p_Dual_Audio_FLAC)_(8BD2BE64).mkv", 1),
        ("[Raizel]_Blue_Exorcist_02_(1080p_Dual_Audio_FLAC)_(07559DF0).mkv", 2),
        ("[Raizel]_Blue_Exorcist_16_(1080p_Dual_Audio_FLAC)_(37.mkv", 16),
        ("[Raizel]_Blue_Exorcist_25_(1080p_Dual_Audio_FLAC)_(A1B2C3D4).mkv", 25),
    ]
    for fn, expected in raizel_cases:
        s_r, ep_r, _ = parse_episode_from_filename(fn, default_season=1)
        assert s_r == 1
        assert ep_r == expected, f"Failed for {fn}: expected ep {expected}, got {ep_r}"


@pytest.mark.anyio
async def test_split_movie_and_series_m3u8_expansion():
    from Backend.helper.encrypt import encode_string, decode_string

    # 1. Simulate a 4-part split movie (e.g. Demon Slayer Infinity Castle 13.5GB in 4 parts)
    parts = [
        {"part_number": 1, "chat_id": 1234567890, "msg_id": 3529, "size_bytes": 3600000000},
        {"part_number": 2, "chat_id": 1234567890, "msg_id": 3530, "size_bytes": 3600000000},
        {"part_number": 3, "chat_id": 1234567890, "msg_id": 3531, "size_bytes": 3600000000},
        {"part_number": 4, "chat_id": 1234567890, "msg_id": 3532, "size_bytes": 2730000000},
    ]
    combined_id = await encode_string({"parts": [{"chat_id": p["chat_id"], "msg_id": p["msg_id"]} for p in parts]})

    # Verify decoding works
    decoded = await decode_string(combined_id)
    assert len(decoded["parts"]) == 4

    # Verify each part can be individually encoded
    part_urls = []
    base_url = "http://127.0.0.1:8000"
    token = "test_token"
    for p in parts:
        pid = await encode_string({"chat_id": p["chat_id"], "msg_id": p["msg_id"]})
        part_urls.append(f"{base_url}/dl/{token}/{pid}/video.mkv")
    
    assert len(part_urls) == 4
    assert len(set(part_urls)) == 4


def test_virtual_dl_part_count_calculation():
    chunk_size = 1024 * 1024 # 1MB

    # Case 1: Exact boundary (1048576 bytes)
    local_start = 0
    local_end = 1048576 # 1MB + 1 byte
    offset = local_start - (local_start % chunk_size)
    part_count = (local_end // chunk_size) - (offset // chunk_size) + 1
    assert part_count == 2

    # Case 2: Zero offset, small read
    local_start = 0
    local_end = 1023
    offset = local_start - (local_start % chunk_size)
    part_count = (local_end // chunk_size) - (offset // chunk_size) + 1
    assert part_count == 1

    # Case 3: Byte 0 only
    local_start = 0
    local_end = 0
    offset = local_start - (local_start % chunk_size)
    part_count = (local_end // chunk_size) - (offset // chunk_size) + 1
    assert part_count == 1

    # Case 4: Seeking inside part 3 (e.g. 2.7GB into split file)
    local_start = 2729000000
    local_end = 2730000000
    offset = local_start - (local_start % chunk_size)
    part_count = (local_end // chunk_size) - (offset // chunk_size) + 1
    assert part_count == 2


def test_season2mpv_multipart_movie_picking():
    from season2mpv import pick_all_part_streams

    # Sample stream list with 4 parts and donation/proxy noise
    sample_streams = [
        {"name": "Telegram 1080p (Donation / Limit)", "url": "http://127.0.0.1:8000/dl/tok/001/video.mkv"},
        {"name": "Telegram 1080p (Proxy)", "url": "http://127.0.0.1:8000/proxy/dl/tok/002/video.mkv"},
        {"name": "Telegram 1080p Part 1/4", "title": "Part 1/4 · 3.4GB", "url": "http://127.0.0.1:8000/dl/tok/part1/video.mkv"},
        {"name": "Telegram 1080p Part 2/4", "title": "Part 2/4 · 3.4GB", "url": "http://127.0.0.1:8000/dl/tok/part2/video.mkv"},
        {"name": "Telegram 1080p Part 3/4", "title": "Part 3/4 · 3.4GB", "url": "http://127.0.0.1:8000/dl/tok/part3/video.mkv"},
        {"name": "Telegram 1080p Part 4/4", "title": "Part 4/4 · 3.3GB", "url": "http://127.0.0.1:8000/dl/tok/part4/video.mkv"},
    ]

    picked = pick_all_part_streams(sample_streams)
    assert len(picked) == 4
    assert "part1" in picked[0]["url"]
    assert "part2" in picked[1]["url"]
    assert "part3" in picked[2]["url"]
    assert "part4" in picked[3]["url"]


@pytest.mark.anyio
async def test_jellyfin_library_strm_and_nfo_generation(tmp_path):
    from Backend.helper.jellyfin_sync import _sanitize_path_name, sync_jellyfin_to_disk, build_jellyfin_zip_buffer
    from unittest.mock import patch
    import Backend

    assert _sanitize_path_name("Demon Slayer: Kimetsu no Yaiba / Movie?") == "Demon Slayer - Kimetsu no Yaiba - Movie"

    mock_movies = [
        {
            "title": "Demon Slayer - Infinity Castle",
            "release_year": 2025,
            "imdb_id": "tt123456",
            "tmdb_id": 99999,
            "description": "Epic movie",
            "telegram": [
                {
                    "quality": "1080p",
                    "id": "movie_stream_token",
                    "parts": [
                        {"chat_id": -100123, "msg_id": 1, "part_number": 1},
                        {"chat_id": -100123, "msg_id": 2, "part_number": 2}
                    ]
                }
            ]
        }
    ]

    mock_series = [
        {
            "title": "Tomodachi Game",
            "release_year": 2022,
            "imdb_id": "tt987654",
            "tmdb_id": 137718,
            "seasons": [
                {
                    "season_number": 1,
                    "episodes": [
                        {
                            "episode_number": 1,
                            "title": "Episode 1",
                            "telegram": [{"quality": "1080p", "id": "ep1_stream_token"}]
                        }
                    ]
                }
            ]
        }
    ]

    class AsyncMockCursor:
        def __init__(self, data):
            self.data = data
        def __aiter__(self):
            return self._iter()
        async def _iter(self):
            for item in self.data:
                yield item

    mock_storage = {
        "movie": type("MockCol", (), {"find": lambda self, q: AsyncMockCursor(mock_movies)})(),
        "tv": type("MockCol", (), {"find": lambda self, q: AsyncMockCursor(mock_series)})()
    }

    with patch.dict(Backend.db.dbs, {"storage_1": mock_storage}):
        target_dir = str(tmp_path / "jellyfin_test")
        stats = await sync_jellyfin_to_disk(target_dir, token="test_token_123", include_nfos=True)
        
        assert stats["total_files"] >= 5
        assert stats["strm_files"] >= 3
        assert stats["nfo_files"] >= 3

        # Verify TV show files created
        import os
        tv_show_dir = os.path.join(target_dir, "TV-Shows", "Tomodachi Game (2022)")
        assert os.path.exists(os.path.join(tv_show_dir, "tvshow.nfo"))
        assert os.path.exists(os.path.join(tv_show_dir, "Season 01", "season.nfo"))
        assert os.path.exists(os.path.join(tv_show_dir, "Season 01", "Tomodachi Game - S01E01 - Episode 1.nfo"))
        assert os.path.exists(os.path.join(tv_show_dir, "Season 01", "Tomodachi Game - S01E01 - Episode 1 [1080p].strm"))

        # Verify movie files created
        movie_dir = os.path.join(target_dir, "Movies", "Demon Slayer - Infinity Castle (2025)")
        assert os.path.exists(os.path.join(movie_dir, "movie.nfo"))
        assert os.path.exists(os.path.join(movie_dir, "Demon Slayer - Infinity Castle (2025) - Part 1 [1080p].strm"))
        assert os.path.exists(os.path.join(movie_dir, "Demon Slayer - Infinity Castle (2025) - Part 2 [1080p].strm"))

        # Verify zip buffer creation
        buf = await build_jellyfin_zip_buffer(token="test_token_123", include_nfos=True)
        assert buf.getbuffer().nbytes > 500


@pytest.mark.anyio
async def test_auto_sync_all_server_and_anilist(tmp_path):
    from Backend.helper.auto_sync import sync_all_server, get_auto_sync_status
    from unittest.mock import patch, AsyncMock
    import Backend

    mock_movies = [
        {
            "_id": "m1",
            "title": "Demon Slayer - Infinity Castle",
            "release_year": 2025,
            "imdb_id": "tt123456",
            "tmdb_id": 99999,
            "is_anime": True,
            "telegram": [{"quality": "1080p", "id": "movie_tok"}]
        }
    ]

    mock_series = [
        {
            "_id": "s1",
            "title": "Solo Leveling",
            "release_year": 2024,
            "imdb_id": "tt21209876",
            "tmdb_id": 209867,
            "is_anime": True,
            "seasons": [
                {
                    "season_number": 1,
                    "episodes": [
                        {"episode_number": 1, "title": "Episode 1", "telegram": [{"quality": "1080p", "id": "ep1_tok"}]}
                    ]
                }
            ]
        }
    ]

    class AsyncMockCursor:
        def __init__(self, data):
            self.data = data
        def __aiter__(self):
            return self._iter()
        async def _iter(self):
            for item in self.data:
                yield item

    mock_storage = {
        "movie": type("MockCol", (), {
            "find": lambda self, q: AsyncMockCursor(mock_movies),
            "update_one": AsyncMock()
        })(),
        "tv": type("MockCol", (), {
            "find": lambda self, q: AsyncMockCursor(mock_series),
            "update_one": AsyncMock()
        })()
    }

    with patch.dict(Backend.db.dbs, {"storage_1": mock_storage}):
        with patch("Backend.helper.auto_sync.search_anilist", AsyncMock(return_value=[{
            "anilist_id": 151807,
            "mal_id": 52299,
            "romaji_title": "Ore dake Level Up na Ken",
            "native_title": "俺だけレベルアップな件",
            "studio": "A-1 Pictures",
            "status": "Finished Airing",
            "anilist_score": "84%",
            "site_url": "https://anilist.co/anime/151807"
        }])):
            target_dir = str(tmp_path / "auto_sync_test")
            res = await sync_all_server(target_dir=target_dir, token="test_token_123", sync_anilist=True, sync_jellyfin=True)

            assert res["status"] == "success"
            assert "jellyfin" in res
            assert "anilist" in res
            assert res["jellyfin"]["total_files"] >= 4
            assert res["anilist"]["scanned_titles"] >= 1

            status = get_auto_sync_status()
            assert "is_syncing" in status
            assert "stats" in status


@pytest.mark.anyio
async def test_direct_tmdb_resolver_and_nced_filtering():
    from Backend.helper.nyaa_resolver import parse_episode_from_filename, inspect_topic_range
    from unittest.mock import AsyncMock, MagicMock

    # 1. Test NCED / NCOP filtering
    s, ep, title = parse_episode_from_filename("[KH] Noragami - NCED - 01.mkv", default_season=1)
    assert ep is None
    assert "Bonus" in title or "Extra" in title

    # 2. Test clean episode parsing
    s, ep, title = parse_episode_from_filename("[KH] NORAGAMI - 01 - A HOUSECAT, A STRAY GOD, AND A .MKV", default_season=1)
    assert s == 1
    assert ep == 1
    assert "mkv" not in title.lower()

    # 3. Test inspect_topic_range standalone with Telegram link and TMDb ID
    mock_msg1 = MagicMock()
    mock_msg1.id = 4844
    mock_msg1.empty = False
    mock_msg1.document = MagicMock()
    mock_msg1.document.file_name = "[KH] NORAGAMI - 01 - A HOUSECAT, A STRAY GOD, AND A .MKV"
    mock_msg1.document.file_size = 1900000000
    mock_msg1.caption = None
    mock_msg1.video = None
    mock_msg1.audio = None

    mock_msg2 = MagicMock()
    mock_msg2.id = 4845
    mock_msg2.empty = False
    mock_msg2.document = MagicMock()
    mock_msg2.document.file_name = "[KH] NORAGAMI - 02 - SNOW-LIKE.MKV"
    mock_msg2.document.file_size = 1900000000
    mock_msg2.caption = None
    mock_msg2.video = None
    mock_msg2.audio = None

    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=[mock_msg1, mock_msg2])

    plan = await inspect_topic_range(
        client=mock_client,
        channel_id=-1001234567890,
        start_msg_id="https://t.me/c/1234567890/4844",
        end_msg_id="https://t.me/c/1234567890/4845",
        nyaa_url_or_id=None,
        override_tmdb_id=64710,
    )

    assert plan["status"] == "success"
    assert plan["aligned_count"] == 2
    assert "{tmdb-64710}" in plan["items"][0]["proposed_caption"]
    assert "S01E01" in plan["items"][0]["proposed_caption"]
    assert "NCED" not in plan["items"][0]["proposed_caption"]
    assert "S01E02" in plan["items"][1]["proposed_caption"]


def test_stream_formatter_presets_and_custom():
    from Backend.helper.stream_formatter import format_stream

    sample_filename = "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR10.HEVC.TrueHD.Atmos.7.1.ITA.ENG-GROUP.mkv"
    sample_size = 65432100000

    # Test Default returns None so built-in formatter is used
    default_res = format_stream("default", sample_filename, sample_size)
    assert default_res is None

    # Test Prism preset formatting
    prism_name, prism_desc = format_stream("prism", sample_filename, sample_size, addon_name="TG")
    assert "4K" in prism_name or "UHD" in prism_name or "TG" in prism_name
    assert len(prism_desc) > 0

    # Test Torrentio preset formatting
    torrentio_name, torrentio_desc = format_stream("torrentio", sample_filename, sample_size, addon_name="TG")
    assert "TG" in torrentio_name
    assert "2160p" in torrentio_name or "HDR" in torrentio_name

    # Test Custom template formatting
    custom_name, custom_desc = format_stream(
        "custom",
        sample_filename,
        sample_size,
        custom_name="⭐ {addon.name} | {stream.resolution}",
        custom_description="🎬 {stream.filename} 📦 {stream.size::sbytes}",
        addon_name="TG",
    )
    assert "⭐ TG | 2160p" in custom_name
    assert "Dune.Part.Two" in custom_desc
    assert "GB" in custom_desc


@pytest.mark.asyncio
async def test_spatial_poster_resolution():
    from Backend.fastapi.routes.stremio_routes import _poster_url
    from Backend.helper.settings_manager import SettingsManager, Settings

    old_current = SettingsManager.current()
    try:
        data = old_current.to_dict()
        data["spatial_poster_enabled"] = True
        data["spatial_poster"] = "https://spatial.example.com/api/poster/{type}/{imdb_id}?token=123"
        SettingsManager._current = Settings(data)

        poster = _poster_url("tt15239678", "https://image.tmdb.org/fallback.jpg", "movie")
        assert poster == "https://spatial.example.com/api/poster/movie/tt15239678?token=123"

        series_poster = _poster_url("tt0944947", "https://image.tmdb.org/fallback.jpg", "series")
        assert series_poster == "https://spatial.example.com/api/poster/series/tt0944947?token=123"
    finally:
        SettingsManager._current = old_current


@pytest.mark.asyncio
async def test_formatter_preview_endpoint():
    from httpx import AsyncClient, ASGITransport
    from Backend.fastapi.main import app
    from Backend.fastapi.security.credentials import require_auth

    app.dependency_overrides[require_auth] = lambda: True
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(
                "/api/admin/settings/formatter-preview",
                json={
                    "preset": "custom",
                    "name": "MY-NAME [{stream.resolution}]",
                    "description": "{stream.filename} - {stream.size::sbytes}",
                    "filename": "Frieren.S01E01.1080p.CR.WEB-DL.mkv",
                },
            )
            assert res.status_code == 200
            data = res.json()
            assert "MY-NAME [1080p]" in data["name"]
            assert "Frieren" in data["description"]
            assert "GB" in data["description"] or "MB" in data["description"]
    finally:
        app.dependency_overrides.pop(require_auth, None)


@pytest.mark.asyncio
async def test_split_movie_and_episode_metadata_resolution():
    from Backend.helper.metadata.entry import metadata

    # 1. Split movie parts should NOT be misidentified as TV episodes
    m1 = await metadata("Demon Slayer Infinity Castle Part 1.mkv", 100123, 101)
    m2 = await metadata("Demon Slayer Infinity Castle Part 2.mkv", 100123, 102)

    assert m1 is not None and m2 is not None
    assert m1["media_type"] == "movie"
    assert m2["media_type"] == "movie"
    assert m1["group_key"] == m2["group_key"]
    assert m1["part_number"] == 1
    assert m2["part_number"] == 2

    # 2. Split TV episode parts should correctly keep S01E01 and attach part numbers
    ep1 = await metadata("[Group] Noragami - S01E01 Part 1 [1080p].mkv", 100123, 201)
    ep2 = await metadata("[Group] Noragami - S01E01 Part 2 [1080p].mkv", 100123, 202)

    assert ep1 is not None and ep2 is not None
    assert ep1["media_type"] == "tv"
    assert ep2["media_type"] == "tv"
    assert ep1["season_number"] == 1
    assert ep1["episode_number"] == 1
    assert ep1["group_key"] == ep2["group_key"]
    assert ep1["part_number"] == 1
    assert ep2["part_number"] == 2


@pytest.mark.asyncio
async def test_consolidate_multiparts_endpoint():
    from httpx import AsyncClient, ASGITransport
    from Backend.fastapi.main import app
    from Backend.fastapi.security.credentials import require_auth
    from unittest.mock import AsyncMock, patch

    app.dependency_overrides[require_auth] = lambda: True
    try:
        with patch("Backend.helper.database.Database.consolidate_multiparts", new_callable=AsyncMock) as mock_cons:
            mock_cons.return_value = {
                "status": "success",
                "consolidated_movies": 2,
                "consolidated_episodes": 3,
                "migrated_from_tv": 1,
                "message": "Multi-part consolidation complete",
            }
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                res = await client.post("/api/admin/tools/consolidate-multiparts")
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "success"
                assert data["consolidated_movies"] == 2
    finally:
        app.dependency_overrides.pop(require_auth, None)


@pytest.mark.asyncio
async def test_list_media_api_projection_and_pagination():
    from unittest.mock import MagicMock, AsyncMock
    from Backend.fastapi.routes.api_routes import list_media_api
    import Backend.fastapi.routes.api_routes as ar
    
    mock_db = MagicMock()
    mock_db.sort_movies = AsyncMock(return_value={'total_count': 1, 'movies': [{'title': 'Test Movie', 'tmdb_id': 123}]})
    mock_db.sort_tv_shows = AsyncMock(return_value={'total_count': 1, 'tv_shows': [{'name': 'Test TV', 'tmdb_id': 456}]})
    
    orig_db = ar.db
    try:
        ar.db = mock_db
        res_movie = await list_media_api(media_type='movie', page=1, page_size=24, search='', custom=False)
        assert res_movie['total_count'] == 1
        assert res_movie['movies'][0]['title'] == 'Test Movie'
        mock_db.sort_movies.assert_called_once()
        
        res_tv = await list_media_api(media_type='tv', page=1, page_size=24, search='', custom=False)
        assert res_tv['total_count'] == 1
        assert res_tv['tv_shows'][0]['name'] == 'Test TV'
        mock_db.sort_tv_shows.assert_called_once()
    finally:
        ar.db = orig_db


def test_kitsu_abbreviation_is_weak_alias():
    # Kitsu lists "Demon Slayer" as an abbreviatedTitle of the 2016 show
    # "Onigiri". An abbreviation match must not outscore the real franchise.
    from Backend.helper.metadata.providers.kitsu import _title_score

    onigiri = {
        "canonicalTitle": "Onigiri",
        "titles": {"en": "Onigiri", "en_jp": "Onigiri", "ja_jp": "鬼斬"},
        "abbreviatedTitles": ["Demon Cutter", "Demon Slayer"],
        "slug": "onigiri",
    }
    kimetsu = {
        "canonicalTitle": "Kimetsu no Yaiba",
        "titles": {
            "en": "Demon Slayer: Kimetsu no Yaiba",
            "en_jp": "Kimetsu no Yaiba",
            "ja_jp": "鬼滅の刃",
        },
        "abbreviatedTitles": ["KnY"],
        "slug": "kimetsu-no-yaiba",
    }

    onigiri_score = _title_score("Demon Slayer", onigiri)
    kimetsu_score = _title_score("Demon Slayer", kimetsu)

    assert onigiri_score < 1.0
    assert kimetsu_score >= onigiri_score

    # Canonical titles and official translations still match exactly
    assert _title_score("Onigiri", onigiri) == 1.0


@pytest.mark.asyncio
async def test_tmdb_skips_lookups_without_api_key():
    from Backend.helper.metadata.providers import tmdb

    assert tmdb.tmdb_api_key() == ""
    assert await tmdb.safe_search("Demon Slayer", "tv") is None
    assert await tmdb.details("tv", 64710) is None
    assert await tmdb.raw_search("Demon Slayer", "tv", None) == []


def _dig(doc, dotted_key):
    cur = doc
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _doc_matches(doc, filt):
    if not filt:
        return True
    if "$and" in filt:
        return all(_doc_matches(doc, c) for c in filt["$and"])
    if "$or" in filt:
        return any(_doc_matches(doc, c) for c in filt["$or"])
    for key, cond in filt.items():
        value = _dig(doc, key)
        if "$regex" in cond:
            flags = re.I if cond.get("$options") == "i" else 0
            if not isinstance(value, str) or not re.search(cond["$regex"], value, flags):
                return False
        elif value != cond:
            return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs
        self._skip = 0
        self._limit = None

    def sort(self, _sort):
        return self

    def skip(self, n):
        self._skip = n
        return self

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, _):
        out = self._docs[self._skip:]
        return out[: self._limit] if self._limit else out


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    async def count_documents(self, filt):
        return len([d for d in self.docs if _doc_matches(d, filt)])

    def find(self, filt, projection=None):
        return _FakeCursor([d for d in self.docs if _doc_matches(d, filt)])


@pytest.mark.asyncio
async def test_search_documents_pagination_and_special_chars():
    # Regression: media search used undefined gather/self._count and raised
    # a 500 whenever the search box was non-empty.
    from Backend.helper.database import Database

    tv_docs = [
        {"_id": 1, "media_type": "tv", "title": "Monster", "tmdb_id": 11, "updated_on": 2},
        {"_id": 2, "media_type": "tv", "title": "Monster Musume", "tmdb_id": 12, "updated_on": 1},
        {"_id": 3, "media_type": "tv", "title": "Death Note", "tmdb_id": 13, "updated_on": 0},
    ]
    movie_docs = [
        {"_id": 4, "media_type": "movie", "title": "Monsters Inc", "tmdb_id": 14, "updated_on": 5},
    ]

    db = Database.__new__(Database)
    db.current_db_index = 1
    db.dbs = {"storage_1": {"tv": _FakeCollection(tv_docs), "movie": _FakeCollection(movie_docs)}}

    tv = await db.search_documents("monster", 1, 24, media_type="tv")
    assert tv["total_count"] == 2
    assert {d["title"] for d in tv["results"]} == {"Monster", "Monster Musume"}

    both = await db.search_documents("monster", 1, 20)
    assert both["total_count"] == 3
    assert both["results"][0]["title"] == "Monsters Inc"  # most recent first

    # Regex metacharacters are escaped instead of raising a server error
    special = await db.search_documents("monster(", 1, 20, media_type="tv")
    assert special["results"] == []

    blank = await db.search_documents("   ", 1, 20)
    assert blank == {"total_count": 0, "results": []}

    page1 = await db.search_documents("monster", 1, 1, media_type="tv")
    page2 = await db.search_documents("monster", 2, 1, media_type="tv")
    assert [d["title"] for d in page1["results"]] == ["Monster"]
    assert [d["title"] for d in page2["results"]] == ["Monster Musume"]


class _LegacyFakeTVCollection:
    """Mimics motor tv collection; seeded with a legacy doc missing season_number."""

    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query):
        for doc in self.docs:
            if "kitsu_id" in query and doc.get("kitsu_id") == query["kitsu_id"]:
                return doc
            if "imdb_id" in query and doc.get("imdb_id") == query["imdb_id"]:
                return doc
        return None

    async def insert_one(self, doc):
        class _Result:
            inserted_id = "new-id"
        self.docs.append(doc)
        return _Result()

    async def replace_one(self, query, doc):
        for i, existing in enumerate(self.docs):
            if existing.get("_id") == query.get("_id"):
                self.docs[i] = doc

        class _Result:
            modified_count = 1
        return _Result()


@pytest.mark.asyncio
async def test_update_tv_show_repairs_legacy_season_without_number():
    # Regression: legacy TV docs with seasons missing season_number raised
    # KeyError during scan insert, failing every episode of that show.
    from Backend.helper.database import Database
    from Backend.helper.modal import TVShowSchema, Season, Episode, QualityDetail

    legacy = {
        "_id": "legacy-id",
        "tmdb_id": -4242, "imdb_id": "tg4242", "kitsu_id": 10,
        "db_index": 1, "title": "Monster", "media_type": "tv",
        "seasons": [
            {"episodes": [{"episode_number": 1, "title": "old", "telegram": []}]}
        ],
    }

    db = Database.__new__(Database)
    db.current_db_index = 1
    db.dbs = {"storage_1": {"tv": _LegacyFakeTVCollection([legacy])}, "tracking": {}}

    incoming = TVShowSchema(
        tmdb_id=-4242, imdb_id="tg4242", kitsu_id=10, db_index=1,
        title="Monster", media_type="tv",
        seasons=[Season(
            season_number=1,
            episodes=[Episode(
                episode_number=7, title="Episode 7",
                telegram=[QualityDetail(quality="1080p", id="enc7",
                                         name="Monster S01E07.mkv", size="500MB")],
            )],
        )],
    )

    result_id = await db.update_tv_show(incoming, {})
    assert result_id is not None

    repaired = db.dbs["storage_1"]["tv"].docs[0]
    assert all("season_number" in s for s in repaired["seasons"])
    assert repaired["seasons"][0]["season_number"] == 1
    episode_numbers = [e["episode_number"] for s in repaired["seasons"] for e in s["episodes"]]
    assert set(episode_numbers) == {1, 7}
