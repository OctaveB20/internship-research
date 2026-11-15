"""Chatbot sur les offres : l'IA n'a que 2 outils, à paramètres fixes (aucun SQL écrit par l'IA)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from src import config, db, llm

MAX_RESULTS = 30
DESCRIPTION_CHARS = 6000

SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keywords": {"type": ["string", "null"], "description": "Mots à chercher dans le titre, l'entreprise, "
                     "l'annonce et le résumé (tous doivent apparaître)."},
        "country": {"type": ["string", "null"], "description": "Pays, ex. France, Germany."},
        "city": {"type": ["string", "null"], "description": "Ville, ex. Paris, München."},
        "work_mode": {"type": ["string", "null"], "enum": ["onsite", "hybrid", "remote", "unknown", None]},
        "domain": {"type": ["string", "null"], "description": "Domaine, ex. contrôle de gestion, FP&A, audit."},
        "min_score": {"type": ["number", "null"], "description": "Score minimum sur 100."},
        "max_duration_months": {"type": ["integer", "null"], "description": "Durée minimale de l'offre ≤ cette valeur."},
        "start_before": {"type": ["string", "null"], "description": "Début au plus tard le (AAAA-MM-JJ)."},
        "status": {"type": ["string", "null"], "enum": [*db.STATUSES, None], "description": "Statut de candidature."},
        "include_eliminated": {"type": ["boolean", "null"], "description": "Inclure les offres éliminées (défaut : non)."},
        "limit": {"type": ["integer", "null"], "description": f"Nombre max de résultats (défaut 10, max {MAX_RESULTS})."},
    },
    "required": [],
}
GET_JOB_SCHEMA = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}

TOOLS = [
    llm.ToolSpec("search_jobs", "Cherche des offres dans la base, triées par score décroissant. Renvoie la liste "
                 "des offres trouvées (titre, entreprise, ville, score, statut, durée, début…).", SEARCH_SCHEMA),
    llm.ToolSpec("get_job", "Détail complet d'une offre : annonce, extraction, détail du score, statut et notes.",
                 GET_JOB_SCHEMA),
]

SYSTEM_PROMPT = """Tu es l'assistant de recherche de stage de l'utilisateur. Tu réponds en français, de façon concise.

- Tu ne connais que les offres de la base : utilise toujours `search_jobs` ou `get_job` avant de parler d'une offre.
- N'invente jamais d'offre, d'entreprise, de chiffre ou de lien absent des résultats des outils. Si rien ne \
correspond, dis-le simplement.
- Cite chaque offre avec son titre, son entreprise et son score (ex. « Stage FP&A — Lumen Capital Partners, 100/100 »).
- Les offres éliminées (score 0) sont masquées par défaut ; explique la raison d'élimination si on te la demande."""

_EX = "json_extract(e.extracted_json, '$.{}')"


