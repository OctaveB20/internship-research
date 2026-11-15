"""Offres : tableau filtrable trié par score + détail d'une offre."""
import pandas as pd
import streamlit as st

from src import db, offers, ui

conn, cfg, profile = ui.setup_page("Offres", "📋")
rows = offers.load_offers(conn)
if not rows:
    st.info("Aucune offre pour l'instant : lance une collecte dans la page Recherche.")
    st.stop()

with st.sidebar:
    st.header("Filtres")
    min_score = st.slider("Score minimum", 0, 100, 0, 5)
    countries = st.multiselect("Pays", sorted({r["pays"] for r in rows if r["pays"]}))
    max_duration = st.slider("Durée max (mois)", 1, 12, 12)
    use_start = st.checkbox("Début avant le…")
    start_before = st.date_input("Début avant", profile.latest_start, disabled=not use_start) if use_start else None
    modes = st.multiselect("Mode", sorted({r["mode"] for r in rows}))
    domains = st.multiselect("Domaine", sorted({r["domaine"] for r in rows if r["domaine"]}))
    hide_elim = st.checkbox("Masquer les éliminées", value=True)

shown = offers.filter_offers(rows, min_score=min_score, countries=countries,
                             max_duration=None if max_duration == 12 else max_duration,
                             start_before=start_before, modes=modes, domains=domains, hide_eliminated=hide_elim)
st.caption(f"{len(shown)} offre(s) affichée(s) sur {len(rows)} (doublons fusionnés).")
if not shown:
    st.warning("Aucune offre ne passe les filtres.")
    st.stop()

df = pd.DataFrame(shown)[["titre", "entreprise", "ville", "mode", "durée", "début", "score", "statut"]]
event = st.dataframe(
    df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
    column_config={"score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f"),
                   "début": st.column_config.DateColumn("début", format="DD/MM/YYYY")},
)
selected = event.selection.rows
if not selected:
    st.caption("Clique sur une ligne pour voir le détail.")
    st.stop()

# ---------- Détail ----------
offer = shown[selected[0]]
job_id = offer["job_id"]
extraction = db.get_extraction(conn, job_id) or {}
score = db.get_score(conn, job_id)
st.divider()
st.subheader(f"{offer['titre']} — {offer['entreprise']}")
left, right = st.columns([3, 2])
with left:
    if extraction.get("summary_fr"):
        st.write(extraction["summary_fr"])
    facts = [f"📍 {offer['ville'] or '?'}, {offer['pays'] or '?'} ({offer['mode']})",
             f"⏱️ {offer['durée'] or 'durée ?'}",
             f"📅 début {offer['début']:%d/%m/%Y}" if offer["début"] else "📅 début ?",
             f"💶 {offer['gratification']:.0f} €/mois" if offer["gratification"] else "💶 gratification ?"]
    st.write(" · ".join(facts))
    if score:
        bd = score["breakdown"]
        if bd["eliminated"]:
            st.error("Éliminée : " + "; ".join(bd["reasons"]))
        st.markdown(f"**Score : {score['score']:.0f}/100**"
                    + (f" (avant élimination : {bd.get('raw_score', 0):.0f}/100)" if bd["eliminated"] else ""))
        comp = pd.DataFrame([{"critère": k, "points": f"{v['points']:g} / {v['max']:g}", "détail": v["detail"]}
                             for k, v in bd["components"].items()])
        st.dataframe(comp, hide_index=True, width="stretch")
        c1, c2 = st.columns(2)
        c1.markdown("**Compétences que tu as** \n" + (", ".join(bd["skills_matched"]) or "—"))
        c2.markdown("**Compétences manquantes** \n" + (", ".join(bd["skills_missing"]) or "—"))
    langs = extraction.get("languages") or []
    if langs:
        st.caption("Langues : " + ", ".join(f"{l['lang']} ({l['level'] or 'niveau non précisé'})" for l in langs))
    with st.expander("Annonce complète"):
        st.text((db.get_job(conn, job_id) or {}).get("job_description") or "")
with right:
    if offer["lien"]:
        st.link_button("Postuler", offer["lien"], type="primary", width="stretch")
    st.caption(f"Source : {offer['source'] or '?'} · publiée le {offer['publiée'] or '?'}")
    app = db.get_application(conn, job_id)
    with st.form(f"app_{job_id}"):
        status = st.selectbox("Statut", db.STATUSES, index=db.STATUSES.index(app["status"]))
        notes = st.text_area("Notes", app["notes"], height=150)
        if st.form_submit_button("Enregistrer"):
            db.set_application(conn, job_id, status, notes)
            st.toast("Enregistré.")
            st.rerun()
