# Internship Scout

Outil Streamlit perso de recherche de stage : collecte des offres via l'API JSearch (RapidAPI), extraction des
infos utiles par IA (Gemini gratuit par défaut, Groq ou Claude en option), score déterministe par rapport à
`profile.yaml`, tableau filtrable, chatbot et suivi des candidatures avec export Excel.
Aucun scraping : uniquement des API.

## Déploiement sur Streamlit Community Cloud
1. *New app* → ce dépôt, fichier principal `app.py`.
2. *Settings → Secrets* :
   ```toml
   RAPIDAPI_KEY = "..."
   GEMINI_API_KEY = "..."
   APP_PASSWORD = "un mot de passe"
   DATABASE_URL = "postgresql://postgres.xxxx:MOT_DE_PASSE@aws-0-eu-west-3.pooler.supabase.com:6543/postgres"
   DEV_MODE = "1"
   ```
3. `DEV_MODE = "1"` : les offres viennent de `tests/fixtures/` (aucun appel JSearch). Passe à `"0"` pour les vraies offres.

Sans `DATABASE_URL`, l'app utilise SQLite, qui est effacé à chaque redémarrage sur Streamlit Cloud. Avec Supabase,
offres, statuts, notes, compteur de quota et cache JSearch sont conservés. Les tables sont créées automatiquement.

## Réglages
- `config.yaml` : fournisseur IA (`llm_provider`, `model`), requêtes, plafond de quota JSearch (180/200).
- `profile.yaml` : ton profil (pays, durée, dates, compétences, langues, poids du score).

## Tests
```bash
pip install -r requirements.txt
python -m pytest -q
# en plus sur Postgres (base de test vidée à chaque test) :
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/scout_test python -m pytest -q
```
