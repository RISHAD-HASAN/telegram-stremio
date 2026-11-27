"""Checks that run before the server starts."""

import pytest

from Backend import config
from Backend.config import Telegram, config_problems, database_problems


@pytest.fixture
def complete_config(monkeypatch):
    monkeypatch.setattr(Telegram, "API_ID", 12345)
    monkeypatch.setattr(Telegram, "API_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(Telegram, "BOT_TOKEN", "123456:placeholder")
    monkeypatch.setattr(Telegram, "DATABASE", ["mongodb://127.0.0.1:9/tracking", "mongodb://127.0.0.1:9/storage1"])


def test_complete_config_has_no_problems(complete_config):
    assert config_problems() == []


@pytest.mark.parametrize("field, empty, word", [
    ("API_ID", 0, "API_ID"),
    ("API_HASH", "", "API_HASH"),
    ("BOT_TOKEN", "", "BOT_TOKEN"),
    ("DATABASE", ["mongodb://127.0.0.1:9/only-one"], "DATABASE"),
])
def test_each_missing_setting_is_named(complete_config, monkeypatch, field, empty, word):
    monkeypatch.setattr(Telegram, field, empty)

    problems = config_problems()

    assert len(problems) == 1
    assert problems[0].startswith(word)


def test_non_numeric_values_fall_back_instead_of_raising(monkeypatch):
    monkeypatch.setenv("API_ID", "not-a-number")
    assert config._int_env("API_ID") == 0
    monkeypatch.setenv("PORT", "")
    assert config._int_env("PORT", 8000) == 8000


async def test_unreachable_database_is_reported_without_the_uri(monkeypatch):
    monkeypatch.setattr(Telegram, "DATABASE", [
        "mongodb://reader:hidden-password@127.0.0.1:9/tracking",
        "mongodb://reader:hidden-password@127.0.0.1:9/storage1",
    ])

    problems = await database_problems(timeout_ms=300)

    assert len(problems) == 2
    assert "tracking database" in problems[0] and "storage database" in problems[1]
    assert all("hidden-password" not in p and "127.0.0.1" not in p for p in problems)
