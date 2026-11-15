"""Tests de src/db.py."""


import pytest

from src import db
from tests.conftest import make_job


def test_init_db_creates_tables_and_is_idempotent(conn):
    db.init_db(conn)  # 2e appel sans erreur
    tables = db.list_tables(conn)
    assert {"jobs", "extractions", "scores", "applications", "api_usage", "api_cache", "settings"} <= tables


def test_upsert_new_then_existing_keeps_first_seen(conn):
    assert db.upsert_job(conn, make_job(), seen_at="2026-11-01T00:00:00+00:00") is True
    assert db.upsert_job(conn, make_job(job_title="Nouveau titre"), seen_at="2026-11-05T00:00:00+00:00") is False
    job = db.get_job(conn, "j1")
    assert db.count_jobs(conn) == 1
    assert job["job_title"] == "Nouveau titre"
    assert job["first_seen"] == "2026-11-01T00:00:00+00:00"
    assert job["last_seen"] == "2026-11-05T00:00:00+00:00"
    assert job["job_is_remote"] == 0


def test_upsert_jobs_counts_new_only(conn):
    assert db.upsert_jobs(conn, [make_job("a"), make_job("b")]) == 2
    assert db.upsert_jobs(conn, [make_job("b"), make_job("c")]) == 1
    assert db.count_jobs(conn) == 3


def test_upsert_without_job_id_raises(conn):
    with pytest.raises(ValueError):
        db.upsert_job(conn, make_job(job_id=None))


def test_upsert_keeps_raw_json_with_extra_fields(conn):
    db.upsert_job(conn, make_job(job_salary_period="MONTH"))
    assert '"job_salary_period": "MONTH"' in db.get_job(conn, "j1")["raw_json"]


def test_jobs_without_extraction(conn):
    db.upsert_jobs(conn, [make_job("a"), make_job("b")])
    db.save_extraction(conn, "a", "claude-haiku-5-5", {"is_internship": True})
    assert [j["job_id"] for j in db.jobs_without_extraction(conn)] == ["b"]
    assert db.get_extraction(conn, "a") == {"is_internship": True}
    assert db.get_extraction(conn, "b") is None


def test_extraction_requires_existing_job(conn):
    with pytest.raises(db.integrity_errors()):
        db.save_extraction(conn, "inconnu", "m", {})


def test_save_score_replaces(conn):
    db.upsert_job(conn, make_job())
    db.save_score(conn, "j1", 40, {"location": 10})
    db.save_score(conn, "j1", 72.5, {"location": 15, "skills": 20})
    s = db.get_score(conn, "j1")
    assert s["score"] == 72.5
    assert s["breakdown"] == {"location": 15, "skills": 20}


def test_application_default_and_update(conn):
    db.upsert_job(conn, make_job())
    assert db.get_application(conn, "j1")["status"] == "à voir"
    db.set_application(conn, "j1", "postulé", "Envoyé le 15/11")
    db.set_application(conn, "j1", "entretien")  # notes conservées si non fournies
    app = db.get_application(conn, "j1")
    assert app["status"] == "entretien"
    assert app["notes"] == "Envoyé le 15/11"


def test_application_rejects_unknown_status(conn):
    db.upsert_job(conn, make_job())
    with pytest.raises(ValueError):
        db.set_application(conn, "j1", "embauché")


def test_api_usage_per_month(conn):
    assert db.get_api_usage(conn, "2026-11") == 0
    assert db.increment_api_usage(conn, "2026-11") == 1
    assert db.increment_api_usage(conn, "2026-11", n=2) == 3
    assert db.increment_api_usage(conn, "2026-12") == 1
    assert db.get_api_usage(conn, "2026-11") == 3


def test_overview_joins_and_sorts_by_score(conn):
    db.upsert_jobs(conn, [make_job("a"), make_job("b"), make_job("c")])
    db.save_score(conn, "a", 30, {})
    db.save_score(conn, "b", 80, {"skills": 25})
    db.save_extraction(conn, "b", "m", {"city": "Paris"})
    db.set_application(conn, "b", "intéressant")
    rows = db.list_jobs_overview(conn)
    assert [r["job_id"] for r in rows] == ["b", "a", "c"]  # sans score en dernier
    assert rows[0]["extraction"] == {"city": "Paris"}
    assert rows[0]["breakdown"] == {"skills": 25}
    assert rows[0]["status"] == "intéressant"
    assert rows[2]["status"] == "à voir" and rows[2]["extraction"] is None


@pytest.mark.parametrize("a, b", [
    (("Acme SAS", "Stagiaire Contrôle de gestion (H/F)", "Paris"),
     ("ACME", "stagiaire controle de gestion H/F", "paris")),
    (("Siemens AG", "Praktikant Finance (m/w/d)", "München"),
     ("Siemens", "Praktikant Finance", "Munchen")),
])
def test_dedup_key_equivalent(a, b):
    ja = make_job(employer_name=a[0], job_title=a[1], job_city=a[2])
    jb = make_job(employer_name=b[0], job_title=b[1], job_city=b[2])
    assert db.dedup_key(ja) == db.dedup_key(jb)


def test_dedupe_jobs_keeps_first_and_distinguishes_cities():
    jobs = [
        make_job("a", job_city="Paris"),
        make_job("b", job_title="Stagiaire controle de gestion", job_city="Paris"),
        make_job("c", job_city="Lyon"),
        make_job("d", job_city=None),
    ]
    assert [j["job_id"] for j in db.dedupe_jobs(jobs)] == ["a", "c", "d"]


def test_api_cache_roundtrip(conn):
    assert db.get_cached_response(conn, "k") is None
    db.save_cached_response(conn, "k", {"status": "OK", "data": [1]})
    db.save_cached_response(conn, "k", {"status": "OK", "data": [2]})
    assert db.get_cached_response(conn, "k") == {"status": "OK", "data": [2]}


def test_connect_uses_database_url_only_without_path(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host.invalid:6543/postgres")
    c = db.connect(tmp_path / "x.db")  # un chemin explicite force SQLite
    assert not c.is_postgres and c.describe() == "SQLite local"


def test_json_helpers_by_dialect(conn):
    db.upsert_job(conn, make_job())
    db.save_extraction(conn, "j1", "m", {"city": "Paris", "duration_months_min": 6, "stipend_monthly_eur": 1400.5})
    db.save_score(conn, "j1", 10, {"eliminated": True})
    row = conn.execute(
        f"SELECT {conn.json_text('e.extracted_json', 'city')} AS city, "
        f"{conn.json_num('e.extracted_json', 'duration_months_min')} AS d, "
        f"{conn.json_num('e.extracted_json', 'stipend_monthly_eur', integer=False)} AS s, "
        f"{conn.json_true('s.breakdown_json', 'eliminated')} AS elim "
        "FROM extractions e JOIN scores s ON s.job_id = e.job_id").fetchone()
    assert (row["city"], row["d"], row["s"], bool(row["elim"])) == ("Paris", 6, 1400.5, True)
    assert conn.execute(f"SELECT 1 FROM jobs WHERE job_city {conn.like} ?", ("%PARIS%",)).fetchone()
