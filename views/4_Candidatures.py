"""Candidatures : pipeline par statut (cartes cliquables), édition rapide et export Excel."""
from datetime import date

import pandas as pd
import streamlit as st

from src import criteria, db, export, offers, ui

conn, cfg, profile = ui.setup_page("Candidatures", ":material/view_kanban:", show_title=False)
user = ui.current_user(cfg)
rows = offers.load_offers(conn, user)

head, add_col, export_col = st.columns([3, 2.1, 1.5], vertical_alignment="bottom")
head.title("Candidatures")

# ---------- Ajout manuel d'une candidature déjà envoyée ----------
COUNTRY_LABELS = {label: name for name, (_, label) in sorted(criteria.EUROPE.items(), key=lambda kv: kv[1][1])}
with add_col.popover("Ajouter une candidature", icon=":material/add:", width="stretch"):
    st.caption("Pour une candidature faite en dehors de l'app (site de l'entreprise, salon, réseau…).")
    with st.form(f"add_application_{user}", clear_on_submit=True, border=False):
        title = st.text_input("Intitulé du poste *", placeholder="Stage contrôle de gestion")
        employer = st.text_input("Entreprise *", placeholder="Nom de l'entreprise")
        c1, c2 = st.columns(2)
        city = c1.text_input("Ville", placeholder="Paris")
        country_label = c2.selectbox("Pays", ["—"] + list(COUNTRY_LABELS) + ["Autre"])
        link = st.text_input("Lien de l'offre", placeholder="https://…")
        c3, c4 = st.columns(2)
        applied_on = c3.date_input("Envoyée le", date.today(), format="DD/MM/YYYY")
        status = c4.selectbox("Statut", [s for s in db.STATUSES if s != db.DEFAULT_STATUS], index=1)  # postulé
        notes = st.text_area("Notes", placeholder="Contact, prochaine étape, date de relance…", height=80)
        if st.form_submit_button("Ajouter", type="primary", icon=":material/check:", width="stretch"):
            if not title.strip() or not employer.strip():
                st.error("L'intitulé et l'entreprise sont obligatoires.")
            else:
                db.add_manual_application(conn, user, title, employer, city=city,
                                          country=COUNTRY_LABELS.get(country_label, ""), link=link, status=status,
                                          notes=notes, applied_on=applied_on.isoformat())
                st.toast(f"Candidature ajoutée : {title} — {employer}")
                st.rerun()
if rows:
    export_col.download_button("Exporter en Excel", export.offers_to_excel(rows),
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
        st.caption("Dans la fiche d'une offre, choisis un statut (« intéressant », « postulé »…), ou ajoute une "
                   "candidature déjà envoyée avec « Ajouter une candidature » : elle apparaîtra ici.")
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
                manual = r["job_id"].startswith(db.MANUAL_PREFIX)
                with st.container(border=True):
                    st.markdown(f"**{r['titre']}**")
                    st.caption(f"{r['entreprise']} · {r['lieu'] or '?'}")
                    with st.container(horizontal=True):
                        if manual:
                            st.badge("ajout manuel", color="violet", icon=":material/edit_note:")
                        else:
                            st.badge(f"{r['score'] or 0:.0f}/100", color=ui.score_color(r["score"], r["éliminée"]))
                        if manual and r["publiée"]:
                            st.badge(f"envoyée le {r['publiée'][8:10]}/{r['publiée'][5:7]}", color="gray")
                    if st.button("Fiche", key=f"cand_open_{r['job_id']}", icon=":material/arrow_forward:",
                                 width="stretch"):
                        ui.open_offer(r["job_id"])
                    if manual:
                        with st.popover("Supprimer", icon=":material/delete:", width="stretch"):
                            st.caption("Supprimer définitivement cette candidature ajoutée à la main ?")
                            if st.button("Oui, supprimer", key=f"del_{r['job_id']}", type="primary"):
                                db.delete_manual_application(conn, r["job_id"])
                                st.toast("Candidature supprimée.")
                                st.rerun()
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
