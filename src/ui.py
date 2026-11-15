"""Éléments communs aux pages Streamlit : config de page, mot de passe, bandeaux, connexion."""
from __future__ import annotations

import hmac
import sqlite3

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


def setup_page(title: str, icon: str = "🎯") -> tuple[sqlite3.Connection, config.AppConfig, config.Profile]:
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
    conn = db.connect()
    db.init_db(conn)
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
            st.caption("⚠️ Base SQLite temporaire sur Streamlit Cloud : effacée au redémarrage de l'app.")
    st.title(title)
    return conn, cfg, profile