def search_jobs(conn: sqlite3.Connection, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Requête SQL paramétrée construite à partir de filtres connus (les autres clés sont ignorées)."""
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    where, params = [], []
    for word in str(f.get("keywords", "")).split():
        where.append("(j.job_title LIKE ? OR j.employer_name LIKE ? OR j.job_description LIKE ? OR "
                     f"{_EX.format('summary_fr')} LIKE ?)")
        params += [f"%{word}%"] * 4
    if "country" in f:
        where.append(f"({_EX.format('country')} LIKE ? OR j.job_country LIKE ?)")
        params += [f"%{f['country']}%"] * 2
    if "city" in f:
        where.append(f"({_EX.format('city')} LIKE ? OR j.job_city LIKE ?)")
        params += [f"%{f['city']}%"] * 2
    if "work_mode" in f:
        where.append(f"{_EX.format('work_mode')} = ?")
        params.append(str(f["work_mode"]))
    if "domain" in f:
        where.append(f"{_EX.format('domain')} LIKE ?")
        params.append(f"%{f['domain']}%")
    if "min_score" in f:
        where.append("s.score >= ?")
        params.append(float(f["min_score"]))
    if "max_duration_months" in f:
        where.append(f"COALESCE({_EX.format('duration_months_min')}, {_EX.format('duration_months_max')}) <= ?")
        params.append(int(f["max_duration_months"]))
    if "start_before" in f:
        where.append(f"{_EX.format('start_date_earliest')} <= ?")
        params.append(str(f["start_before"])[:10])
    if "status" in f:
        where.append("COALESCE(a.status, ?) = ?")
        params += [db.DEFAULT_STATUS, str(f["status"])]
    if not f.get("include_eliminated"):
        where.append("COALESCE(json_extract(s.breakdown_json, '$.eliminated'), 0) = 0")
    limit = max(1, min(int(f.get("limit") or 10), MAX_RESULTS))
    sql = (
        "SELECT j.job_id, j.job_title, j.employer_name, "
        f"COALESCE({_EX.format('city')}, j.job_city) AS city, COALESCE({_EX.format('country')}, j.job_country) AS country, "
        f"{_EX.format('work_mode')} AS work_mode, {_EX.format('duration_months_min')} AS duration_min, "
        f"{_EX.format('duration_months_max')} AS duration_max, {_EX.format('start_date_earliest')} AS start, "
        f"{_EX.format('domain')} AS domain, {_EX.format('stipend_monthly_eur')} AS stipend_eur, "
        f"{_EX.format('summary_fr')} AS summary, s.score, json_extract(s.breakdown_json, '$.reasons') AS elimination, "
        "COALESCE(a.status, ?) AS status, j.job_apply_link AS apply_link "
        "FROM jobs j LEFT JOIN extractions e ON e.job_id = j.job_id LEFT JOIN scores s ON s.job_id = j.job_id "
        "LEFT JOIN applications a ON a.job_id = j.job_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.score IS NULL, s.score DESC LIMIT ?"
    )
    rows = conn.execute(sql, [db.DEFAULT_STATUS, *params, limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["elimination"] = json.loads(d["elimination"]) if d["elimination"] else []
        out.append(d)
    return out


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Offre complète, ou une erreur si l'identifiant est inconnu."""
    job = db.get_job(conn, job_id)
    if not job:
        return {"error": f"Aucune offre avec l'identifiant {job_id!r}."}
    score = db.get_score(conn, job_id)
    app = db.get_application(conn, job_id)
    desc = job.get("job_description") or ""
    return {
        "job_id": job_id, "title": job["job_title"], "employer": job["employer_name"],
        "city": job["job_city"], "country": job["job_country"], "apply_link": job["job_apply_link"],
        "posted": job["job_posted_at_datetime_utc"], "publisher": job["job_publisher"],
        "description": desc[:DESCRIPTION_CHARS] + (" […]" if len(desc) > DESCRIPTION_CHARS else ""),
        "extraction": db.get_extraction(conn, job_id),
        "score": score["score"] if score else None, "score_breakdown": score["breakdown"] if score else None,
        "status": app["status"], "notes": app["notes"],
    }


def run_tool(conn: sqlite3.Connection, name: str, args: dict[str, Any]) -> Any:
    """Exécute un des 2 outils autorisés."""
    if name == "search_jobs":
        return search_jobs(conn, args.get("filters", args))  # tolère {"filters": {...}} ou les filtres à plat
    if name == "get_job":
        return get_job(conn, str(args.get("job_id", "")))
    return {"error": f"Outil inconnu : {name}"}


def ask_llm(conn: sqlite3.Connection, cfg: config.AppConfig, history: list[dict[str, str]],
            client: llm.LLMClient | None = None) -> llm.ChatResult:
    """Appel IA (peut être payant) : répond au dernier message de `history` en interrogeant la base."""
    client = client or llm.make_client(cfg)
    return client.chat(SYSTEM_PROMPT, history, TOOLS, lambda name, args: run_tool(conn, name, args))
