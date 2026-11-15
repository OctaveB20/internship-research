"""Tests de src/scoring.py."""
from datetime import date

import pytest

from tests.conftest import DEV_QUERIES
from src import config, db, extract, jsearch, scoring
from src.extract import JobExtraction

PROFILE = config.Profile(
    target_countries=["France", "Germany"], target_cities=["Paris", "Munich"], remote_ok=True,
    duration_months={"min": 4, "max": 6}, available_from=date(2027, 1, 5), latest_start=date(2027, 3, 31),
    domains=["contrôle de gestion", "FP&A", "finance d'entreprise"],
    skills=["Excel", "Python", "Power BI", "SQL", "SAP"],
    languages={"French": "native", "English": "fluent", "German": "basic"},
    weights={"location": 15, "duration": 20, "start_date": 20, "skills": 25, "languages": 10, "domain": 10},
)
JOB = {"job_city": "Paris", "job_country": "FR"}


def ex(**kw):
    base = dict(is_internship=True, contract_type="stage", city="Paris", country="France", work_mode="onsite",
                duration_months_min=6, duration_months_max=6, start_date_earliest="2027-01-15",
                required_skills=["Excel"], languages=[{"lang": "English", "level": "fluent"}],
                domain="contrôle de gestion")
    return JobExtraction.model_validate({**base, **kw})


def test_perfect_offer_scores_100():
    r = scoring.score_job(ex(), JOB, PROFILE)
    assert r.score == 100 and not r.eliminated and r.skills_matched == ["Excel"]


@pytest.mark.parametrize("kw, reason", [
    ({"is_internship": False, "contract_type": "autre"}, "pas un stage"),
    ({"contract_type": "alternance"}, "pas un stage"),
    ({"contract_type": "working_student"}, "pas un stage"),
    ({"country": "Austria", "city": "Wien"}, "pays hors liste"),
    ({"duration_months_min": 2, "duration_months_max": 2}, "durée incompatible"),
    ({"duration_months_min": 12, "duration_months_max": 12}, "durée incompatible"),
    ({"start_date_earliest": "2027-09-01"}, "début trop tard"),
])
def test_eliminations(kw, reason):
    r = scoring.score_job(ex(**kw), JOB, PROFILE)
    assert r.eliminated and r.score == 0 and any(reason in x for x in r.reasons)
    assert r.raw_score > 0


def test_remote_abroad_not_eliminated_when_remote_ok():
    r = scoring.score_job(ex(country="Spain", city=None, work_mode="remote"), JOB, PROFILE)
    assert not r.eliminated and r.components["location"]["ratio"] == 1


def test_country_falls_back_to_jsearch_code():
    r = scoring.score_job(ex(country=None), {"job_city": "Wien", "job_country": "AT"}, PROFILE)
    assert any("Austria" in x for x in r.reasons)


def test_partial_duration_overlap_is_kept():
    r = scoring.score_job(ex(duration_months_min=3, duration_months_max=6), JOB, PROFILE)
    assert not r.eliminated and r.components["duration"]["ratio"] == 0.7


def test_nulls_give_neutral_half():
    r = scoring.score_job(ex(city=None, duration_months_min=None, duration_months_max=None, start_date_earliest=None,
                             required_skills=[], languages=[], domain=""), {}, PROFILE)
    assert not r.eliminated
    assert all(c["ratio"] == 0.5 for c in r.components.values())
    assert r.score == 50


def test_weights_and_skills_ratio():
    r = scoring.score_job(ex(required_skills=["Excel", "VBA", "SAP BO", "Tableau"]), JOB, PROFILE)
    assert r.components["skills"]["points"] == 12.5  # 2/4 × 25 (SAP couvre SAP BO)
    assert r.skills_missing == ["VBA", "Tableau"]
    assert r.score == pytest.approx(87.5)


def test_zero_weight_component_is_ignored():
    p = PROFILE.model_copy(update={"weights": config.Weights(location=0, duration=0, start_date=0, skills=100,
                                                              languages=0, domain=0)})
    r = scoring.score_job(ex(required_skills=["Excel", "VBA"]), JOB, p)
    assert list(r.components) == ["skills"] and r.score == 50


def test_start_before_availability_is_penalised():
    r = scoring.score_job(ex(start_date_earliest="2026-12-06"), JOB, PROFILE)  # 30 jours trop tôt
    assert r.components["start_date"]["ratio"] == 0.5


def test_language_levels():
    r = scoring.score_job(ex(languages=[{"lang": "Deutsch", "level": "fließend"}, {"lang": "anglais", "level": "B2"}]),
                          JOB, PROFILE)
    assert r.components["languages"]["ratio"] == pytest.approx((1 / 4 + 1) / 2, abs=0.01)
    assert "Deutsch" in r.components["languages"]["detail"]


def test_city_alias_and_domain_proximity():
    r = scoring.score_job(ex(city="München", country="Germany", domain="corporate finance"), JOB, PROFILE)
    assert r.components["location"]["ratio"] == 1
    assert r.components["domain"]["ratio"] == 0.6


def test_stipend_component_when_weighted():
    p = PROFILE.model_copy(update={"min_stipend_eur": 1500, "weights": PROFILE.weights.model_copy(update={"stipend": 10})})
    assert scoring.score_job(ex(stipend_monthly_eur=1200), JOB, p).components["stipend"]["ratio"] == 0.8


def test_fixture_pipeline_eliminates_expected_offers(conn):
    cfg = config.load_config()
    jsearch.collect(conn, DEV_QUERIES, cfg, dev=True)
    extract.extract_new_jobs(conn, cfg, dev=True)
    assert scoring.score_all(conn, config.load_profile()) == 20
    rows = db.list_jobs_overview(conn)
    eliminated = {r["employer_name"] for r in rows if r["breakdown"]["eliminated"]}
    assert eliminated == {"Verdane Industries", "Maison Céleste", "Merlin & Associés", "Kestrel Fund Services",
                          "NeoLedger", "Isarwerk Energie", "Kaltenberg Logistics", "Tegernsee Capital",
                          "Alpenblick Hotels", "Donaustahl GmbH", "Fertois Composites"}
    assert rows[0]["employer_name"] == "Lumen Capital Partners"
