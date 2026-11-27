#!/usr/bin/env python3
"""
Checks for the filler / recap enrichment of the offline indexer.

Covers the Jikan provider and enrich_with_filler():
- known anime titles
- unknown titles (must stay None, not False)
- non-anime content (must not be enriched)
- file name parsing still works with enrichment switched on

This script calls the public Jikan API, so it needs network access and is
slow when the API rate-limits. Set JIKAN_ENABLED=false to skip the live parts.

Run:  python tests/manual/check_filler.py
"""

import sys
import os
import asyncio
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import PROJECT_ROOT, exported_filenames

# Import jikan directly by file path (avoids Backend package dependency chain)
jikan_spec = importlib.util.spec_from_file_location(
    "jikan_provider",
    os.path.join(PROJECT_ROOT, "Backend/helper/metadata/providers/jikan.py"),
)
jikan = importlib.util.module_from_spec(jikan_spec)
jikan_spec.loader.exec_module(jikan)

from indexer import parse_filename, build_payload, enrich_with_filler, JIKAN_ENABLED

FAILURES = 0
TESTS = 0


def check(label, actual, expected):
    global FAILURES, TESTS
    TESTS += 1
    if actual != expected:
        FAILURES += 1
        print(f"  FAIL [{label}]: expected {expected!r}, got {actual!r}")


# ===========================================================================
# Test classify_filler
# ===========================================================================

def test_classify_filler():
    """Test filler type classification."""
    print("=== classify_filler tests ===")

    check("None input", jikan.classify_filler(None), None)
    check("empty dict", jikan.classify_filler({}), "canon")
    check("filler=True", jikan.classify_filler({"filler": True, "recap": False}), "filler")
    check("recap=True", jikan.classify_filler({"filler": False, "recap": True}), "recap")
    check("both=True", jikan.classify_filler({"filler": True, "recap": True}), "filler_recap")
    check("both=False", jikan.classify_filler({"filler": False, "recap": False}), "canon")
    print("  Done")


# ===========================================================================
# Test enrich_with_filler with synthetic payloads
# ===========================================================================

def test_enrich_non_anime():
    """Non-TV content (movies) should not be enriched."""
    print("=== Non-anime enrichment tests ===")

    payload = {
        "media_type": "movie",
        "title": "Some Movie",
        "season_number": None,
        "episode_number": None,
    }
    resolved = {"source": "tmdb", "title": "Some Movie"}
    result = asyncio.run(enrich_with_filler(payload, resolved))
    check("movie is_filler", result.get("is_filler"), None)
    check("movie filler_type", result.get("filler_type"), None)
    print("  Done")


def test_enrich_no_season_episode():
    """TV without season/episode should not be enriched."""
    print("=== Missing season/episode tests ===")

    payload = {
        "media_type": "tv",
        "title": "Some Show",
        "season_number": None,
        "episode_number": None,
    }
    resolved = {"source": "tvdb", "title": "Some Show"}
    result = asyncio.run(enrich_with_filler(payload, resolved))
    check("no season is_filler", result.get("is_filler"), None)
    check("no season filler_type", result.get("filler_type"), None)
    print("  Done")


def test_enrich_unknown_stays_unknown():
    """When Jikan can't find the anime, is_filler stays None (not False)."""
    print("=== Unknown stays unknown tests ===")

    payload = {
        "media_type": "tv",
        "title": "Nonexistent Anime That Does Not Exist XYZ123",
        "season_number": 1,
        "episode_number": 1,
    }
    resolved = {"source": "kitsu", "title": "Nonexistent Anime That Does Not Exist XYZ123"}

    if JIKAN_ENABLED:
        result = asyncio.run(enrich_with_filler(payload, resolved))
        # Should be None because Jikan won't find this fake title
        check("unknown is_filler is None", result.get("is_filler"), None)
        check("unknown filler_type is None", result.get("filler_type"), None)
    else:
        print("  SKIP (JIKAN_ENABLED=false)")
    print("  Done")


