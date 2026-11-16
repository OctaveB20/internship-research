"""Accueil : indicateurs, critères actuels et meilleures offres."""
import streamlit as st

from src import criteria, jsearch, offers, ui

conn, cfg, profile = ui.setup_page("Internship Scout", ":material/target:",
                                   "Collecte des offres → extraction IA → score selon tes critères → suivi des candidatures.")

q = jsearch.quota_status(conn, cfg.quota_soft_limit)
user = ui.current_user(cfg)
rows = offers.load_offers(conn, user)
kept = [r for r in rows if r["extraite"] and not r["éliminée"]]
c1, c2, c3, c4 = st.columns(4)
with c1:
    ui.metric_card("Requêtes JSearch ce mois", f"{q['used']} / {q['plan_limit']}")
with c2:
    ui.metric_card("Offres en base", len(rows))
with c3:
    ui.metric_card("Offres retenues", len(kept), "Non éliminées par tes critères.")
with c4:
    ui.metric_card("Candidatures envoyées", sum(r["statut"] in ("postulé", "entretien", "offre") for r in rows))

crit = criteria.load(conn, profile, cfg, user)
st.caption(f"Critères de {user} : " + ", ".join(crit.titles) + " · " + criteria.describe(crit))

st.subheader(f"Meilleures offres de {user}")
if not kept:
    st.info("Aucune offre retenue pour l'instant : lance une recherche dans la page **Recherche**.")
for r in kept[:5]:
    with st.container(border=True):
        a, b = st.columns([5, 1], vertical_alignment="center")
        with a:
            st.markdown(f"**{r['titre']}** · {r['entreprise']}")
            st.caption(" · ".join(x for x in [f"{r['ville'] or '?'}, {r['pays'] or '?'}", r["durée"],
                                              f"début {r['début']:%d/%m/%Y}" if r["début"] else "",
                                              f"{r['gratification']:.0f} €/mois" if r["gratification"] else ""] if x))
        with b:
            st.badge(f"{r['score']:.0f}/100", color=ui.score_color(r["score"]))
            if r["lien"]:
                st.link_button("Ouvrir", r["lien"], icon=":material/open_in_new:")
