"""Score déterministe (sans IA) d'une offre extraite par rapport à profile.yaml, sur 100."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from src import config, db
from src.db import normalize_text
from src.extract import JobExtraction

NEUTRAL = 0.5  # info absente : la moitié de la composante, sans pénalité totale

COUNTRY_ALIASES = {
    "fr": "France", "france": "France",
    "de": "Germany", "germany": "Germany", "allemagne": "Germany", "deutschland": "Germany",
    "lu": "Luxembourg", "luxembourg": "Luxembourg", "luxemburg": "Luxembourg",
    "at": "Austria", "austria": "Austria", "autriche": "Austria", "osterreich": "Austria",
    "ch": "Switzerland", "switzerland": "Switzerland", "suisse": "Switzerland", "schweiz": "Switzerland",
    "be": "Belgium", "belgium": "Belgium", "belgique": "Belgium", "belgien": "Belgium",
    "nl": "Netherlands", "netherlands": "Netherlands", "pays bas": "Netherlands", "niederlande": "Netherlands",
    "es": "Spain", "spain": "Spain", "espagne": "Spain", "it": "Italy", "italy": "Italy", "italie": "Italy",
    "gb": "United Kingdom", "uk": "United Kingdom", "united kingdom": "United Kingdom", "royaume uni": "United Kingdom",
    "us": "United States", "usa": "United States", "united states": "United States", "etats unis": "United States",
}
CITY_ALIASES = {"munchen": "munich", "koln": "cologne", "wien": "vienna", "frankfurt am main": "frankfurt",
                "nurnberg": "nuremberg", "zurich": "zurich", "geneve": "geneva", "bruxelles": "brussels"}
LANG_ALIASES = {
    "english": "english", "anglais": "english", "englisch": "english", "en": "english",
    "french": "french", "francais": "french", "franzosisch": "french", "fr": "french",
    "german": "german", "allemand": "german", "deutsch": "german", "de": "german",
    "spanish": "spanish", "espagnol": "spanish", "spanisch": "spanish", "es": "spanish",
    "italian": "italian", "italien": "italian", "italienisch": "italian", "it": "italian",
    "dutch": "dutch", "neerlandais": "dutch", "niederlandisch": "dutch", "nl": "dutch",
    "portuguese": "portuguese", "portugais": "portuguese", "portugiesisch": "portuguese", "pt": "portuguese",
}
LEVELS = {
    "native": 5, "natif": 5, "maternelle": 5, "muttersprache": 5, "c2": 5, "bilingual": 5, "bilingue": 5,
    "fluent": 4, "courant": 4, "fliessend": 4, "fließend": 4, "c1": 4, "verhandlungssicher": 4, "excellent": 4,
    "professional": 3, "professionnel": 3, "b2": 3, "good": 3, "bon": 3, "gut": 3,
    "intermediate": 2, "intermediaire": 2, "b1": 2,
    "basic": 1, "notions": 1, "a2": 1, "a1": 1, "scolaire": 1, "grundkenntnisse": 1,
}
DEFAULT_REQUIRED_LEVEL = 3  # langue demandée sans niveau précisé
# Formulations d'une même compétence (après normalisation : minuscules, sans accents ni ponctuation).
SKILL_ALIASES = {
    "ms excel": "excel", "microsoft excel": "excel", "excel avance": "excel", "advanced excel": "excel",
    "powerbi": "power bi", "microsoft power bi": "power bi", "ms office": "office", "microsoft office": "office",
    "python 3": "python", "programmation python": "python",
    "microsoft copilot": "copilot", "m365 copilot": "copilot", "microsoft 365 copilot": "copilot",
    "ia": "ai", "ki": "ai", "intelligence artificielle": "ai", "artificial intelligence": "ai",
    "kunstliche intelligenz": "ai", "ia generative": "ai", "generative ai": "ai", "genai": "ai",
    "automatisation": "automation", "automatisierung": "automation", "process automation": "automation",
    "automatisation des processus": "automation", "rpa": "automation",
    "cloture mensuelle": "closing", "clotures mensuelles": "closing", "cloture": "closing", "clotures": "closing",
    "monthly closing": "closing", "month end closing": "closing", "month end close": "closing",
    "monatsabschluss": "closing", "monatsabschlusse": "closing", "closing": "closing",
    "rapprochement": "reconciliation", "rapprochements": "reconciliation", "rapprochement bancaire": "reconciliation",
    "rapprochements bancaires": "reconciliation", "account reconciliation": "reconciliation",
    "kontenabstimmung": "reconciliation", "kontenabstimmungen": "reconciliation",
    "git": "github", "api": "api", "apis": "api", "rest api": "api", "rest apis": "api",
    "chatgpt": "ai", "chat gpt": "ai", "llm": "ai",
    "budget forecast": "budget", "budget / forecast": "budget", "budget": "budget", "budgets": "budget", "budgeting": "budget", "forecast": "budget",
    "forecasts": "budget", "forecasting": "budget", "previsions": "budget", "planification budgetaire": "budget",
    "budgetplanung": "budget", "planung": "budget",
    "reportings": "reporting", "management reporting": "reporting", "reporting financier": "reporting",
    "financial reporting": "reporting", "berichtswesen": "reporting",
    "analyse des ecarts": "variance analysis", "analyses des ecarts": "variance analysis",
    "abweichungsanalyse": "variance analysis", "abweichungsanalysen": "variance analysis",
    "modelisation financiere": "financial modelling", "financial modeling": "financial modelling",
    "modele financier": "financial modelling", "modeles financiers": "financial modelling",
    "finanzmodellierung": "financial modelling",
    "valorisation": "valuation", "company valuation": "valuation", "unternehmensbewertung": "valuation",
    "konsolidierung": "consolidation",
    "comptabilite": "accounting", "comptabilite generale": "accounting", "buchhaltung": "accounting",
    "rechnungswesen": "accounting",
    "tresorerie": "treasury", "cash management": "treasury",
}
STOPWORDS = {"de", "d", "la", "le", "les", "des", "du", "et", "en", "l", "a", "of", "and", "the"}


# ---------- Normalisation ----------
def canonical_country(value: str | None) -> str | None:
    if not value:
        return None
    key = normalize_text(value)
    return COUNTRY_ALIASES.get(key, value.strip().title())


def canonical_city(value: str | None) -> str:
    key = normalize_text(value)
    return CITY_ALIASES.get(key, key)


def canonical_lang(value: str) -> str:
    key = normalize_text(value)
    return LANG_ALIASES.get(key, key)


def level_value(level: str | None) -> int | None:
    if not level:
        return None
    text = normalize_text(level)
    for word in text.split():
        if word in LEVELS:
            return LEVELS[word]
    return LEVELS.get(text)


def canonical_skill(value: str) -> str:
    key = normalize_text(value)
    return SKILL_ALIASES.get(key, key)


def _skill_covered(required: str, mine: set[str]) -> bool:
    """« Excel » couvre « Excel » ; « SAP » couvre « SAP BO » (mots entiers)."""
    req = canonical_skill(required)
    if req in mine:
        return True
    words = set(req.split())
    return any(set(m.split()) <= words for m in mine)


def _tokens(text: str) -> set[str]:
    return {w for w in normalize_text(text).split() if w not in STOPWORDS}


# ---------- Résultat ----------
@dataclass
class ScoreResult:
    score: float
    eliminated: bool
    reasons: list[str] = field(default_factory=list)
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    skills_matched: list[str] = field(default_factory=list)
    skills_missing: list[str] = field(default_factory=list)
    raw_score: float = 0.0  # score avant élimination (pour l'affichage)

    def breakdown(self) -> dict[str, Any]:
        return {"total": self.score, "raw_score": self.raw_score, "eliminated": self.eliminated, "reasons": self.reasons,
                "components": self.components, "skills_matched": self.skills_matched,
                "skills_missing": self.skills_missing}


# ---------- Filtres éliminatoires ----------
def _offer_country(ex: JobExtraction, job: dict[str, Any]) -> str | None:
    return canonical_country(ex.country) or canonical_country(job.get("job_country"))


def elimination_reasons(ex: JobExtraction, job: dict[str, Any], p: config.Profile) -> list[str]:
    """Raisons d'élimination (liste vide = offre retenue)."""
    reasons = []
    if not ex.is_internship or ex.contract_type in ("alternance", "working_student"):
        reasons.append(f"pas un stage ({ex.contract_type})")
    country = _offer_country(ex, job)
    targets = {canonical_country(c) for c in p.target_countries}
    if country and country not in targets and not (ex.work_mode == "remote" and p.remote_ok):
        reasons.append(f"pays hors liste ({country})")
    lo = ex.duration_months_min or ex.duration_months_max
    hi = ex.duration_months_max or ex.duration_months_min
    if lo and hi and (hi < p.duration_months.min or lo > p.duration_months.max):
        reasons.append(f"durée incompatible ({lo}–{hi} mois)" if lo != hi else f"durée incompatible ({lo} mois)")
    if ex.start_date_earliest and ex.start_date_earliest > p.latest_start:
        reasons.append(f"début trop tard ({ex.start_date_earliest:%d/%m/%Y})")
    title = f" {normalize_text(job.get('job_title'))} "
    for word in p.exclude_title_words:
        if normalize_text(word) and f" {normalize_text(word)} " in title:
            reasons.append(f"titre exclu (« {word} »)")
            break
    return reasons


