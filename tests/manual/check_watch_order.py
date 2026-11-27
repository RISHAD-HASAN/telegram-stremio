#!/usr/bin/env python3
"""
Checks for the watch-order grouping (Backend/helper/watch_order.py).

Run:  python tests/manual/check_watch_order.py
"""
import sys
import os
import asyncio
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import PROJECT_ROOT, exported_filenames

# Import watch_order directly by file path (avoids Backend package dependency chain)
watch_order_spec = importlib.util.spec_from_file_location(
    "watch_order_module",
    os.path.join(PROJECT_ROOT, "Backend/helper/watch_order.py"),
)
watch_order = importlib.util.module_from_spec(watch_order_spec)
watch_order_spec.loader.exec_module(watch_order)

WatchOrderBuilder = watch_order.WatchOrderBuilder
assign_watch_order = watch_order.assign_watch_order
apply_watch_order = watch_order.apply_watch_order
_anime_key = watch_order._anime_key
_entry_title = watch_order._entry_title
_entry_year = watch_order._entry_year
_entry_season = watch_order._entry_season

FAILURES = 0
TESTS = 0


def check(label, actual, expected):
    global FAILURES, TESTS
    TESTS += 1
    if actual != expected:
        FAILURES += 1
        print(f"  FAIL [{label}]: expected {expected!r}, got {actual!r}")


def make_entry(title, year=None, season=1, anilist_id=None, mal_id=None):
    return {
        "anilist_id": anilist_id,
        "mal_id": mal_id,
        "title": title,
        "title_english": title,
        "year": year,
        "season_year": year,
        "season_number": season,
        "episode_number": 1,
        "media_type": "tv",
    }


def test_anime_key():
    print("=== _anime_key tests ===")
    check("anilist_id key", _anime_key({"anilist_id": 123}), ("anilist", "123"))
    check("mal_id key", _anime_key({"mal_id": 456}), ("mal", "456"))
    check("both ids prefers anilist", _anime_key({"anilist_id": 123, "mal_id": 456}), ("anilist", "123"))
    check("no ids", _anime_key({"title": "No ID"}), None)
    check("empty dict", _anime_key({}), None)
    print("  Done")


def test_entry_helpers():
    print("=== Entry helper tests ===")
    check("title", _entry_title({"title": "Bleach"}), "Bleach")
    check("title fallback", _entry_title({"title_english": "Bleach"}), "Bleach")
    check("title unknown", _entry_title({}), "Unknown")
    check("year from year", _entry_year({"year": 2004}), 2004)
    check("year from season_year", _entry_year({"season_year": 2006}), 2006)
    check("year none", _entry_year({}), None)
    check("season", _entry_season({"season_number": 2}), 2)
    check("season none", _entry_season({}), None)
    print("  Done")


async def test_simple_franchise():
    print("=== Simple franchise tests ===")
    entries = [
        make_entry("Fate/Zero", 2011, season=1, anilist_id=100),
        make_entry("Fate/stay night", 2006, season=1, anilist_id=200),
    ]
    builder = WatchOrderBuilder(entries)
    builder._graph[("anilist", "100")].add(("anilist", "200"))
    builder._graph[("anilist", "200")].add(("anilist", "100"))
    builder._order_edges.append((("anilist", "100"), ("anilist", "200")))
    assignments = builder.build()
    check("100 has group", assignments[("anilist", "100")]["watch_order_group"], "franchise_1")
    check("100 index", assignments[("anilist", "100")]["watch_order_index"], 1)
    check("200 has group", assignments[("anilist", "200")]["watch_order_group"], "franchise_1")
    check("200 index", assignments[("anilist", "200")]["watch_order_index"], 2)
    print("  Done")


async def test_three_entry_franchise():
    print("=== Three-entry franchise tests ===")
    entries = [
        make_entry("Series S1", 2010, season=1, anilist_id=1),
        make_entry("Series S2", 2012, season=2, anilist_id=2),
        make_entry("Series S3", 2015, season=3, anilist_id=3),
    ]
    builder = WatchOrderBuilder(entries)
    builder._graph[("anilist", "1")].add(("anilist", "2"))
    builder._graph[("anilist", "2")].add(("anilist", "1"))
    builder._graph[("anilist", "2")].add(("anilist", "3"))
    builder._graph[("anilist", "3")].add(("anilist", "2"))
    builder._order_edges.append((("anilist", "1"), ("anilist", "2")))
    builder._order_edges.append((("anilist", "2"), ("anilist", "3")))
    assignments = builder.build()
    check("S1 index 1", assignments[("anilist", "1")]["watch_order_index"], 1)
    check("S2 index 2", assignments[("anilist", "2")]["watch_order_index"], 2)
    check("S3 index 3", assignments[("anilist", "3")]["watch_order_index"], 3)
    check("All same group", len({assignments[k]["watch_order_group"] for k in assignments}), 1)
    print("  Done")


async def test_single_entry_no_franchise():
    print("=== Single entry tests ===")
    entries = [make_entry("Standalone Movie", 2020, season=1, anilist_id=999)]
    builder = WatchOrderBuilder(entries)
    assignments = builder.build()
    check("No assignments", len(assignments), 0)
    print("  Done")


