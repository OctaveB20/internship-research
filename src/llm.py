"""Couche IA multi-fournisseur : Gemini (gratuit), Groq (gratuit), Anthropic (payant).

Deux opérations, identiques pour les trois fournisseurs :
- `structured_with_llm` : l'IA remplit un outil dont on donne le schéma JSON (extraction) ;
- `chat_with_llm` : boucle de conversation où l'IA peut appeler nos outils (chatbot).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

from src import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RATE_LIMIT_RETRIES = 6  # 429 : on attend le délai indiqué (limites par minute des offres gratuites), plusieurs fois
MAX_WAIT_S = 90
# Erreurs Groq quand le modèle produit un JSON / un appel d'outil invalide : on réessaie au lieu d'abandonner.
INVALID_OUTPUT_CODES = {"json_validate_failed", "tool_use_failed"}

RunTool = Callable[[str, dict[str, Any]], Any]


class LLMError(Exception):
    """Erreur lisible à afficher dans l'UI. `fatal` : inutile de réessayer sur l'offre suivante
    (clé absente ou refusée, modèle inconnu, limite gratuite atteinte)."""

    def __init__(self, message: str, fatal: bool = False, code: str = ""):
        super().__init__(message)
        self.fatal = fatal
        self.code = code  # code d'erreur renvoyé par l'API (ex. json_validate_failed chez Groq)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    def cost_usd(self, cfg: config.AppConfig) -> float:
        return (self.input_tokens * cfg.price_input_per_mtok + self.output_tokens * cfg.price_output_per_mtok) / 1e6


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (type "object")


@dataclass
class ChatResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    provider: str

    def structured(self, system: str, user: str, tool: ToolSpec) -> tuple[dict | None, Usage]: ...

    def chat(self, system: str, history: list[dict[str, str]], tools: list[ToolSpec],
             run_tool: RunTool, max_steps: int = 6) -> ChatResult: ...


# ---------- HTTP commun (Gemini, Groq) ----------
def _retry_delay(resp: requests.Response, attempt: int) -> float:
    """Délai d'attente conseillé : en-tête Retry-After, retryDelay (Gemini), « try again in 7.6s » ou
    « 1m2.5s » (Groq), sinon backoff 5/10/20 s."""
    header = resp.headers.get("Retry-After") if resp.headers else None
    if header and header.replace(".", "", 1).isdigit():
        return min(float(header) + 0.5, MAX_WAIT_S)
    text = resp.text or ""
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
    if match:
        return min(float(match.group(1)) + 1, MAX_WAIT_S)
    match = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", text)
    if match:
        return min(int(match.group(1) or 0) * 60 + float(match.group(2)) + 0.5, MAX_WAIT_S)
    return min(5 * 2 ** attempt, MAX_WAIT_S)


def _error_info(resp: requests.Response) -> tuple[str, str]:
    """(message, code) de l'erreur renvoyée par l'API."""
    try:
        err = resp.json().get("error", {})
    except ValueError:
        return "", ""
    if isinstance(err, dict):
        return str(err.get("message", "")), str(err.get("code") or err.get("type") or "")
    return str(err), ""


def _error_message(resp: requests.Response) -> str:
    return _error_info(resp)[0]


def _post_json(provider: str, url: str, headers: dict[str, str], body: dict[str, Any], timeout: float,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """POST avec réessais sur 429/5xx. Lève LLMError lisible (jamais la clé)."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.Timeout:
            raise LLMError(f"{provider} n'a pas répondu en {timeout:.0f} s.") from None
        except requests.RequestException as e:
            raise LLMError(f"Connexion à {provider} impossible ({type(e).__name__}).") from None
        retries = RATE_LIMIT_RETRIES if resp.status_code == 429 else MAX_RETRIES
        if resp.status_code in RETRY_STATUSES and attempt < retries:
            sleep(_retry_delay(resp, attempt))
            continue
        if resp.status_code == 429:
            raise LLMError(f"Limite de requêtes {provider} atteinte (429) : réessaie dans quelques minutes "
                           "(l'offre gratuite limite les requêtes par minute et par jour).", fatal=True)
        if resp.status_code in (401, 403):
            raise LLMError(f"Clé {provider} refusée ({resp.status_code}) : vérifie-la dans les secrets.", fatal=True)
        if resp.status_code == 404:
            hint = " (gemini-2.5-flash a été arrêté en octobre 2026 : essaie gemini-3.5-flash)" if provider == "Gemini" else ""
            raise LLMError(f"Modèle introuvable chez {provider} (404) : vérifie `model` dans config.yaml{hint}.",
                           fatal=True)
        if resp.status_code >= 400:
            detail, code = _error_info(resp)
            raise LLMError(f"{provider} a répondu HTTP {resp.status_code}" + (f" : {detail[:300]}" if detail else "."),
                           code=code)
        try:
            return resp.json()
        except ValueError:
            raise LLMError(f"Réponse {provider} illisible (JSON invalide).") from None
    raise LLMError(f"{provider} indisponible après {MAX_RETRIES} réessais.")  # pragma: no cover


