"""Tests de src/llm.py : requêtes HTTP simulées pour Gemini et Groq, faux SDK pour Anthropic."""
import json
from types import SimpleNamespace

import pytest
import requests

from src import config, llm

TOOL = llm.ToolSpec("t", "test", {"type": "object", "properties": {
    "a": {"type": ["string", "null"], "description": "x"},
    "d": {"type": ["string", "null"], "format": "date"},
    "e": {"type": "string", "enum": ["x", "y"]},
    "l": {"type": "array", "items": {"type": "object", "properties": {"k": {"type": "integer"}}, "required": ["k"]}},
}, "required": ["a", "e"]})


class Resp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = json.dumps(data) if data is not None else ""

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


@pytest.fixture
def post(monkeypatch):
    calls, queue = [], []

    def fake(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm.requests, "post", fake)
    return SimpleNamespace(calls=calls, queue=queue)


def gemini_text(text, usage=(10, 5)):
    return Resp(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}],
                      "usageMetadata": {"promptTokenCount": usage[0], "candidatesTokenCount": usage[1]}})


def test_to_gemini_schema():
    s = llm.to_gemini_schema(TOOL.parameters)
    assert s["type"] == "OBJECT" and s["required"] == ["a", "e"]
    assert s["properties"]["a"] == {"type": "STRING", "nullable": True, "description": "x"}
    assert "AAAA-MM-JJ" in s["properties"]["d"]["description"]
    assert s["properties"]["e"]["enum"] == ["x", "y"]
    assert s["properties"]["l"]["items"]["properties"]["k"]["type"] == "INTEGER"


