import pytest
import os
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



