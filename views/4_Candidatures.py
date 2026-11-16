"""Candidatures : pipeline par statut (cartes cliquables), édition rapide et export Excel."""
from datetime import date

import pandas as pd
import streamlit as st

from src import db, export, offers, ui

conn, cfg, profile = ui.setup_page("Candidatures", ":material/view_kanban:", show_title=False)
user = ui.current_user(cfg)
rows = offers.load_offers(conn, user)

head, action = st.columns([4, 1.4], vertical_alignment="bottom")
head.title("Candidatures")
if rows:
    action.download_button("Exporter en Excel", export.offers_to_excel(rows),
                           f"offres_stage_{user}_{date.today():%Y-%m-%d}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           icon=":material/download:", width="stretch")
if not rows:
    st.info(f"Aucune offre pour {user} pour l'instant.")
    if st.button("Lancer une recherche", type="primary", icon=":material/search:"):
        ui.go("recherche")
    st.stop()

# ---------- Pipeline ----------
followed = [s for s in db.STATUSES if s != db.DEFAULT_STATUS]
counts = {s: sum(r["statut"] == s for r in rows) for s in db.STATUSES}
cards = st.columns(len(followed))
for col, status in zip(cards, followed):
    with col:
        ui.metric_card(status.capitalize(), counts[status])

if not any(counts[s] for s in followed):
    with st.container(border=True):
        st.markdown("**Aucune candidature suivie pour l'instant**")
        st.caption("Dans la fiche d'une offre, choisis un statut (« intéressant », « postulé »…) : elle apparaîtra ici.")
        if st.button("Parcourir mes offres", type="primary", icon=":material/work:"):
            ui.go("offres")
else:
    columns = st.columns(len(followed))
    for col, status in zip(columns, followed):
        items = [r for r in rows if r["statut"] == status]
        with col:
            st.markdown(f"<div class='scout-section'>{status}</div>", unsafe_allow_html=True)
            if not items:
                st.caption("—")
            for r in items:
                with st.container(border=True):
                    st.markdown(f"**{r['titre']}**")
                    st.caption(f"{r['entreprise']} · {r['lieu'] or '?'}")
                    with st.container(horizontal=True):
                        st.badge(f"{r['score'] or 0:.0f}/100", color=ui.score_color(r["score"], r["éliminée"]))
                    if st.button("Fiche", key=f"cand_open_{r['job_id']}", icon=":material/arrow_forward:",
                                 width="stretch"):
                        ui.open_offer(r["job_id"])
st.caption(f"{counts[db.DEFAULT_STATUS]} offre(s) encore « à voir ».")

# ---------- Édition rapide ----------
st.markdown("#### Modifier les statuts et les notes")
show_all = st.toggle("Inclure les offres « à voir »", value=not any(counts[s] for s in followed))
editable = [r for r in rows if show_all or r["statut"] != db.DEFAULT_STATUS]
editable = [r for r in editable if not r["éliminée"] or r["statut"] != db.DEFAULT_STATUS]
if not editable:
    st.caption("Rien à modifier pour l'instant.")
    st.stop()
df = pd.DataFrame(editable)[["job_id", "titre", "entreprise", "score", "statut", "notes", "lien"]]
edited = st.data_editor(
    df, hide_index=True, width="stretch", disabled=["job_id", "titre", "entreprise", "score", "lien"],
    column_config={"job_id": None, "titre": st.column_config.TextColumn("Titre", width="large"),
                   "entreprise": "Entreprise",
                   "statut": st.column_config.SelectboxColumn("Statut", options=list(db.STATUSES), required=True),
                   "notes": st.column_config.TextColumn("Notes", width="large"),
                   "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f"),
                   "lien": st.column_config.LinkColumn("Lien", display_text="Ouvrir ↗")},
    key=f"apps_editor_{user}",
)
if st.button("Enregistrer les modifications", type="primary", icon=":material/save:"):
    changed = 0
    for before, after in zip(df.to_dict("records"), edited.to_dict("records")):
        if before["statut"] != after["statut"] or before["notes"] != after["notes"]:
            db.set_application(conn, after["job_id"], after["statut"], after["notes"] or "", user)
            changed += 1
    st.toast(f"{changed} offre(s) mise(s) à jour.")
    st.rerun()
