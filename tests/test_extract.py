"""Tests de src/extract.py (faux client IA, aucun appel réseau)."""
from datetime import date

import pytest

from src import config, db, extract, jsearch, llm
from tests.conftest import DEV_QUERIES, make_job
from tests.fakes import FakeLLM

REF = extract.load_reference()
GOOD = next(iter(REF.values()))


def test_reference_covers_all_fixtures_and_validates():
    ids = {j["job_id"] for j in jsearch.load_fixtures()}
    assert set(REF) == ids
    for data in REF.values():
        extract.JobExtraction.model_validate(data)


def test_schema_requires_every_model_field():
    assert set(extract.EXTRACTION_SCHEMA["required"]) == set(extract.JobExtraction.model_fields)


def test_model_cleans_values():
    ex = extract.JobExtraction.model_validate({
        **GOOD, "duration_months_min": 6, "duration_months_max": 4, "city": "  ",
        "required_skills": ["Excel", " excel ", "SAP", ""]})
    assert (ex.duration_months_min, ex.duration_months_max) == (4, 6)
    assert ex.city is None and ex.required_skills == ["Excel", "SAP"]


@pytest.mark.parametrize("bad", [{"contract_type": "CDI"}, {"work_mode": "office"}, {"stipend_monthly_eur": -5},
                                 {"start_date_earliest": "début 2027"}])
def test_model_rejects_invalid(bad):
    with pytest.raises(ValueError):
        extract.JobExtraction.model_validate({**GOOD, **bad})


def test_asap_becomes_posting_date():
    job = make_job(job_posted_at_datetime_utc="2026-11-10T08:00:00.000Z")
    client = FakeLLM([{**GOOD, "start_date_earliest": None, "start_date_text": "Dès que possible"}])
    ex, _, _ = extract.extract_with_llm(job, client)
    assert ex.start_date_earliest == date(2026, 11, 10)


def test_prompt_contains_posting_date_and_full_description():
    job = make_job(job_description="A" * 20000)
    prompt = extract.build_user_prompt(job)
    assert "Date de publication : 2026-11-10" in prompt and "A" * 20000 in prompt


def test_retry_once_then_success():
    client = FakeLLM([{"is_internship": "peut-être"}, GOOD])
    ex, usage, err = extract.extract_with_llm(make_job(), client)
    assert ex is not None and err == "" and client.calls == 2 and usage.input_tokens == 200


def test_two_failures_give_none():
    client = FakeLLM([None, {"foo": 1}])
    ex, _, err = extract.extract_with_llm(make_job(), client)
    assert ex is None and "JSON invalide" in err and client.calls == 2


def test_batch_only_new_jobs_and_failure_does_not_block(conn):
    db.upsert_jobs(conn, [make_job("a"), make_job("b"), make_job("c")])
    db.save_extraction(conn, "a", "m", GOOD)
    client = FakeLLM([None, None, GOOD])  # b échoue 2 fois, c réussit
    s = extract.extract_new_jobs(conn, config.AppConfig(), client=client, dev=False)
    assert (s.extracted, s.failed, client.calls) == (1, 1, 3)
    assert s.usage.input_tokens == 300 and len(s.errors) == 1
    assert db.get_extraction(conn, "c")["is_internship"] is True
    assert db.get_extraction(conn, "b") is None
    s2 = extract.extract_new_jobs(conn, config.AppConfig(), client=FakeLLM([GOOD]), dev=False)
    assert s2.extracted == 1  # seule b est retentée


def test_fatal_error_stops_batch(conn):
    db.upsert_jobs(conn, [make_job("a"), make_job("b")])
    client = FakeLLM([llm.LLMError("quota", fatal=True)])
    s = extract.extract_new_jobs(conn, config.AppConfig(), client=client, dev=False)
    assert s.failed == 2 and client.calls == 1 and "quota" in s.errors[0]


def test_non_fatal_error_continues(conn):
    db.upsert_jobs(conn, [make_job("a"), make_job("b")])
    client = FakeLLM([llm.LLMError("HTTP 500"), GOOD])
    s = extract.extract_new_jobs(conn, config.AppConfig(), client=client, dev=False)
    assert (s.extracted, s.failed) == (1, 1)


def test_missing_key_is_readable(conn, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    db.upsert_job(conn, make_job("a"))
    s = extract.extract_new_jobs(conn, config.AppConfig(), dev=False)
    assert s.failed == 1 and "GEMINI_API_KEY absente" in s.errors[0]


def test_dev_mode_uses_reference_without_llm(conn):
    cfg = config.load_config()
    jsearch.collect(conn, DEV_QUERIES, cfg, dev=True)
    client = FakeLLM()
    s = extract.extract_new_jobs(conn, cfg, client=client, dev=True)
    assert s.from_reference == 20 and client.calls == 0
    assert db.jobs_without_extraction(conn) == []


def test_compare_with_reference():
    got = extract.JobExtraction.model_validate({**GOOD, "city": "PARIS", "stipend_monthly_eur": 999})
    rows = {r["champ"]: r["ok"] for r in extract.compare_with_reference(got, GOOD)}
    assert rows["city"] is True and rows["stipend_monthly_eur"] is False and "summary_fr" not in rows
