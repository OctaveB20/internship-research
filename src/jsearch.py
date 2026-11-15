"""Client JSearch (RapidAPI) + cache disque + garde-fou quota.

Ordre de décision pour chaque requête :
1. DEV_MODE=1 → lecture des fixtures (tests/fixtures/*.json), aucun réseau ;
2. cache du jour présent → lecture du cache, aucun appel ;
3. quota logiciel atteint → QuotaExceeded, aucun appel ;
4. sinon → appel réel (compté dans api_usage), réponse mise en cache.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from src import config, db

# /search a été retiré par JSearch (404 « Endpoint '/search' does not exist ») : /search-v2 le remplace,
# avec les offres dans data.jobs et la page suivante via un curseur.
API_URL = "https://jsearch.p.rapidapi.com/search-v2"
API_HOST = "jsearch.p.rapidapi.com"
FREE_PLAN_LIMIT = 200

JOB_FIELDS = db.JOB_FIELDS


class JSearchError(Exception):
    """Erreur lisible à afficher telle quelle dans l'UI."""


class QuotaExceeded(JSearchError):
    """Plafond logiciel mensuel atteint : l'appel est refusé avant envoi."""


@dataclass
class SearchResult:
    jobs: list[dict[str, Any]]
    source: str  # "fixture" | "cache" | "api"
    next_cursor: str | None = None  # page suivante (None = dernière page)


@dataclass
class CollectSummary:
    fetched: int = 0
    new: int = 0
    api_calls: int = 0  # requêtes réellement envoyées (y compris en erreur)
    cache_hits: int = 0
    errors: list[str] = field(default_factory=list)


# ---------- Paramètres et cache ----------
def build_params(query: str, country: str, date_posted: str = "week", employment_types: str = "INTERN",
                 cursor: str | None = None) -> dict[str, str]:
    """Paramètres GET de /search-v2 (1 page par appel = 1 requête de quota)."""
    params = {
        "query": query,
        "country": country.lower(),
        "date_posted": date_posted,
        "employment_types": employment_types,
    }
    if cursor:
        params["cursor"] = cursor
    return params


