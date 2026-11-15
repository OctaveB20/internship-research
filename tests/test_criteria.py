"""Tests de src/criteria.py et de leur effet sur le score."""
from datetime import date

import pytest
from pydantic import ValidationError

from src import config, criteria as crit, extract, jsearch, scoring
from tests.conftest import make_job


@pytest.fixture
def base():
    return config.load_profile(), config.load_config()


def test_defaults_from_profile(base):
    c = crit.defaults(*base)
    assert c.countries == ["France", "Germany"]
    assert c.start_months == ["2027-01", "2027-02", "2027-03"]
    assert c.durations == [5, 6] and c.titles == ["stage contrôle de gestion", "stage FP&A", "finance intern"]


def test_split_and_clean():
    assert crit.split_list("a, b ;c\n d,, ") == ["a", "b", "c", "d"]
    c = crit.SearchCriteria(titles=["Stage FP&A", "stage fp&a", " "], countries=["France"])
    assert c.titles == ["Stage FP&A"]


def test_validation():
    with pytest.raises(ValidationError):
        crit.SearchCriteria(titles=[], countries=["France"])
    with pytest.raises(ValidationError):
        crit.SearchCriteria(titles=["x"], countries=["Atlantis"])
    with pytest.raises(ValidationError):
        crit.SearchCriteria(titles=["x"], countries=["France"], pages=5)


def test_build_queries_and_cost():
    c = crit.SearchCriteria(titles=["stage FP&A", "Praktikum Controlling"], countries=["France", "Germany", "Spain"],
                            pages=2)
    queries = crit.build_queries(c)
    assert c.request_count() == len(queries) == 12
    assert {q.country for q in queries} == {"fr", "de", "es"} and {q.page for q in queries} == {1, 2}


def test_all_europe_codes_are_unique():
    codes = [code for code, _ in crit.EUROPE.values()]
    assert len(codes) == len(set(codes)) == 30


def test_upcoming_months_crosses_year():
    assert crit.upcoming_months(date(2026, 11, 15), 4) == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert crit.month_label("2027-02") == "févr. 2027"


def test_save_and_load(conn, base):
    profile, cfg = base
    assert crit.load(conn, profile, cfg) == crit.defaults(profile, cfg)
    c = crit.SearchCriteria(titles=["x"], countries=["Spain"], durations=[6], pages=2)
    crit.save(conn, c)
    assert crit.load(conn, profile, cfg) == c


def test_corrupted_settings_fall_back_to_defaults(conn, base):
    from src import db
    db.set_setting(conn, crit.SETTINGS_KEY, '{"titles": []}')
    assert crit.load(conn, *base) == crit.defaults(*base)


def test_apply_to_profile(base):
    profile, _ = base
    c = crit.SearchCriteria(titles=["x"], countries=["Spain", "Italy"], start_months=["2027-04", "2027-02"],
                            durations=[3, 4], cities=["Madrid"], remote_ok=False, min_stipend_eur=800,
                            exclude_words=["CDI"])
    p = crit.apply_to_profile(profile, c)
    assert p.target_countries == ["Spain", "Italy"] and p.target_cities == ["Madrid"] and not p.remote_ok
    assert (p.available_from, p.latest_start) == (date(2027, 2, 1), date(2027, 4, 30))
    assert (p.duration_months.min, p.duration_months.max) == (3, 4)
    assert p.weights.stipend == 10 and p.min_stipend_eur == 800
    assert p.start_months == ["2027-02", "2027-04"] and p.exclude_title_words == ["CDI"]


def test_empty_months_and_durations_keep_profile(base):
    profile, _ = base
    p = crit.apply_to_profile(profile, crit.SearchCriteria(titles=["x"], countries=["France"]))
    assert p.latest_start == profile.latest_start and p.duration_months == profile.duration_months


def _ex(**kw):
    base = dict(is_internship=True, contract_type="stage", city="Madrid", country="Spain", work_mode="onsite",
                duration_months_min=4, duration_months_max=4, start_date_earliest="2027-03-10")
    return extract.JobExtraction.model_validate({**base, **kw})


def test_criteria_change_eliminations(base):
    profile, _ = base
    c = crit.SearchCriteria(titles=["x"], countries=["Spain"], start_months=["2027-02", "2027-04"], durations=[4])
    p = crit.apply_to_profile(profile, c)
    job = make_job(job_title="Prácticas finanzas", job_city="Madrid", job_country="ES")
    r = scoring.score_job(_ex(), job, p)
    assert not r.eliminated  # l'Espagne est cochée
    assert r.components["start_date"]["ratio"] == 0.6  # mars non coché, entre février et avril
    assert scoring.score_job(_ex(start_date_earliest="2027-05-01"), job, p).eliminated
    excluded = crit.apply_to_profile(profile, c.model_copy(update={"exclude_words": ["prácticas"]}))
    assert any("titre exclu" in x for x in scoring.score_job(_ex(), job, excluded).reasons)


def test_excluded_word_matches_whole_words_only(base):
    profile, _ = base
    p = profile.model_copy(update={"exclude_title_words": ["CDI"], "target_countries": ["Spain"]})
    ok = make_job(job_title="Stage contrôle de gestion (CDIscount)")
    assert not any("titre exclu" in x for x in scoring.score_job(_ex(), ok, p).reasons)
    assert any("titre exclu" in x for x in scoring.score_job(_ex(), make_job(job_title="Contrôleur CDI"), p).reasons)


def test_dev_mode_unknown_country_returns_nothing(conn, base):
    _, cfg = base
    assert jsearch.search_jobs(conn, "x", "it", cfg, dev=True).jobs == []


def test_internship_filter_off_by_default():
    assert crit.SearchCriteria(titles=["x"], countries=["France"]).internship_only is False
