"""Extraction structurée des annonces via l'IA (Gemini, Groq ou Anthropic selon config.yaml)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from src import config, db, llm

log = logging.getLogger(__name__)

REFERENCE_PATH = config.FIXTURES_DIR / "extractions" / "reference.json"
MAX_ATTEMPTS = 2
FIXTURE_MODEL = "fixture"

ContractType = Literal["stage", "alternance", "VIE", "working_student", "autre"]
WorkMode = Literal["onsite", "hybrid", "remote", "unknown"]


class Language(BaseModel):
    lang: str
    level: str | None = None


class JobExtraction(BaseModel):
    is_internship: bool
    contract_type: ContractType
    city: str | None = None
    country: str | None = None
    work_mode: WorkMode = "unknown"
    duration_months_min: int | None = Field(None, ge=1, le=36)
    duration_months_max: int | None = Field(None, ge=1, le=36)
    start_date_earliest: date | None = None
    start_date_text: str | None = None
    required_skills: list[str] = []
    nice_to_have_skills: list[str] = []
    languages: list[Language] = []
    education_level: str | None = None
    stipend_monthly_eur: float | None = Field(None, ge=0)
    requires_convention: bool | None = None
    domain: str = ""
    summary_fr: str = ""

    @field_validator("required_skills", "nice_to_have_skills")
    @classmethod
    def _dedupe_skills(cls, skills: list[str]) -> list[str]:
        seen, out = set(), []
        for s in (s.strip() for s in skills):
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    @field_validator("city", "country", "start_date_text", "education_level", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _order_durations(self) -> "JobExtraction":
        lo, hi = self.duration_months_min, self.duration_months_max
        if lo is not None and hi is not None and lo > hi:
            self.duration_months_min, self.duration_months_max = hi, lo
        return self


def _nullable(typ: str, desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": [typ, "null"], "description": desc, **extra}


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_internship": {"type": "boolean", "description": "true seulement pour un vrai stage. false pour CDI, CDD, "
                          "alternance/apprentissage, Werkstudent, freelance, même si l'annonce dit « stage »."},
        "contract_type": {"type": "string", "enum": ["stage", "alternance", "VIE", "working_student", "autre"],
                          "description": "Praktikum = stage ; Werkstudent = working_student ; CDI/CDD/Festanstellung = autre."},
        "city": _nullable("string", "Ville du poste telle qu'écrite."),
        "country": _nullable("string", "Pays du poste, en anglais (France, Germany, Luxembourg…)."),
        "work_mode": {"type": "string", "enum": ["onsite", "hybrid", "remote", "unknown"],
                      "description": "hybrid si télétravail partiel mentionné ; unknown si rien n'est dit."},
        "duration_months_min": _nullable("integer", "Durée minimale en mois."),
        "duration_months_max": _nullable("integer", "Durée maximale en mois (= min si durée fixe)."),
        "start_date_earliest": _nullable("string", "Date de début au plus tôt. « janvier 2027 » → 2027-01-01. "
                                         "« dès que possible » → date de publication.", format="date"),
        "start_date_text": _nullable("string", "Formulation originale de la date de début."),
        "required_skills": {"type": "array", "items": {"type": "string"},
                            "description": "Compétences exigées, noms courts normalisés (Excel, Python, SAP, Power BI, SQL, VBA…)."},
        "nice_to_have_skills": {"type": "array", "items": {"type": "string"},
                                "description": "Compétences « un plus », « appréciées », « von Vorteil », « nice to have »."},
        "languages": {"type": "array", "description": "Langues demandées.",
                      "items": {"type": "object", "properties": {
                          "lang": {"type": "string", "description": "En anglais : French, English, German…"},
                          "level": _nullable("string", "native, fluent, professional, intermediate, basic, ou CECRL (B1…)."),
                      }, "required": ["lang", "level"]}},
        "education_level": _nullable("string", "Niveau d'études demandé (Bac+3, Bac+5, Master…)."),
        "stipend_monthly_eur": _nullable("number", "Gratification mensuelle brute en euros, seulement si un montant est écrit."),
        "requires_convention": _nullable("boolean", "true si convention de stage / Pflichtpraktikum exigé."),
        "domain": {"type": "string", "description": "Domaine métier court en français (contrôle de gestion, FP&A, "
                   "audit, M&A, trésorerie, comptabilité, finance d'entreprise…)."},
        "summary_fr": {"type": "string", "description": "Résumé en français, 2 phrases maximum."},
    },
    "required": ["is_internship", "contract_type", "city", "country", "work_mode", "duration_months_min",
                 "duration_months_max", "start_date_earliest", "start_date_text", "required_skills",
                 "nice_to_have_skills", "languages", "education_level", "stipend_monthly_eur",
                 "requires_convention", "domain", "summary_fr"],
}

EXTRACTION_TOOL = llm.ToolSpec("enregistrer_offre", "Enregistre les informations extraites d'une offre.", EXTRACTION_SCHEMA)

SYSTEM_PROMPT = """Tu extrais des informations d'offres d'emploi (en français, anglais ou allemand) pour une étudiante \
ou un étudiant en finance qui cherche un stage.

