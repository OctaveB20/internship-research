"""Offres : tableau filtrable trié par score, avec lien et critères extraits, + fiche détaillée."""
import json

import pandas as pd
import streamlit as st

from src import db, extract, llm, offers, scoring, ui

conn, cfg, profile = ui.setup_page("Offres", ":material/work:", "Triées par score. Clique sur une ligne pour voir la fiche.")
user = ui.current_user(cfg)
rows = offers.load_offers(conn, user)
if not rows:
    st.info(f"Aucune offre pour {user} : lance une recherche pour {user} dans la page Recherche.")
    st.stop()

COLUMNS = {  # clé interne → (titre affiché, configuration)
    "score": ("Score", st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f", width="small")),
    "titre": ("Titre", st.column_config.TextColumn("Titre", width="large")),
    "entreprise": ("Entreprise", st.column_config.TextColumn("Entreprise", width="medium")),
    "ville": ("Ville", None), "pays": ("Pays", None), "mode": ("Mode", None), "contrat": ("Contrat", None),
    "durée": ("Durée", None),
    "début": ("Début", st.column_config.DateColumn("Début", format="DD/MM/YYYY")),
    "gratification": ("Gratification", st.column_config.NumberColumn("Gratification", format="%.0f €")),
    "domaine": ("Domaine", None), "compétences": ("Compétences", st.column_config.TextColumn("Compétences", width="medium")),
    "langues": ("Langues", None), "niveau": ("Niveau", None), "statut": ("Statut", None),
    "publiée": ("Publiée", None), "source": ("Source", None),
    "lien": ("Lien", st.column_config.LinkColumn("Lien", display_text="Ouvrir ↗", width="small")),
    "raisons": ("Éliminée car", None),
}
DEFAULT_COLUMNS = ["score", "titre", "entreprise", "ville", "pays", "mode", "durée", "début", "gratification",
                   "compétences", "langues", "statut", "lien"]

# ---------- Filtres ----------
with st.sidebar:
    st.subheader("Filtres")
    min_score = st.slider("Score minimum", 0, 100, 0, 5)
    countries = st.multiselect("Pays", sorted({r["pays"] for r in rows if r["pays"]}), placeholder="Tous")
    max_duration = st.slider("Durée max (mois)", 1, 12, 12)
    use_start = st.checkbox("Début avant le…")
    start_before = st.date_input("Début avant", profile.latest_start, disabled=not use_start) if use_start else None
    modes = st.multiselect("Mode", sorted({r["mode"] for r in rows}), placeholder="Tous")
    domains = st.multiselect("Domaine", sorted({r["domaine"] for r in rows if r["domaine"]}), placeholder="Tous")
    hide_elim = st.checkbox("Masquer les éliminées", value=True)
    st.subheader("Affichage")
    columns = st.multiselect("Colonnes du tableau", list(COLUMNS), default=DEFAULT_COLUMNS,
                             format_func=lambda c: COLUMNS[c][0], placeholder="Choisir")

shown = offers.filter_offers(rows, min_score=min_score, countries=countries,
                             max_duration=None if max_duration == 12 else max_duration,
                             start_before=start_before, modes=modes, domains=domains, hide_eliminated=hide_elim)

kept = [r for r in rows if r["extraite"] and not r["éliminée"]]
m1, m2, m3, m4 = st.columns(4)
with m1:
    ui.metric_card("Affichées", f"{len(shown)} / {len(rows)}", "Doublons fusionnés.")
with m2:
    ui.metric_card("Retenues", len(kept), "Extraites et non éliminées par tes critères.")
with m3:
    ui.metric_card("Score ≥ 75", sum((r["score"] or 0) >= 75 for r in kept))
with m4:
    ui.metric_card("Avec gratification", sum(bool(r["gratification"]) for r in kept))

# ---------- Offres masquées ou en attente ----------
n_elim = sum(r["éliminée"] for r in rows)
pending = [r for r in rows if not r["extraite"]]
notes = []
if hide_elim and n_elim:
    notes.append(f"{n_elim} offre(s) éliminée(s) par tes critères sont masquées "
                 "(décoche « Masquer les éliminées » pour les voir avec la raison)")
if pending:
    notes.append(f"{len(pending)} offre(s) pas encore analysée(s) par l'IA (sans score)")
if notes:
    info, action = st.columns([4, 1], vertical_alignment="center")
    info.info(" · ".join(notes) + ".")
    if pending and action.button("Analyser maintenant", icon=":material/auto_awesome:", disabled=not llm.has_key(cfg),
                                 help="Lance l'extraction IA sur les offres en attente, puis recalcule les scores."):
        with st.spinner("Analyse des offres en attente…"):
            summary = extract.extract_new_jobs(conn, cfg)
            scoring.score_all(conn, profile, user)
        for err in summary.errors:
            st.warning(err)
        st.toast(f"{summary.extracted + summary.from_reference} offre(s) analysée(s).")
        st.rerun()

if not shown:
    st.warning("Aucune offre ne passe les filtres.")
    st.stop()

columns = columns or DEFAULT_COLUMNS
df = pd.DataFrame(shown)[columns]
for col in df.columns:  # cases vides plutôt que « None » ou « ? »
    if df[col].dtype == object and col not in ("début",):
        df[col] = df[col].map(lambda v: "—" if v in (None, "", "?") else v)
event = st.dataframe(
    df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
    height=min(36 * (len(df) + 1) + 3, 460),
    column_config={c: (COLUMNS[c][1] or COLUMNS[c][0]) for c in columns},
)
selected = event.selection.rows
offer = shown[selected[0]] if selected else shown[0]
if not selected:
    st.caption("Fiche de la 1re offre ci-dessous. Clique sur une ligne du tableau pour en voir une autre.")

# ---------- Fiche ----------
job_id = offer["job_id"]
job = db.get_job(conn, job_id) or {}
raw = json.loads(job.get("raw_json") or "{}")
extraction = db.get_extraction(conn, job_id) or {}
score = db.get_score(conn, job_id, user)
bd = score["breakdown"] if score else {}

with st.container(border=True):
    head, actions = st.columns([3, 1], vertical_alignment="center")
    with head:
        st.subheader(offer["titre"] or "Sans titre")
        st.markdown(f"**{offer['entreprise'] or '?'}** · {offer['ville'] or 'ville ?'}, {offer['pays'] or 'pays ?'}")
        with st.container(horizontal=True):
            if score:
                st.badge(f"{score['score']:.0f}/100" if not bd.get("eliminated") else "éliminée",
                         color=ui.score_color(score["score"], bd.get("eliminated")), icon=":material/star:")
            if offer["mode"] != "?":
                st.badge(offer["mode"], color="gray", icon=":material/home_work:")
            if offer["contrat"]:
                st.badge(offer["contrat"], color="gray", icon=":material/badge:")
            if offer["durée"]:
                st.badge(offer["durée"], color="gray", icon=":material/schedule:")
            if offer["début"]:
                st.badge(f"début {offer['début']:%d/%m/%Y}", color="gray", icon=":material/event:")
            if offer["gratification"]:
                st.badge(f"{offer['gratification']:.0f} €/mois", color="gray", icon=":material/euro:")
            st.badge(offer["statut"], color="blue", icon=":material/flag:")
    with actions:
        if offer["lien"]:
            st.link_button("Postuler", offer["lien"], type="primary", icon=":material/open_in_new:", width="stretch")
        google = raw.get("job_google_link")
        if google and google != offer["lien"]:
            st.link_button("Voir sur Google Jobs", google, icon=":material/search:", width="stretch")
    if offer["lien"]:
        st.caption(f"Lien de l'offre : {offer['lien']}")
    if bd.get("eliminated"):
        st.error("Éliminée : " + "; ".join(bd["reasons"]) + f" · score avant élimination : {bd.get('raw_score', 0):.0f}/100")
    if offer["résumé"]:
        st.write(offer["résumé"])

left, right = st.columns([3, 2])
with left:
    with st.container(border=True):
        st.markdown("**Critères relevés dans l'annonce**")
        c1, c2, c3 = st.columns(3)
        with c1:
            ui.field("Lieu", ", ".join(x for x in [offer["ville"], offer["pays"]] if x))
            ui.field("Mode de travail", offer["mode"] if offer["mode"] != "?" else "")
            ui.field("Type de contrat", offer["contrat"])
            ui.field("Convention de stage", offer["convention"])
        with c2:
            ui.field("Durée", offer["durée"])
            ui.field("Début", (f"{offer['début']:%d/%m/%Y}" if offer["début"] else "")
                     + (f" ({offer['début_texte']})" if offer["début_texte"] else ""))
            ui.field("Gratification", f"{offer['gratification']:.0f} €/mois" if offer["gratification"] else "")
            ui.field("Niveau d'études", offer["niveau"])
        with c3:
            ui.field("Domaine", offer["domaine"])
            ui.field("Langues", offer["langues"])
            ui.field("Source", offer["source"])
            ui.field("Publiée le", offer["publiée"])

    with st.container(border=True):
        st.markdown("**Compétences**")
        matched = {s.lower() for s in bd.get("skills_matched", [])}
        required = extraction.get("required_skills") or []
        nice = extraction.get("nice_to_have_skills") or []
        if required:
            st.caption("Exigées : vert = tu l'as, rouge = à travailler")
            with st.container(horizontal=True):
                for skill in required:
                    ok = skill.lower() in matched
                    st.badge(skill, color="green" if ok else "red", icon=":material/check:" if ok else ":material/close:")
        if nice:
            st.caption("Atouts appréciés")
            with st.container(horizontal=True):
                for skill in nice:
                    st.badge(skill, color="green" if skill.lower() in matched else "gray")
        if not (required or nice):
            st.caption("Aucune compétence listée dans l'annonce.")

    with st.expander("Annonce complète"):
        st.text(job.get("job_description") or "")

with right:
    if bd.get("components"):
        with st.container(border=True):
            st.markdown("**Détail du score**")
            labels = {"location": "Lieu", "duration": "Durée", "start_date": "Date de début", "skills": "Compétences",
                      "languages": "Langues", "domain": "Domaine", "stipend": "Gratification"}
            for key, comp in bd["components"].items():
                st.progress(comp["ratio"], text=f"{labels.get(key, key)} · {comp['points']:g}/{comp['max']:g}")
                st.caption(comp["detail"])

    with st.container(border=True):
        st.markdown(f"**Suivi de candidature · {user}**")
        app = db.get_application(conn, job_id, user)
        with st.form(f"app_{job_id}", border=False):
            status = st.selectbox("Statut", db.STATUSES, index=db.STATUSES.index(app["status"]))
            notes = st.text_area("Notes", app["notes"], height=120, placeholder="Contact, date de relance…")
            if st.form_submit_button("Enregistrer", type="primary"):
                db.set_application(conn, job_id, status, notes, user)
                st.toast("Enregistré.")
                st.rerun()
