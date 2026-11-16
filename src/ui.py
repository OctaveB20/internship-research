"""Éléments communs aux pages Streamlit : config de page, mot de passe, bandeaux, connexion."""
from __future__ import annotations

import hashlib
import hmac
import html

import streamlit as st

from src import config, criteria, db, llm


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
.block-container {max-width: 1280px; padding-top: 2.4rem;}
h1 {letter-spacing: -0.02em;}
[data-testid="stMetricLabel"] p {font-size: 0.85rem; opacity: 0.75;}
.scout-label {font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; opacity: 0.6; margin-bottom: 0.1rem;}
.scout-value {font-size: 0.98rem; font-weight: 500; margin-bottom: 0.75rem;}
</style>
"""


def score_color(score: float | None, eliminated: bool = False) -> str:
    """Couleur de badge pour un score."""
    if eliminated or score is None:
        return "gray"
    return "green" if score >= 75 else "orange" if score >= 50 else "red"


def metric_card(label: str, value: object, help: str | None = None) -> None:
    """Indicateur dans un cadre."""
    with st.container(border=True):
        st.metric(label, value, help=help)


def field(label: str, value: object) -> None:
    """Petit bloc « libellé / valeur » (valeur absente → « non précisé »)."""
    shown = html.escape(str(value)) if value not in (None, "", []) else "<span style='opacity:.5'>non précisé</span>"
    st.markdown(f"<div class='scout-label'>{html.escape(label)}</div><div class='scout-value'>{shown}</div>",
                unsafe_allow_html=True)


# ---------- Utilisateur courant ----------
def current_user(cfg: config.AppConfig) -> str:
    """Personne choisie (mémorisée pendant la session et dans l'URL : ?user=Marilou)."""
    ss = st.session_state
    if ss.get("user") not in cfg.users:
        wanted = st.query_params.get("user")
        ss.user = wanted if wanted in cfg.users else cfg.users[0]
    return ss.user


def user_selector(cfg: config.AppConfig, where=None, key: str = "user_sidebar", label: str = "Utilisateur",
                  width: int | str = "stretch") -> str:
    """Liste déroulante des utilisateurs, synchronisée avec les autres sélecteurs de la page."""
    st.session_state[key] = current_user(cfg)

    def _changed() -> None:
        st.session_state.user = st.session_state[key]
        st.query_params["user"] = st.session_state.user

    (where or st).selectbox(label, cfg.users, key=key, on_change=_changed, width=width)
    return current_user(cfg)


def setup_page(title: str, icon: str = ":material/target:", subtitle: str = ""
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
        user = user_selector(cfg, label="Utilisateur")
    base = config.load_profile(user=user)  # profil de cette personne (profile.yaml, section users)
    # + critères de sa page Recherche
    profile = criteria.effective_profile(conn, base, criteria.load(conn, base, cfg, user), user)
    signature = hashlib.sha256(profile.model_dump_json().encode()).hexdigest()[:16]
    if db.needs_scoring(conn, user) or db.get_setting(conn, f"profile_signature:{user}") != signature:
        # profil modifié (ex. compétences ajoutées) ou scores manquants : recalcul gratuit et instantané
        from src import scoring
        scoring.score_all(conn, profile, user)
        db.set_setting(conn, f"profile_signature:{user}", signature)
    with st.sidebar:
        if config.dev_mode():
            st.info("Mode dev : offres lues dans les fixtures, aucun appel JSearch.")
        if not llm.has_key(cfg):
            st.caption(f"IA : clé {config.LLM_KEYS[cfg.llm_provider]} absente.")
        else:
            st.caption(f"IA : {cfg.llm_provider} · {cfg.model}")
        if config.on_streamlit_cloud():
            if not config.get_secret("APP_PASSWORD"):
                st.warning("App en ligne sans APP_PASSWORD : toute personne ayant l'URL peut utiliser tes clés.")
        if conn.is_postgres:
            st.caption("Base : Postgres")
        elif config.on_streamlit_cloud():
            st.warning("Base SQLite temporaire : effacée au redémarrage de l'app. Ajoute DATABASE_URL (base Postgres, ex. Neon).")
    with st.sidebar:
        st.caption("Internship Scout · recherche de stage")
    st.title(title)
    if subtitle:
        st.caption(subtitle)
    return conn, cfg, profile
