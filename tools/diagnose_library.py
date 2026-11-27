#!/usr/bin/env python3
"""
diagnose_library.py - find library entries filed in the wrong place.

Parses every file name in a filenames_report.txt export again and compares
the result with where the file sits in the MongoDB library:

  • FILE_NOT_INDEXED   - file name is in the export but not in the library
  • MEDIA_TYPE_WRONG   - indexed as movie but filename says TV (SxE), or vice versa
  • SXE_WRONG          - season/episode parsed from filename differs from index
  • OVA_MISPLACED      - S00 special/decimal OVA indexed as regular episode
  • TITLE_MISMATCH     - indexed title differs from filename-derived title
  • QUALITY_MISMATCH   - stored quality label differs from filename quality

Outputs:
  • diagnose_report.json  - machine-readable full report
  • diagnose_report.csv   - the same findings as a spreadsheet
  • repair_plan.tsv       - the flagged files, for: python tools/repair_library.py --plan repair_plan.tsv

Usage:
  python tools/dump_all_filenames.py
  python tools/diagnose_library.py --dump filenames_report.txt [--out ./diagnose_output]

DATABASE is read from the environment or from config.env in the project root.
The database is only read.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

# Sibling tools; indexer.py puts the project root on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indexer import (
    parse_filename,
    load_records_from_tsv,
    _safe_path,
    _mask_uri,
)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    """Current UTC time without tzinfo, as the reports print it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _classify(parsed: dict, indexed: Optional[dict]) -> list[dict]:
    """Compare parsed filename vs indexed doc. Returns list of issue dicts."""
    issues: list[dict] = []

    if indexed is None:
        issues.append({
            "code": "FILE_NOT_INDEXED",
            "severity": "HIGH",
            "message": "File name is in the export but not in the MongoDB library.",
            "fix": "Index the post again, for example with /scan_files.",
        })
        return issues

    indexed_media_type = indexed.get("media_type")
    indexed_season = indexed.get("season_number")
    parsed_is_movie = bool(parsed.get("is_movie"))
    parsed_special = bool(parsed.get("special"))
    parsed_season = parsed.get("season")
    parsed_episode = parsed.get("episode")
    parsed_title = parsed.get("title") or ""

    # --- Media type check ---
    if parsed_special and indexed_media_type == "movie":
        issues.append({
            "code": "MEDIA_TYPE_WRONG",
            "severity": "HIGH",
            "message": "Filename is an S00 special/OVA but indexed as movie.",
            "fix": "Re-parse via indexer.py; will set media_type=tv, season=0.",
        })
    elif not parsed_special and parsed_is_movie is False and indexed_media_type == "movie":
        issues.append({
            "code": "MEDIA_TYPE_WRONG",
            "severity": "HIGH",
            "message": "Filename has SxE pattern (TV) but indexed as movie.",
            "fix": "Re-parse via indexer.py; will set media_type=tv with correct season/episode.",
        })
    elif parsed_is_movie and indexed_media_type == "tv":
        issues.append({
            "code": "MEDIA_TYPE_WRONG",
            "severity": "HIGH",
            "message": "Filename is a movie but indexed as TV episode.",
            "fix": "Re-parse via indexer.py; will set media_type=movie.",
        })

    # --- SxE check (TV only) ---
    if indexed_media_type == "tv" and not parsed_special:
        indexed_season = indexed.get("season_number")
        indexed_episode = indexed.get("episode_number")
        if parsed_season is not None and parsed_episode is not None:
            if indexed_season != parsed_season or indexed_episode != parsed_episode:
                issues.append({
                    "code": "SXE_WRONG",
                    "severity": "HIGH",
                    "message": f"Filename says S{parsed_season}E{parsed_episode} but index has S{indexed_season}E{indexed_episode}.",
                    "fix": "Re-parse via indexer.py; will correct season/episode.",
                })

    # --- OVA misplaced check ---
    if parsed_special:
        # Decimal OVA like S01E22.5 -> episode 22.5
        if parsed_episode is not None and isinstance(parsed_episode, float):
            indexed_episode = indexed.get("episode_number")
            if indexed_episode != parsed_episode:
                issues.append({
                    "code": "OVA_MISPLACED",
                    "severity": "HIGH",
                    "message": f"Decimal OVA episode {parsed_episode} indexed as {indexed_episode} or wrong type.",
                    "fix": "Re-parse via indexer.py; will flag special=True with float episode.",
                })
        elif indexed_season != 0:
            issues.append({
                "code": "OVA_MISPLACED",
                "severity": "HIGH",
                "message": f"S00 special indexed with season={indexed_season}, expected season=0.",
                "fix": "Re-parse via indexer.py; will set season=0, special=True.",
            })

    # --- Title mismatch ---
    indexed_title = indexed.get("title") or ""
    if parsed_title and indexed_title:
        # Normalize for comparison
        p_norm = re.sub(r"[^a-z0-9]", "", parsed_title.lower())
        i_norm = re.sub(r"[^a-z0-9]", "", indexed_title.lower())
        if p_norm and i_norm and p_norm != i_norm and abs(len(p_norm) - len(i_norm)) > 3:
            issues.append({
                "code": "TITLE_MISMATCH",
                "severity": "MEDIUM",
                "message": f"Filename title '{parsed_title}' differs from indexed '{indexed_title}'.",
                "fix": "Re-resolve via indexer.py; will update title from TMDB/TVDB/Kitsu.",
            })

    # --- Quality mismatch ---
    parsed_quality = parsed.get("quality")
    indexed_quality = indexed.get("quality") or indexed.get("stored_label")
    if parsed_quality and indexed_quality and parsed_quality != indexed_quality:
        issues.append({
            "code": "QUALITY_MISMATCH",
            "severity": "LOW",
            "message": f"Filename quality '{parsed_quality}' differs from indexed '{indexed_quality}'.",
            "fix": "Re-parse via indexer.py; will update quality from filename.",
        })

    return issues


