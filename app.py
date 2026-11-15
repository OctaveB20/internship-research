"""Internship Scout : point d'entrée Streamlit."""
import streamlit as st

from src import config, db

st.set_page_config(page_title="Internship Scout", page_icon="🎯", layout="wide")
st.title("Internship Scout")
st.caption("Recherche de stage : collecte JSearch → extraction Claude → scoring → suivi.")

if config.SECRETS_ERREUR:
    st.warning(f"Secrets Streamlit illisibles : {config.SECRETS_ERREUR}")
if config.dev_mode():
    st.info("Mode dev : aucun appel réseau, les offres viennent de tests/fixtures.")

with db.connect() as conn:
    db.init_db(conn)
    st.metric("Requêtes JSearch ce mois", f"{db.get_api_usage(conn)} / 200")
    st.metric("Offres en base", db.count_jobs(conn))

st.markdown("Utilise le menu à gauche : **Recherche**, **Offres**, **Chat**, **Candidatures**.")
