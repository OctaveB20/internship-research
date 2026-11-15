"""Tests de src/config.py."""
from datetime import date

import pytest
from pydantic import ValidationError

from src import config


def test_project_yaml_files_are_valid():
    cfg = config.load_config()
    assert cfg.model == "claude-haiku-5-5"
    assert cfg.quota_soft_limit == 180
    assert len(cfg.queries) == 3
    prof = config.load_profile()
    assert prof.available_from == date(2027, 1, 5)
    assert prof.duration_months.min == 4


def test_profile_rejects_inverted_duration(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("target_countries: [France]\nduration_months: {min: 6, max: 4}\n"
                 "available_from: 2027-01-05\nlatest_start: 2027-03-31\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        config.load_profile(p)


def test_quota_limit_cannot_exceed_free_plan(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("quota_soft_limit: 500\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        config.load_config(p)


def test_dev_mode_and_secrets(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("RAPIDAPI_KEY", "  abc  ")
    assert config.dev_mode() is True
    assert config.get_secret("RAPIDAPI_KEY") == "abc"
    monkeypatch.setenv("DEV_MODE", "0")
    monkeypatch.delenv("RAPIDAPI_KEY")
    assert config.dev_mode() is False
    assert config.get_secret("RAPIDAPI_KEY") == ""
