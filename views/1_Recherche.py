"""Recherche : critères (intitulés, pays, mois de début, durées…) → collecte JSearch → extraction IA → scoring."""
import json
from datetime import date

import pandas as pd
import streamlit as st

from src import config, criteria as crit, extract, jsearch, llm, scoring, ui

conn, cfg, _ = ui.setup_page("Recherche", ":material/search:",
                              "Choisis tes critères, puis lance la collecte, l'extraction IA et le calcul des scores.")
base_profile = config.load_profile()
user = ui.current_user(cfg)
saved = crit.load(conn, base_profile, cfg, user)
months = crit.upcoming_months(date.today(), 15)

# État des cases : repris des critères enregistrés quand il manque (1re visite ou retour sur la page,
# Streamlit oubliant l'état des cases d'une page quittée).
defaults = {"crit_titles": ", ".join(saved.titles), "crit_cities": ", ".join(saved.cities),
            "crit_remote": saved.remote_ok, "crit_posted": saved.date_posted, "crit_pages": saved.pages,
            "crit_stipend": int(saved.min_stipend_eur or 0), "crit_exclude": ", ".join(saved.exclude_words),
            "crit_intern_only": saved.internship_only}
defaults |= {f"c_{n}": n in saved.countries for n in crit.EUROPE}
defaults |= {f"m_{ym}": ym in saved.start_months for ym in months}
defaults |= {f"d_{d}": d in saved.durations for d in crit.DURATIONS}
if st.session_state.get("crit_user") != user:  # changement d'utilisateur : on recharge ses critères
    for key in defaults:
        st.session_state.pop(key, None)
    st.session_state.crit_user = user
for key, value in defaults.items():
    st.session_state.setdefault(key, value)


def set_all(prefix: str, keys, value: bool) -> None:
    for k in keys:
        st.session_state[f"{prefix}{k}"] = value


# ---------- Intitulé ----------
st.text_input("Intitulé(s) recherché(s)", key="crit_titles",
              placeholder="stage contrôle de gestion, Praktikum Controlling, finance intern",
              help="Plusieurs intitulés : sépare-les par des virgules. Astuce : ajoute l'intitulé dans la langue "
                   "du pays (Praktikum… pour l'Allemagne, stage… pour la France).")

# ---------- Pays ----------
box_countries = st.container(border=True)
box_countries.markdown("**Pays**")
row = box_countries.container(horizontal=True)
row.button("Toute l'Europe", on_click=set_all, args=("c_", crit.EUROPE, True))
row.button("Aucun", on_click=set_all, args=("c_", crit.EUROPE, False))
row.button("France + Allemagne", on_click=lambda: (set_all("c_", crit.EUROPE, False),
                                                  set_all("c_", ["France", "Germany"], True)))
cols = box_countries.columns(6)
for i, (name, (code, label)) in enumerate(sorted(crit.EUROPE.items(), key=lambda kv: kv[1][1])):
    cols[i % 6].checkbox(label, key=f"c_{name}")

# ---------- Mois de début et durée ----------
left, right = st.columns([5, 3])
with left.container(border=True):
    st.markdown("**Mois de début**")
    row = st.container(horizontal=True)
    row.button("Tous les mois", on_click=set_all, args=("m_", months, True))
    row.button("Aucun mois", on_click=set_all, args=("m_", months, False), help="Aucun = pas de contrainte")
    mcols = st.columns(4)
    for i, ym in enumerate(months):
        mcols[i % 4].checkbox(crit.month_label(ym), key=f"m_{ym}")
with right.container(border=True):
    st.markdown("**Durée du stage**")
    dcols = st.columns(3)
    for i, d in enumerate(crit.DURATIONS):
        dcols[i % 3].checkbox(f"{d} mois", key=f"d_{d}")
    st.caption("Aucune case = toutes durées.")

# ---------- Autres critères ----------
with st.expander("Autres critères", expanded=False):
    o1, o2, o3 = st.columns(3)
    o1.text_input("Villes préférées (bonus)", key="crit_cities", placeholder="Paris, Munich",
                  help="Une offre dans ces villes gagne des points, les autres ne sont pas éliminées.")
    o1.checkbox("J'accepte le full remote", key="crit_remote")
    o2.selectbox("Offres publiées depuis", list(crit.DATE_POSTED), format_func=crit.DATE_POSTED.get, key="crit_posted",
                 help="« 30 derniers jours » ou « toutes » donne beaucoup plus d'offres que « 7 derniers jours ».")
    o2.select_slider("Pages de résultats par requête", [1, 2, 3], key="crit_pages",
                     help="1 page ≈ 10 offres = 1 requête JSearch. 2 pages = 2 fois plus d'offres et de requêtes.")
    o3.number_input("Gratification minimale (€/mois, 0 = indifférent)", 0, 5000, step=100, key="crit_stipend",
                    help="Les offres sous ce montant perdent des points (pas éliminées : souvent non indiqué).")
    o3.text_input("Mots à exclure du titre", key="crit_exclude", placeholder="alternance, CDI, Werkstudent",
                  help="Une offre dont le titre contient un de ces mots est éliminée.")
    o1.checkbox("Seulement les offres marquées « stage » par Google", key="crit_intern_only",
                help="Coché : moins d'offres, car beaucoup de stages européens ne sont pas marqués comme tels. "
                     "Décoché (conseillé) : plus d'offres ; l'IA et le score écartent ensuite les CDI, alternances…")

