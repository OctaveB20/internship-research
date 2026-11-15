"""Critères de recherche choisis dans la page Recherche (enregistrés en base).

Ils servent à deux choses :
- construire les requêtes JSearch (intitulés × pays × pages, date de publication) ;
- compléter profile.yaml pour le score (pays, mois de début, durées, villes, remote, gratification, mots exclus).
"""
from __future__ import annotations

import calendar
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src import config, db

SETTINGS_KEY = "search_criteria"
HISTORY_KEY = "searched_countries"  # tous les pays déjà recherchés

# Nom (anglais, utilisé par le score) → (code ISO pour JSearch, libellé affiché)
EUROPE: dict[str, tuple[str, str]] = {
    "France": ("fr", "France"), "Germany": ("de", "Allemagne"), "Belgium": ("be", "Belgique"),
    "Luxembourg": ("lu", "Luxembourg"), "Switzerland": ("ch", "Suisse"), "Netherlands": ("nl", "Pays-Bas"),
    "United Kingdom": ("gb", "Royaume-Uni"), "Ireland": ("ie", "Irlande"), "Spain": ("es", "Espagne"),
    "Portugal": ("pt", "Portugal"), "Italy": ("it", "Italie"), "Austria": ("at", "Autriche"),
    "Denmark": ("dk", "Danemark"), "Sweden": ("se", "Suède"), "Norway": ("no", "Norvège"),
    "Finland": ("fi", "Finlande"), "Poland": ("pl", "Pologne"), "Czech Republic": ("cz", "Tchéquie"),
    "Hungary": ("hu", "Hongrie"), "Romania": ("ro", "Roumanie"), "Greece": ("gr", "Grèce"),
    "Croatia": ("hr", "Croatie"), "Slovakia": ("sk", "Slovaquie"), "Slovenia": ("si", "Slovénie"),
    "Bulgaria": ("bg", "Bulgarie"), "Estonia": ("ee", "Estonie"), "Latvia": ("lv", "Lettonie"),
    "Lithuania": ("lt", "Lituanie"), "Malta": ("mt", "Malte"), "Cyprus": ("cy", "Chypre"),
}
MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
DURATIONS = list(range(1, 13))
DATE_POSTED = {"today": "aujourd'hui", "3days": "3 derniers jours", "week": "7 derniers jours",
               "month": "30 derniers jours", "all": "toutes"}


class SearchCriteria(BaseModel):
    titles: list[str] = Field(min_length=1)
    countries: list[str] = Field(min_length=1)
    start_months: list[str] = []  # "AAAA-MM" ; vide = pas de contrainte de mois
    durations: list[int] = []  # mois acceptés ; vide = toutes durées
    cities: list[str] = []
    remote_ok: bool = True
    date_posted: Literal["all", "today", "3days", "week", "month"] = "week"
    pages: int = Field(1, ge=1, le=3)
    min_stipend_eur: float | None = Field(None, ge=0)
    exclude_words: list[str] = []
    internship_only: bool = False  # True = seulement les offres marquées « stage » (INTERN) par Google : bien moins d'offres

    @field_validator("titles", "cities", "exclude_words")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for v in (v.strip() for v in values):
            if v and v.lower() not in {o.lower() for o in out}:
                out.append(v)
        return out

    @field_validator("countries")
    @classmethod
    def _known(cls, values: list[str]) -> list[str]:
        unknown = [v for v in values if v not in EUROPE]
        if unknown:
            raise ValueError(f"pays inconnus : {unknown}")
        return values

    def request_count(self) -> int:
        """Requêtes JSearch nécessaires (hors cache)."""
        return len(self.titles) * len(self.countries) * self.pages


def split_list(text: str) -> list[str]:
    """« a, b ; c » ou une valeur par ligne → ["a", "b", "c"]."""
    for sep in (";", "\n"):
        text = text.replace(sep, ",")
    return [t.strip() for t in text.split(",") if t.strip()]


def upcoming_months(today: date, n: int = 15) -> list[str]:
    """Les n prochains mois (mois courant compris), format AAAA-MM."""
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_label(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTHS_FR[int(m) - 1]} {y}"


