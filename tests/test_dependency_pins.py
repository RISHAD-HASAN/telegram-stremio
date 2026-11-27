"""requirements.txt and uv.lock must describe the same versions."""

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins():
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if match:
            pins[_normal(match.group(1))] = match.group(2)
    return pins


def _locked():
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {_normal(p["name"]): p["version"] for p in data["package"]}


def test_every_requirement_is_an_exact_pin():
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.match(r"^[A-Za-z0-9_.-]+==[^\s;]+(\s*;.*)?$", line), line


def test_lock_file_has_the_pinned_versions():
    locked = _locked()
    assert {name: locked.get(name) for name in _pins()} == _pins()


def test_project_version_is_the_same_everywhere():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "Backend" / "__init__.py").read_text(encoding="utf-8")).group(1)
    assert _locked()["telegram-stremio"] == project == package
