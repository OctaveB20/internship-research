"""Offres : recherche rapide, vues, filtres, tableau compact trié par score, fiche détaillée avec navigation."""
import json

import pandas as pd
import streamlit as st

from src import db, extract, llm, offers, scoring, ui

conn, cfg, profile = ui.setup_page("Offres", ":material/work:", show_title=False)
user = ui.current_user(cfg)
rows = offers.load_offers(conn, user)

st.title("Offres")
if not rows:
    st.info(f"Aucune offre pour {user} pour l'instant.")
    if st.button("Lancer une recherche", type="primary", icon=":material/search:"):
        ui.go("recherche")
    st.stop()

COLUMNS = {  # clé interne → configuration de colonne (largeurs en pixels pour tenir sans défilement)
    "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f", width=90),
    "titre": st.column_config.TextColumn("Titre", width=250),
    "entreprise": st.column_config.TextColumn("Entreprise", width=140),
    "lieu": st.column_config.TextColumn("Lieu", width=135),
    "période": st.column_config.TextColumn("Période", width=115, help="Durée · mois de début"),
    "mode": st.column_config.TextColumn("Mode", width=75),
    "contrat": st.column_config.TextColumn("Contrat", width=85),
    "durée": st.column_config.TextColumn("Durée", width=75),
    "début": st.column_config.DateColumn("Début", format="DD/MM/YYYY", width=90),
    "gratification": st.column_config.NumberColumn("Gratif.", format="%.0f €", width=70),
    "domaine": st.column_config.TextColumn("Domaine", width=130),
    "compétences": st.column_config.TextColumn("Compétences", width=160),
    "langues": st.column_config.TextColumn("Langues", width=140),
    "statut": st.column_config.TextColumn("Statut", width=90),
    "publiée": st.column_config.TextColumn("Publiée", width=90),
    "source": st.column_config.TextColumn("Source", width=100),
    "raisons": st.column_config.TextColumn("Éliminée car", width=180),
    "lien": st.column_config.LinkColumn("Lien", display_text="Ouvrir ↗", width=75),
}
DEFAULT_COLUMNS = ["score", "titre", "entreprise", "lieu", "période", "statut", "lien"]
LABELS = {key: conf["label"] for key, conf in COLUMNS.items()}

# ---------- Indicateurs ----------
kept = [r for r in rows if r["extraite"] and not r["éliminée"]]
pending = [r for r in rows if not r["extraite"]]
new_ids = set(st.session_state.get(f"last_new_{user}", []))
m1, m2, m3, m4 = st.columns(4)
with m1:
    ui.metric_card("Retenues", len(kept), f"sur {len(rows)} trouvées")
with m2:
    ui.metric_card("Score ≥ 75", sum((r["score"] or 0) >= 75 for r in kept), "les plus compatibles")
with m3:
    ui.metric_card("Suivies", sum(r["statut"] != db.DEFAULT_STATUS for r in rows), "statut autre que « à voir »")
with m4:
    ui.metric_card("Avec gratification", sum(bool(r["gratification"]) for r in kept), "montant indiqué")

# ---------- Barre d'outils : recherche, vue, filtres, colonnes ----------
VIEWS = ["Retenues"] + (["Nouvelles"] if new_ids else []) + ["À voir", "Suivies", "Éliminées", "Toutes"]
search_col, filters_col, columns_col = st.columns([5.4, 1.3, 1.5], vertical_alignment="bottom")
query = search_col.text_input("Rechercher", placeholder="Titre, entreprise, ville, compétence…",
                              label_visibility="collapsed", key=f"offers_query_{user}")
with filters_col.popover("Filtres", icon=":material/tune:", width="stretch"):
    min_score = st.slider("Score minimum", 0, 100, 0, 5, key="f_min_score")
    countries = st.multiselect("Pays", sorted({r["pays"] for r in rows if r["pays"]}), placeholder="Tous",
                               key="f_countries")
    modes = st.multiselect("Mode", sorted({r["mode"] for r in rows if r["mode"] != "?"}), placeholder="Tous",
                           key="f_modes")
    domains = st.multiselect("Domaine", sorted({r["domaine"] for r in rows if r["domaine"]}), placeholder="Tous",
                             key="f_domains")
    max_duration = st.slider("Durée max (mois)", 1, 12, 12, key="f_max_duration")
    use_start = st.checkbox("Début avant le…", key="f_use_start")
    start_before = st.date_input("Début avant", profile.latest_start, key="f_start_before") if use_start else None
with columns_col.popover("Colonnes", icon=":material/view_column:", width="stretch"):
    columns = st.multiselect("Colonnes affichées", list(COLUMNS), default=DEFAULT_COLUMNS, key="f_columns",
                             format_func=LABELS.get, placeholder="Choisir") or DEFAULT_COLUMNS
