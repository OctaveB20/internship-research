"""Éléments communs aux pages Streamlit : config de page, mot de passe, bandeaux, connexion."""
from __future__ import annotations

import hashlib
import hmac
import html

import streamlit as st

from src import config, criteria, db, llm, questionnaire


def require_password() -> None:
    """Bloque la page tant que APP_PASSWORD (s'il est défini) n'a pas été saisi."""
    expected = config.get_secret("APP_PASSWORD")
    if not expected or st.session_state.get("auth_ok"):
        return
    pwd = st.text_input("Mot de passe", type="password")
    if pwd and hmac.compare_digest(pwd, expected):
        st.session_state.auth_ok = True
        st.rerun()
    if pwd:
        st.error("Mot de passe incorrect.")
    st.stop()


@st.cache_resource(show_spinner=False)
def _shared_postgres(url: str, owner: str) -> db.Database:
    """Une seule connexion Postgres pour toute l'app (évite d'en ouvrir une à chaque clic)."""
    conn = db.connect(url=url)
    db.init_db(conn, owner)
    return conn


def _postgres_error(e: Exception) -> str:
    """Message lisible, sans l'adresse (qui contient le mot de passe)."""
    first_line = str(e).strip().splitlines()[0] if str(e).strip() else ""
    for secret in (config.database_url(), config.get_secret("DATABASE_PASSWORD")):
        if secret:
            first_line = first_line.replace(secret, "***")
    hint = ("Vérifie DATABASE_URL dans les secrets : prends l'adresse avec « pooling » activé "
            "(Neon : Connect → Connection pooling), avec le mot de passe de la base.")
    if "password authentication failed" in first_line:
        hint = "Mot de passe refusé : mets le mot de passe de la base Postgres (pas celui de ton compte) dans DATABASE_URL."
    return f"Connexion à la base Postgres impossible ({type(e).__name__} : {first_line[:200]}). {hint}"


def get_connection(owner: str = db.DEFAULT_USER) -> db.Database:
    """Postgres si DATABASE_URL est défini (connexion partagée, recréée si elle a été coupée), sinon SQLite."""
    url = config.database_url()
    if not url:
        conn = db.connect()
        db.init_db(conn, owner)
        return conn
    for attempt in range(2):
        try:
            conn = _shared_postgres(url, owner)
            conn.execute("SELECT 1")
            return conn
        except Exception as e:  # connexion coupée par le serveur après inactivité : on en rouvre une
            _shared_postgres.clear()
            if attempt == 1:
                st.error(_postgres_error(e))
                st.stop()
    raise AssertionError  # pragma: no cover


CSS = """
<style>
.block-container {max-width: 1240px; padding-top: 2.2rem; padding-bottom: 4rem;}
h1 {letter-spacing: -0.025em; font-weight: 700;}
h2, h3 {letter-spacing: -0.01em;}
/* Cadres : ombre légère pour détacher les blocs */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}
/* Cartes d'indicateurs */
.scout-stat {border: 1px solid var(--scout-border, rgba(127,127,127,.22)); border-radius: 0.75rem;
             padding: 0.85rem 1rem; min-height: 6.4rem; margin-bottom: 0.75rem;}
.scout-stat .label {font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.62;}
.scout-stat .value {font-size: 1.7rem; font-weight: 650; line-height: 1.25; margin-top: 0.15rem;}
.scout-stat .hint {font-size: 0.8rem; opacity: 0.6; margin-top: 0.1rem;}
/* Libellé / valeur des fiches */
.scout-label {font-size: 0.74rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.6; margin-bottom: 0.1rem;}
.scout-value {font-size: 0.97rem; font-weight: 500; margin-bottom: 0.8rem;}
/* En-tête de page */
.scout-kicker {font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.55; margin-bottom: -0.6rem;}
/* Petits titres de section */
.scout-section {font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.6;
                margin: 0.6rem 0 0.2rem;}
</style>
"""


def score_color(score: float | None, eliminated: bool = False) -> str:
    """Couleur de badge pour un score."""
    if eliminated or score is None:
        return "gray"
    return "green" if score >= 75 else "orange" if score >= 50 else "red"


def metric_card(label: str, value: object, help: str | None = None) -> None:
    """Indicateur dans un cadre (libellé, grande valeur, précision facultative)."""
    hint = f"<div class='hint'>{html.escape(help)}</div>" if help else ""
    st.markdown(f"<div class='scout-stat'><div class='label'>{html.escape(label)}</div>"
                f"<div class='value'>{html.escape(str(value))}</div>{hint}</div>", unsafe_allow_html=True)


def field(label: str, value: object) -> None:
    """Petit bloc « libellé / valeur » (valeur absente → « non précisé »)."""
    shown = html.escape(str(value)) if value not in (None, "", []) else "<span style='opacity:.5'>non précisé</span>"
    st.markdown(f"<div class='scout-label'>{html.escape(label)}</div><div class='scout-value'>{shown}</div>",
                unsafe_allow_html=True)


