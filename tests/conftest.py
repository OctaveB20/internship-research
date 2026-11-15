"""Fixtures pytest communes."""
import pytest

from src import db


@pytest.fixture
def conn(tmp_path):
    """Base SQLite vierge dans un dossier temporaire."""
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
