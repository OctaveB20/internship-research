"""Stockage SQLite : schéma, upserts et requêtes simples (sans ORM)."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src import config

STATUSES = ("à voir", "intéressant", "postulé", "entretien", "refusé", "offre")
DEFAULT_STATUS = STATUSES[0]

JOB_FIELDS = (
    "job_id", "job_title", "employer_name", "job_city", "job_country", "job_is_remote",
    "job_description", "job_apply_link", "job_posted_at_datetime_utc", "job_publisher",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_title TEXT,
    employer_name TEXT,
    job_city TEXT,
    job_country TEXT,
    job_is_remote INTEGER,
    job_description TEXT,
    job_apply_link TEXT,
    job_posted_at_datetime_utc TEXT,
    job_publisher TEXT,
    raw_json TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    extracted_json TEXT NOT NULL,
    extracted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scores (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    score REAL NOT NULL,
    breakdown_json TEXT NOT NULL,
    scored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage (
    month TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    """Horodatage UTC ISO 8601 (à la seconde)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_month() -> str:
    """Mois courant UTC, format YYYY-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ---------- Connexion ----------
def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Ouvre la base (créée si besoin), lignes accessibles par nom de colonne."""
    path = Path(path) if path else config.db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Crée les tables manquantes."""
    conn.executescript(SCHEMA)


# ---------- jobs ----------
def upsert_job(conn: sqlite3.Connection, job: dict[str, Any], seen_at: str | None = None) -> bool:
    """Insère ou met à jour une offre JSearch. Renvoie True si l'offre est nouvelle."""
    if not job.get("job_id"):
        raise ValueError("job_id manquant")
    seen_at = seen_at or now_iso()
    is_new = conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone() is None
    values = {f: job.get(f) for f in JOB_FIELDS}
    if values["job_is_remote"] is not None:
        values["job_is_remote"] = int(bool(values["job_is_remote"]))
    values.update(raw_json=json.dumps(job, ensure_ascii=False), first_seen=seen_at, last_seen=seen_at)
    cols = list(values)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("job_id", "first_seen"))
    conn.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(job_id) DO UPDATE SET {updates}",
        list(values.values()),
    )
    conn.commit()
    return is_new