default_view = "Nouvelles" if st.session_state.pop("offers_view_new", False) and new_ids else "Retenues"
view = st.segmented_control("Vue", VIEWS, default=default_view, label_visibility="collapsed",
                            key=f"offers_view_{user}_{'n' if new_ids else ''}") or "Retenues"

# ---------- Filtrage ----------
base = {"Retenues": kept, "Nouvelles": [r for r in rows if r["job_id"] in new_ids],
        "À voir": [r for r in rows if r["statut"] == db.DEFAULT_STATUS and not r["éliminée"]],
        "Suivies": [r for r in rows if r["statut"] != db.DEFAULT_STATUS],
        "Éliminées": [r for r in rows if r["éliminée"]], "Toutes": rows}[view]
shown = offers.filter_offers(base, min_score=min_score, countries=countries,
                             max_duration=None if max_duration == 12 else max_duration, start_before=start_before,
                             modes=modes, domains=domains, hide_eliminated=False)
if query.strip():  # sans accents ni majuscules ; « Munich » trouve « München »
    words = [scoring.canonical_city(w) for w in db.normalize_text(query).split()]

    def haystack(r: dict) -> str:
        text = db.normalize_text(" ".join(str(r[k] or "") for k in ("titre", "entreprise", "lieu", "compétences",
                                                                     "domaine")))
        return f"{text} {' '.join(scoring.canonical_city(w) for w in text.split())}"

    shown = [r for r in shown if all(w in haystack(r) for w in words)]

notes = []
hidden = sum(r["éliminée"] for r in rows)
if view == "Retenues" and hidden:
    notes.append(f"{hidden} offre(s) éliminée(s) par tes critères (vue « Éliminées » pour voir pourquoi)")
if pending:
    notes.append(f"{len(pending)} offre(s) pas encore analysée(s) par l'IA")
info_col, action_col = st.columns([5, 1.4], vertical_alignment="center")
info_col.caption(f"**{len(shown)}** offre(s) affichée(s)" + (" · " + " · ".join(notes) if notes else ""))
if pending and action_col.button("Analyser maintenant", icon=":material/auto_awesome:", disabled=not llm.has_key(cfg),
                                 width="stretch", help="Extraction IA des offres en attente, puis recalcul des scores."):
    with st.spinner("Analyse des offres en attente…"):
        summary = extract.extract_new_jobs(conn, cfg)
        scoring.score_all(conn, profile, user)
    for err in summary.errors:
        st.warning(err)
    st.toast(f"{summary.extracted + summary.from_reference} offre(s) analysée(s)"
             + (f", {summary.skipped} écartée(s) sans IA" if summary.skipped else "") + ".")
    st.rerun()

if not shown:
    st.info("Aucune offre ne correspond : change de vue, efface la recherche ou élargis les filtres.")
    st.stop()

# ---------- Tableau ----------
df = pd.DataFrame(shown)[columns]
for col in df.columns:  # cases vides plutôt que « None » ou « ? » (texte : type object ou str selon pandas)
    if col != "début" and (df[col].dtype == object or pd.api.types.is_string_dtype(df[col])):
        df[col] = df[col].map(lambda v: "—" if v is None or (isinstance(v, float) and pd.isna(v)) or v in ("", "?")
                              else v)
event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                     height=min(35 * (len(df) + 1) + 3, 420), column_config={c: COLUMNS[c] for c in columns},
                     key=f"offers_table_{user}_{view}")

# Offre affichée : clic dans le tableau, lien depuis une autre page, ou boutons précédente / suivante.
ids = [r["job_id"] for r in shown]
sel_key = f"offer_selected_{user}"
if st.session_state.get("focus_job"):
    st.session_state[sel_key] = st.session_state.pop("focus_job")
if event.selection.rows:
    picked = ids[event.selection.rows[0]]
    if st.session_state.get(f"{sel_key}_last_click") != picked:
        st.session_state[sel_key] = picked
        st.session_state[f"{sel_key}_last_click"] = picked
current = st.session_state.get(sel_key)
if current not in ids:  # offre d'une autre page ou plus dans la vue : on cherche dans toutes, sinon la 1re
    current = current if current in {r["job_id"] for r in rows} else ids[0]
by_id = {r["job_id"]: r for r in rows}
offer = by_id[current]
position = ids.index(current) if current in ids else None


def move(step: int) -> None:
    st.session_state[sel_key] = ids[(position + step) % len(ids)] if position is not None else ids[0]


# ---------- Fiche ----------
job_id = offer["job_id"]
job = db.get_job(conn, job_id) or {}
raw = json.loads(job.get("raw_json") or "{}")
extraction = db.get_extraction(conn, job_id) or {}
score = db.get_score(conn, job_id, user)
bd = score["breakdown"] if score else {}

