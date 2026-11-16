"""Stockage : SQLite en local, Postgres (Neon, Supabase…) si DATABASE_URL est défini. Requêtes simples, sans ORM.

Les deux bases passent par la même classe `Database` : les requêtes s'écrivent avec des `?`
(traduits en `%s` pour Postgres) et les rares différences de SQL (JSON, LIKE) passent par ses méthodes.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src import config

DEFAULT_USER = "Octave"  # propriétaire des offres trouvées avant l'ajout des utilisateurs
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
CREATE TABLE IF NOT EXISTS job_users (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (job_id, username)
);
CREATE TABLE IF NOT EXISTS scores (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    breakdown_json TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (job_id, username)
);
CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, username)
);
CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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


# ---------- Connexion (SQLite ou Postgres) ----------
class Row(dict):
    """Ligne accessible par nom de colonne (row["score"]) ou par position (row[0])."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _Cursor:
    def __init__(self, cursor: Any):
        self._cur = cursor
        self._cols = [d[0] for d in cursor.description] if cursor.description else []

    def fetchone(self) -> Row | None:
        row = self._cur.fetchone()
        return Row(zip(self._cols, row)) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [Row(zip(self._cols, r)) for r in self._cur.fetchall()]


class Database:
    """Connexion commune aux deux moteurs."""

    def __init__(self, raw: Any, dialect: str, url: str = ""):
        self.raw, self.dialect, self._url = raw, dialect, url

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgres"

    def reconnect(self) -> None:
        """Rouvre la connexion Postgres (coupée par le serveur après une longue inactivité)."""
        try:
            self.raw.close()
        except Exception:  # déjà fermée
            pass
        self.raw = _pg_connect(self._url)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Cursor:
        if self.is_postgres:
            sql = sql.replace("?", "%s")
        params = list(params)
        try:
            cur = self.raw.cursor()
            cur.execute(sql, params)
        except Exception as e:
            # Neon / Supabase ferment les connexions restées inactives (ex. pendant une longue analyse IA) :
            # on se reconnecte et on relance la requête une fois. Les autres erreurs remontent telles quelles.
            if not (self.is_postgres and self._url and _is_connection_lost(e)):
                raise
            self.reconnect()
            cur = self.raw.cursor()
            cur.execute(sql, params)
        return _Cursor(cur)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- différences de SQL ---
    @property
    def like(self) -> str:
        """Recherche insensible à la casse (LIKE l'est déjà en SQLite)."""
        return "ILIKE" if self.is_postgres else "LIKE"

    def json_text(self, col: str, key: str) -> str:
        """Champ texte d'une colonne JSON."""
        return f"({col}::jsonb ->> '{key}')" if self.is_postgres else f"json_extract({col}, '$.{key}')"

    def json_num(self, col: str, key: str, integer: bool = True) -> str:
        """Champ numérique d'une colonne JSON."""
        if self.is_postgres:
            return f"CAST({col}::jsonb ->> '{key}' AS {'INTEGER' if integer else 'DOUBLE PRECISION'})"
        return f"json_extract({col}, '$.{key}')"

    def json_true(self, col: str, key: str) -> str:
        """Condition vraie si le booléen JSON vaut true (faux s'il est absent)."""
        if self.is_postgres:
            return f"COALESCE(({col}::jsonb ->> '{key}')::boolean, false)"
        return f"(COALESCE(json_extract({col}, '$.{key}'), 0) = 1)"

    def describe(self) -> str:
        """Où sont les données (pour l'UI), sans jamais afficher d'identifiants."""
        return "Postgres" if self.is_postgres else "SQLite local"


def integrity_errors() -> tuple[type[Exception], ...]:
    """Exceptions de contrainte (clé étrangère, CHECK…) des deux moteurs."""
    errors: list[type[Exception]] = [sqlite3.IntegrityError]
    try:
        import psycopg
        errors.append(psycopg.IntegrityError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(errors)


def _pg_connect(url: str) -> Any:
    import psycopg
    # autocommit : une requête = une transaction ; prepare_threshold=None : obligatoire avec les
    # connexions « poolées » (Neon, Supabase), qui ne gèrent pas les requêtes préparées.
    return psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=10)