def cache_path(params: dict[str, str], day: date, cache_dir: Path = config.CACHE_DIR) -> Path:
    """data/cache/{hash(params)}_{YYYY-MM-DD}.json"""
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    return cache_dir / f"{digest}_{day.isoformat()}.json"


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------- Normalisation ----------
def _posted_utc(raw: dict[str, Any]) -> str | None:
    """job_posted_at_datetime_utc, ou reconstruit depuis job_posted_at_timestamp."""
    if raw.get("job_posted_at_datetime_utc"):
        return raw["job_posted_at_datetime_utc"]
    ts = raw.get("job_posted_at_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return None


def _apply_link(raw: dict[str, Any]) -> str | None:
    """job_apply_link (ancien nom), apply_url (search-v2), sinon le 1er lien de candidature connu."""
    for key in ("job_apply_link", "apply_url", "job_apply_url"):
        if raw.get(key):
            return raw[key]
    for option in raw.get("apply_options") or []:
        if isinstance(option, dict) and option.get("apply_link"):
            return option["apply_link"]
    return raw.get("job_google_link")


def normalize_job(raw: dict[str, Any]) -> dict[str, Any]:
    """Garde l'objet brut et garantit la présence des champs utilisés par l'app."""
    job = dict(raw)
    for f in JOB_FIELDS:
        job.setdefault(f, None)
    job["job_posted_at_datetime_utc"] = _posted_utc(raw)
    job["job_apply_link"] = _apply_link(raw)
    if job["job_is_remote"] is None and raw.get("work_arrangement"):
        job["job_is_remote"] = str(raw["work_arrangement"]).lower() in ("remote", "fully_remote", "full_remote")
    return job


def parse_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """(offres, curseur de la page suivante). Accepte data = {jobs, cursor} (search-v2) ou une liste (ancien format)."""
    if str(payload.get("status", "OK")).upper() != "OK":
        msg = (payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else payload.get("error")
        raise JSearchError(f"JSearch a renvoyé une erreur : {msg or payload.get('status')}")
    data = payload.get("data") or []
    cursor = None
    if isinstance(data, dict):
        data, cursor = data.get("jobs") or [], data.get("cursor") or None
    if not isinstance(data, list):
        raise JSearchError("Réponse JSearch inattendue (champ « data » absent ou invalide).")
    return [normalize_job(j) for j in data if isinstance(j, dict) and j.get("job_id")], cursor


def parse_response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Offres d'une réponse JSearch (sans le curseur)."""
    return parse_page(payload)[0]


# ---------- Mode dev ----------
def load_fixtures(country: str | None = None, fixtures_dir: Path = config.FIXTURES_DIR) -> list[dict[str, Any]]:
    """Offres des fixtures dont parameters.country correspond (toutes si pays non précisé).
    Un pays sans fixture ne renvoie rien, comme une vraie recherche sans résultat."""
    payloads = [_read_json(p) for p in sorted(fixtures_dir.glob("*.json"))]
    if country:
        payloads = [p for p in payloads if str(p.get("parameters", {}).get("country", "")).lower() == country.lower()]
    return [job for p in payloads for job in parse_response(p)]


# ---------- Appel réel ----------
def fetch_jobs_api(params: dict[str, str], api_key: str, timeout: float = 30) -> dict[str, Any]:
    """Appel PAYANT à JSearch (1 requête de quota). Lève JSearchError avec un message lisible."""
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": API_HOST}
    try:
        resp = requests.get(API_URL, headers=headers, params=params, timeout=timeout)
    except requests.Timeout:
        raise JSearchError(f"JSearch n'a pas répondu en {timeout:.0f} s : réessaie plus tard.") from None
    except requests.RequestException as e:
        raise JSearchError(f"Connexion à JSearch impossible ({type(e).__name__}).") from None
    if resp.status_code == 429:
        raise JSearchError("Quota RapidAPI dépassé (429) : plus de requêtes JSearch disponibles pour le moment.")
    if resp.status_code in (401, 403):
        raise JSearchError(f"Accès refusé par RapidAPI ({resp.status_code}) : vérifie RAPIDAPI_KEY "
                           "et ton abonnement au plan JSearch.")
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error") if isinstance(body, dict) else ""
        except ValueError:
            detail = ""
        raise JSearchError(f"JSearch a répondu HTTP {resp.status_code}" + (f" : {str(detail)[:200]}" if detail else "."))
    try:
        return resp.json()
    except ValueError:
        raise JSearchError("Réponse JSearch illisible (JSON invalide).") from None


def quota_status(conn: db.Database, soft_limit: int) -> dict[str, int]:
    """Compteur du mois : utilisé, plafond logiciel, plafond du plan gratuit, restant."""
    used = db.get_api_usage(conn)
    return {"used": used, "soft_limit": soft_limit, "plan_limit": FREE_PLAN_LIMIT,
            "remaining": max(0, soft_limit - used)}


def search_jobs(conn: db.Database, query: str, country: str, cfg: config.AppConfig,
                cursor: str | None = None, *, dev: bool | None = None, today: date | None = None,
                cache_dir: Path = config.CACHE_DIR, fixtures_dir: Path = config.FIXTURES_DIR) -> SearchResult:
    """Une recherche : fixtures (dev), sinon cache du jour, sinon appel API sous garde-fou quota."""
    dev = config.dev_mode() if dev is None else dev
    if dev:
        return SearchResult(load_fixtures(country, fixtures_dir) if not cursor else [], "fixture")

    params = build_params(query, country, cfg.date_posted, cfg.employment_types, cursor)
    cached = cache_path(params, today or datetime.now(timezone.utc).date(), cache_dir)
    if cached.exists():
        return _result(_read_json(cached), "cache")
    if conn.is_postgres:  # Streamlit Cloud : le disque est effacé au redémarrage, la base non
        stored = db.get_cached_response(conn, cached.name)
        if stored is not None:
            return _result(stored, "cache")

    used = db.get_api_usage(conn)
    if used >= cfg.quota_soft_limit:
        raise QuotaExceeded(f"Quota mensuel atteint : {used} / {cfg.quota_soft_limit} requêtes JSearch "
                            f"(plan gratuit : {FREE_PLAN_LIMIT}). Attends le mois prochain ou relève "
                            "quota_soft_limit dans config.yaml.")
    api_key = config.get_secret("RAPIDAPI_KEY")
    if not api_key:
        raise JSearchError("RAPIDAPI_KEY absente : ajoute-la dans .env (ou les Secrets Streamlit), "
                           "ou active DEV_MODE=1.")
    try:
        payload = fetch_jobs_api(params, api_key, cfg.request_timeout_s)
    finally:
        db.increment_api_usage(conn)  # toute requête envoyée est comptée, même en erreur (prudence)
    result = _result(payload, "api")
    _write_json(cached, payload)
    if conn.is_postgres:
        db.save_cached_response(conn, cached.name, payload)
    return result


def _result(payload: dict[str, Any], source: str) -> SearchResult:
    jobs, cursor = parse_page(payload)
    return SearchResult(jobs, source, cursor)


def collect(conn: db.Database, queries: list[config.SearchQuery], cfg: config.AppConfig,
            **kwargs: Any) -> CollectSummary:
    """Lance les requêtes, enregistre les offres en base. Une erreur n'arrête que sa requête ;
    un quota atteint arrête la collecte."""
    summary = CollectSummary()
    usage_before = db.get_api_usage(conn)
    cursors: dict[tuple[str, str], str | None] = {}  # curseur de la page suivante, par (intitulé, pays)
    try:
        for q in queries:
            key = (q.query, q.country)
            cursor = None
            if q.page > 1:
                cursor = cursors.get(key)
                if not cursor:
                    continue  # pas de page suivante : rien à demander (et rien à payer)
            try:
                res = search_jobs(conn, q.query, q.country, cfg, cursor, **kwargs)
            except QuotaExceeded as e:
                summary.errors.append(str(e))
                break
            except JSearchError as e:
                summary.errors.append(f"« {q.query} » ({q.country.upper()}) : {e}")
                cursors[key] = None
                continue
            cursors[key] = res.next_cursor
            summary.cache_hits += res.source == "cache"
            summary.fetched += len(res.jobs)
            summary.new += db.upsert_jobs(conn, res.jobs)
    finally:
        summary.api_calls = db.get_api_usage(conn) - usage_before
    return summary
