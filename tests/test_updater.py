"""update.py must only ever touch a folder it is meant to update."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

UPDATER = Path(__file__).resolve().parent.parent / "update.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def run_updater(cwd, **env):
    clean = {k: v for k, v in os.environ.items() if k not in ("DATABASE", "UPSTREAM_REPO", "UPSTREAM_BRANCH")}
    clean.update(env)
    result = subprocess.run([sys.executable, str(UPDATER)], cwd=cwd, env=clean, capture_output=True, text=True, timeout=60)
    return result.returncode, result.stdout + result.stderr


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "source@localhost")
    git(repo, "config", "user.name", "source")
    (repo / "VERSION").write_text("new\n")
    (repo / ".gitignore").write_text("config.env\nlog.txt\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "source")
    return repo


def test_does_nothing_without_an_update_source(tmp_path):
    folder = tmp_path / "install"
    folder.mkdir()
    (folder / "VERSION").write_text("old\n")

    code, output = run_updater(folder)

    assert code == 0
    assert "No update source configured" in output
    assert not (folder / ".git").exists()
    assert (folder / "VERSION").read_text() == "old\n"


def test_leaves_an_ordinary_checkout_alone(tmp_path, source):
    folder = tmp_path / "checkout"
    folder.mkdir()
    git(folder, "init", "-q", "-b", "master")
    git(folder, "config", "user.email", "dev@localhost")
    git(folder, "config", "user.name", "dev")
    (folder / "VERSION").write_text("old\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "local work")
    (folder / "VERSION").write_text("uncommitted\n")

    code, output = run_updater(folder, UPSTREAM_REPO=str(source))

    assert code == 0
    assert "git pull" in output
    assert (folder / "VERSION").read_text() == "uncommitted\n"


def test_updates_a_deployed_copy_and_keeps_ignored_files(tmp_path, source):
    folder = tmp_path / "deployed"
    folder.mkdir()
    (folder / "VERSION").write_text("old\n")
    (folder / ".gitignore").write_text("config.env\nlog.txt\n")
    (folder / "config.env").write_text(f'UPSTREAM_REPO="{source}"\n')

    code, output = run_updater(folder)

    assert code == 0, output
    assert (folder / "VERSION").read_text() == "new\n"
    assert (folder / "config.env").exists()

    # a later start updates again through the repository the updater created
    (source / "VERSION").write_text("newer\n")
    git(source, "commit", "-q", "-am", "next")
    code, output = run_updater(folder)

    assert code == 0, output
    assert (folder / "VERSION").read_text() == "newer\n"


def test_rejects_a_source_that_looks_like_an_option(tmp_path):
    folder = tmp_path / "install"
    folder.mkdir()
    marker = tmp_path / "marker"

    code, output = run_updater(folder, UPSTREAM_REPO=f"--upload-pack=touch {marker}")

    assert code == 0
    assert "not valid" in output
    assert not marker.exists()
    assert not (folder / ".git").exists()


def test_does_not_log_credentials_from_the_source_url(tmp_path):
    folder = tmp_path / "install"
    folder.mkdir()

    run_updater(folder, UPSTREAM_REPO="https://someone:not-a-real-token@invalid.invalid/owner/name.git")

    assert "not-a-real-token" not in (folder / "log.txt").read_text()
