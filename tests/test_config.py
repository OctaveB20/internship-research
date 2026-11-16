"""Tests de src/config.py."""
from datetime import date

import pytest
from pydantic import ValidationError

from src import config


def test_project_yaml_files_are_valid():
    cfg = config.load_config()
    assert cfg.llm_provider in config.LLM_KEYS and cfg.model
    assert cfg.quota_soft_limit == 180
    assert len(cfg.default_titles) == 3
    prof = config.load_profile()
    assert prof.available_from == date(2027, 1, 5)
    assert prof.duration_months.min == 5


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


@pytest.mark.parametrize("raw, password, expected", [
    ('"postgresql://postgres.abc:[p@ss:w]@aws-0.pooler.supabase.com:6543/postgres" ', "",
     "postgresql://postgres.abc:p%40ss%3Aw@aws-0.pooler.supabase.com:6543/postgres"),
    ("postgresql://postgres.abc:[YOUR-PASSWORD]@h:6543/postgres", "vrai#mdp",
     "postgresql://postgres.abc:vrai%23mdp@h:6543/postgres"),
    ("postgresql://neondb_owner:abc123@ep-x-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
     "", "postgresql://neondb_owner:abc123@ep-x-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"),
    ("", "", ""),
])
def test_normaliser_url(raw, password, expected):
    assert config.normaliser_url(raw, password) == expected


def test_profile_per_user(tmp_path):
    p = tmp_path / "profile.yaml"
    p.write_text("target_countries: [France]\nduration_months: {min: 5, max: 6}\navailable_from: 2027-01-05\n"
                 "latest_start: 2027-03-31\nskills: [Excel]\nusers:\n  Marilou:\n    skills: [SAP, Excel]\n",
                 encoding="utf-8")
    assert config.load_profile(p).skills == ["Excel"]
    assert config.load_profile(p, user="Marilou").skills == ["SAP", "Excel"]
    assert config.load_profile(p, user="Alex").skills == ["Excel"]  # pas de section : valeurs communes
    assert config.profile_users(p) == ["Marilou"]


def test_octave_profile_from_cv():
    octave = config.load_profile(user="Octave")
    assert {"Python", "Microsoft Copilot", "Streamlit", "Clôture mensuelle"} <= set(octave.skills)
    assert "Power BI" not in octave.skills and octave.languages["English"] == "bilingual"
    assert octave.languages["Spanish"] == "intermediate"