async def test_jikan_search_real():
    """Test Jikan search against real API for a known anime."""
    print("=== Jikan search real API tests ===")

    if not JIKAN_ENABLED:
        print("  SKIP (JIKAN_ENABLED=false)")
        return

    # Bleach is MAL ID 243, a well-known anime with filler
    results = await jikan.search_anime("Bleach")
    check("Bleach search returns results", len(results) > 0, True)
    if results:
        bleach = results[0]
        check("Bleach mal_id", bleach.get("mal_id"), 243)
        print(f"  Found: {bleach.get('title')} (MAL {bleach.get('mal_id')})")

    # Fullmetal Alchemist Brotherhood
    results = await jikan.search_anime("Fullmetal Alchemist Brotherhood")
    check("FMAB search returns results", len(results) > 0, True)
    print("  Done")


async def test_jikan_episode_filler_real():
    """Test Jikan episode filler lookup against real API."""
    print("=== Jikan episode filler real API tests ===")

    if not JIKAN_ENABLED:
        print("  SKIP (JIKAN_ENABLED=false)")
        return

    # Bleach MAL ID 243 - known to have filler episodes
    # Let's check a few episodes
    for ep_num in [1, 20, 60, 100, 160]:
        info = await jikan.get_episode_filler(243, ep_num)
        if info:
            ftype = jikan.classify_filler(info)
            print(f"  Bleach E{ep_num}: filler={info.get('filler')}, recap={info.get('recap')} -> {ftype}")
        else:
            print(f"  Bleach E{ep_num}: no data from Jikan")

    # FMAB MAL ID 5114 - known to have very little filler
    for ep_num in [1, 10, 30, 64]:
        info = await jikan.get_episode_filler(5114, ep_num)
        if info:
            ftype = jikan.classify_filler(info)
            print(f"  FMAB E{ep_num}: filler={info.get('filler')}, recap={info.get('recap')} -> {ftype}")
        else:
            print(f"  FMAB E{ep_num}: no data from Jikan")
    print("  Done")


# ===========================================================================
# Regression: filler enrichment doesn't break filename parsing
# ===========================================================================

def test_parsing_regression_with_filler():
    """Every filename in the dump still parses (filler enrichment is additive)."""
    print("=== Parsing regression tests ===")

    filenames = exported_filenames()
    if filenames is None:
        print("  SKIP library export: filenames_report.txt not found")
        return

    exceptions = 0
    no_title = 0
    for fn in filenames:
        try:
            p = parse_filename(fn)
            if not p.get("title") and not p.get("explicit_tmdb_id") and not p.get("explicit_imdb_id"):
                no_title += 1
        except Exception as e:
            exceptions += 1
            print(f"  EXCEPTION [{fn[:100]}]: {e}")

    check("dump exceptions", exceptions, 0)
    check("dump no-title", no_title, 0)
    print(f"  {len(filenames)} filenames parsed, {exceptions} exceptions, {no_title} no-title")
    print("  Done")


# ===========================================================================
# Test enrich_with_filler payload integration
# ===========================================================================

def test_payload_has_filler_fields():
    """build_payload + enrich_with_filler produces is_filler/filler_type fields."""
    print("=== Payload integration tests ===")

    parsed = parse_filename("Bleach S01E01 1080p.mkv")
    payload = build_payload(parsed, {"source": "kitsu", "title": "Bleach"}, channel_id=-1001234567890, msg_id=42)

    # Before enrichment, fields should not exist (or be None)
    check("payload has season_number", payload.get("season_number"), 1)
    check("payload has episode_number", payload.get("episode_number"), 1)

    # Enrichment happens via enrich_with_filler (async), test it directly
    resolved = {"source": "kitsu", "title": "Bleach", "kitsu_id": 123}
    result = asyncio.run(enrich_with_filler(dict(payload), resolved))

    # Fields should exist on the enriched payload (None if Jikan can't find it)
    check("enriched has is_filler key", "is_filler" in result, True)
    check("enriched has filler_type key", "filler_type" in result, True)
    print("  Done")


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    print("=== Filler enrichment checks ===\n")

    test_classify_filler()
    test_enrich_non_anime()
    test_enrich_no_season_episode()
    test_enrich_unknown_stays_unknown()
    test_payload_has_filler_fields()
    test_parsing_regression_with_filler()

    # Real API tests (may be skipped if no network)
    asyncio.run(test_jikan_search_real())
    asyncio.run(test_jikan_episode_filler_real())

    print(f"\n=== Results: {TESTS} assertions, {FAILURES} failures ===")
    if FAILURES > 0:
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)
