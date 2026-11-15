"""Internship Scout : page d'accueil (les pages sont dans pages/)."""
import streamlit as st

from src import jsearch, offers, ui

conn, cfg, profile = ui.setup_page("Internship Scout")
st.caption("Collecte JSearch → extraction IA → score par rapport à ton profil → suivi des candidatures.")

q = jsearch.quota_status(conn, cfg.quota_soft_limit)
rows = offers.load_offers(conn)
kept = [r for r in rows if r["extraite"] and not r["éliminée"]]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Requêtes JSearch ce mois", f"{q['used']} / {q['plan_limit']}")
c2.metric("Offres en base", len(rows))
c3.metric("Offres retenues", len(kept))
c4.metric("Candidatures envoyées", sum(r["statut"] in ("postulé", "entretien", "offre") for r in rows))

if kept:
    st.subheader("Top 5")
    for r in kept[:5]:
        st.markdown(f"**{r['score']:.0f}/100** · {r['titre']} — {r['entreprise']} ({r['ville'] or '?'})")

st.markdown("Pages : **Recherche** (collecte), **Offres** (tableau et détail), **Chat**, **Candidatures** (suivi et export).")
