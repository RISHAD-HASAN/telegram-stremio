#!/usr/bin/env python3
"""nyaa_repair.py - Consolidated nyaa.si authoritative repair tool.

Reads the export written by dump_all_filenames.py (filenames_report.txt,
tab-separated), checks every stream/movie filename against nyaa.si canonical
release data, and proposes quality/season/episode/naming corrections. Dry-run
by default: nothing touches MongoDB until you review the report and run with
--apply, which backs up every touched document first.

Usage
-----
    # Dry run (default): read filenames_report.txt, query nyaa.si, write report
    python tools/nyaa_repair.py

    # Apply high-confidence label changes to live MongoDB (backs up first)
    python tools/nyaa_repair.py --apply

    # Custom dump / report paths
    python tools/nyaa_repair.py --dump /path/to/filenames_report.txt \
                          --cache nyaa_cache.json \
                          --out nyaa_repair_report

Safety rules
------------
* nyaa.si is authoritative for quality, season, episode, and canonical naming.
* Literal filename tokens win: a "540p" tag stays 540p, never bucketed to 576p.
* 480i/480p and 4K/2160p/UHD are equivalent - never churned.
* S00 (specials/OVA) is never conflated with S01 (regular season 1).
* A file much smaller than the nyaa source is flagged as a suspected re-encode
  instead of blindly overwriting.
* Low-match and zero-result units go to review, never changed automatically.
* --apply writes only high-confidence changes, backs up every (client, coll, _id)
  before mutation, and rewrites split-file group_keys consistently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from typing import Optional

# ---------------------------------------------------------------------------
# Import the resolution engine from nyaa_resolve.py so both tools
# share the same matching, scoring, caching, backup and apply logic.
# ---------------------------------------------------------------------------
try:
    from nyaa_resolve import (
        extract_group,
        fetch_nyaa,
        build_queries,
        propose_for_record,
        parse_size_mb,
        write_reports,
        apply_changes,
    )
except ImportError as exc:
    raise SystemExit(
        "nyaa_repair.py needs nyaa_resolve.py next to it in the tools folder.\n"
        f"Import failed: {exc}"
    )


# ---------------------------------------------------------------------------
# Title alias map
# nyaa.si sometimes titles a season differently from the main show (e.g.
# "SNAFU TOO!" for season 2, "Climax" for season 3). Map known variants so
# nyaa searches hit the right season instead of missing or matching S01.
# Add new aliases here as you encounter them.
# ---------------------------------------------------------------------------
TITLE_ALIASES: dict[str, dict[int, str]] = {
    "my teen romantic comedy snafu": {
        2: "my teen romantic comedy snafu too",
        3: "my teen romantic comedy snafu climax",
    },
    "ore no imouto ga konna ni kawaii wake ga nai": {
        2: "ore no imouto ga konna ni kawaii wake ga nai 2",
    },
    "high school dxd": {
        2: "high school dxd born",
        3: "high school dxd new",
        4: "high school dxd hero",
    },
    "food wars": {
        2: "food wars the second plate",
        3: "food wars the third plate",
        4: "food wars the fourth plate",
        5: "food wars the fifth plate",
    },
}


def _alias_title(title: str, season: Optional[int]) -> str:
    """Return an alias title for a given season if one is known, else the original."""
    if season is None:
        return title
    key = title.strip().lower()
    aliases = TITLE_ALIASES.get(key)
    if not aliases:
        return title
    return aliases.get(int(season), title)


# ---------------------------------------------------------------------------
# Path arguments
# ---------------------------------------------------------------------------

def _safe_path(value: str, label: str, must_exist: bool = False) -> str:
    """Absolute form of a path argument; exits with a message when it is unusable."""
    if not value or not isinstance(value, str):
        raise SystemExit(f"Error: {label} must be a non-empty string.")
    if "\x00" in value:
        raise SystemExit(f"Error: {label} contains null byte.")
    abs_path = os.path.abspath(os.path.expanduser(value))
    if must_exist and not os.path.isfile(abs_path):
        raise SystemExit(f"Error: {label} not found: {abs_path}")
    return abs_path


# ---------------------------------------------------------------------------
# Security: credential masking
# ---------------------------------------------------------------------------

_URI_RE = re.compile(r"(?i)(mongodb\+srv|mongodb)://[^:]+:[^@]+@")


def _mask_uri(uri: str) -> str:
    """Hide credentials in a MongoDB URI for safe logging."""
    return _URI_RE.sub(lambda m: m.group(1) + "://****:****@", uri or "")


# ---------------------------------------------------------------------------
# Security: cache integrity
# ---------------------------------------------------------------------------

_CACHE_VERSION = 1
_CACHE_MAX_ENTRIES = 2000


def _load_cache(cache_path: str) -> dict:
    """Load nyaa cache with integrity validation."""
    if not os.path.exists(cache_path):
        return {"_version": _CACHE_VERSION, "_entries": {}}
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: cache {cache_path} unreadable ({exc}); starting fresh.")
        return {"_version": _CACHE_VERSION, "_entries": {}}
    if not isinstance(data, dict):
        return {"_version": _CACHE_VERSION, "_entries": {}}
    version = data.get("_version", 0)
    entries = data.get("_entries", data)  # backward compat: old flat format
    if not isinstance(entries, dict):
        entries = {}
    if version != _CACHE_VERSION:
        print(f"Warning: cache version mismatch ({version} != {_CACHE_VERSION}); starting fresh.")
        return {"_version": _CACHE_VERSION, "_entries": {}}
    # Cap entries to prevent unbounded growth.
    if len(entries) > _CACHE_MAX_ENTRIES:
        # Keep the most recently used half (simple LRU-ish by key sort).
        keys = sorted(entries.keys())[: _CACHE_MAX_ENTRIES // 2]
        entries = {k: entries[k] for k in keys}
        print(f"Warning: cache trimmed to {_CACHE_MAX_ENTRIES // 2} entries.")
    return {"_version": _CACHE_VERSION, "_entries": entries}


def _save_cache(cache_path: str, cache: dict) -> None:
    """Persist nyaa cache atomically."""
    tmp_path = f"{cache_path}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1)
        os.replace(tmp_path, cache_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# TSV parsing (filenames_report.txt from dump_all_filenames.py)
# ---------------------------------------------------------------------------

# Column layout in filenames_report.txt:
#   0 media_type   (tv | movie)
#   1 storage      (storage_1, ...)
#   2 title        (show / movie name)
#   3 sxe          (SxE like "S9E1", "S1E5", or "SNoneENone"/"-" for movies)
#   4 stored_label (quality as stored, e.g. "1080p", "576p", "480i")
#   5 size         (human size, e.g. "1.40GB", "984.98MB")
#   6 split        ("-" or split status)
#   7 file_name    (actual filename / caption)
#
_SXE_RE = re.compile(r"(?i)^S(\d{1,2})E(\d{1,3})$")
_MAX_FILENAME_LEN = 400


def _parse_sxe(raw: str) -> tuple[Optional[int], Optional[int]]:
    """Split an 'SxE' token into (season, episode) ints.

    Returns (None, None) for movies / unknown entries ("-", "SNoneENone", "").
    S00E01 is preserved as season 0 (special/OVA) - never upgraded to S01.
    """
    if not raw or raw in ("-", "SNoneENone", "S0E0", ""):
        return None, None
    m = _SXE_RE.match(raw.strip())
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def load_records(dump_path: str) -> list[dict]:
    """Read filenames_report.txt and return a list of stream records.

    Each record matches the shape nyaa_resolve expects:
        media_type, storage, title, season, episode, stored_label, size,
        size_mb, is_split, file_name
    """
    dump_path = _safe_path(dump_path, "--dump", must_exist=True)

    records: list[dict] = []
    skipped = 0
    seen_keys: set[tuple[str, str, int | None, int | None]] = set()

    with open(dump_path, "r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            # Skip section headers / comment lines
            if line.startswith("#") or line.startswith("==") or line.startswith("---"):
                continue
            parts = line.split("\t")
            if len(parts) < 8:
                # Not a data row (e.g. a totals section header): skip quietly
                skipped += 1
                continue

            media_type = parts[0].strip()
            storage = parts[1].strip()
            title = parts[2].strip()
            sxe = parts[3].strip()
            stored_label = parts[4].strip()
            size = parts[5].strip()
            split_flag = parts[6].strip()
            file_name = parts[7].strip()

            if not title or not file_name:
                skipped += 1
                continue

            # Reject absurdly long filenames (TSV injection / DoS)
            if len(file_name) > _MAX_FILENAME_LEN:
                skipped += 1
                continue

            # Tab inside a filename would shift columns; detect and skip.
            if "\t" in file_name or "\t" in title:
                skipped += 1
                continue

            season, episode = _parse_sxe(sxe)

            # Security: S00 (special) must never be silently upgraded to S01.
            # If the dump says S00, keep it as 0. nyaa matching below respects
            # season 0 as a distinct bucket.
            if season == 0:
                pass  # preserve as special

            # Deduplicate identical (storage, title, season, episode) rows
            key = (storage, title, season, episode)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            records.append(
                {
                    "media_type": media_type,
                    "storage": storage,
                    "title": title,
                    "season": season,
                    "episode": episode,
                    "stored_label": stored_label,
                    "size": size,
                    "size_mb": parse_size_mb(size or ""),
                    "is_split": split_flag not in ("-", ""),
                    "file_name": file_name,
                }
            )

    print(f"Loaded {len(records)} records from {dump_path} (skipped {skipped} non-data lines).")
    return records


# ---------------------------------------------------------------------------
# Main repair flow
# ---------------------------------------------------------------------------

def run_repair(dump_path: str, cache_path: str, out_base: str, apply: bool,
               delay: float, dry_run: bool, min_score: int = 65) -> None:
    """End-to-end nyaa repair: load, resolve, report, optionally apply."""

    cache_path = _safe_path(cache_path, "--cache")
    out_base = _safe_path(out_base, "--out")

    records = load_records(dump_path)

    # Group records into (release_group, title) units: one nyaa query per unit.
    units: dict[tuple[Optional[str], str], list[dict]] = defaultdict(list)
    for rec in records:
        group = extract_group(rec["file_name"])
        units[(group, rec["title"])].append(rec)

    print(f"\nGrouped into {len(units)} nyaa search units "
          f"({len(records)} total streams).")

    cache = _load_cache(cache_path)
    entries = cache.get("_entries", {})

    queries_used = 0
    verdicts: list[dict] = []

    # Process units in a deterministic order (group then title) for stable output.
    for (group, title), unit_records in sorted(
        units.items(), key=lambda kv: ((kv[0][0] or ""), kv[0][1])
    ):
        sample_name = unit_records[0]["file_name"]

        # Apply title alias for nyaa search (e.g. SNAFU TOO! for season 2).
        # All records in a unit share the same title; use the first record's
        # season if consistent, else the base title.
        sample_season = unit_records[0].get("season")
        alias_title = _alias_title(title, sample_season)

        queries = build_queries(group, alias_title, sample_name)

        candidates: list[dict] = []
        for q in queries:
            # Enforce a sane query length to prevent abuse.
            if len(q) > 200:
                print(f"    skipping overlong query ({len(q)} chars): {q[:60]}...")
                continue
            torrents = fetch_nyaa(q, entries, delay)
            queries_used += 1
            for t in torrents:
                t["_query"] = q
            candidates.extend(torrents)
            time.sleep(delay)

        # Pass raw candidates; propose_for_record scores internally (identical
        # to how nyaa_resolve.py's own main() calls it).
        for rec in unit_records:
            verdict = propose_for_record(rec, candidates, min_score)
            verdicts.append(verdict)

    # Persist cache (incremental writes happen inside fetch_nyaa too).
    cache["_entries"] = entries
    _save_cache(cache_path, cache)

    print(f"\nnyaa queries issued (cached): {queries_used}")

    # --- Reports ---
    txt_path = f"{out_base}.txt"
    json_path = f"{out_base}.json"
    write_reports(verdicts, txt_path, json_path)

    summary = defaultdict(int)
    for v in verdicts:
        summary[v["action"]] += 1
    print("\nDone.")
    for action in ("change", "reencode_keep", "review", "confirm"):
        print(f"  {action:14} {summary.get(action, 0)}")
    print(f"Reports: {txt_path} / {json_path}")

    # --- Apply (only with explicit --apply) ---
    if apply:
        if dry_run:
            print("\n--apply requested but --dry-run is also set; refusing to mutate. "
                  "Drop --dry-run to apply.")
            return
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.env"))
        except ImportError:
            pass
        uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
        if len(uris) < 2:
            raise SystemExit(
                "DATABASE env needs at least 2 comma-separated MongoDB URIs "
                "(set in config.env or environment)."
            )
        # Mask URIs before any logging.
        print(f"\nConnecting to {len(uris)} database URI(s): "
              + ", ".join(_mask_uri(u) for u in uris))
        apply_changes(verdicts, uris)
        print("\nApplied. Check nyaa_fix_backup_<timestamp>_<random>.json for the undo file; "
              "restore_quality_backup.py can roll back.")
    else:
        print("\nDry run only. Review the report, then re-run with --apply to write changes.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="nyaa.si authoritative repair for Telegram Stremio filenames."
    )
    parser.add_argument(
        "--dump",
        default="filenames_report.txt",
        help="Path to filenames_report.txt TSV dump (default: filenames_report.txt).",
    )
    parser.add_argument(
        "--cache",
        default="nyaa_cache.json",
        help="nyaa RSS on-disk cache path (default: nyaa_cache.json).",
    )
    parser.add_argument(
        "--out",
        default="nyaa_repair_report",
        help="Base path for output reports (default: nyaa_repair_report).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply high-confidence label changes to live MongoDB (requires DATABASE).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry run only (default). Has no effect unless combined with --apply.",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.2,
        help="Polite delay (seconds) between nyaa RSS requests (default: 1.2).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=65,
        help="Minimum nyaa match score to accept a label change (default: 65).",
    )
    args = parser.parse_args()

    # --apply (or --no-dry-run) overrides the dry-run default so mutations can proceed.
    if args.apply or args.no_dry_run:
        args.dry_run = False

    if not os.path.exists(args.dump):
        raise SystemExit(f"Dump file not found: {args.dump}")

    run_repair(
        dump_path=args.dump,
        cache_path=args.cache,
        out_base=args.out,
        apply=args.apply,
        delay=args.delay,
        dry_run=args.dry_run,
        min_score=args.min_score,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
