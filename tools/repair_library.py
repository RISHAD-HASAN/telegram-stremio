#!/usr/bin/env python3
"""
repair_library.py - report where the files of a repair plan belong.

Checks the plan written by diagnose_library.py and runs it through indexer.py:
a fresh parse of every file name and a provider lookup. The result is a
report of the title, season and episode each file should be filed under.

Nothing is written to MongoDB. --apply is switched off in this release; run
python tools/indexer.py --apply to read why.

Workflow:
  1. python tools/diagnose_library.py --dump filenames_report.txt --out ./diagnose_output
  2. Review diagnose_output/diagnose_report.csv
  3. python tools/repair_library.py --plan diagnose_output/repair_plan.tsv
  4. Correct the entries on the server (docs/usage.md, "When the match is wrong")
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)

from indexer import APPLY_UNAVAILABLE, _safe_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report where the files of a repair plan belong, via indexer.py."
    )
    parser.add_argument(
        "--plan", required=True,
        help="Path to repair_plan.tsv (from diagnose_library.py)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Switched off in this release; the tool only reports",
    )
    parser.add_argument(
        "--channel", type=int, default=0,
        help="Telegram channel ID for encoded_string (default: 0)",
    )
    parser.add_argument(
        "--anime-channel", action="store_true", default=False,
        help="Treat input as an anime channel (Kitsu-first resolution)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=12,
        help="Max concurrent API requests (default: 12)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.2,
        help="Delay between API requests in seconds (default: 0.2)",
    )
    parser.add_argument(
        "--out", default="repair_report",
        help="Base path for indexer report output (default: repair_report)",
    )
    args = parser.parse_args()

    if args.apply:
        print(APPLY_UNAVAILABLE)
        return 2

    # Validate plan file
    plan_path = _safe_path(args.plan, "--plan", must_exist=True)

    # Check DATABASE env for --apply
    if args.apply:
        uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
        if len(uris) < 2:
            print("[repair] ERROR: DATABASE env needs at least 2 comma-separated MongoDB URIs")
            print("         Example: export DATABASE='mongodb_uri_1,mongodb_uri_2'")
            return 1

    mode = "APPLY (live DB writes)" if args.apply else "DRY-RUN (no DB writes)"
    print(f"[repair] Plan file: {plan_path}")
    print(f"[repair] Mode: {mode}")
    print(f"[repair] Channel ID: {args.channel}")
    print(f"[repair] Concurrency: {args.concurrency} | Delay: {args.delay}s")
    if args.anime_channel:
        print("[repair] Anime channel: True (Kitsu-first resolution)")
    print("-" * 70)

    # Build the indexer.py command
    cmd = [
        sys.executable, os.path.join(TOOLS_DIR, "indexer.py"),
        "--dump", str(plan_path),
        "--channel", str(args.channel),
        "--concurrency", str(args.concurrency),
        "--delay", str(args.delay),
        "--out", args.out,
    ]
    if args.apply:
        cmd.append("--apply")
    else:
        cmd.append("--dry-run")
    if args.anime_channel:
        cmd.append("--anime-channel")

    print(f"[repair] Running: {' '.join(cmd)}")
    print("-" * 70)

    # Run indexer.py in the caller's directory, so relative paths mean the
    # same thing to both scripts
    result = subprocess.run(cmd)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