def upsert_jobs(conn: sqlite3.Connection, jobs: Iterable[dict[str, Any]], seen_at: str | None = None) -> int:
    """Upsert d'une liste d'offres. Renvoie le nombre d'offres nouvelles."""
    return sum(upsert_job(conn, j, seen_at) for j in jobs)


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """Une offre par son job_id, ou None."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def count_jobs(conn: sqlite3.Connection) -> int:
    """Nombre d'offres en base."""
    return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def jobs_without_extraction(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Offres jamais extraites (pour ne jamais payer deux fois la même annonce)."""
    rows = conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN extractions e ON e.job_id = j.job_id "
        "WHERE e.job_id IS NULL ORDER BY j.first_seen"
    ).fetchall()
    return [dict(r) for r in rows]


def list_jobs_overview(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Offres jointes à leur extraction, score et statut (JSON décodés), triées par score."""
    rows = conn.execute(
        "SELECT j.*, e.extracted_json, e.model, s.score, s.breakdown_json, "
        "COALESCE(a.status, ?) AS status, COALESCE(a.notes, '') AS notes "
        "FROM jobs j "
        "LEFT JOIN extractions e ON e.job_id = j.job_id "
        "LEFT JOIN scores s ON s.job_id = j.job_id "
        "LEFT JOIN applications a ON a.job_id = j.job_id "
        "ORDER BY s.score IS NULL, s.score DESC, j.last_seen DESC",
        (DEFAULT_STATUS,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["extraction"] = json.loads(d.pop("extracted_json")) if d["extracted_json"] else None
        d["breakdown"] = json.loads(d.pop("breakdown_json")) if d["breakdown_json"] else None
        out.append(d)
    return out


# ---------- Dédoublonnage d'affichage ----------
_GENDER_TAGS = re.compile(r"\(?\b(h|f|m|w|d|x)\s*/\s*(h|f|m|w|d|x)(\s*/\s*(h|f|m|w|d|x))?\b\)?")
_LEGAL_SUFFIXES = re.compile(r"\b(sas|sasu|sa|sarl|se|gmbh|ag|kg|inc|ltd|llc|plc|group|groupe)\b")


def normalize_text(text: str | None) -> str:
    """Minuscules, sans accents ni ponctuation, espaces compactés."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w/]+", " ", text)
    return " ".join(text.split())


def dedup_key(job: dict[str, Any]) -> tuple[str, str, str]:
    """Clé (entreprise, titre, ville) normalisée, insensible à « (H/F) », « GmbH »…"""
    employer = _LEGAL_SUFFIXES.sub(" ", normalize_text(job.get("employer_name")))
    title = _GENDER_TAGS.sub(" ", normalize_text(job.get("job_title")))
    city = normalize_text(job.get("job_city"))
    return (" ".join(employer.split()), " ".join(title.replace("/", " ").split()), city)


def dedupe_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Garde une seule offre par clé : la première rencontrée (trier avant, ex. par score)."""
    seen: set[tuple[str, str, str]] = set()
    out = []
    for job in jobs:
        key = dedup_key(job)
        if key not in seen:
            seen.add(key)
            out.append(job)
    return out


# ---------- extractions ----------
def save_extraction(conn: sqlite3.Connection, job_id: str, model: str, data: dict[str, Any]) -> None:
    """Enregistre (ou remplace) l'extraction d'une offre."""
    conn.execute(
        "INSERT OR REPLACE INTO extractions (job_id, model, extracted_json, extracted_at) VALUES (?, ?, ?, ?)",
        (job_id, model, json.dumps(data, ensure_ascii=False, default=str), now_iso()),
    )
    conn.commit()


def get_extraction(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """Extraction décodée d'une offre, ou None."""
    row = conn.execute("SELECT extracted_json FROM extractions WHERE job_id = ?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------- scores ----------
def save_score(conn: sqlite3.Connection, job_id: str, score: float, breakdown: dict[str, Any]) -> None:
    """Enregistre (ou remplace) le score d'une offre."""
    conn.execute(
        "INSERT OR REPLACE INTO scores (job_id, score, breakdown_json, scored_at) VALUES (?, ?, ?, ?)",
        (job_id, float(score), json.dumps(breakdown, ensure_ascii=False, default=str), now_iso()),
    )
    conn.commit()


def get_score(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """{'score', 'breakdown', 'scored_at'} ou None."""
    row = conn.execute("SELECT * FROM scores WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    return {"score": row["score"], "breakdown": json.loads(row["breakdown_json"]), "scored_at": row["scored_at"]}


# ---------- applications ----------
def set_application(conn: sqlite3.Connection, job_id: str, status: str, notes: str | None = None) -> None:
    """Met à jour le statut (et les notes si fournies) d'une candidature."""
    if status not in STATUSES:
        raise ValueError(f"Statut inconnu : {status!r} (attendu : {', '.join(STATUSES)})")
    current = get_application(conn, job_id)
    notes = current["notes"] if notes is None else notes
    conn.execute(
        "INSERT OR REPLACE INTO applications (job_id, status, notes, updated_at) VALUES (?, ?, ?, ?)",
        (job_id, status, notes, now_iso()),
    )
    conn.commit()


def get_application(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    """Statut et notes d'une offre (« à voir » par défaut)."""
    row = conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    if row:
        return dict(row)
    return {"job_id": job_id, "status": DEFAULT_STATUS, "notes": "", "updated_at": None}


# ---------- api_usage ----------
def get_api_usage(conn: sqlite3.Connection, month: str | None = None) -> int:
    """Nombre d'appels JSearch réels du mois (YYYY-MM)."""
    row = conn.execute("SELECT count FROM api_usage WHERE month = ?", (month or current_month(),)).fetchone()
    return row[0] if row else 0


def increment_api_usage(conn: sqlite3.Connection, month: str | None = None, n: int = 1) -> int:
    """Ajoute n appels au compteur du mois. Renvoie le nouveau total."""
    month = month or current_month()
    conn.execute(
        "INSERT INTO api_usage (month, count) VALUES (?, ?) "
        "ON CONFLICT(month) DO UPDATE SET count = count + excluded.count",
        (month, n),
    )
    conn.commit()
    return get_api_usage(conn, month)