Règles :
- N'extrais que ce qui est écrit dans l'annonce. Si une information est absente, mets null (ou une liste vide).
- Ne devine jamais : pas de durée, de date, de gratification ou de langue supposée.
- Une alternance, un contrat d'apprentissage, un Werkstudent, un CDI, un CDD ou une Festanstellung ne sont pas des \
stages (is_internship = false), même si le titre est trompeur.
- Les dates sont au format AAAA-MM-JJ. Un mois sans jour → le 1er du mois. « Dès que possible », « ASAP », \
« ab sofort » → la date de publication fournie.
- Le résumé est en français, 2 phrases maximum."""

ASAP = re.compile(r"d[eè]s que possible|asap|as soon as possible|ab sofort|immediately|imm[ée]diatement|"
                  r"zum n[aä]chstm[oö]glichen", re.I)


def build_user_prompt(job: dict[str, Any]) -> str:
    """Annonce complète + métadonnées JSearch (dont la date de publication pour « dès que possible »)."""
    posted = (job.get("job_posted_at_datetime_utc") or "")[:10] or "inconnue"
    return (f"Titre : {job.get('job_title')}\nEntreprise : {job.get('employer_name')}\n"
            f"Lieu indiqué : {job.get('job_city') or '?'}, {job.get('job_country') or '?'}"
            f"{' (remote)' if job.get('job_is_remote') else ''}\nDate de publication : {posted}\n\n"
            f"Annonce :\n{_description(job)}")


MAX_DESCRIPTION_CHARS = 8000  # ≈ 2 000 tokens : l'essentiel d'une annonce, et des requêtes IA plus rapides


def _description(job: dict[str, Any]) -> str:
    """Annonce nettoyée (espaces et lignes vides en double retirés), coupée si elle est très longue."""
    text = job.get("job_description") or ""
    lines, seen = [], set()
    for line in (re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in text.splitlines()):
        key = line.lower()
        if line and key not in seen:  # les annonces répètent souvent les mêmes lignes (pied de page…)
            seen.add(key)
            lines.append(line)
    text = "\n".join(lines)
    if len(text) <= MAX_DESCRIPTION_CHARS:
        return text
    return text[:MAX_DESCRIPTION_CHARS] + "\n[… annonce tronquée, la suite n'est pas fournie]"


# ---------- Tri sans IA ----------
RULES_MODEL = "règles"
_INTERNSHIP_WORDS = re.compile(r"\b(stage|stages|stagiaire|intern|interns|internship|praktikum|praktikant|praktikantin|"
                               r"pflichtpraktikum|tirocinio|tirocinante|practicas|becario|becaria|stagiair|estagio|"
                               r"trainee|vie|v i e)\b")
_TITLE_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b(alternance|alternant|alternante|apprentissage|apprenti|apprentie|contrat pro|"
                r"contrat de professionnalisation|ausbildung|duales studium|dual student|apprentice|apprenticeship)\b"),
     "alternance", "alternance / apprentissage"),
    (re.compile(r"\b(werkstudent|werkstudentin|working student|student assistant|studentische aushilfe)\b"),
     "working_student", "Werkstudent"),
    (re.compile(r"\b(cdi|cdd|festanstellung|unbefristet|befristet|permanent|full time|vollzeit|"
                r"temps plein|interim|freelance|freiberuflich)\b"), "autre", "CDI / CDD / emploi"),
    (re.compile(r"\b(senior|head of|director|directeur|directrice|leiter|leiterin|teamleiter|lead|principal|"
                r"manager|managerin|chef de|responsable|expert|experte|consultant confirme)\b"),
     "autre", "poste confirmé"),
]


def rule_based_extraction(job: dict[str, Any]) -> JobExtraction | None:
    """Offre écartée d'après son seul titre (CDI, alternance, Werkstudent, poste senior…), sans appel IA.
    None si le titre ne permet pas de conclure (ou parle de stage) : l'IA analyse alors l'annonce."""
    title = db.normalize_text(job.get("job_title"))
    alternance = _TITLE_RULES[0][0].search(title)
    if _INTERNSHIP_WORDS.search(title) and not alternance:
        return None
    for pattern, contract, label in _TITLE_RULES:
        if pattern.search(title):
            return JobExtraction(
                is_internship=False, contract_type=contract, city=job.get("job_city"), country=None,
                domain="", summary_fr=f"Écartée sans analyse IA : le titre indique un(e) {label}.")
    return None


def _apply_asap(ex: JobExtraction, job: dict[str, Any]) -> JobExtraction:
    """« Dès que possible » sans date → date de publication (filet de sécurité déterministe)."""
    posted = (job.get("job_posted_at_datetime_utc") or "")[:10]
    if ex.start_date_earliest is None and ex.start_date_text and ASAP.search(ex.start_date_text) and posted:
        ex.start_date_earliest = date.fromisoformat(posted)
    return ex


def extract_with_llm(job: dict[str, Any], client: llm.LLMClient,
                     attempts: int = MAX_ATTEMPTS) -> tuple[JobExtraction | None, llm.Usage, str]:
    """Appel IA (peut être payant selon le fournisseur). Renvoie (extraction ou None, tokens, erreur)."""
    usage, error = llm.Usage(), ""
    for _ in range(attempts):
        data, u = client.structured(SYSTEM_PROMPT, build_user_prompt(job), EXTRACTION_TOOL)
        usage.add(u)
        if data is None:
            error = "réponse sans JSON exploitable"
            continue
        try:
            return _apply_asap(JobExtraction.model_validate(data), job), usage, ""
        except ValidationError as e:
            error = f"JSON invalide ({e.error_count()} erreur(s) : {e.errors()[0]['loc']})"
    return None, usage, error


# ---------- Référence (mode dev) ----------
def load_reference(path: Path = REFERENCE_PATH) -> dict[str, dict[str, Any]]:
    """Extractions de référence des fixtures (écrites à la main), par job_id."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def compare_with_reference(got: JobExtraction, expected: dict[str, Any]) -> list[dict[str, Any]]:
    """Comparaison champ par champ (hors résumé) pour juger la qualité d'une extraction réelle."""
    ref = JobExtraction.model_validate(expected)
    rows = []
    for name in JobExtraction.model_fields:
        if name == "summary_fr":
            continue
        a, b = getattr(got, name), getattr(ref, name)
        if name.endswith("skills"):
            ok = {s.lower() for s in a} == {s.lower() for s in b}
        elif name == "languages":
            ok = {l.lang.lower() for l in a} == {l.lang.lower() for l in b}
        elif isinstance(a, str) and isinstance(b, str):
            ok = a.strip().lower() == b.strip().lower()
        else:
            ok = a == b
        fmt = lambda v: ", ".join(f"{l.lang} ({l.level})" if isinstance(l, Language) else str(l) for l in v) \
            if isinstance(v, list) else ("" if v is None else str(v))
        rows.append({"champ": name, "IA": fmt(a), "référence": fmt(b), "ok": ok})
    return rows


