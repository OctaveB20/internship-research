"""Chat : questions sur les offres de la base."""
import streamlit as st

from src import chat, llm, ui

conn, cfg, profile = ui.setup_page("Chat", "💬")
st.caption("Exemples : « Quelles offres FP&A à Munich ? », « Les 3 meilleures offres qui commencent en janvier », "
           "« Pourquoi l'offre Kestrel est éliminée ? »")

if "chat" not in st.session_state:
    st.session_state.chat = []
if st.sidebar.button("Nouvelle conversation"):
    st.session_state.chat = []

for msg in st.session_state.chat:
    st.chat_message(msg["role"]).markdown(msg["content"])

if not llm.has_key(cfg):
    st.warning(f"Ajoute la clé {cfg.llm_provider.upper()}_API_KEY dans les secrets pour utiliser le chat.")
    st.stop()

if question := st.chat_input("Ta question sur les offres…"):
    st.chat_message("user").markdown(question)
    st.session_state.chat.append({"role": "user", "content": question})
    with st.chat_message("assistant"), st.spinner("Je cherche dans la base…"):
        try:
            res = chat.ask_llm(conn, cfg, st.session_state.chat[-12:])
            answer = res.text or "Je n'ai pas de réponse."
        except llm.LLMError as e:
            answer = f"⚠️ {e}"
            res = None
        st.markdown(answer)
        if res and res.tool_calls:
            st.caption("Recherches : " + " · ".join(f"{c['name']}({', '.join(f'{k}={v}' for k, v in c['args'].items())})"
                                                     for c in res.tool_calls))
    st.session_state.chat.append({"role": "assistant", "content": answer})
