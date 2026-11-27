#!/usr/bin/env python3
"""
indexer_gap.py - Telegram Stremio reconciliation / gap tool.

Compares filenames_report.txt against MongoDB to find files that are:
  - MISSING: on Telegram but not indexed in MongoDB
  - MISPLACED: indexed but with wrong season/episode/title
  - PRESENT: already correctly indexed (skip)

Outputs:
  - gap_report.txt / gap_report.json  (human readable)
  - gaps_only.txt                     (TSV for: python tools/indexer.py --dump gaps_only.txt)

Usage:
  python tools/indexer_gap.py --dump filenames_report.txt
  python tools/indexer.py --dump gaps_only.txt --concurrency 12 --delay 0.2
"""

import os
import sys
import json
import re
import argparse
from datetime import datetime

from indexer import (
    parse_filename,
    load_records_from_tsv,
    _safe_path,
    _mask_uri,
)


def _norm_title(title: str) -> str:
    """Normalize title for comparison: lowercase, strip non-alphanumeric."""
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def collect_indexed(uris: list[str]) -> tuple[dict, dict]:
    """
    Connect to MongoDB and build a lookup of already-indexed items.

    Returns:
        tv_indexed:     {(norm_title, season, episode): [info, ...]}
        movie_indexed:  {norm_title: [info, ...]}
    """
    try:
        from pymongo import MongoClient
    except ImportError:
        raise SystemExit("pymongo is required. Install with: pip install pymongo")

    tv_indexed = {}      # (norm_title, season, episode) -> list of info dicts
    movie_indexed = {}   # norm_title -> list of info dicts

    for uri in uris:
        client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        try:
            db = client[os.getenv("DB_NAME", "dbFyvio")]

            # --- Movies ---
            for doc in db["movie"].find({}):
                title = doc.get("title") or ""
                nt = _norm_title(title)
                if nt:
                    movie_indexed.setdefault(nt, []).append({
                        "collection": "movie",
                        "doc_title": title,
                        "tmdb_id": doc.get("tmdb_id"),
                    })

            # --- TV ---
            for doc in db["tv"].find({}):
                title = doc.get("title") or ""
                nt = _norm_title(title)
                for season in doc.get("seasons", []) or []:
                    sn = season.get("season_number")
                    if sn is None:
                        continue
                    for ep in season.get("episodes", []) or []:
                        en = ep.get("episode_number")
                        if en is None:
                            continue
                        key = (nt, sn, en)
                        tv_indexed.setdefault(key, []).append({
                            "collection": "tv",
                            "doc_title": title,
                            "season_number": sn,
                            "episode_number": en,
                            "tmdb_id": doc.get("tmdb_id"),
                        })
        except Exception as e:
            print(f"  [gap] MongoDB query error on {_mask_uri(uri)}: {e}")
        finally:
            client.close()

    return tv_indexed, movie_indexed


def find_gaps(records: list[dict], tv_indexed: dict, movie_indexed: dict) -> dict:
    """
    Compare parsed records against indexed data.

    Returns: {missing: [], misplaced: [], present: []}
    """
    missing = []
    misplaced = []
    present = []

    for rec in records:
        file_name = rec.get("file_name", "")
        parsed = parse_filename(file_name)
        title = parsed.get("title") or rec.get("title") or ""
        season = parsed.get("season")
        episode = parsed.get("episode")
        is_movie = parsed.get("is_movie", False)
        special = parsed.get("special", False)
        absolute = parsed.get("absolute", False)

        nt = _norm_title(title)

        if is_movie:
            # Movie: check if title exists in movie_indexed
            if nt in movie_indexed:
                present.append({**rec, "parsed": parsed, "gap_status": "present"})
            else:
                missing.append({**rec, "parsed": parsed, "gap_status": "missing"})
        else:
            # TV / anime: check if (title, season, episode) exists
            if special and season == 0:
                key = (nt, 0, int(episode) if isinstance(episode, int) else None)
            elif absolute:
                key = (nt, None, int(episode) if isinstance(episode, int) else None)
            else:
                key = (nt, season, episode)

            if key in tv_indexed:
                present.append({**rec, "parsed": parsed, "gap_status": "present"})
            else:
                # Title exists in DB but wrong season/episode -> misplaced
                title_exists = any(k[0] == nt for k in tv_indexed)
                if title_exists:
                    misplaced.append({**rec, "parsed": parsed, "gap_status": "misplaced"})
                else:
                    missing.append({**rec, "parsed": parsed, "gap_status": "missing"})

    return {"missing": missing, "misplaced": misplaced, "present": present}