nav_left, nav_mid, nav_right = st.columns([1, 4, 1], vertical_alignment="center")
nav_left.button("Précédente", icon=":material/chevron_left:", on_click=move, args=(-1,), width="stretch",
                disabled=len(ids) < 2)
nav_mid.markdown(f"<div style='text-align:center;opacity:.6;font-size:.85rem'>"
                 f"{'Offre ' + str(position + 1) + ' sur ' + str(len(ids)) if position is not None else 'Offre hors de la vue actuelle'}"
                 f"</div>", unsafe_allow_html=True)
nav_right.button("Suivante", icon=":material/chevron_right:", on_click=move, args=(1,), width="stretch",
                 disabled=len(ids) < 2)

with st.container(border=True):
    head, actions = st.columns([3, 1], vertical_alignment="top")
    with head:
        st.subheader(offer["titre"] or "Sans titre")
        st.markdown(f"**{offer['entreprise'] or '?'}** · {offer['lieu'] or 'lieu non précisé'}")
        with st.container(horizontal=True):
            if score:
                st.badge(f"{score['score']:.0f}/100" if not bd.get("eliminated") else "éliminée",
                         color=ui.score_color(score["score"], bd.get("eliminated")), icon=":material/star:")
            for text, icon in [(offer["mode"] if offer["mode"] != "?" else "", ":material/home_work:"),
                               (offer["contrat"], ":material/badge:"), (offer["durée"], ":material/schedule:"),
                               (f"début {offer['début']:%d/%m/%Y}" if offer["début"] else "", ":material/event:"),
                               (f"{offer['gratification']:.0f} €/mois" if offer["gratification"] else "",
                                ":material/euro:")]:
                if text:
                    st.badge(text, color="gray", icon=icon)
        if offer["résumé"]:
            st.write(offer["résumé"])
    with actions:
        if offer["lien"]:
            st.link_button("Postuler", offer["lien"], type="primary", icon=":material/open_in_new:", width="stretch")
        google = raw.get("job_google_link")
        if google and google != offer["lien"]:
            st.link_button("Google Jobs", google, icon=":material/search:", width="stretch")
        if llm.has_key(cfg) and st.button("Demander à l'assistant", icon=":material/chat:", width="stretch",
                                          key=f"ask_{job_id}"):
            ui.ask_assistant(f"Analyse l'offre {job_id} ({offer['titre']} — {offer['entreprise']}) : est-elle adaptée "
                             f"à mon profil, quels points mettre en avant dans ma candidature et que me manque-t-il ?")
    if bd.get("eliminated"):
        st.error("Éliminée : " + "; ".join(bd["reasons"]) + f" · score avant élimination : {bd.get('raw_score', 0):.0f}/100")

    # Statut : un clic suffit (enregistré tout de suite)
    status_key = f"status_{user}_{job_id}"

    def save_status() -> None:
        new = st.session_state.get(status_key)
        if new:
            db.set_application(conn, job_id, new, None, user)
            st.toast(f"Statut : {new}")

    st.markdown("<div class='scout-section'>Statut de ta candidature</div>", unsafe_allow_html=True)
    st.segmented_control("Statut", list(db.STATUSES), default=offer["statut"], key=status_key,
                         on_change=save_status, label_visibility="collapsed")

left, right = st.columns([3, 2])
with left:
    with st.container(border=True):
        st.markdown("**Critères relevés dans l'annonce**")
        c1, c2, c3 = st.columns(3)
        with c1:
            ui.field("Lieu", offer["lieu"])
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
            names = {"location": "Lieu", "duration": "Durée", "start_date": "Date de début", "skills": "Compétences",
                     "languages": "Langues", "domain": "Domaine", "stipend": "Gratification"}
            for key, comp in bd["components"].items():
                st.progress(comp["ratio"], text=f"{names.get(key, key)} · {comp['points']:g}/{comp['max']:g}")
                st.caption(comp["detail"])

    with st.container(border=True):
        st.markdown("**Notes**")
        app = db.get_application(conn, job_id, user)
        with st.form(f"notes_{user}_{job_id}", border=False):
            notes_text = st.text_area("Notes", app["notes"], height=110, label_visibility="collapsed",
                                      placeholder="Contact, date de relance, questions à poser…")
            if st.form_submit_button("Enregistrer les notes", icon=":material/save:"):
                db.set_application(conn, job_id, app["status"], notes_text, user)
                st.toast("Notes enregistrées.")
                st.rerun()
        if app.get("updated_at"):
            st.caption(f"Dernière mise à jour : {str(app['updated_at'])[:16].replace('T', ' à ')}")
