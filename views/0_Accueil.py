"""Accueil : questionnaire de profil, indicateurs et meilleures offres."""
import streamlit as st

from src import criteria, jsearch, offers, questionnaire as qz, ui

conn, cfg, profile = ui.setup_page("Accueil", ":material/target:", show_title=False)
user = ui.current_user(cfg)
answers = qz.load_answers(conn, user)

st.markdown("<div class='scout-kicker'>Internship Scout</div>", unsafe_allow_html=True)
st.title(f"Bonjour {user}")
st.caption("Collecte des offres → analyse par l'IA → score selon ton profil et tes critères → suivi des candidatures.")


# ---------- Questionnaire ----------
def questionnaire_form() -> None:
    """Compétences, langues et domaines de la personne (pré-remplis avec son profil actuel)."""
    current = set(profile.skills)
    known = {s for s in qz.ALL_SKILLS}
    with st.form(f"questionnaire_{user}", border=False):
        st.markdown("**1 · Tes compétences**")
        st.caption("Coche ce que tu sais vraiment utiliser : les offres qui demandent ces compétences gagnent des points.")
        picked: list[str] = []
        for group, skills in qz.SKILL_GROUPS.items():
            picked += st.pills(group, skills, selection_mode="multi",
                               default=[s for s in skills if s in current], key=f"qz_{user}_{group}") or []
        other_skills = st.text_input("Autres compétences (séparées par des virgules)",
                                     ", ".join(s for s in profile.skills if s not in known),
                                     placeholder="ex. Salesforce, Qlik, Bloomberg")

        st.markdown("**2 · Tes langues**")
        levels = list(qz.LEVELS)
        lang_cols = st.columns(3)
        langs = {}
        mine = {lang.lower(): lvl for lang, lvl in profile.languages.items()}
        for i, (lang, label) in enumerate(qz.LANGUAGES.items()):
            with lang_cols[i % 3]:
                langs[lang] = st.selectbox(label, levels, index=levels.index(qz.level_label(mine.get(lang.lower()))),
                                           key=f"qz_{user}_lang_{lang}")

        st.markdown("**3 · Les domaines qui t'intéressent**")
        domains = st.pills("Domaines", qz.DOMAINS, selection_mode="multi", label_visibility="collapsed",
                           default=[d for d in qz.DOMAINS if d in profile.domains], key=f"qz_{user}_domains") or []
        other_domains = st.text_input("Autres domaines", ", ".join(d for d in profile.domains if d not in qz.DOMAINS),
                                      placeholder="ex. private equity, finance durable")
        if st.form_submit_button("Enregistrer mon profil", type="primary", icon=":material/check:"):
            result = qz.from_form(picked, other_skills, langs, domains, other_domains)
            qz.save_answers(conn, user, result)
            st.session_state[f"edit_profile_{user}"] = False
            st.toast(f"Profil de {user} enregistré : les scores sont recalculés.")
            st.rerun()


editing = st.session_state.get(f"edit_profile_{user}", False)
if answers is None or editing:
    with st.container(border=True):
        st.markdown(f"#### {'Modifie ton profil' if answers else 'Complète ton profil · 2 minutes'}")
        if answers is None:
            st.caption("Quelques questions pour que le score reflète vraiment tes compétences. "
                       "Pré-rempli avec ton profil actuel : vérifie et corrige.")
        questionnaire_form()
        if answers is not None and st.button("Annuler"):
            st.session_state[f"edit_profile_{user}"] = False
            st.rerun()
else:
    with st.container(border=True):
        head, action = st.columns([5, 1], vertical_alignment="center")
        head.markdown("**Ton profil**")
        if action.button("Modifier", icon=":material/edit:", width="stretch"):
            st.session_state[f"edit_profile_{user}"] = True
            st.rerun()
        with st.container(horizontal=True):
            for skill in answers.skills:
                st.badge(skill, color="blue")
        langs = [f"{qz.LANGUAGES.get(l, l)} : {qz.level_label(v)}" for l, v in answers.languages.items()]
        st.caption("Langues — " + (" · ".join(langs) or "aucune") + "  |  Domaines — "
                   + (", ".join(answers.domains) or "non précisés"))

# ---------- Indicateurs ----------
q = jsearch.quota_status(conn, cfg.quota_soft_limit)
rows = offers.load_offers(conn, user)
kept = [r for r in rows if r["extraite"] and not r["éliminée"]]
c1, c2, c3, c4 = st.columns(4)
with c1:
    ui.metric_card("Offres retenues", len(kept), f"sur {len(rows)} trouvées")
with c2:
    ui.metric_card("Score ≥ 75", sum((r["score"] or 0) >= 75 for r in kept), "les plus compatibles")
with c3:
    ui.metric_card("Candidatures", sum(r["statut"] in ("postulé", "entretien", "offre") for r in rows),
                   "postulé · entretien · offre")
with c4:
    ui.metric_card("Requêtes JSearch", f"{q['used']} / {q['plan_limit']}", "ce mois-ci, partagées")

crit = criteria.load(conn, profile, cfg, user)
st.caption(f"Critères de {user} : " + ", ".join(crit.titles) + " · " + criteria.describe(crit))
with st.container(horizontal=True):
    if st.button("Mes offres", icon=":material/work:", type="primary"):
        ui.go("offres")
    if st.button("Nouvelle recherche", icon=":material/search:"):
        ui.go("recherche")
    if st.button("Candidatures", icon=":material/view_kanban:"):
        ui.go("candidatures")

# ---------- Meilleures offres ----------
st.markdown(f"#### Meilleures offres de {user}")
if not kept:
    st.info("Aucune offre retenue pour l'instant : lance une recherche depuis la page **Recherche**.")
for r in kept[:5]:
    with st.container(border=True):
        left, right = st.columns([5, 1], vertical_alignment="center")
        with left:
            st.markdown(f"**{r['titre']}**  \n{r['entreprise']} · {r['ville'] or '?'}, {r['pays'] or '?'}")
            with st.container(horizontal=True):
                for text in [r["mode"] if r["mode"] != "?" else "", r["durée"],
                             f"début {r['début']:%d/%m/%Y}" if r["début"] else "",
                             f"{r['gratification']:.0f} €/mois" if r["gratification"] else ""]:
                    if text:
                        st.badge(text, color="gray")
        with right:
            st.badge(f"{r['score']:.0f}/100", color=ui.score_color(r["score"]), icon=":material/star:")
            if st.button("Voir la fiche", key=f"home_open_{r['job_id']}", icon=":material/arrow_forward:",
                         width="stretch"):
                ui.open_offer(r["job_id"])
