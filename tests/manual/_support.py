"""Shared pieces of the standalone check scripts in this folder."""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The scripts import the Backend package and the offline tools directly
for _path in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "tools")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def exported_filenames():
    """File names from a library export, or None when there is no export.

    The export is written by tools/dump_all_filenames.py. Its path comes from
    the FILENAMES_REPORT environment variable, or filenames_report.txt in the
    current directory or the project root.
    """
    candidates = [
        os.getenv("FILENAMES_REPORT") or "",
        "filenames_report.txt",
        os.path.join(PROJECT_ROOT, "filenames_report.txt"),
    ]
    path = next((p for p in candidates if p and os.path.isfile(p)), None)
    if not path:
        return None
    names = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 8 and parts[7]:
                names.append(parts[7])
    return names