def test_gemini_structured(post):
    post.queue.append(gemini_text('```json\n{"a": "ok", "e": "x"}\n```'))
    client = llm.GeminiClient("KEY", "gemini-2.5-flash")
    data, usage = client.structured("sys", "user", TOOL)
    assert data == {"a": "ok", "e": "x"} and (usage.input_tokens, usage.output_tokens) == (10, 5)
    call = post.calls[0]
    assert call["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert call["headers"]["x-goog-api-key"] == "KEY"
    assert call["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_retries_on_429_then_succeeds(post):
    slept = []
    post.queue += [Resp(429, {"error": {"details": [{"retryDelay": "7s"}]}}), gemini_text('{"a": null, "e": "y"}')]
    data, _ = llm.GeminiClient("K", "m", sleep=slept.append).structured("s", "u", TOOL)
    assert data["e"] == "y" and slept == [8.0]


def test_gemini_429_exhausted_is_fatal(post):
    post.queue += [Resp(429, {})] * (llm.RATE_LIMIT_RETRIES + 1)
    with pytest.raises(llm.LLMError, match="Limite") as e:
        llm.GeminiClient("SECRET", "m", sleep=lambda s: None).structured("s", "u", TOOL)
    assert e.value.fatal and "SECRET" not in str(e.value)


@pytest.mark.parametrize("resp, msg, fatal", [
    (Resp(403, {}), "Clé Gemini refusée", True),
    (Resp(404, {}), "Modèle introuvable", True),
    (Resp(400, {"error": {"message": "bad schema"}}), "bad schema", False),
    (requests.Timeout(), "n'a pas répondu", False),
])
def test_gemini_errors(post, resp, msg, fatal):
    post.queue.append(resp)
    with pytest.raises(llm.LLMError, match=msg) as e:
        llm.GeminiClient("K", "m").structured("s", "u", TOOL)
    assert e.value.fatal is fatal


def test_gemini_invalid_json_returns_none(post):
    post.queue.append(gemini_text("pas du json"))
    assert llm.GeminiClient("K", "m").structured("s", "u", TOOL)[0] is None


def test_gemini_chat_with_tool_call(post):
    post.queue += [
        Resp(200, {"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "search_jobs", "args": {"city": "Paris"}}, "thoughtSignature": "sig"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2}}),
        gemini_text("Voici 1 offre.", (4, 6)),
    ]
    seen = []
    res = llm.GeminiClient("K", "m").chat("sys", [{"role": "user", "content": "offres à Paris ?"}], [TOOL],
                                          lambda n, a: seen.append((n, a)) or [{"title": "X"}])
    assert res.text == "Voici 1 offre." and seen == [("search_jobs", {"city": "Paris"})]
    assert (res.usage.input_tokens, res.usage.output_tokens) == (7, 8)
    second = post.calls[1]["json"]["contents"]
    assert second[1]["parts"][0]["thoughtSignature"] == "sig"  # contenu du modèle renvoyé tel quel
    assert second[2]["parts"][0]["functionResponse"]["response"] == {"result": [{"title": "X"}]}


def test_tool_exception_is_returned_to_model(post):
    post.queue += [Resp(200, {"candidates": [{"content": {"parts": [{"functionCall": {"name": "x", "args": {}}}]}}]}),
                   gemini_text("ok")]

    def boom(n, a):
        raise ValueError("mauvais filtre")
    llm.GeminiClient("K", "m").chat("s", [{"role": "user", "content": "q"}], [TOOL], boom)
    assert "mauvais filtre" in json.dumps(post.calls[1]["json"])


def test_groq_structured_uses_json_mode_and_compact_schema(post):
    post.queue.append(Resp(200, {"choices": [{"message": {"content": '{"a": "v", "e": "x"}'}}],
                                 "usage": {"prompt_tokens": 9, "completion_tokens": 4}}))
    client = llm.GroqClient("GK", "llama")
    data, usage = client.structured("consigne", "u", TOOL)
    assert data == {"a": "v", "e": "x"} and usage.input_tokens == 9
    body = post.calls[0]["json"]
    assert post.calls[0]["headers"]["Authorization"] == "Bearer GK"
    assert body["response_format"] == {"type": "json_object"} and "tools" not in body
    system = body["messages"][0]["content"]
    assert system.startswith("consigne") and '- e ("x" | "y")' in system and "- l (liste d'objets {k})" in system


def test_groq_invalid_json_is_retried_not_fatal(post):
    post.queue.append(Resp(400, {"error": {"message": "Failed to validate JSON", "code": "json_validate_failed"}}))
    assert llm.GroqClient("K", "m").structured("s", "u", TOOL) == (None, llm.Usage())


def test_groq_other_400_still_raises(post):
    post.queue.append(Resp(400, {"error": {"message": "bad model param", "code": "invalid_request"}}))
    with pytest.raises(llm.LLMError, match="bad model param") as e:
        llm.GroqClient("K", "m").structured("s", "u", TOOL)
    assert e.value.code == "invalid_request"


def test_groq_waits_for_rate_limit_from_message(post):
    slept = []
    post.queue += [Resp(429, {"error": {"message": "Rate limit reached on tokens per minute. "
                                                   "Please try again in 7.66s.", "code": "rate_limit_exceeded"}}),
                   Resp(429, {"error": {"message": "Please try again in 1m2.5s"}}),
                   Resp(200, {"choices": [{"message": {"content": '{"a": null, "e": "y"}'}}], "usage": {}})]
    data, _ = llm.GroqClient("K", "m", sleep=slept.append).structured("s", "u", TOOL)
    assert data["e"] == "y" and slept == [pytest.approx(8.16), pytest.approx(63.0)]


def test_groq_chat_with_simple_schema_and_retry(post):
    post.queue += [
        Resp(400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed"}}),
        Resp(200, {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c2", "function": {"name": "get_job", "arguments": '{"job_id": "j1"}'}}]}}], "usage": {}}),
        Resp(200, {"choices": [{"message": {"content": "Réponse"}}], "usage": {}}),
    ]
    res = llm.GroqClient("K", "m").chat("s", [{"role": "user", "content": "q"}], [TOOL], lambda n, a: {"ok": a["job_id"]})
    assert res.text == "Réponse"
    params = post.calls[0]["json"]["tools"][0]["function"]["parameters"]
    assert params["properties"]["a"]["type"] == "string" and None not in params["properties"]["e"]["enum"]
    assert post.calls[2]["json"]["messages"][-1] == {"role": "tool", "tool_call_id": "c2", "content": '{"ok": "j1"}'}


def test_to_simple_schema_removes_nulls():
    from src import chat
    s = llm.to_simple_schema(chat.SEARCH_SCHEMA)
    assert s["properties"]["work_mode"] == {"type": "string", "enum": ["onsite", "hybrid", "remote", "unknown"]}
    assert s["properties"]["limit"]["type"] == "integer"


def test_describe_schema_lists_every_extraction_field():
    from src import extract
    text = llm.describe_schema(extract.EXTRACTION_SCHEMA)
    assert all(f"- {name} (" in text for name in extract.EXTRACTION_SCHEMA["properties"])


def test_anthropic_structured_and_chat():
    def block(**kw):
        return SimpleNamespace(**kw)

    responses = [
        SimpleNamespace(content=[block(type="tool_use", name="t", input={"a": "z", "e": "x"}, id="u0")],
                        stop_reason="tool_use", usage=SimpleNamespace(input_tokens=5, output_tokens=2)),
        SimpleNamespace(content=[block(type="tool_use", name="search_jobs", input={"city": "Paris"}, id="u1")],
                        stop_reason="tool_use", usage=SimpleNamespace(input_tokens=5, output_tokens=2)),
        SimpleNamespace(content=[block(type="text", text="Fini")], stop_reason="end_turn",
                        usage=SimpleNamespace(input_tokens=1, output_tokens=1)),
    ]
    sent = []
    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.append(kw) or responses.pop(0)))
    client = llm.AnthropicClient("K", "claude-haiku-5-5", sdk_client=sdk)
    assert client.structured("s", "u", TOOL)[0] == {"a": "z", "e": "x"}
    assert "tool_choice" not in sent[0]
    res = client.chat("s", [{"role": "user", "content": "q"}], [TOOL], lambda n, a: ["r"])
    assert res.text == "Fini" and res.usage.input_tokens == 6
    assert sent[2]["messages"][-1]["content"][0]["tool_use_id"] == "u1"


def test_make_client_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = config.AppConfig()
    assert not llm.has_key(cfg)
    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY absente") as e:
        llm.make_client(cfg)
    assert e.value.fatal
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert isinstance(llm.make_client(cfg), llm.GeminiClient)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert isinstance(llm.make_client(config.AppConfig(llm_provider="groq", model="llama")), llm.GroqClient)


def test_usage_cost():
    u = llm.Usage(1_000_000, 2_000_000)
    assert u.cost_usd(config.AppConfig()) == 0
    assert u.cost_usd(config.AppConfig(price_input_per_mtok=0.1, price_output_per_mtok=0.5)) == pytest.approx(1.1)


def test_groq_retired_model_is_replaced_automatically(post, monkeypatch):
    class Models:
        status_code = 200

        def json(self):
            return {"data": [{"id": "whisper-large-v3"}, {"id": "llama-3.1-8b-instant"}, {"id": "openai/gpt-oss-120b"}]}

    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: Models())
    post.queue += [Resp(404, {"error": {"message": "The model `llama-3.3-70b-versatile` has been decommissioned",
                                        "code": "model_decommissioned"}}),
                   Resp(200, {"choices": [{"message": {"content": '{"a": "v", "e": "x"}'}}], "usage": {}})]
    client = llm.GroqClient("K", "llama-3.3-70b-versatile")
    data, _ = client.structured("s", "u", TOOL)
    assert data == {"a": "v", "e": "x"} and client.model == "openai/gpt-oss-120b"
    assert "openai/gpt-oss-120b" in client.notice
    assert post.calls[1]["json"]["model"] == "openai/gpt-oss-120b"
    assert post.calls[1]["json"]["reasoning_effort"] == "low"


def test_groq_404_without_replacement_is_readable(post, monkeypatch):
    monkeypatch.setattr(llm.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()))
    post.queue.append(Resp(404, {"error": {"message": "model gone"}}))
    with pytest.raises(llm.LLMError, match="Modèle introuvable.*model gone") as e:
        llm.GroqClient("K", "old").structured("s", "u", TOOL)
    assert e.value.fatal


def test_no_reasoning_effort_for_other_models(post):
    post.queue.append(Resp(200, {"choices": [{"message": {"content": "{}"}}], "usage": {}}))
    llm.GroqClient("K", "llama-3.1-8b-instant").structured("s", "u", TOOL)
    assert "reasoning_effort" not in post.calls[0]["json"]
