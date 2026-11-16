"""Smoke tests des pages Streamlit en mode dev (aucun appel réseau)."""
import pytest
from streamlit.testing.v1 import AppTest

from src.config import ROOT

PAGES = [str(ROOT / p) for p in ("app.py", "views/0_Accueil.py", "views/1_Recherche.py", "views/2_Offres.py", "views/3_Chat.py",
                                 "views/4_Candidatures.py")]
RECHERCHE, OFFRES = PAGES[2], PAGES[3]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "1")
    monkeypatch.setenv("JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def button(at, label):
    return next(b for b in at.button if b.label == label)


def launch(at):
    button(at, "Lancer la recherche").click().run()


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_on_empty_db(env, page):
    assert not AppTest.from_file(page, default_timeout=30).run().exception


def test_full_dev_pipeline_from_ui(env):
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    launch(at)
    assert not at.exception
    assert "20 nouvelle(s) offre(s)" in at.success[0].value
    for page in PAGES:
        assert not AppTest.from_file(page, default_timeout=30).run().exception
    offres = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert len(offres.dataframe[0].value) == 8


def test_password_gate(env, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    at = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert not at.title  # page bloquée
    at.text_input[0].input("faux").run()
    assert at.error and not at.title
    at.text_input[0].input("s3cret").run()
    assert at.title[0].value == "Offres"


def test_pipeline_on_postgres(env, monkeypatch):
    from tests.conftest import TEST_PG_URL, reset_postgres
    from src import db, ui
    if not TEST_PG_URL:
        pytest.skip("TEST_DATABASE_URL non défini")
    reset_postgres(TEST_PG_URL)
    ui._shared_postgres.clear()
    monkeypatch.setenv("DATABASE_URL", TEST_PG_URL)
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    assert any("Postgres" in c.value for c in at.sidebar.caption)
    launch(at)
    assert not at.exception and "20 nouvelle(s) offre(s)" in at.success[0].value
    for page in PAGES:
        assert not AppTest.from_file(page, default_timeout=30).run().exception
    with db.connect(url=TEST_PG_URL) as c:
        assert db.count_jobs(c) == 20
    ui._shared_postgres.clear()


def test_bad_database_url_is_readable(env, monkeypatch):
    from src import ui
    ui._shared_postgres.clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres.x:monmdp@127.0.0.1:1/postgres")
    at = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert not at.exception
    assert "Connexion à la base Postgres impossible" in at.error[0].value and "monmdp" not in at.error[0].value
    ui._shared_postgres.clear()


def test_search_criteria_ui(env):
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    checked = lambda prefix: [c.label for c in at.checkbox if c.key.startswith(prefix) and c.value]  # noqa: E731
    assert set(checked("c_")) == {"Allemagne", "France"} and set(checked("d_")) == {"5 mois", "6 mois"}
    assert set(checked("m_")) == {"janv. 2027", "févr. 2027", "mars 2027"}
    button(at, "Toute l'Europe").click().run()
    assert len(checked("c_")) == 30
    assert any("3 intitulé(s) × 30 pays" in c.value for c in at.caption)  # 90 requêtes
    button(at, "Aucun").click().run()
    assert any("coche au moins un pays" in w.value for w in at.warning)
    assert button(at, "Lancer la recherche").disabled
    button(at, "France + Allemagne").click().run()
    at.text_input(key="crit_titles").input("stage FP&A").run()
    at.checkbox(key="d_4").check().run()
    button(at, "Enregistrer les critères").click().run()
    again = AppTest.from_file(RECHERCHE, default_timeout=30).run()  # nouvelle session : critères relus en base
    assert again.text_input(key="crit_titles").value == "stage FP&A"
    assert again.checkbox(key="d_4").value is True


def test_quota_blocks_too_big_search(env, monkeypatch):
    monkeypatch.setenv("DEV_MODE", "0")
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    button(at, "Toute l'Europe").click().run()
    at.select_slider(key="crit_pages").set_value(3).run()  # 3 × 30 × 3 = 270 > 180
    assert any("décoche des pays" in e.value for e in at.error)
    assert button(at, "Lancer la recherche").disabled


def test_user_switch_isolates_offers(env):
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    assert at.selectbox(key="user_sidebar").value == "Octave"
    launch(at)  # Octave : France + Allemagne
    at.selectbox(key="user_recherche").set_value("Marilou").run()
    assert at.selectbox(key="user_sidebar").value == "Marilou"
    at.text_input(key="crit_titles").input("Praktikum Controlling").run()
    button(at, "Aucun").click().run()
    at.checkbox(key="c_Germany").check().run()
    launch(at)
    assert "10 nouvelle(s) offre(s) pour Marilou" in at.success[0].value
    # Retour à Octave : ses critères reviennent
    at.selectbox(key="user_sidebar").set_value("Octave").run()
    assert at.text_input(key="crit_titles").value.startswith("stage contrôle de gestion")


def test_profile_change_triggers_rescoring(env, monkeypatch):
    from src import config, db
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    launch(at)
    with db.connect() as c:
        before = db.get_setting(c, "profile_signature:Octave")
    original = config.load_profile
    monkeypatch.setattr(config, "load_profile", lambda path=config.PROFILE_PATH, user=None:
                        original(path, user).model_copy(update={"skills": ["SAP"]}))
    AppTest.from_file(OFFRES, default_timeout=30).run()
    with db.connect() as c:
        assert db.get_setting(c, "profile_signature:Octave") != before


def test_offres_views_search_and_focus(env):
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    launch(at)
    assert any(b.label.startswith("Voir les 20 nouvelle") for b in at.button)  # bilan + bouton gardés en session
    at.text_input(key="crit_titles").input("stage FP&A").run()  # un autre clic : le bilan reste affiché
    assert at.success and any(b.label.startswith("Voir les 20 nouvelle") for b in at.button)

    offres = AppTest.from_file(OFFRES, default_timeout=30).run()
    assert len(offres.dataframe[0].value) == 8 and "None" not in offres.dataframe[0].value.to_string()
    offres.text_input(key="offers_query_Octave").input("munich").run()
    assert set(offres.dataframe[0].value["entreprise"]) == {"Aubrac Systems", "Brennwald AG", "Helix Mobility GmbH"}

    from src import db
    with db.connect() as c:
        kestrel = next(r["job_id"] for r in db.list_jobs_overview(c, "Octave") if r["employer_name"] == "Kestrel Fund Services")
    focused = AppTest.from_file(OFFRES, default_timeout=30)
    focused.session_state["focus_job"] = kestrel  # ex. clic sur « Fiche » depuis Candidatures
    focused.run()
    assert focused.subheader[0].value == "Corporate Finance Internship"
    assert any("Éliminée" in e.value for e in focused.error)


def test_candidatures_empty_state_and_cards(env):
    from src import db
    at = AppTest.from_file(RECHERCHE, default_timeout=30).run()
    launch(at)
    cand = AppTest.from_file(PAGES[5], default_timeout=30).run()
    assert any("Aucune candidature suivie" in m.value for m in cand.markdown)
    with db.connect() as c:
        job_id = db.list_jobs_overview(c, "Octave")[0]["job_id"]
        db.set_application(c, job_id, "postulé", "envoyé", "Octave")
    cand = AppTest.from_file(PAGES[5], default_timeout=30).run()
    assert not cand.exception and any(b.label == "Fiche" for b in cand.button)


def test_assistant_shows_starter_questions(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    chat = AppTest.from_file(PAGES[4], default_timeout=30).run()
    assert not chat.exception and any(b.label == "Mes 5 meilleures offres" for b in chat.button)


def test_add_manual_application_from_ui(env):
    cand = AppTest.from_file(PAGES[5], default_timeout=30).run()
    assert not cand.exception
    form = [t for t in cand.text_input]
    next(t for t in form if t.label.startswith("Intitulé")).input("Stage FP&A")
    next(t for t in form if t.label.startswith("Entreprise")).input("Acme")
    next(b for b in cand.button if b.label == "Ajouter").click().run()
    assert not cand.exception
    cand = AppTest.from_file(PAGES[5], default_timeout=30).run()
    assert any(b.label == "Fiche" for b in cand.button)
    assert any("Stage FP&A" in m.value for m in cand.markdown)
