"""Smoke tests des pages Streamlit en mode dev (aucun appel réseau)."""
import pytest
from streamlit.testing.v1 import AppTest

from src.config import ROOT

PAGES = [str(ROOT / p) for p in ("app.py", "pages/1_Recherche.py", "pages/2_Offres.py", "pages/3_Chat.py",
                                 "pages/4_Candidatures.py")]
RECHERCHE, OFFRES = PAGES[1], PAGES[2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_on_empty_db(env, page):
    assert not AppTest.from_file(page, default_timeout=30).run().exception


def test_full_dev_pipeline_from_ui(env):
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    at.button[0].click().run()
    assert not at.exception
    assert "20 nouvelle(s) offre(s)" in at.success[0].value
    for page in PAGES:
        assert not AppTest.from_file(page, default_timeout=30).run().exception
    offres = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert len(offres.dataframe[0].value) == 8


def test_password_gate(env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert not at.title  # page bloquée
    at.text_input[0].input("faux").run()
    assert at.error and not at.title
    at.text_input[0].input("s3cret").run()
    assert at.title[0].value == "Offres"


def test_pipeline_on_postgres(env, monkeypatch):
    from tests.conftest import TEST_PG_URL, reset_postgres
    from src import db, ui
    if not TEST_PG_URL:
        pytest.skip("TEST_DATABASE_URL non défini")
    reset_postgres(TEST_PG_URL)
    ui._shared_postgres.clear()
    monkeypatch.setenv("DATABASE_URL", TEST_PG_URL)
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    assert any("Supabase" in c.value for c in at.sidebar.caption)
    at.button[0].click().run()
    assert not at.exception and "20 nouvelle(s) offre(s)" in at.success[0].value
    for page in PAGES:
        assert not AppTest.from_file(page, default_timeout=30).run().exception
    with db.connect(url=TEST_PG_URL) as c:
        assert db.count_jobs(c) == 20
    ui._shared_postgres.clear()


def test_bad_database_url_is_readable(env, monkeypatch):
    from src import ui
    ui._shared_postgres.clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres.x:monmdp@127.0.0.1:1/postgres")
    at = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert not at.exception
    assert "Connexion à Supabase impossible" in at.error[0].value and "monmdp" not in at.error[0].value
    ui._shared_postgres.clear()