def write_gap_report(gaps: dict, out_base: str = "gap_report"):
    """Write human-readable and JSON gap reports."""
    txt_path = _safe_path(f"{out_base}.txt", "report output path")
    json_path = _safe_path(f"{out_base}.json", "report output path")

    missing = gaps["missing"]
    misplaced = gaps["misplaced"]
    present = gaps["present"]

    total = len(missing) + len(misplaced) + len(present)

    lines = [
        "TELEGRAM-STREMIO GAP RECONCILIATION REPORT",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Total files checked: {total}",
        f"  MISSING (not in MongoDB):  {len(missing)}",
        f"  MISPLACED (wrong SxE):     {len(misplaced)}",
        f"  PRESENT (already indexed): {len(present)}",
        "",
    ]

    lines.append("== MISSING (needs indexing) ==")
    for r in missing:
        p = r["parsed"]
        where = f"S{p['season'] or 0:02d}E{p['episode'] or 0:00d}" if not p["is_movie"] else "movie"
        lines.append(f"  {where} | {p.get('title') or 'unknown'}")
        lines.append(f"      file : {r['file_name'][:100]}")
        lines.append("")

    lines.append("== MISPLACED (in DB but wrong season/episode) ==")
    for r in misplaced:
        p = r["parsed"]
        where = f"S{p['season'] or 0:02d}E{p['episode'] or 0:00d}" if not p["is_movie"] else "movie"
        lines.append(f"  {where} | {p.get('title') or 'unknown'}")
        lines.append(f"      file : {r['file_name'][:100]}")
        lines.append("")

    lines.append("== PRESENT (already indexed, skip) ==")
    for r in present[:30]:
        p = r["parsed"]
        where = f"S{p['season'] or 0:02d}E{p['episode'] or 0:00d}" if not p["is_movie"] else "movie"
        lines.append(f"  {where} | {p.get('title') or 'unknown'}")
    if len(present) > 30:
        lines.append(f"  ... and {len(present) - 30} more")

    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(gaps, fh, indent=2, ensure_ascii=False, default=str)

    print(f"\nReports: {txt_path} / {json_path}")
    print(f"  Missing: {len(missing)} | Misplaced: {len(misplaced)} | Present: {len(present)}")


def write_gaps_tsv(gaps: dict, out_path: str = "gaps_only.txt"):
    """Write TSV of missing + misplaced files, ready for indexer.py --dump."""
    path = _safe_path(out_path, "gaps output path")

    # TSV format matching filenames_report.txt:
    # media_type\tstorage\ttitle\tsxe\tstored_label\tsize\tsplit_flag\tfile_name
    lines = ["media_type\tstorage\ttitle\tsxe\tstored_label\tsize\tsplit_flag\tfile_name"]

    for r in gaps["missing"] + gaps["misplaced"]:
        p = r["parsed"]
        file_name = r["file_name"]
        title = p.get("title") or r.get("title") or ""
        season = p.get("season")
        episode = p.get("episode")
        is_movie = p.get("is_movie", False)

        if is_movie:
            sxe = ""
        elif season is not None and episode is not None:
            sxe = f"S{season:02d}E{episode:02d}"
        else:
            sxe = ""

        media_type = "movie" if is_movie else "tv"
        lines.append(
            f"{media_type}\tscan\t{title}\t{sxe}\t\t\t\t{file_name}"
        )

    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines))

    total_gaps = len(gaps["missing"]) + len(gaps["misplaced"])
    print(f"Gaps TSV: {path} ({total_gaps} files)")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Telegram Stremio gap reconciliation - find missing/misplaced files vs MongoDB."
    )
    parser.add_argument(
        "--dump", default=None,
        help="Path to filenames_report.txt TSV dump",
    )
    parser.add_argument(
        "--out", default="gap_report",
        help="Base path for output reports (default: gap_report)",
    )
    parser.add_argument(
        "--gaps-tsv", default="gaps_only.txt",
        help="Output TSV of missing+misplaced files for indexer.py --dump (default: gaps_only.txt)",
    )
    parser.add_argument(
        "--no-tsv", action="store_true", default=False,
        help="Skip writing gaps_only.txt TSV",
    )
    args = parser.parse_args()

    # Load records
    records = []
    if args.dump:
        records = load_records_from_tsv(args.dump)
    else:
        default_dump = os.path.join(os.getcwd(), "filenames_report.txt")
        if os.path.exists(default_dump):
            records = load_records_from_tsv(default_dump)
        else:
            raise SystemExit(
                "No input specified. Use --dump <file>.\n"
                "Example: python tools/indexer_gap.py --dump filenames_report.txt"
            )

    if not records:
        print("No records to process.")
        return 0

    # Get MongoDB URIs
    uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
    if len(uris) < 2:
        raise SystemExit(
            "DATABASE env needs at least 2 comma-separated MongoDB URIs "
            "(set in config.env or environment)."
        )

    print(f"\nConnecting to {len(uris)} database URI(s): "
          + ", ".join(_mask_uri(u) for u in uris))

    # Collect indexed data from MongoDB
    tv_indexed, movie_indexed = collect_indexed(uris)
    print(f"  Indexed TV (season,episode) entries: {len(tv_indexed)}")
    print(f"  Indexed movie titles: {len(movie_indexed)}")

    # Find gaps
    gaps = find_gaps(records, tv_indexed, movie_indexed)

    # Write reports
    write_gap_report(gaps, args.out)

    # Write gaps TSV
    if not args.no_tsv:
        write_gaps_tsv(gaps, args.gaps_tsv)

    # Summary + next step
    total_gaps = len(gaps["missing"]) + len(gaps["misplaced"])
    if total_gaps == 0:
        print("\nNo gaps found - everything is already indexed!")
    else:
        print(f"\n{total_gaps} files need attention. Next step:")
        print(f"  python tools/indexer.py --dump {args.gaps_tsv} --concurrency 12 --delay 0.2")

    return 0


if __name__ == "__main__":
    sys.exit(main())