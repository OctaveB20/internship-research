"""Tests de src/jsearch.py : aucun appel réseau réel (requests.get est remplacé)."""
import json
from datetime import date

import pytest
import requests

from src import config, db, jsearch
from tests.conftest import DEV_QUERIES

FIXTURE_FR = config.FIXTURES_DIR / "search_fr_controle_gestion.json"
FAKE_KEY = "cle-de-test-secrete"
DAY = date(2026, 11, 15)


@pytest.fixture
def cfg():
    return config.AppConfig(quota_soft_limit=180)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code, self._payload, self._bad = status_code, payload, bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._payload


@pytest.fixture
def fake_get(monkeypatch):
    """Remplace requests.get ; `fake_get.response` définit la réponse, `fake_get.calls` les appels."""
    class Fake:
        calls: list = []
        response = FakeResponse(200, json.loads(FIXTURE_FR.read_text(encoding="utf-8")))

        def __call__(self, url, headers=None, params=None, timeout=None):
            self.calls.append({"url": url, "headers": headers, "params": params})
            if isinstance(self.response, Exception):
                raise self.response
            return self.response

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(jsearch.requests, "get", fake)
    monkeypatch.setenv("RAPIDAPI_KEY", FAKE_KEY)
    return fake


@pytest.fixture
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("appel réseau interdit")
    monkeypatch.setattr(jsearch.requests, "get", boom)


def search(conn, cfg, tmp_path, **kw):
    kw.setdefault("dev", False)
    kw.setdefault("today", DAY)
    return jsearch.search_jobs(conn, "stage contrôle de gestion Paris", "fr", cfg, cache_dir=tmp_path / "cache", **kw)


