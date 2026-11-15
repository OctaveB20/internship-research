"""Fixtures pytest communes.

`conn` tourne sur SQLite, et aussi sur Postgres si TEST_DATABASE_URL est défini
(ex. postgresql://postgres@127.0.0.1:54329/scout_test) : chaque test passe alors sur les deux bases.
"""
import os

import pytest

from src import db

TEST_PG_URL = os.getenv("TEST_DATABASE_URL", "")
TABLES = "applications, scores, extractions, api_usage, api_cache, jobs"


def reset_postgres(url: str) -> None:
    """Vide la base de test Postgres (tables supprimées)."""
    with db.connect(url=url) as c:
        c.execute(f"DROP TABLE IF EXISTS {TABLES} CASCADE")


@pytest.fixture(autouse=True)
def _no_database_url(monkeypatch):
    """Les tests n'utilisent jamais une vraie base Supabase par accident."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)


@pytest.fixture(params=["sqlite", "postgres"])
def conn(request, tmp_path):
    """Base vierge : SQLite temporaire, ou Postgres de test vidé."""
    if request.param == "postgres":
        if not TEST_PG_URL:
            pytest.skip("TEST_DATABASE_URL non défini")
        reset_postgres(TEST_PG_URL)
        c = db.connect(url=TEST_PG_URL)
    else:
        c = db.connect(tmp_path / "test.db")
    db.init_db(c)
    yield c
    c.close()


def make_job(job_id: str = "j1", **overrides) -> dict:
    """Offre JSearch minimale."""
    job = {
        "job_id": job_id,
        "job_title": "Stagiaire Contrôle de gestion (H/F)",
        "employer_name": "Acme SAS",
        "job_city": "Paris",
        "job_country": "FR",
        "job_is_remote": False,
        "job_description": "Stage de 6 mois…",
        "job_apply_link": "https://example.com/apply",
        "job_posted_at_datetime_utc": "2026-11-10T08:00:00.000Z",
        "job_publisher": "LinkedIn",
    }
    job.update(overrides)
    return job
