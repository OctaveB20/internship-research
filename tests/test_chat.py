"""Tests de src/chat.py : outils SQL paramétrés + boucle avec faux client."""
import pytest

from tests.conftest import DEV_QUERIES
from src import chat, config, db, extract, jsearch, scoring
from tests.fakes import FakeLLM


@pytest.fixture
def full(conn):
    cfg = config.load_config()
    jsearch.collect(conn, DEV_QUERIES, cfg, dev=True)
    extract.extract_new_jobs(conn, cfg, dev=True)
    scoring.score_all(conn, config.load_profile())
    return conn


def names(rows):
    return {r["employer_name"] for r in rows}


def test_default_hides_eliminated_and_sorts(full):
    rows = chat.search_jobs(full, {"limit": 30})
    assert len(rows) == 9 and all(r["score"] > 0 for r in rows)
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)


def test_filters(full):
    assert names(chat.search_jobs(full, {"city": "München"})) == {"Brennwald AG", "Helix Mobility GmbH", "Aubrac Systems"}
    assert names(chat.search_jobs(full, {"domain": "FP&A"})) == {"Lumen Capital Partners", "Helix Mobility GmbH"}
    assert names(chat.search_jobs(full, {"work_mode": "remote"})) == {"DataFin Analytics"}
    assert names(chat.search_jobs(full, {"keywords": "Power BI", "country": "Germany"})) >= {"Helix Mobility GmbH"}
    assert all(r["start"] <= "2027-01-31" for r in chat.search_jobs(full, {"start_before": "2027-01-31"}))
    assert all(r["score"] >= 90 for r in chat.search_jobs(full, {"min_score": 90}))


def test_include_eliminated_and_status(full):
    elim = chat.search_jobs(full, {"keywords": "Kestrel", "include_eliminated": True})
    assert len(elim) == 1 and "pays hors liste (Luxembourg)" in elim[0]["elimination"]
    job_id = chat.search_jobs(full, {"keywords": "Lumen"})[0]["job_id"]
    db.set_application(full, job_id, "postulé")
    assert names(chat.search_jobs(full, {"status": "postulé"})) == {"Lumen Capital Partners"}


def test_injection_is_just_text(full):
    assert chat.search_jobs(full, {"keywords": "'; DROP TABLE jobs; --", "city": "x' OR '1'='1"}) == []
    assert db.count_jobs(full) == 20


def test_unknown_filters_ignored_and_limit_capped(full):
    assert len(chat.search_jobs(full, {"sql": "SELECT 1", "limit": 999, "include_eliminated": True})) == 20


def test_get_job(full):
    job_id = chat.search_jobs(full, {"keywords": "Lumen"})[0]["job_id"]
    job = chat.get_job(full, job_id)
    assert job["employer"] == "Lumen Capital Partners" and job["extraction"]["domain"] == "FP&A"
    assert job["score_breakdown"]["components"]
    assert "error" in chat.get_job(full, "inconnu")


def test_run_tool_dispatch(full):
    assert chat.run_tool(full, "search_jobs", {"filters": {"city": "Paris"}})
    assert "error" in chat.run_tool(full, "drop_db", {})


def test_ask_llm_routes_tools(full):
    client = FakeLLM(chat_script=[("search_jobs", {"domain": "FP&A"}), ("get_job", {"job_id": "x"})])
    res = chat.ask_llm(full, config.AppConfig(), [{"role": "user", "content": "FP&A ?"}], client=client)
    assert len(res.outputs[0]) == 2 and "error" in res.outputs[1]


def test_tool_schemas_have_known_filters_only():
    assert set(chat.SEARCH_SCHEMA["properties"]) == {
        "keywords", "country", "city", "work_mode", "domain", "min_score", "max_duration_months",
        "start_before", "status", "include_eliminated", "limit"}
