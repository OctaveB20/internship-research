"""Internship Scout : point d'entrée Streamlit (navigation entre les pages)."""
import streamlit as st

pages = [
    st.Page("pages/0_Accueil.py", title="Accueil", icon=":material/home:", default=True),
    st.Page("pages/1_Recherche.py", title="Recherche", icon=":material/search:"),
    st.Page("pages/2_Offres.py", title="Offres", icon=":material/work:"),
    st.Page("pages/3_Chat.py", title="Chat", icon=":material/chat:"),
    st.Page("pages/4_Candidatures.py", title="Candidatures", icon=":material/view_kanban:"),
]
st.navigation(pages).run()
