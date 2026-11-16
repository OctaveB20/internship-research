"""Chat : questions sur les offres de la base."""
import streamlit as st

from src import chat, llm, ui

conn, cfg, profile = ui.setup_page("Chat", ":material/chat:", "Pose tes questions sur les offres de la base.")
st.caption("Exemples : « Quelles offres FP&A à Munich ? », « Les 3 meilleures offres qui commencent en janvier », "
           "« Pourquoi l'offre Kestrel est éliminée ? »")

user = ui.current_user(cfg)
key = f"chat_{user}"  # une conversation par personne
if key not in st.session_state:
    st.session_state[key] = []
if st.sidebar.button("Nouvelle conversation"):
    st.session_state[key] = []
history = st.session_state[key]

for msg in history:
    st.chat_message(msg["role"]).markdown(msg["content"])

if not llm.has_key(cfg):
    st.warning(f"Ajoute la clé {cfg.llm_provider.upper()}_API_KEY dans les secrets pour utiliser le chat.")
    st.stop()

if question := st.chat_input("Ta question sur les offres…"):
    st.chat_message("user").markdown(question)
    history.append({"role": "user", "content": question})
    with st.chat_message("assistant"), st.spinner("Je cherche dans la base…"):
        try:
            res = chat.ask_llm(conn, cfg, history[-12:], user=user)
            answer = res.text or "Je n'ai pas de réponse."
        except llm.LLMError as e:
            answer = f"⚠️ {e}"
            res = None
        st.markdown(answer)
        if res and res.tool_calls:
            st.caption("Recherches : " + " · ".join(f"{c['name']}({', '.join(f'{k}={v}' for k, v in c['args'].items())})"
                                                     for c in res.tool_calls))
    history.append({"role": "assistant", "content": answer})