STATUS_COLORS = {"à voir": "gray", "intéressant": "blue", "postulé": "violet", "entretien": "orange",
                 "refusé": "red", "offre": "green"}


# ---------- Navigation entre les pages ----------
PAGE_FILES = {"accueil": "views/0_Accueil.py", "recherche": "views/1_Recherche.py", "offres": "views/2_Offres.py",
              "assistant": "views/3_Chat.py", "candidatures": "views/4_Candidatures.py"}


def go(page: str, **state: object) -> None:
    """Ouvre une autre page, en lui transmettant des valeurs (ex. l'offre à afficher)."""
    st.session_state.update(state)
    try:
        st.switch_page(PAGE_FILES[page])
    except st.errors.StreamlitAPIException:  # page lancée seule (tests) : pas de navigation possible
        st.rerun()


def open_offer(job_id: str) -> None:
    """Affiche la fiche d'une offre dans la page Offres."""
    go("offres", focus_job=job_id)


def ask_assistant(question: str) -> None:
    """Ouvre l'assistant avec une question déjà posée."""
    go("assistant", chat_prefill=question)


# ---------- Utilisateur courant ----------
def current_user(cfg: config.AppConfig) -> str:
    """Personne choisie (mémorisée pendant la session et dans l'URL : ?user=Marilou)."""
    ss = st.session_state
    if ss.get("user") not in cfg.users:
        wanted = st.query_params.get("user")
        ss.user = wanted if wanted in cfg.users else cfg.users[0]
    return ss.user


def user_selector(cfg: config.AppConfig, where=None, key: str = "user_sidebar", label: str = "Utilisateur",
                  width: int | str = "stretch", label_visibility: str = "visible") -> str:
    """Liste déroulante des utilisateurs, synchronisée avec les autres sélecteurs de la page."""
    st.session_state[key] = current_user(cfg)

    def _changed() -> None:
        st.session_state.user = st.session_state[key]
        st.query_params["user"] = st.session_state.user

    (where or st).selectbox(label, cfg.users, key=key, on_change=_changed, width=width,
                            label_visibility=label_visibility)
    return current_user(cfg)


def setup_page(title: str, icon: str = ":material/target:", subtitle: str = "", show_title: bool = True
               ) -> tuple[db.Database, config.AppConfig, config.Profile]:
    """À appeler en tête de chaque page. Renvoie (connexion, config, profil)."""
    st.set_page_config(page_title=f"{title} · Internship Scout", page_icon=icon, layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    require_password()
    if config.SECRETS_ERREUR:
        st.warning(f"Secrets Streamlit illisibles : {config.SECRETS_ERREUR}")
    try:
        cfg = config.load_config()
        config.load_profile()  # vérifie profile.yaml dès maintenant
    except Exception as e:  # YAML invalide : message lisible
        st.error(f"config.yaml ou profile.yaml invalide : {e}")
        st.stop()
    conn = get_connection(cfg.users[0])
    with st.sidebar:
        user = user_selector(cfg, label="Profil actif")
    base = questionnaire.user_profile(conn, user)  # profile.yaml + réponses au questionnaire de l'accueil
    # + critères de sa page Recherche
    profile = criteria.effective_profile(conn, base, criteria.load(conn, base, cfg, user), user)
    signature = hashlib.sha256(profile.model_dump_json().encode()).hexdigest()[:16]
    if db.needs_scoring(conn, user) or db.get_setting(conn, f"profile_signature:{user}") != signature:
        # profil modifié (ex. compétences ajoutées) ou scores manquants : recalcul gratuit et instantané
        from src import scoring
        scoring.score_all(conn, profile, user)
        db.set_setting(conn, f"profile_signature:{user}", signature)
    with st.sidebar:
        if config.on_streamlit_cloud() and not config.get_secret("APP_PASSWORD"):
            st.warning("App en ligne sans APP_PASSWORD : toute personne ayant l'URL peut utiliser tes clés.")
        if not conn.is_postgres and config.on_streamlit_cloud():
            st.warning("Base SQLite temporaire : effacée au redémarrage de l'app. Ajoute DATABASE_URL (base Postgres, ex. Neon).")
        status = [f"IA : {cfg.llm_provider} · {cfg.model}" if llm.has_key(cfg)
                  else f"IA : clé {config.LLM_KEYS[cfg.llm_provider]} absente",
                  "Base : Postgres" if conn.is_postgres else "Base : SQLite"]
        if config.dev_mode():
            status.append("Mode dev : fixtures, aucun appel JSearch")
        st.caption(" · ".join(status))
    if show_title:
        st.title(title)
        if subtitle:
            st.caption(subtitle)
    return conn, cfg, profile
