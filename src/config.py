"""Configuration : config.yaml, profile.yaml et secrets (.env en local, st.secrets en ligne)."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
FIXTURES_DIR = ROOT / "tests" / "fixtures"
CONFIG_PATH = ROOT / "config.yaml"
PROFILE_PATH = ROOT / "profile.yaml"

load_dotenv(ROOT / ".env")


def _load_streamlit_secrets() -> str:
    """Copie st.secrets dans os.environ (sans écraser le .env). Renvoie un message d'erreur ou ''."""
    try:
        import streamlit as st

        for key, value in st.secrets.items():
            # Tolère une section ([general]…) et les noms en minuscules.
            pairs = value.items() if hasattr(value, "items") else [(key, value)]
            for k, v in pairs:
                if isinstance(v, (str, int, float, bool)):
                    os.environ.setdefault(k.upper(), str(v))
    except Exception as e:  # pas de secrets.toml en local : normal ; TOML invalide : signalé
        if "NotFound" in type(e).__name__ or isinstance(e, FileNotFoundError):
            return ""
        return f"{type(e).__name__} : {e}"
    return ""


SECRETS_ERREUR = _load_streamlit_secrets()


def get_secret(name: str) -> str:
    """Lit une clé (RAPIDAPI_KEY, ANTHROPIC_API_KEY…). Chaîne vide si absente. Ne jamais la logger."""
    return os.getenv(name, "").strip()


def dev_mode() -> bool:
    """True si DEV_MODE=1 : aucun appel réseau, lecture des fixtures."""
    return os.getenv("DEV_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def db_path() -> Path:
    """Chemin de la base SQLite (JOBS_DB pour le surcharger)."""
    return Path(os.getenv("JOBS_DB", DATA_DIR / "jobs.db"))


# ---------- config.yaml ----------
class SearchQuery(BaseModel):
    query: str
    country: str = "fr"


class AppConfig(BaseModel):
    model: str = "claude-haiku-5-5"
    quota_soft_limit: int = Field(180, ge=0, le=200)
    date_posted: Literal["all", "today", "3days", "week", "month"] = "week"
    employment_types: str = "INTERN"
    request_timeout_s: float = 30
    queries: list[SearchQuery] = []


# ---------- profile.yaml ----------
class DurationRange(BaseModel):
    min: int = Field(ge=1)
    max: int = Field(ge=1)

    @model_validator(mode="after")
    def _check(self) -> "DurationRange":
        if self.min > self.max:
            raise ValueError("duration_months.min doit être ≤ max")
        return self


class Weights(BaseModel):
    location: float = 15
    duration: float = 20
    start_date: float = 20
    skills: float = 25
    languages: float = 10
    domain: float = 10
    stipend: float = 0


class Profile(BaseModel):
    target_countries: list[str]
    target_cities: list[str] = []
    remote_ok: bool = True
    duration_months: DurationRange
    available_from: date
    latest_start: date
    domains: list[str] = []
    skills: list[str] = []
    languages: dict[str, str] = {}
    min_stipend_eur: float | None = None
    weights: Weights = Weights()

    @model_validator(mode="after")
    def _check_dates(self) -> "Profile":
        if self.available_from > self.latest_start:
            raise ValueError("available_from doit être ≤ latest_start")
        return self


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Charge et valide config.yaml."""
    return AppConfig.model_validate(_read_yaml(path))


def load_profile(path: Path = PROFILE_PATH) -> Profile:
    """Charge et valide profile.yaml."""
    return Profile.model_validate(_read_yaml(path))
