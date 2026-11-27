#!/usr/bin/env python3
"""
reindex_full_pipeline.py - diagnose and repair the library in one run.

Starts from an export written by dump_all_filenames.py and then runs:
  1. diagnose_library.py       - find entries filed in the wrong place
  2. repair_library.py         - run the flagged files through indexer.py and
                                 report where each of them belongs

Every step is written to reindex_pipeline_<timestamp>.log, so the changes can
be reviewed afterwards.

Telegram is not contacted; the tools work from the MongoDB library alone.

Usage:
  python tools/dump_all_filenames.py
  python tools/reindex_full_pipeline.py --dump filenames_report.txt

The run only reports. --apply is switched off in this release; run
python tools/indexer.py --apply to read why.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS_DIR)

from indexer import APPLY_UNAVAILABLE, _safe_path, _mask_uri


def _utcnow() -> datetime:
    """Current UTC time without tzinfo, as the reports print it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PipelineLogger:
    """Writes every pipeline step to a timestamped log file."""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self._ensure_dir()
        self._write("=== Telegram Stremio Library Re-Index Pipeline ===")
        self._write(f"Started: {_utcnow().isoformat()} UTC")
        self._write(f"Log file: {log_path}")
        self._write("=" * 70)

    def _ensure_dir(self):
        d = os.path.dirname(self.log_path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _write(self, msg: str):
        line = f"[{_utcnow().isoformat()} UTC] {msg}"
        print(line)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def section(self, title: str):
        self._write("")
        self._write(f"--- {title} ---")
        self._write("")

    def step(self, name: str, detail: str = ""):
        self._write(f"[STEP] {name}" + (f" - {detail}" if detail else ""))

    def result(self, name: str, ok: bool, detail: str = ""):
        status = "OK" if ok else "FAILED"
        self._write(f"[RESULT] {name}: {status}" + (f" - {detail}" if detail else ""))

    def data(self, label: str, data: dict):
        self._write(f"[DATA] {label}: {json.dumps(data, default=str, ensure_ascii=False)}")


def check_env() -> list[str]:
    """Validate DATABASE env var."""
    uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
    if len(uris) < 2:
        print("[pipeline] ERROR: DATABASE env needs at least 2 comma-separated MongoDB URIs")
        print("         Example: export DATABASE='mongodb_uri_1,mongodb_uri_2'")
        return []
    return uris


def run(cmd: list[str], logger: PipelineLogger, label: str) -> int:
    """Run a subprocess command, logging output."""
    logger.step(label, " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger._write(f"    {line}")
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger._write(f"    STDERR: {line}")
    logger.result(label, result.returncode == 0, f"exit code {result.returncode}")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Complete library re-index pipeline with detailed logging."
    )
    parser.add_argument(
        "--dump", required=True,
        help="Path to filenames_report.txt (from dump_all_filenames.py)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Switched off in this release; the pipeline only reports",
    )
    parser.add_argument(
        "--out", default="./reindex_output",
        help="Output directory for all reports and logs (default: ./reindex_output)",
    )
    parser.add_argument(
        "--channel", type=int, default=0,
        help="Telegram channel ID for encoded_string (default: 0)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=12,
        help="Max concurrent API requests (default: 12)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.2,
        help="Delay between API requests in seconds (default: 0.2)",
    )
    args = parser.parse_args()

    if args.apply:
        print(APPLY_UNAVAILABLE)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _utcnow().strftime("%Y%m%d_%H%M%S")
    log_path = out_dir / f"reindex_pipeline_{timestamp}.log"
    logger = PipelineLogger(str(log_path))

    # --- Validate environment ---
    uris = check_env()
    if not uris:
        return 1
    logger.data("database_uris", [{"masked": _mask_uri(u)} for u in uris])

    # --- Validate dump file ---
    dump_path = _safe_path(args.dump, "--dump", must_exist=True)
    logger.step("Validate dump file", str(dump_path))

    from indexer import load_records_from_tsv
    try:
        records = load_records_from_tsv(dump_path)
        logger.result("Load dump", True, f"{len(records)} records loaded")
    except Exception as e:
        logger.result("Load dump", False, str(e))
        return 1

    logger.data("dump_summary", {
        "total_records": len(records),
        "tv": sum(1 for r in records if r.get("media_type") == "tv"),
        "movies": sum(1 for r in records if r.get("media_type") == "movie"),
        "with_sxe": sum(1 for r in records if r.get("season") is not None and r.get("episode") is not None),
        "s00_specials": sum(1 for r in records if r.get("season") == 0),
    })

    # --- Step 1: Diagnose ---
    logger.section("STEP 1: Diagnose library misclassifications")
    diagnose_dir = out_dir / "diagnose_output"
    diagnose_cmd = [
        sys.executable, os.path.join(TOOLS_DIR, "diagnose_library.py"),
        "--dump", str(dump_path),
        "--out", str(diagnose_dir),
    ]
    rc = run(diagnose_cmd, logger, "diagnose_library.py")

    # Read diagnose results if available
    diagnose_json = diagnose_dir / "diagnose_report.json"
    stats = {}
    if diagnose_json.exists():
        try:
            with open(diagnose_json, encoding="utf-8") as f:
                diag = json.load(f)
            stats = diag.get("stats", {})
            logger.data("diagnose_stats", stats)
        except Exception as e:
            logger._write(f"    Warning: could not read diagnose report: {e}")

    if rc != 0:
        logger._write("Diagnose step failed - aborting pipeline.")
        return rc

    # --- Step 2: Repair ---
    logger.section("STEP 2: Repair flagged entries")
    repair_plan = diagnose_dir / "repair_plan.tsv"
    if not repair_plan.exists():
        logger._write("No repair_plan.tsv found - nothing to repair.")
        logger.result("Pipeline", True, "No issues found, nothing to fix")
        return 0

    # Count issues needing fix
    needs_fix = 0
    try:
        with open(repair_plan, encoding="utf-8") as f:
            next(f)  # skip header
            for line in f:
                if line.strip():
                    needs_fix += 1
        logger.data("repair_plan", {"files_needing_fix": needs_fix})
    except Exception:
        needs_fix = 0

    if needs_fix == 0:
        logger.result("Repair plan", True, "No files need repair")
        logger.result("Pipeline", True, "Library is clean")
        return 0

    logger.step("Repair plan", f"{needs_fix} files flagged for repair")

    # Run repair via indexer.py
    repair_cmd = [
        sys.executable, os.path.join(TOOLS_DIR, "repair_library.py"),
        "--plan", str(repair_plan),
        "--channel", str(args.channel),
        "--concurrency", str(args.concurrency),
        "--delay", str(args.delay),
        "--out", str(out_dir / "repair_report"),
    ]
    # repair_library.py is a dry run unless --apply is given
    if args.apply:
        repair_cmd.append("--apply")

    rc = run(repair_cmd, logger, "repair_library.py")

    # --- Summary ---
    logger.section("PIPELINE SUMMARY")
    logger.data("final_stats", {
        "dump_records": len(records),
        "diagnose_stats": stats,
        "repair_needs_fix": needs_fix,
        "mode": "APPLY" if args.apply else "DRY-RUN",
        "repair_exit_code": rc,
    })
    logger._write("")
    logger._write(f"Log saved: {log_path}")
    logger._write(f"Reports in: {out_dir}/")
    if args.apply:
        logger._write("BACKUPS: nyaa_fix_backup_<timestamp>_<random>.json written before every mutation")
        logger._write("         To roll back: restore documents from the backup JSON file")
    else:
        logger._write("DRY-RUN: No MongoDB writes were made")
        logger._write("         Correct the listed entries on the server (docs/usage.md)")

    logger.result("Pipeline", rc == 0, f"exit code {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