# ---------- Composantes (chacune renvoie un ratio 0–1 et un détail) ----------
def score_location(ex: JobExtraction, job: dict[str, Any], p: config.Profile) -> tuple[float, str]:
    if ex.work_mode == "remote":
        return (1.0, "full remote accepté") if p.remote_ok else (0.2, "full remote non souhaité")
    city = ex.city or job.get("job_city")
    if not city:
        return NEUTRAL, "ville inconnue"
    if canonical_city(city) in {canonical_city(c) for c in p.target_cities}:
        return 1.0, f"{city} : ville cible"
    country = _offer_country(ex, job)
    if country in {canonical_country(c) for c in p.target_countries}:
        return 0.4, f"{city} : pays cible, autre ville"
    return 0.0, f"{city} : hors zone"


def score_duration(ex: JobExtraction, p: config.Profile) -> tuple[float, str]:
    lo = ex.duration_months_min or ex.duration_months_max
    hi = ex.duration_months_max or ex.duration_months_min
    if not lo:
        return NEUTRAL, "durée non précisée"
    label = f"{lo} mois" if lo == hi else f"{lo}–{hi} mois"
    if p.duration_months.min <= lo and hi <= p.duration_months.max:
        return 1.0, f"{label} : dans ta fourchette"
    if hi >= p.duration_months.min and lo <= p.duration_months.max:
        return 0.7, f"{label} : en partie dans ta fourchette"
    return 0.0, f"{label} : hors fourchette"