def _severity_order(code: str) -> int:
    order = {
        "FILE_NOT_INDEXED": 0,
        "MEDIA_TYPE_WRONG": 1,
        "SXE_WRONG": 2,
        "OVA_MISPLACED": 3,
        "TITLE_MISMATCH": 4,
        "QUALITY_MISMATCH": 5,
    }
    return order.get(code, 99)


# ---------------------------------------------------------------------------
# Index lookup
# ---------------------------------------------------------------------------

DB_NAME = os.getenv("DB_NAME", "dbFyvio")


def load_index(uris: list[str]) -> dict[str, dict]:
    """Where every indexed stream is filed, keyed by its stored file name.

    The first URI is the tracking database; the library lives in the others.
    Raises RuntimeError when a storage database cannot be read: a partial index
    would report every file stored there as missing.
    """
    from pymongo import MongoClient

    index: dict[str, dict] = {}
    for uri in uris[1:]:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        try:
            db = client[DB_NAME]
            for doc in db["movie"].find({}):
                for stream in doc.get("telegram") or []:
                    if stream.get("name"):
                        index[stream["name"]] = {
                            "media_type": "movie",
                            "title": doc.get("title"),
                            "quality": stream.get("quality"),
                        }
            for doc in db["tv"].find({}):
                for season in doc.get("seasons") or []:
                    for episode in season.get("episodes") or []:
                        for stream in episode.get("telegram") or []:
                            if stream.get("name"):
                                index[stream["name"]] = {
                                    "media_type": "tv",
                                    "title": doc.get("title"),
                                    "season_number": season.get("season_number"),
                                    "episode_number": episode.get("episode_number"),
                                    "quality": stream.get("quality"),
                                }
        except Exception as e:
            reason = str(e).split(",")[0][:160]
            raise RuntimeError(f"could not read {_mask_uri(uri)}: {reason}") from None
        finally:
            client.close()
    return index


# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Telegram Stremio library misclassifications."
    )
    parser.add_argument("--dump", required=True,
                        help="Path to filenames_report.txt (from dump_all_filenames.py)")
    parser.add_argument("--out", default="./diagnose_output",
                        help="Output directory for reports (default: ./diagnose_output)")
    parser.add_argument("--channel", default=None,
                        help="Optional channel ID filter (only diagnose this channel)")
    args = parser.parse_args()

    dump_path = _safe_path(args.dump, "--dump", must_exist=True)
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    # --- Load dump records ---
    print(f"[diagnose] Loading dump: {dump_path}")
    dump_records = load_records_from_tsv(dump_path)
    print(f"[diagnose] Loaded {len(dump_records)} records from dump")

    # Filter by channel if specified
    if args.channel:
        dump_records = [r for r in dump_records if str(r.get("storage")) == args.channel or str(r.get("file_name", "")).find(args.channel) >= 0]
        print(f"[diagnose] Filtered to {len(dump_records)} records for channel {args.channel}")

    # --- Connect to MongoDB ---
    uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
    if len(uris) < 2:
        print("[diagnose] ERROR: DATABASE needs at least 2 comma-separated MongoDB URIs "
              "(set it in config.env or in the environment)")
        return 1

    print(f"[diagnose] Connecting to {len(uris)} database URI(s): " + ", ".join(_mask_uri(u) for u in uris))

    try:
        indexed_by_filename = load_index(uris)
    except RuntimeError as e:
        print(f"[diagnose] ERROR: {e}")
        print("[diagnose] No report was written. Check that MongoDB is reachable and run again.")
        return 1
    print(f"[diagnose] Indexed {len(indexed_by_filename)} stream names in MongoDB")

    # --- Diagnose each dump record ---
    results: list[dict] = []
    stats = {
        "total": len(dump_records),
        "ok": 0,
        "file_not_indexed": 0,
        "media_type_wrong": 0,
        "sxe_wrong": 0,
        "ova_misplaced": 0,
        "title_mismatch": 0,
        "quality_mismatch": 0,
    }

    for rec in dump_records:
        file_name = rec.get("file_name", "")
        storage = rec.get("storage", "")
        title = rec.get("title", "")
        season = rec.get("season")
        episode = rec.get("episode")
        stored_label = rec.get("stored_label", "")

        # Try to match by encoded_string first, then by filename
        indexed = None
        match_method = "none"
        # The dump doesn't have chat_id:msg_id, so we match by filename
        indexed = indexed_by_filename.get(file_name)
        if indexed:
            match_method = "filename"

        # Parse the filename fresh
        parsed = parse_filename(file_name)

        # Attach dump metadata
        parsed["_dump_storage"] = storage
        parsed["_dump_title"] = title
        parsed["_dump_season"] = season
        parsed["_dump_episode"] = episode
        parsed["_dump_stored_label"] = stored_label
        parsed["_file_name"] = file_name
        parsed["_match_method"] = match_method

        issues = _classify(parsed, indexed)

        if not issues:
            stats["ok"] += 1
            status = "OK"
        else:
            status = "ISSUES"
            for issue in issues:
                code = issue["code"]
                if code == "FILE_NOT_INDEXED":
                    stats["file_not_indexed"] += 1
                elif code == "MEDIA_TYPE_WRONG":
                    stats["media_type_wrong"] += 1
                elif code == "SXE_WRONG":
                    stats["sxe_wrong"] += 1
                elif code == "OVA_MISPLACED":
                    stats["ova_misplaced"] += 1
                elif code == "TITLE_MISMATCH":
                    stats["title_mismatch"] += 1
                elif code == "QUALITY_MISMATCH":
                    stats["quality_mismatch"] += 1

        results.append({
            "file_name": file_name,
            "storage": storage,
            "status": status,
            "match_method": match_method,
            "parsed_title": parsed.get("title"),
            "parsed_season": parsed.get("season"),
            "parsed_episode": parsed.get("episode"),
            "parsed_quality": parsed.get("quality"),
            "parsed_is_movie": parsed.get("is_movie"),
            "parsed_special": parsed.get("special"),
            "indexed_title": (indexed or {}).get("title"),
            "indexed_media_type": (indexed or {}).get("media_type"),
            "indexed_season": (indexed or {}).get("season_number"),
            "indexed_episode": (indexed or {}).get("episode_number"),
            "indexed_quality": (indexed or {}).get("quality"),
            "issues": issues,
        })

    # --- Write reports ---
    # JSON report
    report = {
        "generated_at": _utcnow().isoformat() + "Z",
        "dump_file": dump_path,
        "total_records": stats["total"],
        "stats": stats,
        "results": results,
    }
    json_path = os.path.join(out_dir, "diagnose_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)

    # CSV report
    csv_path = os.path.join(out_dir, "diagnose_report.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "file_name", "storage", "status", "match_method",
            "parsed_title", "parsed_season", "parsed_episode", "parsed_quality",
            "indexed_title", "indexed_media_type", "indexed_season", "indexed_episode",
            "issue_codes", "issue_messages",
        ])
        for r in results:
            issue_codes = ";".join(i["code"] for i in r["issues"])
            issue_msgs = ";".join(i["message"] for i in r["issues"])
            writer.writerow([
                r["file_name"], r["storage"], r["status"], r["match_method"],
                r["parsed_title"], r["parsed_season"], r["parsed_episode"], r["parsed_quality"],
                r["indexed_title"], r["indexed_media_type"], r["indexed_season"], r["indexed_episode"],
                issue_codes, issue_msgs,
            ])

    # Repair plan TSV (files that need fixing)
    repair_path = os.path.join(out_dir, "repair_plan.tsv")
    needs_fix = [r for r in results if r["status"] != "OK"]
    with open(repair_path, "w", encoding="utf-8") as f:
        f.write("media_type\tstorage\ttitle\tsxe\tstored_label\tsize\tsplit\tfile_name\n")
        for r in needs_fix:
            # Build a minimal TSV line that indexer.py --dump can consume
            # indexer expects: type, storage, title, sxe, label, size, split, filename
            media_type = "movie" if r["parsed_is_movie"] else "tv"
            sxe = "-"
            if r["parsed_season"] is not None and r["parsed_episode"] is not None:
                ep = r["parsed_episode"]
                if isinstance(ep, float):
                    sxe = f"S{r['parsed_season']}E{ep}"
                else:
                    sxe = f"S{r['parsed_season']:02d}E{r['parsed_episode']:02d}"
            elif r["parsed_special"]:
                sxe = f"S{r['parsed_season'] or 0}E{r['parsed_episode'] or 1}"
            f.write(f"{media_type}\t{r['storage']}\t{r['parsed_title'] or r['file_name']}\t{sxe}\t{r['parsed_quality'] or ''}\t\t-\t{r['file_name']}\n")

    # --- Print summary ---
    print("\n" + "=" * 70)
    print("DIAGNOSIS COMPLETE")
    print("=" * 70)
    print(f"Total files in dump : {stats['total']}")
    print(f"OK                  : {stats['ok']}")
    print(f"Issues found        : {stats['total'] - stats['ok']}")
    print(f"  FILE_NOT_INDEXED  : {stats['file_not_indexed']}")
    print(f"  MEDIA_TYPE_WRONG  : {stats['media_type_wrong']}")
    print(f"  SXE_WRONG         : {stats['sxe_wrong']}")
    print(f"  OVA_MISPLACED     : {stats['ova_misplaced']}")
    print(f"  TITLE_MISMATCH    : {stats['title_mismatch']}")
    print(f"  QUALITY_MISMATCH  : {stats['quality_mismatch']}")
    print(f"\nReports written to: {out_dir}/")
    print("  diagnose_report.json  - full machine-readable report")
    print("  diagnose_report.csv   - the same findings as a spreadsheet")
    print(f"  repair_plan.tsv       - {len(needs_fix)} files needing repair")
    print(f"\nTo see where each file belongs: python tools/repair_library.py --plan {repair_path}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
