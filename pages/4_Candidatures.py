"""Candidatures : suivi par statut + export Excel."""
from datetime import date

import pandas as pd
import streamlit as st

from src import db, export, offers, ui

conn, cfg, profile = ui.setup_page("Candidatures", "🗂️")
rows = offers.load_offers(conn)
if not rows:
    st.info("Aucune offre pour l'instant : lance une collecte dans la page Recherche.")
    st.stop()

st.download_button("Exporter toutes les offres (Excel)", export.offers_to_excel(rows),
                   f"offres_stage_{date.today():%Y-%m-%d}.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ---------- Kanban ----------
followed = [s for s in db.STATUSES if s != db.DEFAULT_STATUS]
cols = st.columns(len(followed))
for col, status in zip(cols, followed):
    items = [r for r in rows if r["statut"] == status]
    col.markdown(f"**{status.capitalize()}** ({len(items)})")
    for r in items:
        col.markdown(f"<div style='border:1px solid #8884;border-radius:8px;padding:6px;margin-bottom:6px;"
                     f"font-size:0.85em'><b>{r['titre']}</b><br>{r['entreprise']} · {r['score'] or 0:.0f}/100</div>",
                     unsafe_allow_html=True)
st.caption(f"{sum(r['statut'] == db.DEFAULT_STATUS for r in rows)} offre(s) encore « à voir ».")

# ---------- Édition rapide ----------
st.subheader("Modifier les statuts et les notes")
show_all = st.checkbox("Inclure les offres « à voir » et les éliminées")
editable = [r for r in rows if show_all or r["statut"] != db.DEFAULT_STATUS]
if not editable:
    st.caption("Aucune candidature suivie : passe une offre en « intéressant » (ici ou dans la page Offres).")
    editable = [r for r in rows if not r["éliminée"]]
df = pd.DataFrame(editable)[["job_id", "titre", "entreprise", "score", "statut", "notes"]]
edited = st.data_editor(
    df, hide_index=True, width="stretch", disabled=["job_id", "titre", "entreprise", "score"],
    column_config={"job_id": None, "statut": st.column_config.SelectboxColumn("statut", options=list(db.STATUSES), required=True),
                   "notes": st.column_config.TextColumn("notes", width="large"),
                   "score": st.column_config.NumberColumn("score", format="%.0f")},
    key="apps_editor",
)
if st.button("Enregistrer les modifications", type="primary"):
    changed = 0
    for before, after in zip(df.to_dict("records"), edited.to_dict("records")):
        if before["statut"] != after["statut"] or before["notes"] != after["notes"]:
            db.set_application(conn, after["job_id"], after["statut"], after["notes"] or "")
            changed += 1
    st.toast(f"{changed} offre(s) mise(s) à jour.")
    st.rerun()
