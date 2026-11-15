"""Recherche : collecte JSearch → extraction IA → scoring."""
import json

import pandas as pd
import streamlit as st

from src import config, extract, jsearch, llm, scoring, ui

conn, cfg, profile = ui.setup_page("Recherche", "🔎")

# ---------- Quota ----------
q = jsearch.quota_status(conn, cfg.quota_soft_limit)
c1, c2, c3 = st.columns(3)
c1.metric("Requêtes JSearch ce mois", f"{q['used']} / {q['plan_limit']}")
c2.metric("Plafond de l'app", q["soft_limit"], help="quota_soft_limit dans config.yaml")
c3.metric("Encore possibles", q["remaining"])
st.progress(min(q["used"] / q["plan_limit"], 1.0))

# ---------- Requêtes ----------
labels = {f"{x.query} ({x.country})": x for x in cfg.queries}
chosen = st.multiselect("Requêtes", list(labels), default=list(labels))
with st.expander("Ajouter une requête ponctuelle"):
    col1, col2 = st.columns([3, 1])
    extra_query = col1.text_input("Requête", placeholder="stage contrôle de gestion Lyon")
    extra_country = col2.text_input("Pays (code)", "fr", max_chars=2)
queries = [labels[c] for c in chosen]
if extra_query.strip():
    queries.append(config.SearchQuery(query=extra_query.strip(), country=extra_country.strip() or "fr"))

cost = 0 if config.dev_mode() else len(queries)
st.caption(f"Coût max : {cost} requête(s) JSearch (0 si déjà faites aujourd'hui : cache).")
launch = st.button("Lancer", type="primary", disabled=not queries)

if launch:
    with st.status("Collecte en cours…", expanded=True) as status:
        status.write("1/3 · Collecte des offres")
        col = jsearch.collect(conn, queries, cfg)
        for err in col.errors:
            st.error(err)
        status.write(f"{col.fetched} offres reçues, {col.new} nouvelles "
                     f"({col.api_calls} appel(s) JSearch, {col.cache_hits} depuis le cache).")

        status.write("2/3 · Extraction IA des nouvelles offres")
        bar = st.progress(0.0)
        ext = extract.extract_new_jobs(conn, cfg, progress=lambda i, n, t: bar.progress(i / n, text=f"{i}/{n} · {t[:60]}"))
        for err in ext.errors:
            st.warning(err)

        status.write("3/3 · Calcul des scores")
        n_scored = scoring.score_all(conn, profile)
        status.update(label="Terminé", state="complete" if not (col.errors or ext.errors) else "error")

    cost_usd = ext.usage.cost_usd(cfg)
    st.success(
        f"**{col.new} nouvelle(s) offre(s)**, {ext.extracted + ext.from_reference} extraite(s)"
        + (f" (dont {ext.from_reference} depuis la référence des fixtures)" if ext.from_reference else "")
        + (f", {ext.failed} échec(s)" if ext.failed else "")
        + f", {n_scored} offre(s) scorée(s). Tokens IA : {ext.usage.input_tokens:,} en entrée, "
        f"{ext.usage.output_tokens:,} en sortie · coût estimé : "
        + ("0 $ (offre gratuite)" if cost_usd == 0 else f"{cost_usd:.4f} $")
    )
    st.caption("Résultats dans la page **Offres** (menu de gauche).")

st.divider()
if st.button("Recalculer les scores", help="Après modification de profile.yaml. Gratuit, aucun appel."):
    st.toast(f"{scoring.score_all(conn, profile)} offre(s) rescorée(s).")

# ---------- Test réel de l'IA ----------
TEST_CACHE = config.CACHE_DIR / "llm_test.json"
with st.expander("Tester l'extraction IA sur 3 offres des fixtures (vrai appel)"):
    st.caption("Compare l'extraction de l'IA à la référence écrite à la main. Le résultat est gardé en cache : "
               "le bouton ne refait l'appel que si tu le demandes.")
    if st.button("Lancer le test (3 appels IA)", disabled=not llm.has_key(cfg)):
        reference = extract.load_reference()
        test_employers = ["Lumen Capital Partners", "Brennwald AG", "Maison Céleste"]  # EN, DE, FR (alternance)
        fixtures = {j["employer_name"]: j for j in jsearch.load_fixtures() if j["job_id"] in reference}
        picks = [fixtures[name] for name in test_employers]
        results, usage = [], llm.Usage()
        try:
            client = llm.make_client(cfg)
            for job in picks:
                ex, u, err = extract.extract_with_llm(job, client)
                usage.add(u)
                results.append({"job": f"{job['job_title']} — {job['employer_name']}", "error": err,
                                "extraction": ex.model_dump(mode="json") if ex else None,
                                "reference": reference[job["job_id"]]})
            TEST_CACHE.parent.mkdir(parents=True, exist_ok=True)
            TEST_CACHE.write_text(json.dumps({"model": cfg.model, "provider": cfg.llm_provider, "results": results,
                                              "tokens": [usage.input_tokens, usage.output_tokens]},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
        except llm.LLMError as e:
            st.error(str(e))
    if TEST_CACHE.exists():
        test = json.loads(TEST_CACHE.read_text(encoding="utf-8"))
        st.caption(f"{test['provider']} · {test['model']} · {test['tokens'][0]:,} tokens en entrée, "
                   f"{test['tokens'][1]:,} en sortie")
        for res in test["results"]:
            st.markdown(f"**{res['job']}**")
            if not res["extraction"]:
                st.error(f"Échec : {res['error']}")
                continue
            got = extract.JobExtraction.model_validate(res["extraction"])
            rows = extract.compare_with_reference(got, res["reference"])
            ok = sum(r["ok"] for r in rows)
            st.write(f"{ok}/{len(rows)} champs identiques à la référence · résumé : {got.summary_fr}")
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