def _safe_json(text: str) -> dict | None:
    """Parse un objet JSON, en tolérant un bloc ```json autour."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _tool_output(run_tool: RunTool, name: str, args: dict[str, Any]) -> Any:
    """Exécute un outil ; une exception devient un message d'erreur pour l'IA."""
    try:
        return run_tool(name, args)
    except Exception as e:  # l'IA doit pouvoir corriger son appel
        return {"error": f"{type(e).__name__}: {e}"}


# ---------- Gemini ----------
def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema → sous-ensemble OpenAPI accepté par Gemini (nullable, pas d'additionalProperties)."""
    out: dict[str, Any] = {}
    typ = schema.get("type")
    if isinstance(typ, list):
        non_null = [t for t in typ if t != "null"]
        out["nullable"] = "null" in typ
        typ = non_null[0] if non_null else "string"
    if typ:
        out["type"] = typ.upper()
    desc = schema.get("description", "")
    if schema.get("format") == "date":
        desc = (desc + " Format AAAA-MM-JJ.").strip()
    if desc:
        out["description"] = desc
    if "enum" in schema:
        out["enum"] = [e for e in schema["enum"] if e is not None]
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out


class GeminiClient:
    provider = "Gemini"

    def __init__(self, api_key: str, model: str, timeout: float = 90, sleep: Callable[[float], None] = time.sleep):
        self.api_key, self.model, self.timeout, self.sleep = api_key, model, timeout, sleep

    def _call(self, body: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        data = _post_json(self.provider, GEMINI_URL.format(model=self.model),
                          {"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                          body, self.timeout, self.sleep)
        meta = data.get("usageMetadata", {})
        usage = Usage(meta.get("promptTokenCount", 0),
                      meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0))
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "réponse vide")
            raise LLMError(f"Gemini n'a renvoyé aucune réponse ({reason}).")
        return candidates[0], usage

    def structured(self, system: str, user: str, tool: ToolSpec) -> tuple[dict | None, Usage]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": to_gemini_schema(tool.parameters)},
        }
        cand, usage = self._call(body)
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        return _safe_json(text), usage

    def chat(self, system: str, history: list[dict[str, str]], tools: list[ToolSpec],
             run_tool: RunTool, max_steps: int = 6) -> ChatResult:
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in history]
        decls = [{"name": t.name, "description": t.description, "parameters": to_gemini_schema(t.parameters)}
                 for t in tools]
        result = ChatResult("")
        for _ in range(max_steps):
            body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                    "tools": [{"functionDeclarations": decls}]}
            cand, usage = self._call(body)
            result.usage.add(usage)
            content = cand.get("content") or {"role": "model", "parts": []}
            calls = [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]
            if not calls:
                result.text = "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought"))
                return result
            contents.append(content)  # renvoyé tel quel (signatures de réflexion comprises)
            responses = []
            for call in calls:
                args = call.get("args") or {}
                output = _tool_output(run_tool, call["name"], args)
                result.tool_calls.append({"name": call["name"], "args": args})
                responses.append({"functionResponse": {"name": call["name"], "response": {"result": output}}})
            contents.append({"role": "user", "parts": responses})
        result.text = "Je n'ai pas réussi à conclure en un nombre raisonnable d'étapes : reformule ta question."
        return result


