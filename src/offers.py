"""Mise à plat et filtrage des offres pour les pages Offres, Candidatures et l'export."""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from src import db

MODE_LABELS = {"onsite": "sur site", "hybrid": "hybride", "remote": "remote", "unknown": "?"}
CONTRACT_LABELS = {"stage": "stage", "alternance": "alternance", "VIE": "VIE", "working_student": "Werkstudent",
                   "autre": "autre (CDI, CDD…)"}


def flatten(row: dict[str, Any]) -> dict[str, Any]:
    """Une ligne de db.list_jobs_overview → champs simples (extraction et score aplatis)."""
    ex = row.get("extraction") or {}
    bd = row.get("breakdown") or {}
    lo, hi = ex.get("duration_months_min"), ex.get("duration_months_max")
    start = ex.get("start_date_earliest")
    langs = ex.get("languages") or []
    return {
        "job_id": row["job_id"],
        "titre": row.get("job_title"),
        "entreprise": row.get("employer_name"),
        "ville": ex.get("city") or row.get("job_city"),
        "pays": ex.get("country") or row.get("job_country"),
        "mode": MODE_LABELS.get(ex.get("work_mode"), "?") if ex else "?",
        "durée": (f"{lo} mois" if lo == hi else f"{lo or '?'}–{hi or '?'} mois") if (lo or hi) else "",
        "durée_min": lo or hi,
        "début": date.fromisoformat(start) if start else None,
        "domaine": ex.get("domain") or "",
        "gratification": ex.get("stipend_monthly_eur"),
        "score": row.get("score"),
        "éliminée": bool(bd.get("eliminated")),
        "raisons": "; ".join(bd.get("reasons") or []),
        "statut": row.get("status") or db.DEFAULT_STATUS,
        "notes": row.get("notes") or "",
        "lien": row.get("job_apply_link"),
        "source": row.get("job_publisher"),
        "publiée": (row.get("job_posted_at_datetime_utc") or "")[:10],
        "extraite": bool(ex),
        "contrat": CONTRACT_LABELS.get(ex.get("contract_type"), "") if ex else "",
        "compétences": ", ".join(ex.get("required_skills") or []),
        "atouts": ", ".join(ex.get("nice_to_have_skills") or []),
        "langues": ", ".join(f"{l['lang']}" + (f" ({l['level']})" if l.get("level") else "") for l in langs),
        "niveau": ex.get("education_level") or "",
        "convention": {True: "oui", False: "non"}.get(ex.get("requires_convention"), ""),
        "début_texte": ex.get("start_date_text") or "",
        "résumé": ex.get("summary_fr") or "",
    }


def filter_offers(rows: Iterable[dict[str, Any]], *, min_score: float = 0, countries: list[str] | None = None,
                  max_duration: int | None = None, start_before: date | None = None,
                  modes: list[str] | None = None, domains: list[str] | None = None,
                  hide_eliminated: bool = True) -> list[dict[str, Any]]:
    """Filtres de la sidebar. Une info inconnue (durée, début) ne fait pas sortir l'offre."""
    out = []
    for r in rows:
        if hide_eliminated and r["éliminée"]:
            continue
        if min_score and (r["score"] or 0) < min_score:
            continue
        if countries and r["pays"] not in countries:
            continue
        if max_duration and r["durée_min"] and r["durée_min"] > max_duration:
            continue
        if start_before and r["début"] and r["début"] > start_before:
            continue
        if modes and r["mode"] not in modes:
            continue
        if domains and r["domaine"] not in domains:
            continue
        out.append(r)
    return out


def load_offers(conn, user: str | None = None) -> list[dict[str, Any]]:
    """Offres (de `user` si précisé), triées par score et dédoublonnées (entreprise, titre, ville)."""
    return [flatten(r) for r in db.dedupe_jobs(db.list_jobs_overview(conn, user))]