# ---------- Fixtures ----------
@pytest.mark.parametrize("path", sorted(config.FIXTURES_DIR.glob("*.json")), ids=lambda p: p.name)
def test_fixture_files_are_realistic(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "OK" and len(payload["data"]) == 10
    for job in payload["data"]:
        assert all(f in job for f in jsearch.JOB_FIELDS if f != "job_posted_at_datetime_utc")
    types = {j["job_employment_type"] for j in payload["data"]}
    assert "FULLTIME" in types  # au moins un CDI / Festanstellung à éliminer


def test_fixtures_contain_alternance_and_three_languages():
    jobs = jsearch.load_fixtures()
    text = " ".join(j["job_description"] for j in jobs)
    assert "alternance" in text.lower() or "apprentissage" in text.lower()
    assert "Werkstudent" in text
    assert "Missions" in text and "Aufgaben" in text and "Requirements" in text  # FR, DE, EN


# ---------- Mode dev ----------
def test_dev_mode_reads_fixtures_by_country_without_network(conn, cfg, tmp_path, no_network):
    fr = jsearch.search_jobs(conn, "x", "fr", cfg, dev=True)
    de = jsearch.search_jobs(conn, "x", "de", cfg, dev=True)
    assert fr.source == de.source == "fixture"
    assert len(fr.jobs) == len(de.jobs) == 10
    assert fr.jobs[0]["job_id"] != de.jobs[0]["job_id"]
    assert jsearch.search_jobs(conn, "x", "es", cfg, dev=True).jobs == []  # pays sans fixture : rien
    assert db.get_api_usage(conn) == 0


def test_dev_mode_follows_env(conn, cfg, monkeypatch, no_network):
    monkeypatch.setenv("DEV_MODE", "1")
    assert jsearch.search_jobs(conn, "x", "fr", cfg).source == "fixture"


def test_collect_dev_mode_upserts_and_dedupes_runs(conn, cfg, no_network):
    queries = DEV_QUERIES  # 2 requêtes fr + 1 de
    s1 = jsearch.collect(conn, queries, cfg, dev=True)
    assert (s1.fetched, s1.new, s1.api_calls, s1.errors) == (30, 20, 0, [])
    s2 = jsearch.collect(conn, queries, cfg, dev=True)
    assert s2.new == 0 and db.count_jobs(conn) == 20


# ---------- Normalisation ----------
def test_normalize_fills_missing_fields_and_rebuilds_datetime():
    job = jsearch.normalize_job({"job_id": "x", "job_posted_at_timestamp": 1763164800})
    assert job["job_city"] is None
    assert job["job_posted_at_datetime_utc"] == "2025-11-15T00:00:00.000Z"


def test_fixture_job_without_utc_datetime_gets_one():
    job = next(j for j in jsearch.load_fixtures("de") if j["employer_name"] == "DataFin Analytics")
    assert job["job_posted_at_datetime_utc"].startswith("2026-11-13T")


def test_parse_response_error_status():
    with pytest.raises(jsearch.JSearchError, match="Invalid"):
        jsearch.parse_response({"status": "ERROR", "error": {"message": "Invalid query"}})


def test_build_params():
    p = jsearch.build_params("finance intern Munich", "DE")
    assert p == {"query": "finance intern Munich", "country": "de", "date_posted": "week", "employment_types": "INTERN"}
    assert jsearch.build_params("x", "fr", cursor="CUR2")["cursor"] == "CUR2"


# ---------- Appel réel simulé + cache ----------
def test_api_call_counts_and_caches(conn, cfg, tmp_path, fake_get):
    res = search(conn, cfg, tmp_path)
    assert res.source == "api" and len(res.jobs) == 10
    assert db.get_api_usage(conn) == 1
    call = fake_get.calls[0]
    assert call["url"] == "https://jsearch.p.rapidapi.com/search-v2"
    assert call["headers"] == {"X-RapidAPI-Key": FAKE_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    assert call["params"]["employment_types"] == "INTERN" and "page" not in call["params"]
    assert len(list((tmp_path / "cache").glob("*_2026-11-15.json"))) == 1


def test_same_query_same_day_uses_cache(conn, cfg, tmp_path, fake_get):
    search(conn, cfg, tmp_path)
    res = search(conn, cfg, tmp_path)
    assert res.source == "cache" and len(res.jobs) == 10
    assert len(fake_get.calls) == 1 and db.get_api_usage(conn) == 1


def test_next_day_or_other_page_calls_again(conn, cfg, tmp_path, fake_get):
    search(conn, cfg, tmp_path)
    search(conn, cfg, tmp_path, today=date(2026, 11, 16))
    search(conn, cfg, tmp_path, cursor="CUR2")
    assert len(fake_get.calls) == 3 and db.get_api_usage(conn) == 3


# ---------- Garde-fou quota ----------
def test_quota_reached_blocks_before_any_call(conn, cfg, tmp_path, fake_get):
    db.increment_api_usage(conn, n=180)
    with pytest.raises(jsearch.QuotaExceeded, match="180 / 180"):
        search(conn, cfg, tmp_path)
    assert fake_get.calls == [] and db.get_api_usage(conn) == 180


def test_quota_reached_still_serves_cache(conn, cfg, tmp_path, fake_get):
    search(conn, cfg, tmp_path)
    db.increment_api_usage(conn, n=500)
    assert search(conn, cfg, tmp_path).source == "cache"


def test_collect_stops_on_quota(conn, tmp_path, fake_get):
    cfg = config.AppConfig(quota_soft_limit=1)
    queries = [config.SearchQuery(query=q, country="fr") for q in ("a", "b", "c")]
    s = jsearch.collect(conn, queries, cfg, dev=False, today=DAY, cache_dir=tmp_path)
    assert s.api_calls == 1 and len(fake_get.calls) == 1
    assert len(s.errors) == 1 and "Quota mensuel atteint" in s.errors[0]


def test_quota_status(conn):
    db.increment_api_usage(conn, n=42)
    assert jsearch.quota_status(conn, 180) == {"used": 42, "soft_limit": 180, "plan_limit": 200, "remaining": 138}


# ---------- Erreurs ----------
def test_missing_key_no_call(conn, cfg, tmp_path, fake_get, monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY")
    with pytest.raises(jsearch.JSearchError, match="RAPIDAPI_KEY absente"):
        search(conn, cfg, tmp_path)
    assert fake_get.calls == [] and db.get_api_usage(conn) == 0


@pytest.mark.parametrize("response, message", [
    (FakeResponse(429), "Quota RapidAPI dépassé"),
    (FakeResponse(403), "Accès refusé"),
    (FakeResponse(500), "HTTP 500"),
    (FakeResponse(200, bad_json=True), "JSON invalide"),
    (requests.Timeout(), "n'a pas répondu"),
    (requests.ConnectionError(), "Connexion à JSearch impossible"),
])
def test_api_errors_are_readable_counted_and_not_cached(conn, cfg, tmp_path, fake_get, response, message):
    fake_get.response = response
    with pytest.raises(jsearch.JSearchError, match=message) as exc:
        search(conn, cfg, tmp_path)
    assert FAKE_KEY not in str(exc.value)
    assert db.get_api_usage(conn) == 1  # requête envoyée = comptée (prudence)
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())


def test_collect_keeps_going_after_api_error(conn, cfg, tmp_path, fake_get):
    fake_get.response = FakeResponse(500)
    queries = [config.SearchQuery(query=q, country="fr") for q in ("a", "b")]
    s = jsearch.collect(conn, queries, cfg, dev=False, today=DAY, cache_dir=tmp_path)
    assert len(s.errors) == 2 and "« a »" in s.errors[0]


def test_fixture_duplicate_across_publishers_is_deduped():
    jobs = jsearch.load_fixtures()
    assert len(db.dedupe_jobs(jobs)) == len(jobs) - 1  # Altaïr publié sur LinkedIn et Indeed


def test_postgres_keeps_cache_when_disk_is_wiped(conn, cfg, tmp_path, fake_get):
    if not conn.is_postgres:
        pytest.skip("copie en base uniquement avec Postgres")
    search(conn, cfg, tmp_path)
    for f in (tmp_path / "cache").iterdir():
        f.unlink()  # redémarrage de Streamlit Cloud
    assert search(conn, cfg, tmp_path).source == "cache"
    assert len(fake_get.calls) == 1 and db.get_api_usage(conn) == 1


# ---------- Format search-v2 ----------
def v2_payload(jobs, cursor):
    return {"status": "OK", "request_id": "r", "data": {"jobs": jobs, "cursor": cursor}}


def test_parse_v2_payload_and_renamed_fields():
    jobs, cursor = jsearch.parse_page(v2_payload(
        [{"job_id": "a", "job_title": "Intern", "apply_url": "https://x/apply", "work_arrangement": "remote"},
         {"job_title": "sans id"}], "CUR2"))
    assert cursor == "CUR2" and len(jobs) == 1
    assert jobs[0]["job_apply_link"] == "https://x/apply" and jobs[0]["job_is_remote"] is True
    assert jsearch.parse_page(v2_payload([], None)) == ([], None)


def test_apply_link_fallbacks():
    job = jsearch.normalize_job({"job_id": "a", "apply_options": [{"apply_link": "https://opt"}]})
    assert job["job_apply_link"] == "https://opt"


def test_collect_follows_cursor_between_pages(conn, cfg, tmp_path, fake_get):
    pages = [FakeResponse(200, v2_payload([{"job_id": f"p1-{i}"} for i in range(10)], "CUR2")),
             FakeResponse(200, v2_payload([{"job_id": f"p2-{i}"} for i in range(3)], None))]
    fake_get.response = None

    def respond(url, headers=None, params=None, timeout=None):
        fake_get.calls.append({"params": params})
        return pages.pop(0)
    jsearch.requests.get = respond
    queries = [config.SearchQuery(query="x", country="be", page=p) for p in (1, 2, 3)]
    s = jsearch.collect(conn, queries, cfg, dev=False, today=DAY, cache_dir=tmp_path)
    assert s.fetched == 13 and s.api_calls == 2  # pas de 3e appel : la page 2 n'a pas de curseur
    assert "cursor" not in fake_get.calls[0]["params"] and fake_get.calls[1]["params"]["cursor"] == "CUR2"


def test_http_error_shows_api_message_and_counts_call(conn, cfg, tmp_path, fake_get):
    fake_get.response = FakeResponse(404, {"message": "Endpoint '/search' does not exist"})
    s = jsearch.collect(conn, [config.SearchQuery(query="x", country="be")], cfg, dev=False, today=DAY,
                        cache_dir=tmp_path)
    assert "HTTP 404 : Endpoint '/search' does not exist" in s.errors[0]
    assert s.api_calls == 1