# ---------- Groq (API compatible OpenAI) ----------
def to_simple_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """JSON Schema sans « null » (types multiples, enum avec None), mieux accepté par les API compatibles OpenAI."""
    out = {k: v for k, v in schema.items() if k not in ("type", "enum", "properties", "items")}
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if typ:
        out["type"] = typ
    if "enum" in schema:
        out["enum"] = [e for e in schema["enum"] if e is not None]
    if "properties" in schema:
        out["properties"] = {k: to_simple_schema(v) for k, v in schema["properties"].items()}
    if "items" in schema:
        out["items"] = to_simple_schema(schema["items"])
    return out


def describe_schema(schema: dict[str, Any]) -> str:
    """Consigne compacte : une ligne par champ attendu (nom, type, description)."""
    lines = ["Réponds uniquement avec un objet JSON contenant exactement ces clés (null si l'information est absente) :"]
    for name, prop in schema.get("properties", {}).items():
        typ = prop.get("type")
        typ = "|".join(typ) if isinstance(typ, list) else typ
        if prop.get("enum"):
            typ = " | ".join(f'"{e}"' for e in prop["enum"] if e is not None)
        if typ == "array" and prop.get("items", {}).get("type") == "object":
            keys = ", ".join(prop["items"].get("properties", {}))
            typ = f"liste d'objets {{{keys}}}"
        elif typ == "array":
            typ = "liste de textes"
        lines.append(f"- {name} ({typ}) : {prop.get('description', '')}".rstrip(" :"))
    return "\n".join(lines)


# ---------- Client Groq ----------
class GroqClient:
    provider = "Groq"

    def __init__(self, api_key: str, model: str, timeout: float = 90, sleep: Callable[[float], None] = time.sleep):
        self.api_key, self.model, self.timeout, self.sleep = api_key, model, timeout, sleep

    def _call(self, body: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        data = _post_json(self.provider, GROQ_URL, {"Authorization": f"Bearer {self.api_key}"},
                          {"model": self.model, **body}, self.timeout, self.sleep)
        u = data.get("usage", {})
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("Groq n'a renvoyé aucune réponse.")
        return choices[0]["message"], Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

    @staticmethod
    def _tool(t: ToolSpec) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": to_simple_schema(t.parameters)}}

    def structured(self, system: str, user: str, tool: ToolSpec) -> tuple[dict | None, Usage]:
        """Mode JSON (plus fiable avec Llama que l'appel d'outil forcé) et schéma compact dans la consigne,
        pour rester sous la limite de tokens par minute de l'offre gratuite."""
        prompt = f"{system}\n\n{describe_schema(tool.parameters)}"
        try:
            msg, usage = self._call({
                "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}, "temperature": 0,
            })
        except LLMError as e:
            if e.code in INVALID_OUTPUT_CODES:
                return None, Usage()  # JSON refusé par Groq : extract.py réessaie
            raise
        return _safe_json(msg.get("content") or ""), usage

    def chat(self, system: str, history: list[dict[str, str]], tools: list[ToolSpec],
             run_tool: RunTool, max_steps: int = 6) -> ChatResult:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *history]
        result = ChatResult("")
        for _ in range(max_steps):
            try:
                msg, usage = self._call({"messages": messages, "tools": [self._tool(t) for t in tools]})
            except LLMError as e:
                if e.code not in INVALID_OUTPUT_CODES:
                    raise
                msg, usage = self._call({"messages": messages, "tools": [self._tool(t) for t in tools]})  # 2e essai
            result.usage.add(usage)
            calls = msg.get("tool_calls") or []
            if not calls:
                result.text = msg.get("content") or ""
                return result
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                args = _safe_json(call["function"].get("arguments", "{}")) or {}
                output = _tool_output(run_tool, call["function"]["name"], args)
                result.tool_calls.append({"name": call["function"]["name"], "args": args})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(output, ensure_ascii=False, default=str)})
        result.text = "Je n'ai pas réussi à conclure en un nombre raisonnable d'étapes : reformule ta question."
        return result