def _is_connection_lost(error: Exception) -> bool:
    """Connexion fermée côté serveur ou réseau (à distinguer d'une erreur SQL)."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover
        return False
    if isinstance(error, psycopg.InterfaceError):  # « the connection is closed »
        return True
    return isinstance(error, psycopg.OperationalError) and not isinstance(error, psycopg.errors.QueryCanceled)


def connect(path: Path | str | None = None, url: str | None = None) -> Database:
    """Postgres si une URL est donnée (ou DATABASE_URL défini et pas de chemin), sinon SQLite."""
    url = url if url is not None else ("" if path else config.database_url())
    if url:
        return Database(_pg_connect(url), "postgres", url)
    path = Path(path) if path else config.db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, check_same_thread=False)
    raw.execute("PRAGMA foreign_keys = ON")
    return Database(raw, "sqlite")


def init_db(conn: Database, owner: str = DEFAULT_USER) -> None:
    """Crée les tables manquantes et migre les anciennes bases (offres sans utilisateur → `owner`)."""
    _migrate_per_user_tables(conn, owner)
    for statement in SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()
    if get_setting(conn, "users_migrated") is None:
        # Offres collectées avant l'ajout des utilisateurs : attribuées à `owner` (une seule fois).
        conn.execute("INSERT INTO job_users (job_id, username, first_seen) SELECT job_id, ?, first_seen FROM jobs WHERE true "
                     "ON CONFLICT (job_id, username) DO NOTHING", (owner,))
        set_setting(conn, "users_migrated", owner)


def column_names(conn: Database, table: str) -> set[str]:
    """Colonnes d'une table (vide si elle n'existe pas)."""
    if conn.is_postgres:
        sql = ("SELECT column_name FROM information_schema.columns "
               "WHERE table_schema = current_schema() AND table_name = ?")
        return {r[0] for r in conn.execute(sql, (table,)).fetchall()}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_per_user_tables(conn: Database, owner: str) -> None:
    """Anciennes tables scores/applications (sans colonne username) → nouvelles, en gardant statuts et notes."""
    if "username" not in column_names(conn, "applications") and column_names(conn, "applications"):
        conn.execute("ALTER TABLE applications RENAME TO applications_old")
        conn.commit()
        for statement in SCHEMA.split(";"):
            if "TABLE IF NOT EXISTS applications" in statement:
                conn.execute(statement)
        conn.execute("INSERT INTO applications (job_id, username, status, notes, updated_at) "
                     "SELECT job_id, ?, status, notes, updated_at FROM applications_old", (owner,))
        conn.execute("DROP TABLE applications_old")
        conn.commit()
    if "username" not in column_names(conn, "scores") and column_names(conn, "scores"):
        conn.execute("DROP TABLE scores")  # recalculés gratuitement au prochain « Enregistrer » / recherche
        conn.commit()

def list_tables(conn: Database) -> set[str]:
    """Noms des tables existantes."""
    sql = ("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
           if conn.is_postgres else "SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r[0] for r in conn.execute(sql).fetchall()}


# ---------- jobs ----------
def upsert_job(conn: Database, job: dict[str, Any], seen_at: str | None = None) -> bool:
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


def upsert_jobs(conn: Database, jobs: Iterable[dict[str, Any]], seen_at: str | None = None) -> int:
    """Upsert d'une liste d'offres. Renvoie le nombre d'offres nouvelles."""
    return sum(upsert_job(conn, j, seen_at) for j in jobs)


def get_job(conn: Database, job_id: str) -> dict[str, Any] | None:
    """Une offre par son job_id, ou None."""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def count_jobs(conn: Database) -> int:
    """Nombre d'offres en base."""
    return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def jobs_without_extraction(conn: Database) -> list[dict[str, Any]]:
    """Offres jamais extraites (pour ne jamais payer deux fois la même annonce)."""
    rows = conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN extractions e ON e.job_id = j.job_id "
        "WHERE e.job_id IS NULL ORDER BY j.first_seen"
    ).fetchall()
    return [dict(r) for r in rows]