async def test_two_separate_franchises():
    print("=== Two separate franchises tests ===")
    entries = [
        make_entry("Franchise A S1", 2010, season=1, anilist_id=1),
        make_entry("Franchise A S2", 2012, season=2, anilist_id=2),
        make_entry("Franchise B S1", 2015, season=1, anilist_id=3),
        make_entry("Franchise B S2", 2017, season=2, anilist_id=4),
    ]
    builder = WatchOrderBuilder(entries)
    builder._graph[("anilist", "1")].add(("anilist", "2"))
    builder._graph[("anilist", "2")].add(("anilist", "1"))
    builder._order_edges.append((("anilist", "1"), ("anilist", "2")))
    builder._graph[("anilist", "3")].add(("anilist", "4"))
    builder._graph[("anilist", "4")].add(("anilist", "3"))
    builder._order_edges.append((("anilist", "3"), ("anilist", "4")))
    assignments = builder.build()
    check("Two groups", len({assignments[k]["watch_order_group"] for k in assignments}), 2)
    check("1 in franchise_1", assignments[("anilist", "1")]["watch_order_group"], "franchise_1")
    check("2 in franchise_1", assignments[("anilist", "2")]["watch_order_group"], "franchise_1")
    check("3 in franchise_2", assignments[("anilist", "3")]["watch_order_group"], "franchise_2")
    check("4 in franchise_2", assignments[("anilist", "4")]["watch_order_group"], "franchise_2")
    print("  Done")


def test_apply_watch_order():
    print("=== apply_watch_order tests ===")
    assignments = {
        ("anilist", "100"): {"watch_order_group": "franchise_1", "watch_order_index": 1},
        ("anilist", "200"): {"watch_order_group": "franchise_1", "watch_order_index": 2},
    }
    payload1 = {"anilist_id": 100, "title": "Prequel", "watch_order_group": None, "watch_order_index": None}
    result1 = apply_watch_order(dict(payload1), assignments)
    check("apply group", result1.get("watch_order_group"), "franchise_1")
    check("apply index", result1.get("watch_order_index"), 1)
    payload2 = {"anilist_id": 200, "title": "Sequel", "watch_order_group": None, "watch_order_index": None}
    result2 = apply_watch_order(dict(payload2), assignments)
    check("apply group 2", result2.get("watch_order_group"), "franchise_1")
    check("apply index 2", result2.get("watch_order_index"), 2)
    payload3 = {"anilist_id": 999, "title": "Standalone", "watch_order_group": None, "watch_order_index": None}
    result3 = apply_watch_order(dict(payload3), assignments)
    check("no match group", result3.get("watch_order_group"), None)
    check("no match index", result3.get("watch_order_index"), None)
    payload4 = {"title": "No ID", "watch_order_group": None, "watch_order_index": None}
    result4 = apply_watch_order(dict(payload4), assignments)
    check("no key group", result4.get("watch_order_group"), None)
    check("no key index", result4.get("watch_order_index"), None)
    print("  Done")


async def test_integration_with_mock_relations():
    print("=== Integration tests (mocked relations) ===")
    entries = [
        make_entry("Bleach S1", 2004, season=1, anilist_id=243),
        make_entry("Bleach S2", 2006, season=2, anilist_id=244),
        make_entry("Naruto S1", 2002, season=1, anilist_id=20),
        make_entry("Naruto S2", 2007, season=2, anilist_id=21),
    ]
    builder = WatchOrderBuilder(entries)
    builder._graph[("anilist", "243")].add(("anilist", "244"))
    builder._graph[("anilist", "244")].add(("anilist", "243"))
    builder._order_edges.append((("anilist", "243"), ("anilist", "244")))
    builder._graph[("anilist", "20")].add(("anilist", "21"))
    builder._graph[("anilist", "21")].add(("anilist", "20"))
    builder._order_edges.append((("anilist", "20"), ("anilist", "21")))
    assignments = builder.build()
    check("Bleach S1 index 1", assignments[("anilist", "243")]["watch_order_index"], 1)
    check("Bleach S2 index 2", assignments[("anilist", "244")]["watch_order_index"], 2)
    check("Naruto S1 index 1", assignments[("anilist", "20")]["watch_order_index"], 1)
    check("Naruto S2 index 2", assignments[("anilist", "21")]["watch_order_index"], 2)
    check("Two groups", len({assignments[k]["watch_order_group"] for k in assignments}), 2)
    payloads = [
        {"anilist_id": 243, "title": "Bleach S1", "watch_order_group": None, "watch_order_index": None},
        {"anilist_id": 244, "title": "Bleach S2", "watch_order_group": None, "watch_order_index": None},
        {"anilist_id": 20, "title": "Naruto S1", "watch_order_group": None, "watch_order_index": None},
        {"anilist_id": 21, "title": "Naruto S2", "watch_order_group": None, "watch_order_index": None},
    ]
    for p in payloads:
        apply_watch_order(p, assignments)
    check("payload Bleach S1", payloads[0]["watch_order_index"], 1)
    check("payload Bleach S2", payloads[1]["watch_order_index"], 2)
    check("payload Naruto S1", payloads[2]["watch_order_index"], 1)
    check("payload Naruto S2", payloads[3]["watch_order_index"], 2)
    print("  Done")


def test_parsing_regression():
    print("=== Parsing regression tests ===")
    from indexer import parse_filename
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


if __name__ == "__main__":
    print("=== Watch-order checks ===\n")
    test_anime_key()
    test_entry_helpers()
    asyncio.run(test_simple_franchise())
    asyncio.run(test_three_entry_franchise())
    asyncio.run(test_single_entry_no_franchise())
    asyncio.run(test_two_separate_franchises())
    test_apply_watch_order()
    asyncio.run(test_integration_with_mock_relations())
    test_parsing_regression()
    print(f"\n=== Results: {TESTS} assertions, {FAILURES} failures ===")
    if FAILURES > 0:
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)