def _month_bounds(ym: str) -> tuple[date, date]:
    y, m = map(int, ym.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


# ---------- Valeurs par défaut, lecture, enregistrement ----------
def defaults(profile: config.Profile, cfg: config.AppConfig) -> SearchCriteria:
    """Critères initiaux, tirés de profile.yaml et config.yaml."""
    from src.scoring import canonical_country

    countries = [c for c in (canonical_country(x) for x in profile.target_countries) if c in EUROPE] or ["France"]
    months = [ym for ym in upcoming_months(profile.available_from.replace(day=1), 15)
              if _month_bounds(ym)[0] <= profile.latest_start]
    d = profile.duration_months
    return SearchCriteria(titles=cfg.default_titles, countries=countries, start_months=months,
                          durations=list(range(d.min, d.max + 1)), cities=profile.target_cities,
                          remote_ok=profile.remote_ok, date_posted=cfg.date_posted,
                          min_stipend_eur=profile.min_stipend_eur)


def load(conn: db.Database, profile: config.Profile, cfg: config.AppConfig) -> SearchCriteria:
    """Critères enregistrés, sinon ceux par défaut."""
    raw = db.get_setting(conn, SETTINGS_KEY)
    if raw:
        try:
            return SearchCriteria.model_validate_json(raw)
        except ValueError:
            pass  # critères d'une ancienne version : on repart des valeurs par défaut
    return defaults(profile, cfg)


def save(conn: db.Database, criteria: SearchCriteria) -> None:
    db.set_setting(conn, SETTINGS_KEY, criteria.model_dump_json())


# ---------- Utilisation ----------
def build_queries(criteria: SearchCriteria) -> list[config.SearchQuery]:
    """Une requête JSearch par intitulé, pays et page."""
    return [config.SearchQuery(query=t, country=EUROPE[c][0], page=p)
            for t in criteria.titles for c in criteria.countries for p in range(1, criteria.pages + 1)]


def searched_countries(conn: db.Database) -> list[str]:
    """Pays déjà recherchés (toutes recherches confondues)."""
    raw = db.get_setting(conn, HISTORY_KEY)
    if raw:
        return json.loads(raw)
    # Pas encore d'historique (offres collectées avant son ajout) : pays des offres déjà en base.
    by_code = {code: name for name, (code, _) in EUROPE.items()}
    codes = {str(r[0]).lower() for r in conn.execute("SELECT DISTINCT job_country FROM jobs").fetchall() if r[0]}
    return [by_code[c] for c in sorted(codes) if c in by_code]


def record_searched(conn: db.Database, countries: list[str]) -> None:
    """Ajoute des pays à l'historique des recherches."""
    known = searched_countries(conn)
    db.set_setting(conn, HISTORY_KEY, json.dumps(known + [c for c in countries if c not in known]))


def effective_profile(conn: db.Database, profile: config.Profile, criteria: SearchCriteria) -> config.Profile:
    """Profil utilisé pour le score : critères cochés + tous les pays déjà recherchés.
    Changer les pays d'une nouvelle recherche ne fait donc pas disparaître les offres des recherches précédentes."""
    return apply_to_profile(profile, criteria, extra_countries=searched_countries(conn))


def apply_to_profile(profile: config.Profile, criteria: SearchCriteria,
                     extra_countries: list[str] | None = None) -> config.Profile:
    """profile.yaml complété par les critères cochés (pays, dates, durées, villes…)."""
    countries = criteria.countries + [c for c in (extra_countries or []) if c not in criteria.countries]
    update: dict = {"target_countries": countries, "target_cities": criteria.cities,
                    "remote_ok": criteria.remote_ok, "min_stipend_eur": criteria.min_stipend_eur,
                    "exclude_title_words": criteria.exclude_words, "start_months": sorted(criteria.start_months)}
    if criteria.start_months:
        months = sorted(criteria.start_months)
        update["available_from"] = _month_bounds(months[0])[0]
        update["latest_start"] = _month_bounds(months[-1])[1]
    if criteria.durations:
        update["duration_months"] = config.DurationRange(min=min(criteria.durations), max=max(criteria.durations))
    weights = profile.weights
    if criteria.min_stipend_eur and not weights.stipend:
        weights = weights.model_copy(update={"stipend": 10})  # gratification minimale → elle compte dans le score
    update["weights"] = weights
    return profile.model_copy(update=update)


def describe(criteria: SearchCriteria) -> str:
    """Résumé lisible des critères."""
    parts = [f"{len(criteria.countries)} pays"]
    if criteria.start_months:
        months = sorted(criteria.start_months)
        parts.append(f"début {month_label(months[0])}" + (f" → {month_label(months[-1])}" if len(months) > 1 else ""))
    if criteria.durations:
        parts.append("durée " + ", ".join(str(d) for d in sorted(criteria.durations)) + " mois")
    return " · ".join(parts)