# ---------- Anthropic (SDK officiel) ----------
class AnthropicClient:
    provider = "Anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 90, sdk_client: Any = None):
        if sdk_client is None:
            import anthropic
            sdk_client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self.client, self.model = sdk_client, model

    def _create(self, **kwargs: Any) -> Any:
        import anthropic
        try:
            return self.client.messages.create(model=self.model, max_tokens=16000, **kwargs)
        except anthropic.AuthenticationError:
            raise LLMError("Clé Anthropic refusée : vérifie ANTHROPIC_API_KEY.", fatal=True) from None
        except anthropic.NotFoundError:
            raise LLMError("Modèle Anthropic introuvable : vérifie `model` dans config.yaml.", fatal=True) from None
        except anthropic.RateLimitError:
            raise LLMError("Limite de requêtes Anthropic atteinte : réessaie plus tard.", fatal=True) from None
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic a répondu HTTP {e.status_code}.") from None
        except anthropic.APIConnectionError:
            raise LLMError("Connexion à Anthropic impossible.") from None

    @staticmethod
    def _usage(resp: Any) -> Usage:
        return Usage(resp.usage.input_tokens, resp.usage.output_tokens)

    @staticmethod
    def _tool(t: ToolSpec) -> dict[str, Any]:
        return {"name": t.name, "description": t.description, "input_schema": t.parameters}

    def structured(self, system: str, user: str, tool: ToolSpec) -> tuple[dict | None, Usage]:
        # tool_choice "auto" + consigne explicite : le choix forcé est refusé par certains modèles récents.
        resp = self._create(system=f"{system}\n\nRéponds uniquement en appelant l'outil `{tool.name}`.",
                            messages=[{"role": "user", "content": user}], tools=[self._tool(tool)],
                            output_config={"effort": "low"})
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool.name:
                return (block.input if isinstance(block.input, dict) else None), self._usage(resp)
        return None, self._usage(resp)

    def chat(self, system: str, history: list[dict[str, str]], tools: list[ToolSpec],
             run_tool: RunTool, max_steps: int = 6) -> ChatResult:
        messages: list[dict[str, Any]] = [dict(m) for m in history]
        result = ChatResult("")
        for _ in range(max_steps):
            resp = self._create(system=system, messages=messages, tools=[self._tool(t) for t in tools])
            result.usage.add(self._usage(resp))
            if resp.stop_reason != "tool_use":
                result.text = "".join(b.text for b in resp.content if b.type == "text")
                return result
            messages.append({"role": "assistant", "content": resp.content})
            outputs = []
            for block in resp.content:
                if block.type == "tool_use":
                    output = _tool_output(run_tool, block.name, block.input)
                    result.tool_calls.append({"name": block.name, "args": block.input})
                    outputs.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(output, ensure_ascii=False, default=str)})
            messages.append({"role": "user", "content": outputs})
        result.text = "Je n'ai pas réussi à conclure en un nombre raisonnable d'étapes : reformule ta question."
        return result


# ---------- Fabrique ----------
def make_client(cfg: config.AppConfig) -> LLMClient:
    """Client du fournisseur choisi dans config.yaml. Lève LLMError si la clé manque."""
    key_name = config.LLM_KEYS[cfg.llm_provider]
    api_key = config.get_secret(key_name)
    if not api_key:
        raise LLMError(f"{key_name} absente : ajoute-la dans les secrets (llm_provider = {cfg.llm_provider}).",
                       fatal=True)
    cls = {"gemini": GeminiClient, "groq": GroqClient, "anthropic": AnthropicClient}[cfg.llm_provider]
    return cls(api_key, cfg.model, cfg.llm_timeout_s)


def has_key(cfg: config.AppConfig) -> bool:
    """True si la clé du fournisseur configuré est présente."""
    return bool(config.get_secret(config.LLM_KEYS[cfg.llm_provider]))
