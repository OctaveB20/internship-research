"""Éléments communs aux pages Streamlit : config de page, mot de passe, bandeaux, connexion."""
from __future__ import annotations

import hmac

import streamlit as st

from src import config, db, llm


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
def _shared_postgres(url: str) -> db.Database:
    """Une seule connexion Postgres pour toute l'app (évite d'en ouvrir une à chaque clic)."""
    conn = db.connect(url=url)
    db.init_db(conn)
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


def get_connection() -> db.Database:
    """Postgres si DATABASE_URL est défini (connexion partagée, recréée si elle a été coupée), sinon SQLite."""
    url = config.database_url()
    if not url:
        conn = db.connect()
        db.init_db(conn)
        return conn
    for attempt in range(2):
        try:
            conn = _shared_postgres(url)
            conn.execute("SELECT 1")
            return conn
        except Exception as e:  # connexion coupée par le serveur après inactivité : on en rouvre une
            _shared_postgres.clear()
            if attempt == 1:
                st.error(_postgres_error(e))
                st.stop()
    raise AssertionError  # pragma: no cover


def setup_page(title: str, icon: str = "🎯") -> tuple[db.Database, config.AppConfig, config.Profile]:
    """À appeler en tête de chaque page. Renvoie (connexion, config, profil)."""
    st.set_page_config(page_title=f"{title} · Internship Scout", page_icon=icon, layout="wide")
    require_password()
    if config.SECRETS_ERREUR:
        st.warning(f"Secrets Streamlit illisibles : {config.SECRETS_ERREUR}")
    try:
        cfg, profile = config.load_config(), config.load_profile()
    except Exception as e:  # YAML invalide : message lisible
        st.error(f"config.yaml ou profile.yaml invalide : {e}")
        st.stop()
    conn = get_connection()
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
    st.title(title)
    return conn, cfg, profile
