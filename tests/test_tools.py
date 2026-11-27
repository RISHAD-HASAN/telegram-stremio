"""The library export tool and the tools that read its output."""

import json

import diagnose_library
import dump_all_filenames
import indexer
import nyaa_repair


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    def find(self, _query):
        return list(self.docs)


def make_database():
    return {
        "movie": FakeCollection([
            {
                "title": "Paper Lanterns",
                "telegram": [
                    {"quality": "1080p", "name": "Paper.Lanterns.2019.1080p.BluRay.mkv", "size": "2.5GB"},
                    {"quality": "720p", "name": "", "size": "1GB"},
                ],
            },
        ]),
        "tv": FakeCollection([
            {
                "title": "Harbor Lights",
                "seasons": [
                    {
                        "season_number": 0,
                        "episodes": [
                            {"episode_number": 1, "telegram": [
                                {"quality": "720p", "name": "Harbor Lights S00E01 720p.mkv", "size": "300.5MB"},
                            ]},
                        ],
                    },
                    {
                        "season_number": 2,
                        "episodes": [
                            {"episode_number": 3, "telegram": [
                                {"quality": "1080p", "name": "Harbor\tLights S02E03 1080p.mkv.001",
                                 "size": "3GB", "group_key": "abc", "parts": [{}, {}]},
                            ]},
                        ],
                    },
                ],
            },
        ]),
    }


def test_collect_records_reads_movies_and_episodes():
    records = dump_all_filenames.collect_records(make_database(), "storage_1")

    assert [r["media_type"] for r in records] == ["movie", "tv", "tv"]
    movie, special, split = records
    assert movie["title"] == "Paper Lanterns" and movie["season"] is None
    assert movie["size_mb"] == 2560.0
    assert (special["season"], special["episode"]) == (0, 1)
    assert special["size_mb"] == 300.5
    assert split["is_split"] is True
    # a tab inside a name would shift the columns of the text export
    assert "\t" not in split["file_name"]


def test_export_is_readable_by_the_other_tools(tmp_path):
    records = dump_all_filenames.collect_records(make_database(), "storage_1")
    txt_path, json_path = dump_all_filenames.write_reports(records, str(tmp_path / "filenames_report"))

    loaded = indexer.load_records_from_tsv(txt_path)
    assert {r["file_name"] for r in loaded} == {r["file_name"] for r in records}

    repaired = nyaa_repair.load_records(txt_path)
    by_name = {r["file_name"]: r for r in repaired}
    assert by_name["Harbor Lights S00E01 720p.mkv"]["season"] == 0
    assert by_name["Harbor Lights S00E01 720p.mkv"]["episode"] == 1
    assert by_name["Paper.Lanterns.2019.1080p.BluRay.mkv"]["media_type"] == "movie"

    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["total"] == 3
    assert {"file_name", "title", "season", "episode", "stored_label", "size", "is_split", "media_type"} <= set(data["records"][0])


def test_mask_uri_hides_the_password():
    masked = dump_all_filenames.mask_uri("mongodb+srv://reader:s3cret@cluster.example.net/?retryWrites=true")
    assert "s3cret" not in masked
    assert masked.startswith("mongodb+srv://reader:")


def test_diagnose_flags_an_episode_filed_under_the_wrong_season():
    parsed = indexer.parse_filename("Harbor Lights S02E03 1080p.mkv")
    wrong = {"media_type": "tv", "title": "Harbor Lights", "season_number": 1, "episode_number": 3, "quality": "1080p"}
    right = dict(wrong, season_number=2)

    assert "SXE_WRONG" in [i["code"] for i in diagnose_library._classify(parsed, wrong)]
    assert diagnose_library._classify(parsed, right) == []
    assert [i["code"] for i in diagnose_library._classify(parsed, None)] == ["FILE_NOT_INDEXED"]


def test_diagnose_handles_a_special_filed_as_a_movie():
    parsed = indexer.parse_filename("Harbor Lights S00E01 720p.mkv")
    as_movie = {"media_type": "movie", "title": "Harbor Lights", "quality": "720p"}

    codes = [i["code"] for i in diagnose_library._classify(parsed, as_movie)]
    assert "MEDIA_TYPE_WRONG" in codes


# The tools are started as scripts ("python tools/indexer.py"), which puts only
# the tools folder on the import path. These tests start them the same way,
# from another directory, instead of importing them through pytest's path.

import csv
import os
import subprocess
import sys

import pytest

import reindex_full_pipeline

TOOLS = os.path.dirname(os.path.abspath(indexer.__file__))
DUMP_LINE = "tv\tstorage_1\tX\t-\t1080p\t1GB\t-\t{name}\n"


