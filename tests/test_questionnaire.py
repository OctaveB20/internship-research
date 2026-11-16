"""Tests du questionnaire de profil (src/questionnaire.py) et de la page Accueil."""
import pytest
from streamlit.testing.v1 import AppTest

from src import config, questionnaire as qz, scoring
from src.config import ROOT
from src.extract import JobExtraction

ACCUEIL = str(ROOT / "views/0_Accueil.py")


def test_from_form_maps_levels_and_free_text():
    a = qz.from_form(["Excel", "SAP"], "Salesforce, excel", {"English": "courant", "German": "aucun",
                                                             "French": "bilingue / natif"},
                     ["audit"], "private equity")
    assert a.skills == ["Excel", "SAP", "Salesforce"]  # doublon « excel » retiré
    assert a.languages == {"English": "fluent", "French": "native"}  # « aucun » = langue non parlée
    assert a.domains == ["audit", "private equity"]


def test_answers_are_per_user_and_override_profile(conn):
    assert qz.load_answers(conn, "Marilou") is None
    qz.save_answers(conn, "Marilou", qz.ProfileAnswers(skills=["SAP"], languages={"German": "native"},
                                                       domains=["audit"]))
    marilou = qz.user_profile(conn, "Marilou")
    assert marilou.skills == ["SAP"] and marilou.languages == {"German": "native"} and marilou.domains == ["audit"]
    assert qz.user_profile(conn, "Alex").skills == config.load_profile(user="Alex").skills
    assert "Python" in qz.user_profile(conn, "Octave").skills  # profile.yaml tant qu'il n'a pas répondu


def test_empty_domains_keep_profile_domains():
    p = config.load_profile(user="Octave")
    assert qz.apply_answers(p, qz.ProfileAnswers(skills=["Excel"])).domains == p.domains


@pytest.mark.parametrize("level, label", [("native", "bilingue / natif"), ("bilingual", "bilingue / natif"),
                                          ("fluent", "courant"), ("B1", "intermédiaire"), (None, "aucun")])
def test_level_label(level, label):
    assert qz.level_label(level) == label


@pytest.mark.parametrize("offer_skill", ["Budgeting", "Forecasting", "Variance analysis", "Abweichungsanalyse",
                                         "Financial modeling", "Buchhaltung", "Treasury", "ChatGPT"])
def test_questionnaire_skills_match_offer_wording(offer_skill):
    p = config.load_profile().model_copy(update={"skills": ["Budget / forecast", "Analyse des écarts",
                                                            "Modélisation financière", "Comptabilité", "Trésorerie",
                                                            "IA"]})
    ex = JobExtraction(is_internship=True, contract_type="stage", required_skills=[offer_skill])
    assert scoring.score_job(ex, {}, p).skills_matched == [offer_skill]


def test_every_questionnaire_skill_is_known_to_scoring():
    """Chaque compétence proposée doit au moins se reconnaître elle-même."""
    for skill in qz.ALL_SKILLS:
        assert scoring._skill_covered(skill, {scoring.canonical_skill(skill)}), skill


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)


def test_home_shows_questionnaire_then_profile(env):
    from src import db
    at = AppTest.from_file(ACCUEIL, default_timeout=30).run()
    assert not at.exception and at.title[0].value == "Bonjour Octave"
    assert any("Complète ton profil" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Enregistrer mon profil").click().run()
    assert not at.exception
    with db.connect() as c:
        saved = qz.load_answers(c, "Octave")
    assert saved and "Python" in saved.skills and saved.languages["English"] == "native"
    again = AppTest.from_file(ACCUEIL, default_timeout=30).run()
    assert not any("Complète ton profil" in m.value for m in again.markdown)
    assert any(b.label == "Modifier" for b in again.button)