def list_jobs_overview(conn: Database, user: str | None = None) -> list[dict[str, Any]]:
    """Offres jointes à leur extraction, score et statut (JSON décodés), triées par score.
    Avec `user` : seulement les offres trouvées par cette personne, avec ses scores et ses statuts."""
    who = user or DEFAULT_USER
    only_user = ("JOIN job_users ju ON ju.job_id = j.job_id AND ju.username = ? " if user else "")
    params = [DEFAULT_STATUS] + ([user] if user else []) + [who, who]
    rows = conn.execute(
        "SELECT j.*, e.extracted_json, e.model, s.score, s.breakdown_json, "
        "COALESCE(a.status, ?) AS status, COALESCE(a.notes, '') AS notes "
        "FROM jobs j "
        + only_user +
        "LEFT JOIN extractions e ON e.job_id = j.job_id "
        "LEFT JOIN scores s ON s.job_id = j.job_id AND s.username = ? "
        "LEFT JOIN applications a ON a.job_id = j.job_id AND a.username = ? "
        "ORDER BY s.score IS NULL, s.score DESC, j.last_seen DESC",
        params,
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
# Variantes d'un même mot dans les titres : « Stagiaire » = « Stage », « Praktikant » = « Praktikum »…
_TITLE_SYNONYMS = {"stagiaire": "stage", "praktikant": "praktikum", "praktikantin": "praktikum",
                   "internship": "intern", "trainee": "intern"}


def normalize_text(text: str | None) -> str:
    """Minuscules, sans accents ni ponctuation, espaces compactés."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^\w/]+", " ", text)
    return " ".join(text.split())


def dedup_key(job: dict[str, Any]) -> tuple[str, str, str]:
    """Clé (entreprise, titre, ville) normalisée, insensible à « (H/F) », « GmbH »…"""
    employer = _LEGAL_SUFFIXES.sub(" ", normalize_text(job.get("employer_name")))
    title = _GENDER_TAGS.sub(" ", normalize_text(job.get("job_title"))).replace("/", " ")
    title = " ".join(_TITLE_SYNONYMS.get(w, w) for w in title.split())
    city = normalize_text(job.get("job_city"))
    return (" ".join(employer.split()), title, city)


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
def save_extraction(conn: Database, job_id: str, model: str, data: dict[str, Any]) -> None:
    """Enregistre (ou remplace) l'extraction d'une offre."""
    conn.execute(
        "INSERT INTO extractions (job_id, model, extracted_json, extracted_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(job_id) DO UPDATE SET model = excluded.model, extracted_json = excluded.extracted_json, "
        "extracted_at = excluded.extracted_at",
        (job_id, model, json.dumps(data, ensure_ascii=False, default=str), now_iso()),
    )
    conn.commit()


def get_extraction(conn: Database, job_id: str) -> dict[str, Any] | None:
    """Extraction décodée d'une offre, ou None."""
    row = conn.execute("SELECT extracted_json FROM extractions WHERE job_id = ?", (job_id,)).fetchone()
    return json.loads(row[0]) if row else None


# ---------- scores ----------
def save_score(conn: Database, job_id: str, score: float, breakdown: dict[str, Any], user: str = DEFAULT_USER) -> None:
    """Enregistre (ou remplace) le score d'une offre pour une personne."""
    conn.execute(
        "INSERT INTO scores (job_id, username, score, breakdown_json, scored_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id, username) DO UPDATE SET score = excluded.score, "
        "breakdown_json = excluded.breakdown_json, scored_at = excluded.scored_at",
        (job_id, user, float(score), json.dumps(breakdown, ensure_ascii=False, default=str), now_iso()),
    )
    conn.commit()


def get_score(conn: Database, job_id: str, user: str = DEFAULT_USER) -> dict[str, Any] | None:
    """{'score', 'breakdown', 'scored_at'} ou None."""
    row = conn.execute("SELECT * FROM scores WHERE job_id = ? AND username = ?", (job_id, user)).fetchone()
    if not row:
        return None
    return {"score": row["score"], "breakdown": json.loads(row["breakdown_json"]), "scored_at": row["scored_at"]}


# ---------- applications ----------
def set_application(conn: Database, job_id: str, status: str, notes: str | None = None,
                    user: str = DEFAULT_USER) -> None:
    """Met à jour le statut (et les notes si fournies) d'une candidature de `user`."""
    if status not in STATUSES:
        raise ValueError(f"Statut inconnu : {status!r} (attendu : {', '.join(STATUSES)})")
    current = get_application(conn, job_id, user)
    notes = current["notes"] if notes is None else notes
    conn.execute(
        "INSERT INTO applications (job_id, username, status, notes, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(job_id, username) DO UPDATE SET status = excluded.status, notes = excluded.notes, "
        "updated_at = excluded.updated_at",
        (job_id, user, status, notes, now_iso()),
    )
    conn.commit()


def get_application(conn: Database, job_id: str, user: str = DEFAULT_USER) -> dict[str, Any]:
    """Statut et notes d'une offre pour `user` (« à voir » par défaut)."""
    row = conn.execute("SELECT * FROM applications WHERE job_id = ? AND username = ?", (job_id, user)).fetchone()
    if row:
        return dict(row)
    return {"job_id": job_id, "username": user, "status": DEFAULT_STATUS, "notes": "", "updated_at": None}


# ---------- job_users (qui a trouvé quelle offre) ----------
def link_jobs_to_user(conn: Database, job_ids: Iterable[str], user: str) -> int:
    """Rattache des offres à une personne. Renvoie le nombre d'offres nouvelles pour elle."""
    added = 0
    for job_id in job_ids:
        if conn.execute("SELECT 1 FROM job_users WHERE job_id = ? AND username = ?", (job_id, user)).fetchone():
            continue
        conn.execute("INSERT INTO job_users (job_id, username, first_seen) VALUES (?, ?, ?)", (job_id, user, now_iso()))
        added += 1
    conn.commit()
    return added


def user_job_ids(conn: Database, user: str) -> set[str]:
    """Offres trouvées par une personne."""
    return {r[0] for r in conn.execute("SELECT job_id FROM job_users WHERE username = ?", (user,)).fetchall()}


# ---------- api_cache (copie du cache disque, utile quand le disque est temporaire) ----------
def get_cached_response(conn: Database, key: str) -> dict[str, Any] | None:
    """Réponse JSearch mise en cache sous cette clé, ou None."""
    row = conn.execute("SELECT payload FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def save_cached_response(conn: Database, key: str, payload: dict[str, Any]) -> None:
    """Enregistre une réponse JSearch (clé = nom du fichier de cache)."""
    conn.execute(
        "INSERT INTO api_cache (cache_key, payload, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
        (key, json.dumps(payload, ensure_ascii=False), now_iso()),
    )
    conn.commit()


def needs_scoring(conn: Database, user: str) -> bool:
    """True si `user` a des offres analysées par l'IA mais aucun score (à recalculer)."""
    row = conn.execute(
        "SELECT 1 FROM job_users ju JOIN extractions e ON e.job_id = ju.job_id "
        "LEFT JOIN scores s ON s.job_id = ju.job_id AND s.username = ju.username "
        "WHERE ju.username = ? AND s.job_id IS NULL LIMIT 1", (user,)).fetchone()
    return row is not None


# ---------- settings ----------
def get_setting(conn: Database, key: str) -> str | None:
    """Valeur enregistrée (texte), ou None."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: Database, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


# ---------- api_usage ----------
def get_api_usage(conn: Database, month: str | None = None) -> int:
    """Nombre d'appels JSearch réels du mois (YYYY-MM)."""
    row = conn.execute("SELECT count FROM api_usage WHERE month = ?", (month or current_month(),)).fetchone()
    return row[0] if row else 0


def increment_api_usage(conn: Database, month: str | None = None, n: int = 1) -> int:
    """Ajoute n appels au compteur du mois. Renvoie le nouveau total."""
    month = month or current_month()
    conn.execute(
        "INSERT INTO api_usage (month, count) VALUES (?, ?) "
        "ON CONFLICT(month) DO UPDATE SET count = api_usage.count + excluded.count",
        (month, n),
    )
    conn.commit()
    return get_api_usage(conn, month)