# ---------- Lecture du formulaire ----------
ss = st.session_state
form = dict(
    titles=crit.split_list(ss.crit_titles), countries=[n for n in crit.EUROPE if ss[f"c_{n}"]],
    start_months=[ym for ym in months if ss[f"m_{ym}"]], durations=[d for d in crit.DURATIONS if ss[f"d_{d}"]],
    cities=crit.split_list(ss.crit_cities), remote_ok=ss.crit_remote, date_posted=ss.crit_posted,
    pages=ss.crit_pages, min_stipend_eur=ss.crit_stipend or None, exclude_words=crit.split_list(ss.crit_exclude),
    internship_only=ss.crit_intern_only,
)
problems = [msg for ok, msg in [(form["titles"], "écris au moins un intitulé"),
                                (form["countries"], "coche au moins un pays")] if not ok]
current = None if problems else crit.SearchCriteria(**form)

# ---------- Quota et lancement ----------
st.divider()
q = jsearch.quota_status(conn, cfg.quota_soft_limit)
n_req = 0 if (current is None or config.dev_mode()) else current.request_count()
c1, c2, c3, c4 = st.columns(4)
real_req = current.request_count() if current else 0
with c1:
    ui.metric_card("Requêtes JSearch ce mois", f"{q['used']} / {q['plan_limit']}")
with c2:
    ui.metric_card("Encore possibles", q["remaining"], f"Plafond de l'app : {q['soft_limit']} (config.yaml)")
with c3:
    ui.metric_card("Cette recherche", real_req,
                   "Intitulés × pays × pages. 0 si déjà faite aujourd'hui (cache)."
                   + (" En mode dev, aucune n'est envoyée." if config.dev_mode() else ""))
with c4:
    ui.metric_card("Après", max(0, q["remaining"] - n_req))
st.progress(min(q["used"] / q["plan_limit"], 1.0))
too_big = n_req > q["remaining"]
if problems:
    st.warning("Pour lancer : " + " et ".join(problems) + ".")
elif too_big:
    st.error(f"Cette recherche demande {n_req} requêtes, il en reste {q['remaining']} ce mois-ci : "
             "décoche des pays ou des intitulés.")
elif current:
    st.caption(f"{len(current.titles)} intitulé(s) × {len(current.countries)} pays × {current.pages} page(s) · "
               + crit.describe(current))

row = st.container(horizontal=True, vertical_alignment="bottom")
ui.user_selector(cfg, row, key="user_recherche", label="Recherche pour", width=200)
save_only = row.button("Enregistrer les critères", disabled=current is None,
                      help="Enregistre et recalcule les scores des offres déjà en base. Gratuit, aucun appel.")
launch = row.button("Lancer la recherche", type="primary", icon=":material/rocket_launch:",
                   disabled=current is None or too_big)

if (save_only or launch) and current:
    crit.save(conn, current, user)
    if launch:
        crit.record_searched(conn, current.countries, user)
    profile = crit.effective_profile(conn, base_profile, current, user)
    if save_only:
        st.toast(f"Critères de {user} enregistrés, {scoring.score_all(conn, profile, user)} offre(s) rescorée(s).")

if launch and current:
    run_cfg = cfg.model_copy(update={"date_posted": current.date_posted,
                                     "employment_types": "INTERN" if current.internship_only else ""})
    with st.status("Recherche en cours…", expanded=True) as status:
        status.write("1/3 · Collecte des offres")
        col = jsearch.collect(conn, crit.build_queries(current), run_cfg, user=user)
        for err in col.errors:
            st.error(err)
        status.write(f"{col.fetched} offres reçues, {col.new} nouvelles "
                     f"({col.api_calls} appel(s) JSearch, {col.cache_hits} depuis le cache).")
        if col.per_query:
            st.dataframe(pd.DataFrame(col.per_query), hide_index=True, width="stretch")
        if col.fetched and not col.new:
            st.info("Toutes ces offres étaient déjà en base. Pour en trouver d'autres : élargis la période de "
                    "publication, ajoute des pages, d'autres pays ou des intitulés dans la langue du pays.")
        elif not col.fetched and not col.errors:
            st.info("JSearch n'a renvoyé aucune offre. Décoche « Seulement les offres marquées stage », passe à "
                    "« 30 derniers jours » et essaie des intitulés plus courts ou dans la langue du pays.")

        status.write("2/3 · Extraction IA des nouvelles offres")
        bar = st.progress(0.0)
        ext = extract.extract_new_jobs(conn, cfg, progress=lambda i, n, t: bar.progress(i / n, text=f"{i}/{n} · {t[:60]}"))
        for err in ext.errors:
            st.warning(err)

        status.write("3/3 · Calcul des scores")
        n_scored = scoring.score_all(conn, profile, user)
        status.update(label="Terminé", state="complete" if not (col.errors or ext.errors) else "error")

    cost_usd = ext.usage.cost_usd(cfg)
    st.success(
        f"**{col.new} nouvelle(s) offre(s) pour {user}**, {ext.extracted + ext.from_reference} extraite(s)"
        + (f" (dont {ext.from_reference} depuis la référence des fixtures)" if ext.from_reference else "")
        + (f", {ext.failed} échec(s)" if ext.failed else "")
        + f", {n_scored} offre(s) scorée(s). Tokens IA : {ext.usage.input_tokens:,} en entrée, "
        f"{ext.usage.output_tokens:,} en sortie · coût estimé : "
        + ("0 $ (offre gratuite)" if cost_usd == 0 else f"{cost_usd:.4f} $")
    )
    st.caption("Résultats dans la page **Offres** (menu de gauche).")

st.divider()
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