def score_start(ex: JobExtraction, p: config.Profile) -> tuple[float, str]:
    start = ex.start_date_earliest
    if not start:
        return NEUTRAL, "date de début non précisée" + (f" ({ex.start_date_text})" if ex.start_date_text else "")
    if start > p.latest_start:
        return 0.0, f"début le {start:%d/%m/%Y}, après ta date limite"
    if start >= p.available_from:
        if p.start_months and f"{start:%Y-%m}" not in p.start_months:
            return 0.6, f"début le {start:%d/%m/%Y} : mois non coché"
        return 1.0, f"début le {start:%d/%m/%Y} : compatible"
    days_early = (p.available_from - start).days
    return max(0.0, 1 - days_early / 60), f"début le {start:%d/%m/%Y}, {days_early} j avant ta disponibilité"


def score_skills(ex: JobExtraction, p: config.Profile) -> tuple[float, str, list[str], list[str]]:
    mine = {canonical_skill(s) for s in p.skills}
    matched = [s for s in ex.required_skills if _skill_covered(s, mine)]
    missing = [s for s in ex.required_skills if s not in matched]
    bonus = [s for s in ex.nice_to_have_skills if _skill_covered(s, mine)]
    if not ex.required_skills:
        return NEUTRAL, "aucune compétence exigée listée", bonus, []
    ratio = len(matched) / len(ex.required_skills)
    detail = f"{len(matched)}/{len(ex.required_skills)} compétences exigées couvertes"
    if bonus:
        detail += f" (+ atouts : {', '.join(bonus)})"
    return ratio, detail, matched + bonus, missing


