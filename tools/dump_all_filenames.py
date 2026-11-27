#!/usr/bin/env python3
"""
dump_all_filenames.py - export every indexed file name from MongoDB.

Reads the storage databases and writes one row per stream: where it is filed
(title, season, episode, quality label) and the file name it was indexed from.
The other tools in this folder start from this export.

Outputs (in the current directory unless --out is given):
  filenames_report.txt    tab-separated, read by indexer.py, indexer_gap.py,
                          diagnose_library.py and nyaa_repair.py
  filenames_report.json   the same rows as JSON, read by nyaa_resolve.py

Columns of the .txt file:
  media_type  storage  title  SxE  quality  size  split  file_name

Usage:
  python tools/dump_all_filenames.py
  python tools/dump_all_filenames.py --out exports/filenames_report

The database is only read, never changed. DATABASE is taken from the
environment or from config.env in the project root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_NAME = os.getenv("DB_NAME", "dbFyvio")

_SIZE_RE = re.compile(r"(?i)^\s*([\d.]+)\s*(B|KB|MB|GB|TB)\s*$")
_SIZE_FACTORS = {"B": 1 / (1024 * 1024), "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024}


def _load_config_env() -> None:
    """Fill missing environment variables from config.env, when the file exists."""
    path = os.path.join(PROJECT_ROOT, "config.env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def mask_uri(uri: str) -> str:
    """The URI without its password, for log lines."""
    return re.sub(r"://([^:/@]*):[^@]*@", r"://\1:*****@", uri).split("?")[0]


def size_in_mb(text: str) -> float | None:
    match = _SIZE_RE.match(text or "")
    if not match:
        return None
    return round(float(match.group(1)) * _SIZE_FACTORS[match.group(2).upper()], 2)


def _one_line(text) -> str:
    """Tabs and line breaks would shift the columns of the .txt export."""
    return re.sub(r"[\t\r\n]+", " ", str(text or "")).strip()


def _stream_record(media_type, storage, title, season, episode, stream) -> dict | None:
    name = _one_line(stream.get("name"))
    if not name:
        return None
    size = _one_line(stream.get("size"))
    return {
        "media_type": media_type,
        "storage": storage,
        "title": _one_line(title),
        "season": season,
        "episode": episode,
        "stored_label": _one_line(stream.get("quality")),
        "size": size,
        "size_mb": size_in_mb(size),
        "is_split": bool(stream.get("group_key") or stream.get("parts")),
        "file_name": name,
    }


def collect_records(database, storage: str) -> list[dict]:
    """Every stream of one storage database, movies first, then shows in episode order."""
    records = []
    for doc in database["movie"].find({}):
        for stream in doc.get("telegram") or []:
            record = _stream_record("movie", storage, doc.get("title"), None, None, stream)
            if record:
                records.append(record)
    for doc in database["tv"].find({}):
        for season in doc.get("seasons") or []:
            for episode in season.get("episodes") or []:
                for stream in episode.get("telegram") or []:
                    record = _stream_record(
                        "tv", storage, doc.get("title"),
                        season.get("season_number"), episode.get("episode_number"), stream,
                    )
                    if record:
                        records.append(record)
    return records


def _sxe(record: dict) -> str:
    if record["season"] is None or record["episode"] is None:
        return "-"
    return f"S{record['season']}E{record['episode']}"


def write_reports(records: list[dict], base_path: str) -> tuple[str, str]:
    txt_path, json_path = f"{base_path}.txt", f"{base_path}.json"
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(txt_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# Indexed file names, exported {generated}\n")
        fh.write("# media_type\tstorage\ttitle\tsxe\tquality\tsize\tsplit\tfile_name\n")
        for r in records:
            fh.write("\t".join([
                r["media_type"], r["storage"], r["title"], _sxe(r), r["stored_label"],
                r["size"], "split" if r["is_split"] else "-", r["file_name"],
            ]) + "\n")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": generated, "total": len(records), "records": records},
                  fh, indent=2, ensure_ascii=False)
    return txt_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export every indexed file name from MongoDB.")
    parser.add_argument("--out", default="filenames_report",
                        help="output path without extension (default: filenames_report)")
    args = parser.parse_args()

    _load_config_env()
    uris = [u.strip() for u in (os.getenv("DATABASE") or "").split(",") if u.strip()]
    if len(uris) < 2:
        print("DATABASE needs two comma-separated MongoDB URIs (tracking first, then storage). "
              "Set it in config.env or in the environment.")
        return 1

    try:
        from pymongo import MongoClient
    except ImportError:
        print("pymongo is not installed. Run: pip install -r requirements.txt")
        return 1

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    records: list[dict] = []
    # The first URI is the tracking database; the library lives in the others
    for index, uri in enumerate(uris[1:], start=1):
        storage = f"storage_{index}"
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        try:
            found = collect_records(client[DB_NAME], storage)
        except Exception as exc:
            print(f"Could not read {storage} ({mask_uri(uri)}): {exc}")
            return 1
        finally:
            client.close()
        print(f"{storage}: {len(found)} streams ({mask_uri(uri)})")
        records.extend(found)

    txt_path, json_path = write_reports(records, args.out)
    movies = sum(1 for r in records if r["media_type"] == "movie")
    print(f"Exported {len(records)} streams ({movies} movie, {len(records) - movies} episode).")
    print(f"  {txt_path}")
    print(f"  {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