def _run_tool(script, args, cwd, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "DATABASE")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, os.path.join(TOOLS, script), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120,
    )


def test_indexer_started_as_a_script_reads_names_like_the_bot(tmp_path):
    names = ["The Long Road (2019) 1080p BluRay x264.mkv", "Harbor Lights S01E05v2 1080p.mkv",
             "Harbor Lights NCOP 1080p.mkv"]
    (tmp_path / "filenames_report.txt").write_text("".join(DUMP_LINE.format(name=n) for n in names), encoding="utf-8")

    done = _run_tool("indexer.py", ["--dump", "filenames_report.txt", "--parse-only"], tmp_path)
    assert done.returncode == 0, done.stderr[-400:]

    with open(tmp_path / "indexer_report_parse.tsv", encoding="utf-8") as fh:
        rows = {row["name"]: row for row in csv.DictReader(fh, delimiter="\t")}
    # The built-in fallback parser drops the article, misses "v2" and keeps the extra
    assert rows[names[0]]["title"] == "The Long Road"
    assert (rows[names[1]]["season"], rows[names[1]]["episode"]) == ("1", "5")
    assert rows[names[2]]["type"] == "skip"


def test_repair_passes_relative_paths_through_unchanged(tmp_path, monkeypatch):
    import repair_library

    (tmp_path / "plan.tsv").write_text(DUMP_LINE.format(name="Harbor Lights S01E01 1080p.mkv"), encoding="utf-8")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(repair_library.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["repair_library.py", "--plan", "plan.tsv", "--out", "report"])

    assert repair_library.main() == 0
    assert seen["cmd"][1] == os.path.join(TOOLS, "indexer.py")
    # Run in the caller's directory, so "report" is written where the user is
    assert "cwd" not in seen["kwargs"]
    assert "--dry-run" in seen["cmd"] and "--apply" not in seen["cmd"]


def test_pipeline_starts_its_steps_with_options_they_accept(tmp_path, monkeypatch):
    (tmp_path / "filenames_report.txt").write_text(DUMP_LINE.format(name="Harbor Lights S01E01 1080p.mkv"), encoding="utf-8")
    commands = []

    def fake_run(cmd, logger, label):
        commands.append(cmd)
        if label == "diagnose_library.py":
            out_dir = cmd[cmd.index("--out") + 1]
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "repair_plan.tsv"), "w", encoding="utf-8") as fh:
                fh.write("header\n" + DUMP_LINE.format(name="Harbor Lights S01E01 1080p.mkv"))
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE", "mongodb://localhost:27017/a,mongodb://localhost:27017/b")
    monkeypatch.setattr(reindex_full_pipeline, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["reindex_full_pipeline.py", "--dump", "filenames_report.txt", "--out", "out"])

    assert reindex_full_pipeline.main() == 0
    assert [os.path.basename(c[1]) for c in commands] == ["diagnose_library.py", "repair_library.py"]
    for cmd in commands:
        assert os.path.isfile(cmd[1]), cmd[1]
        accepted = _run_tool(os.path.basename(cmd[1]), ["--help"], tmp_path).stdout
        for option in (token for token in cmd[2:] if token.startswith("--")):
            assert option in accepted, f"{os.path.basename(cmd[1])} does not accept {option}"


def test_diagnose_stops_when_a_storage_database_cannot_be_read():
    uris = ["mongodb://reader:hidden-password@127.0.0.1:9/a", "mongodb://reader:hidden-password@127.0.0.1:9/b"]
    with pytest.raises(RuntimeError) as failure:
        diagnose_library.load_index(uris)
    assert "could not read" in str(failure.value)
    assert "hidden-password" not in str(failure.value)


@pytest.mark.parametrize("script, args", [
    ("indexer.py", ["--dump", "filenames_report.txt", "--apply"]),
    ("repair_library.py", ["--plan", "filenames_report.txt", "--apply"]),
    ("reindex_full_pipeline.py", ["--dump", "filenames_report.txt", "--apply"]),
])
def test_apply_is_refused_before_anything_is_read_or_written(tmp_path, script, args):
    (tmp_path / "filenames_report.txt").write_text(DUMP_LINE.format(name="Harbor Lights S01E01 1080p.mkv"), encoding="utf-8")
    # An unreachable database: a tool that tried to connect would hang or fail differently
    done = _run_tool(script, args, tmp_path, {"DATABASE": "mongodb://127.0.0.1:9/a,mongodb://127.0.0.1:9/b"})
    assert done.returncode == 2
    assert "--apply is switched off" in done.stdout
    written = sorted(p.name for p in tmp_path.iterdir() if p.name not in ("filenames_report.txt", "log.txt"))
    assert written == []
