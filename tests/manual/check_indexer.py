#!/usr/bin/env python3
"""
Checks for the offline indexer (tools/indexer.py).

Runs parse_filename() over a set of naming patterns: no exceptions, no missing
titles, and season 0 specials never filed under season 1. When a library
export (filenames_report.txt from tools/dump_all_filenames.py) is present,
every name in it is parsed as well.

Run:  python tests/manual/check_indexer.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import exported_filenames

from indexer import parse_filename, build_payload

FAILURES = 0
TESTS = 0


def check(label, actual, expected):
    global FAILURES, TESTS
    TESTS += 1
    if actual != expected:
        FAILURES += 1
        print(f"  FAIL [{label}]: expected {expected!r}, got {actual!r}")


# ===========================================================================
# Hand-crafted edge cases: each asserts specific parse behavior
# ===========================================================================

def test_s00_special():
    """S00E01 is season 0, episode 1, special=True, not S01E01."""
    p = parse_filename("My.Teen.Romantic.Comedy.SNAFU.S00E01.1080p.mkv")
    check("S00 season", p.get("season"), 0)
    check("S00 episode", p.get("episode"), 1)
    check("S00 special", p.get("special"), True)
    check("S00 absolute", p.get("absolute"), False)

    p = parse_filename("SNAFU S00E01.mkv")
    check("S00 short season", p.get("season"), 0)
    check("S00 short special", p.get("special"), True)

    # Explicit OVA tag
    p = parse_filename("Grimgar OVA S00E02.mkv")
    check("OVA S00 season", p.get("season"), 0)
    check("OVA S00 special", p.get("special"), True)


def test_s01_regular():
    """S01E02 must remain season 1, episode 2 - not affected by S00 fixes."""
    p = parse_filename("[shiggy] My Teen Romantic Comedy SNAFU S01E02 (BDRip 1920x1080 HEVC FLAC) [062C81CF].mkv")
    check("S01 season", p.get("season"), 1)
    check("S01 episode", p.get("episode"), 2)
    check("S01 special", p.get("special"), False)


def test_h264_codec_not_episode():
    """H.264 / H.265 / x264 / x265 must NOT be parsed as episode 264/265."""
    p = parse_filename("The Boy and the Beast.2015.1080p.HMAX.WEB DL.DUAL.DDP5.1.H.264.MSubs ToonsHub.mkv")
    check("H.264 not episode", p.get("episode"), None)
    check("H.264 absolute", p.get("absolute"), False)

    p = parse_filename("Show.Name.S01E02.x265.1080p.mkv")
    check("x265 with S01 season", p.get("season"), 1)
    check("x265 with S01 episode", p.get("episode"), 2)


def test_fps_pair_not_episode():
    """Frame-rate pairs like "143 8561fps" must not create episode 143."""
    p = parse_filename("[Raze] Sakamoto Days 01 (Dual Audio DDP5 1) (Multi Sub) x265 10bit 1080p 143 8561fps mkv.mkv")
    check("FPS pair episode", p.get("episode"), 1)
    check("FPS pair absolute", p.get("absolute"), True)


def test_mismatched_brackets():
    """[YURI} instead of [YURI] must be handled as a release group."""
    p = parse_filename("[YURI} Anohana The Flower We Saw That Day The Movie [BD 1080p x264 FLAC].mkv")
    check("Mismatched group", p.get("group"), "YURI")
    check("Mismatched title", p.get("title"), "Anohana The Flower We Saw That Day The Movie")
    check("Mismatched movie", p.get("is_movie"), True)


def test_movie_with_year():
    """Movie filenames with year in parentheses."""
    p = parse_filename("Castle in the Sky (1986) [1080p x265 HEVC 10bit BluRay Dual Audio].mkv")
    check("Movie year", p.get("year"), 1986)
    check("Movie is_movie", p.get("is_movie"), True)
    check("Movie quality", p.get("quality"), "1080p")

    p = parse_filename("The Boy and the Beast.2015.1080p.HMAX.WEB DL.mkv")
    check("Movie year dot", p.get("year"), 2015)


def test_absolute_episodes():
    """Absolute episode numbers (e.g. One Piece 1223)."""
    p = parse_filename("One Piece 1223 720.mkv")
    check("Absolute episode", p.get("episode"), 1223)
    check("Absolute flag", p.get("absolute"), True)
    check("Absolute season", p.get("season"), None)

    p = parse_filename("Show.Name.S01E01.mkv")
    check("S01 not absolute", p.get("absolute"), False)


def test_explicit_ids():
    """{tmdb-...}, {imdb-...}, {tvdb-...} tags."""
    p = parse_filename("{tmdb-61223} Show Name S01E01.mkv")
    check("Explicit TMDB id", p.get("explicit_tmdb_id"), 61223)

    p = parse_filename("{imdb-tt0111161} Show Name S01E01.mkv")
    check("Explicit IMDB id", p.get("explicit_imdb_id"), "tt0111161")

    p = parse_filename("{tvdb-12345} Show Name S01E01.mkv")
    check("Explicit TVDB id", p.get("explicit_tvdb_id"), 12345)


def test_combined_episodes():
    """E01-E05 combined ranges (no SxxExx prefix so combined kicks in)."""
    p = parse_filename("Show Name E01-E05.mkv")
    check("Combined start", p.get("combined_start"), 1)
    check("Combined end", p.get("combined_end"), 5)
    check("Combined flag", p.get("combined"), True)

    p = parse_filename("Show Name S01 E01-E05.mkv")
    check("Combined with S01 season", p.get("season"), 1)
    check("Combined with S01 episode", p.get("episode"), 1)
    # "S01 E01-E05" is a five-episode pack, like "E01-E05" above.
    check("Combined with S01 combined flag", p.get("combined"), True)
    check("Combined with S01 start", p.get("combined_start"), 1)
    check("Combined with S01 end", p.get("combined_end"), 5)


def test_split_files():
    """Split file parts."""
    p = parse_filename("Show Name - S01E01 - Part 1 of 2.mkv")
    check("Split part", p.get("split_part"), 1)
    check("Split flag", p.get("is_split"), True)


def test_title_aliases():
    """SNAFU TOO! -> season 2, Climax -> season 3."""
    p = parse_filename("My Teen Romantic Comedy SNAFU TOO! E01.mkv")
    check("Alias SNAFU season", p.get("season"), 2)
    check("Alias SNAFU title_inferred", p.get("title_inferred_from_alias"), True)

    p = parse_filename("My Teen Romantic Comedy SNAFU Climax E01.mkv")
    check("Alias SNAFU Climax season", p.get("season"), 3)
    check("Alias SNAFU Climax title_inferred", p.get("title_inferred_from_alias"), True)


def test_quality_labels():
    """Various quality labels parse correctly."""
    cases = [
        ("Show 1080p.mkv", "1080p"),
        ("Show 720p.mkv", "720p"),
        # Interlaced labels map to the progressive tier
        ("Show 480i.mkv", "480p"),
        ("Show 4K.mkv", "4K"),
        ("Show 1920x1080.mkv", "1080p"),
        ("Show HD.mkv", "HD"),
    ]
    for fname, expected_q in cases:
        p = parse_filename(fname)
        check(f"Quality {fname}", p.get("quality"), expected_q)


def test_dimensions_not_years():
    """1920x1080 must not make 1920 a year."""
    p = parse_filename("Show.Name.S01E02 (BDRip 1920x1080 HEVC FLAC) [062C81CF].mkv")
    check("Dimensions not year", p.get("year"), None)


def test_release_groups():
    """[Group] at start is captured as release group."""
    p = parse_filename("[Raze] Sakamoto Days 01 (Dual Audio DDP5 1) (Multi Sub) x265 10bit 1080p mkv.mkv")
    check("Release group", p.get("group"), "Raze")


# ===========================================================================
# Full dump regression: every filename from filenames_report.txt
# ===========================================================================

def test_full_dump():
    """All filenames from filenames_report.txt parse without exception."""
    filenames = exported_filenames()
    if filenames is None:
        print("  SKIP library export: filenames_report.txt not found")
        return

    exceptions = 0
    no_title = 0
    specials = 0
    movies = 0

    for fn in filenames:
        try:
            p = parse_filename(fn)
            if p.get("special"):
                specials += 1
            if p.get("is_movie"):
                movies += 1
            if not p.get("title") and not p.get("explicit_tmdb_id") and not p.get("explicit_imdb_id"):
                no_title += 1
                print(f"  NO TITLE: {fn[:120]}")
        except Exception as e:
            exceptions += 1
            print(f"  EXCEPTION [{fn[:100]}]: {e}")

    print(f"  Full dump: {len(filenames)} filenames, {exceptions} exceptions, {no_title} no-title, {specials} specials, {movies} movies")
    check("dump exceptions", exceptions, 0)
    check("dump no-title", no_title, 0)


# ===========================================================================
# Payload building: S00 must stay S00 through build_payload
# ===========================================================================

def test_payload_s00():
    """build_payload must preserve season 0, not convert to 1."""
    parsed = parse_filename("SNAFU S00E01.mkv")
    payload = build_payload(parsed, {}, channel_id=-1001234567890, msg_id=42)
    check("payload S00 season_number", payload.get("season_number"), 0)
    check("payload S00 episode_number", payload.get("episode_number"), 1)
    check("payload S00 episode_title", payload.get("episode_title"), "S00E01")

    parsed = parse_filename("SNAFU S01E02.mkv")
    payload = build_payload(parsed, {}, channel_id=-1001234567890, msg_id=43)
    check("payload S01 season_number", payload.get("season_number"), 1)
    check("payload S01 episode_number", payload.get("episode_number"), 2)


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    print("=== Indexer regression tests ===\n")

    test_s00_special()
    print("  S00 special tests: done")

    test_s01_regular()
    print("  S01 regular tests: done")

    test_h264_codec_not_episode()
    print("  H.264 codec tests: done")

    test_fps_pair_not_episode()
    print("  FPS pair tests: done")

    test_mismatched_brackets()
    print("  Mismatched bracket tests: done")

    test_movie_with_year()
    print("  Movie year tests: done")

    test_absolute_episodes()
    print("  Absolute episode tests: done")

    test_explicit_ids()
    print("  Explicit ID tag tests: done")

    test_combined_episodes()
    print("  Combined episode tests: done")

    test_split_files()
    print("  Split file tests: done")

    test_title_aliases()
    print("  Title alias tests: done")

    test_quality_labels()
    print("  Quality label tests: done")

    test_dimensions_not_years()
    print("  Dimension/year tests: done")

    test_release_groups()
    print("  Release group tests: done")

    test_payload_s00()
    print("  Payload S00 tests: done")

    test_full_dump()
    print("  Full dump regression: done")

    print(f"\n=== Results: {TESTS} assertions, {FAILURES} failures ===")
    if FAILURES > 0:
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)