# ---------- Lot ----------
@dataclass
class ExtractSummary:
    extracted: int = 0
    from_reference: int = 0
    skipped: int = 0  # écartées d'après leur titre, sans appel IA
    models: dict[str, int] = field(default_factory=dict)  # réponses par modèle (rotation Groq)
    failed: int = 0
    usage: llm.Usage = field(default_factory=llm.Usage)
    errors: list[str] = field(default_factory=list)


def extract_new_jobs(conn: db.Database, cfg: config.AppConfig, *, client: llm.LLMClient | None = None,
                     dev: bool | None = None, limit: int | None = None,
                     progress: Callable[[int, int, str], None] | None = None) -> ExtractSummary:
    """Extrait les offres jamais extraites. En mode dev, les fixtures connues utilisent la référence
    (aucun appel). Un échec est loggé et n'arrête pas le lot."""
    dev = config.dev_mode() if dev is None else dev
    jobs = db.jobs_without_extraction(conn)[:limit]
    reference = load_reference() if dev else {}
    summary = ExtractSummary()
    for i, job in enumerate(jobs, 1):
        if progress:
            progress(i, len(jobs), job.get("job_title") or "")
        if job["job_id"] in reference:
            db.save_extraction(conn, job["job_id"], FIXTURE_MODEL, reference[job["job_id"]])
            summary.from_reference += 1
            continue
        quick = rule_based_extraction(job)
        if quick is not None:  # CDI, alternance… : écartée sans dépenser de requête IA
            db.save_extraction(conn, job["job_id"], RULES_MODEL, quick.model_dump(mode="json"))
            summary.skipped += 1
            continue
        try:
            client = client or llm.make_client(cfg)
            ex, usage, error = extract_with_llm(job, client)
        except llm.LLMError as e:
            log.warning("Extraction échouée pour %s : %s", job["job_id"], e)
            if e.fatal:  # clé absente, limite gratuite atteinte… : inutile d'insister sur les suivantes
                summary.failed += len(jobs) - i + 1
                summary.errors.append(f"{e} ({len(jobs) - i + 1} offre(s) non extraite(s), relance plus tard)")
                break
            summary.failed += 1
            summary.errors.append(f"{job.get('job_title')} ({job.get('employer_name')}) : {e}")
            continue
        summary.usage.add(usage)
        if ex is None:
            summary.failed += 1
            summary.errors.append(f"{job.get('job_title')} ({job.get('employer_name')}) : {error}")
            log.warning("Extraction échouée pour %s : %s", job["job_id"], error)
            continue
        db.save_extraction(conn, job["job_id"], getattr(client, "model", cfg.model), ex.model_dump(mode="json"))
        summary.extracted += 1
    if getattr(client, "notice", ""):
        summary.errors.insert(0, client.notice)  # ex. modèle Groq retiré, remplacé automatiquement
    summary.models = dict(getattr(client, "used", {}) or {})
    return summary
