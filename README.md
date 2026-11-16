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
   DATABASE_URL = "postgresql://neondb_owner:MOT_DE_PASSE@ep-xxxx-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require"
   DEV_MODE = "1"
   ```
3. `DEV_MODE = "1"` : les offres viennent de `tests/fixtures/` (aucun appel JSearch). Passe à `"0"` pour les vraies offres.

Sans `DATABASE_URL`, l'app utilise SQLite, qui est effacé à chaque redémarrage sur Streamlit Cloud. Avec une base Postgres (ex. Neon, gratuit),
offres, statuts, notes, compteur de quota et cache JSearch sont conservés. Les tables sont créées automatiquement.

## Réglages
- Utilisateurs (Octave, Marilou, Alex, liste dans `config.yaml`) : chacun a ses critères, ses offres, ses scores et
  ses candidatures. Choix dans la barre latérale ou par l'URL (`?user=Marilou`).
- Page **Recherche** : intitulés, pays (toute l'Europe), mois de début, durées et autres critères, enregistrés en base.
- `config.yaml` : fournisseur IA (`llm_provider`, `model`), intitulés par défaut, plafond de quota JSearch (180/200).
- `profile.yaml` : ton profil (pays, durée, dates, compétences, langues, poids du score).

## Tests
```bash
pip install -r requirements.txt
python -m pytest -q
# en plus sur Postgres (base de test vidée à chaque test) :
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/scout_test python -m pytest -q
```
