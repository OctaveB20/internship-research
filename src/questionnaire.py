"""Questionnaire de profil (page Accueil) : compétences, langues et domaines de chaque personne, enregistrés en base.

Les réponses remplacent, pour cette personne, les valeurs de profile.yaml utilisées par le score.
"""
from __future__ import annotations

from pydantic import BaseModel, field_validator

from src import config, db

SETTINGS_KEY = "profile_answers"

# Compétences proposées, par thème (les libellés sont reconnus par le score, y compris leurs variantes).
SKILL_GROUPS: dict[str, list[str]] = {
    "Bureautique": ["Excel", "VBA", "PowerPoint", "Word", "Google Sheets"],
    "Finance & contrôle de gestion": ["Clôture mensuelle", "Budget / forecast", "Reporting", "Analyse des écarts",
                                      "Rapprochement", "Consolidation", "Modélisation financière", "Valorisation",
                                      "Comptabilité", "Trésorerie"],
    "ERP & outils finance": ["SAP", "Oracle", "Sage", "Cegid", "Pennylane", "Anaplan"],
    "Data & BI": ["Power BI", "Tableau", "SQL", "Python", "R", "Looker"],
    "IA & automatisation": ["IA", "Microsoft Copilot", "ChatGPT", "Automatisation", "Power Automate"],
    "Outils techniques": ["GitHub", "Streamlit", "API", "Supabase"],
}
ALL_SKILLS = [s for group in SKILL_GROUPS.values() for s in group]

DOMAINS = ["contrôle de gestion", "FP&A", "finance d'entreprise", "audit", "M&A", "trésorerie", "comptabilité",
           "conseil", "banque d'investissement", "gestion d'actifs", "data / BI finance", "achats", "supply chain",
           "marketing", "RH"]

LANGUAGES = {"French": "Français", "English": "Anglais", "German": "Allemand", "Spanish": "Espagnol",
             "Italian": "Italien", "Dutch": "Néerlandais", "Portuguese": "Portugais"}
# Libellé affiché → niveau compris par le score (None = je ne parle pas cette langue).
LEVELS: dict[str, str | None] = {"aucun": None, "notions": "basic", "intermédiaire": "intermediate",
                                 "professionnel": "professional", "courant": "fluent", "bilingue / natif": "native"}
LEVEL_LABELS = {v: k for k, v in LEVELS.items() if v}


class ProfileAnswers(BaseModel):
    skills: list[str] = []
    languages: dict[str, str] = {}  # {"English": "fluent"}
    domains: list[str] = []

    @field_validator("skills", "domains")
    @classmethod
    def _clean(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for v in (v.strip() for v in values):
            if v and v.lower() not in {o.lower() for o in out}:
                out.append(v)
        return out


def _key(user: str) -> str:
    return f"{SETTINGS_KEY}:{user}"


def load_answers(conn: db.Database, user: str) -> ProfileAnswers | None:
    """Réponses enregistrées de `user`, ou None s'il n'a pas encore rempli le questionnaire."""
    raw = db.get_setting(conn, _key(user))
    if not raw:
        return None
    try:
        return ProfileAnswers.model_validate_json(raw)
    except ValueError:
        return None


def save_answers(conn: db.Database, user: str, answers: ProfileAnswers) -> None:
    db.set_setting(conn, _key(user), answers.model_dump_json())


def apply_answers(profile: config.Profile, answers: ProfileAnswers | None) -> config.Profile:
    """Profil de profile.yaml dont compétences, langues et domaines sont remplacés par les réponses."""
    if answers is None:
        return profile
    update: dict = {"skills": answers.skills, "languages": answers.languages}
    if answers.domains:
        update["domains"] = answers.domains
    return profile.model_copy(update=update)


def user_profile(conn: db.Database, user: str) -> config.Profile:
    """Profil complet d'une personne : profile.yaml (commun + section users) puis réponses au questionnaire."""
    return apply_answers(config.load_profile(user=user), load_answers(conn, user))


def level_label(level: str | None) -> str:
    """Niveau du profil (native, fluent…) → libellé du questionnaire."""
    from src.scoring import level_value

    if not level:
        return "aucun"
    if level in LEVEL_LABELS:
        return LEVEL_LABELS[level]
    value = level_value(level) or 0
    return {5: "bilingue / natif", 4: "courant", 3: "professionnel", 2: "intermédiaire", 1: "notions"}.get(value, "aucun")


def from_form(skills: list[str], other_skills: str, languages: dict[str, str], domains: list[str],
              other_domains: str) -> ProfileAnswers:
    """Réponses du formulaire → ProfileAnswers (textes libres séparés par des virgules)."""
    from src.criteria import split_list

    return ProfileAnswers(
        skills=list(skills) + split_list(other_skills),
        languages={lang: LEVELS[label] for lang, label in languages.items() if LEVELS.get(label)},
        domains=list(domains) + split_list(other_domains),
    )