def score_languages(ex: JobExtraction, p: config.Profile) -> tuple[float, str]:
    if not ex.languages:
        return NEUTRAL, "aucune langue précisée"
    mine = {canonical_lang(k): level_value(v) or 0 for k, v in p.languages.items()}
    ratios, notes = [], []
    for lang in ex.languages:
        need = level_value(lang.level) or DEFAULT_REQUIRED_LEVEL
        have = mine.get(canonical_lang(lang.lang), 0)
        ratios.append(min(1.0, have / need))
        if have < need:
            notes.append(f"{lang.lang} {lang.level or ''}".strip())
    detail = "niveaux suffisants" if not notes else f"niveau à renforcer : {', '.join(notes)}"
    return sum(ratios) / len(ratios), detail


def score_domain(ex: JobExtraction, p: config.Profile) -> tuple[float, str]:
    if not ex.domain:
        return NEUTRAL, "domaine non précisé"
    offer = normalize_text(ex.domain)
    for d in p.domains:
        mine = normalize_text(d)
        if mine and (mine in offer or offer in mine):
            return 1.0, f"{ex.domain} : domaine visé"
    if any(_tokens(ex.domain) & _tokens(d) for d in p.domains):
        return 0.6, f"{ex.domain} : domaine proche"
    return 0.0, f"{ex.domain} : hors domaines visés"


def score_stipend(ex: JobExtraction, p: config.Profile) -> tuple[float, str]:
    if ex.stipend_monthly_eur is None:
        return NEUTRAL, "gratification non précisée"
    if not p.min_stipend_eur:
        return 1.0, f"{ex.stipend_monthly_eur:.0f} €/mois"
    ratio = min(1.0, ex.stipend_monthly_eur / p.min_stipend_eur)
    return ratio, f"{ex.stipend_monthly_eur:.0f} €/mois (minimum souhaité : {p.min_stipend_eur:.0f} €)"


# ---------- Score total ----------
def score_job(ex: JobExtraction, job: dict[str, Any], p: config.Profile) -> ScoreResult:
    """Score sur 100 = somme des composantes pondérées / somme des poids. 0 si éliminée."""
    w = p.weights
    skills_ratio, skills_detail, matched, missing = score_skills(ex, p)
    parts = {
        "location": (w.location, *score_location(ex, job, p)),
        "duration": (w.duration, *score_duration(ex, p)),
        "start_date": (w.start_date, *score_start(ex, p)),
        "skills": (w.skills, skills_ratio, skills_detail),
        "languages": (w.languages, *score_languages(ex, p)),
        "domain": (w.domain, *score_domain(ex, p)),
        "stipend": (w.stipend, *score_stipend(ex, p)),
    }
    components = {name: {"points": round(weight * ratio, 1), "max": weight, "ratio": round(ratio, 2), "detail": detail}
                  for name, (weight, ratio, detail) in parts.items() if weight > 0}
    total_weight = sum(c["max"] for c in components.values()) or 1
    raw = round(100 * sum(c["points"] for c in components.values()) / total_weight, 1)
    reasons = elimination_reasons(ex, job, p)
    return ScoreResult(0.0 if reasons else raw, bool(reasons), reasons, components, matched, missing, raw)


def score_all(conn: db.Database, profile: config.Profile, user: str = db.DEFAULT_USER) -> int:
    """(Re)calcule le score des offres extraites de `user`. Gratuit et instantané. Renvoie le nombre scoré."""
    rows = conn.execute("SELECT j.*, e.extracted_json FROM jobs j JOIN extractions e ON e.job_id = j.job_id "
                        "JOIN job_users ju ON ju.job_id = j.job_id AND ju.username = ?", (user,)).fetchall()
    for row in rows:
        job = dict(row)
        ex = JobExtraction.model_validate_json(job.pop("extracted_json"))
        result = score_job(ex, job, profile)
        db.save_score(conn, job["job_id"], result.score, result.breakdown(), user)
    return len(rows)
