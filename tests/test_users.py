"""Plusieurs utilisateurs : chacun ses offres, scores, statuts, critères ; migration des anciennes bases."""
import pytest

from src import chat, config, criteria as crit, db, extract, jsearch, offers, scoring
from tests.conftest import DEV_QUERIES, TEST_PG_URL, make_job, reset_postgres

OLD_SCHEMA = """
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, job_title TEXT, employer_name TEXT, job_city TEXT, job_country TEXT,
    job_is_remote INTEGER, job_description TEXT, job_apply_link TEXT, job_posted_at_datetime_utc TEXT,
    job_publisher TEXT, raw_json TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE scores (job_id TEXT PRIMARY KEY, score DOUBLE PRECISION NOT NULL, breakdown_json TEXT NOT NULL,
    scored_at TEXT NOT NULL);
CREATE TABLE applications (job_id TEXT PRIMARY KEY, status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO jobs VALUES ('j1', 'Stage', 'Acme', 'Paris', 'FR', 0, '', 'https://x', NULL, NULL, '{}', '2026-11-01', '2026-11-01');
INSERT INTO jobs VALUES ('j2', 'Stage 2', 'Beta', 'Lyon', 'FR', 0, '', 'https://y', NULL, NULL, '{}', '2026-11-01', '2026-11-01');
INSERT INTO scores VALUES ('j1', 80, '{}', '2026-11-01');
INSERT INTO applications VALUES ('j1', 'postulé', 'relance lundi', '2026-11-02');
INSERT INTO settings VALUES ('search_criteria', '{"titles": ["stage FP&A"], "countries": ["Belgium"]}');
"""


@pytest.fixture(params=["sqlite", "postgres"])
def old_db(request, tmp_path):
    """Base au format d'avant les utilisateurs."""
    if request.param == "postgres":
        if not TEST_PG_URL:
            pytest.skip("TEST_DATABASE_URL non défini")
        reset_postgres(TEST_PG_URL)
        c = db.connect(url=TEST_PG_URL)
    else:
        c = db.connect(tmp_path / "old.db")
    for statement in OLD_SCHEMA.split(";"):
        if statement.strip():
            c.execute(statement)
    c.commit()
    yield c
    c.close()


def test_migration_gives_everything_to_octave(old_db):
    db.init_db(old_db, owner="Octave")
    db.init_db(old_db, owner="Octave")  # idempotent
    assert db.user_job_ids(old_db, "Octave") == {"j1", "j2"} and db.user_job_ids(old_db, "Marilou") == set()
    app = db.get_application(old_db, "j1", "Octave")
    assert (app["status"], app["notes"]) == ("postulé", "relance lundi")
    assert db.get_application(old_db, "j1", "Marilou")["status"] == "à voir"
    assert "username" in db.column_names(old_db, "scores")  # scores recréés (recalculés ensuite)
    assert db.list_jobs_overview(old_db, "Alex") == []
    p, cfg = config.load_profile(), config.load_config()
    assert crit.load(old_db, p, cfg, "Octave").countries == ["Belgium"]  # anciens critères → Octave
    assert crit.load(old_db, p, cfg, "Marilou") == crit.defaults(p, cfg)


def test_new_jobs_after_migration_are_not_given_to_octave(old_db):
    db.init_db(old_db, owner="Octave")
    db.upsert_job(old_db, make_job("j3"))
    db.init_db(old_db, owner="Octave")
    assert "j3" not in db.user_job_ids(old_db, "Octave")


@pytest.fixture
def two_users(conn):
    """Octave a trouvé les offres FR, Marilou les offres DE."""
    cfg = config.load_config()
    s_oct = jsearch.collect(conn, DEV_QUERIES[:2], cfg, user="Octave", dev=True)
    s_mar = jsearch.collect(conn, DEV_QUERIES[2:], cfg, user="Marilou", dev=True)
    assert (s_oct.new, s_mar.new) == (10, 10)
    extract.extract_new_jobs(conn, cfg, dev=True)
    profile = config.load_profile()
    scoring.score_all(conn, profile, "Octave")
    scoring.score_all(conn, profile, "Marilou")
    return conn


def test_each_user_sees_only_their_offers(two_users):
    octave = {r["pays"] for r in offers.load_offers(two_users, "Octave")}
    marilou = {r["pays"] for r in offers.load_offers(two_users, "Marilou")}
    assert "France" in octave and "Germany" not in octave
    assert marilou == {"Germany", "Austria"}
    assert offers.load_offers(two_users, "Alex") == []


def test_same_offer_found_by_two_users_is_new_for_the_second(two_users):
    s = jsearch.collect(two_users, DEV_QUERIES[2:], config.load_config(), user="Octave", dev=True)
    assert s.new == 10
    assert len(db.user_job_ids(two_users, "Octave")) == 20


def test_statuses_and_scores_are_per_user(two_users):
    job_id = offers.load_offers(two_users, "Marilou")[0]["job_id"]
    db.link_jobs_to_user(two_users, [job_id], "Octave")
    db.set_application(two_users, job_id, "entretien", "jeudi", "Marilou")
    assert db.get_application(two_users, job_id, "Octave")["status"] == "à voir"
    strict = config.load_profile().model_copy(update={"target_countries": ["Spain"]})
    scoring.score_all(two_users, strict, "Octave")  # les critères d'Octave ne touchent pas aux scores de Marilou
    assert db.get_score(two_users, job_id, "Octave")["breakdown"]["eliminated"]
    assert not db.get_score(two_users, job_id, "Marilou")["breakdown"]["eliminated"]


def test_criteria_and_history_are_per_user(conn):
    p, cfg = config.load_profile(), config.load_config()
    crit.save(conn, crit.SearchCriteria(titles=["Praktikum"], countries=["Germany"]), "Marilou")
    crit.record_searched(conn, ["Germany"], "Marilou")
    assert crit.load(conn, p, cfg, "Marilou").titles == ["Praktikum"]
    assert crit.load(conn, p, cfg, "Alex") == crit.defaults(p, cfg)
    assert crit.searched_countries(conn, "Alex") == []


def test_chat_only_sees_user_offers(two_users):
    assert all(r["country"] == "Germany" for r in chat.search_jobs(two_users, {"limit": 30}, "Marilou"))
    fr_job = offers.load_offers(two_users, "Octave")[0]["job_id"]
    assert "error" in chat.get_job(two_users, fr_job, "Marilou")
    assert chat.get_job(two_users, fr_job, "Octave")["title"]


def test_config_users():
    assert config.load_config().users == ["Octave", "Marilou", "Alex"]


def test_needs_scoring_after_migration(two_users):
    assert not db.needs_scoring(two_users, "Octave")
    two_users.execute("DELETE FROM scores WHERE username = ?", ("Octave",))
    assert db.needs_scoring(two_users, "Octave") and not db.needs_scoring(two_users, "Marilou")
