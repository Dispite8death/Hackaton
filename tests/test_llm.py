"""ИИ-модуль без сети: подменяем клиент и проверяем разбор ответа и учёт токенов."""
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest

anthropic = pytest.importorskip("anthropic")

from audit.llm import LLMChecker  # noqa: E402


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _resp(stop, text=None):
    content = [NS(type="server_tool_use")] + ([NS(type="text", text=text)] if text else [])
    usage = NS(input_tokens=1000, output_tokens=200, server_tool_use=NS(web_search_requests=2))
    return NS(stop_reason=stop, content=content, usage=usage)


def test_llm_checker_parses_json_and_handles_pause(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    c = LLMChecker(model="claude-opus-5-5")
    body = ('{"event_date": "2026-10-23", "event_date_basis": "в тексте: 23 октября", "claims": ['
            '{"claim": "Атомный диктант 21.09", "status": "confirmed", "source": "https://example.org", "note": "ok"}]}')
    fake = FakeMessages([_resp("pause_turn"), _resp("end_turn", body)])
    c.client = NS(beta=NS(messages=fake))
    res = c.check_news("Ходили в музей", "23 октября дети посетили музей", datetime(2026, 10, 26), "МАДОУ №5")
    assert res["event_date"] == date(2026, 10, 23)
    assert res["claims"][0]["status"] == "confirmed"
    assert c.usage["requests"] == 2 and c.usage["web_searches"] == 4
    kw = fake.calls[0]
    assert kw["tools"][0]["type"] == "web_search_20260209"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert len(fake.calls[1]["messages"]) == 2      # пауза продолжена с ответом ассистента


def test_llm_checker_refusal_returns_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    c = LLMChecker()
    c.client = NS(beta=NS(messages=FakeMessages([_resp("refusal")])))
    assert c.check_news("t", "x", datetime(2026, 1, 1), "s") is None


def _settings(**kw):
    from audit.llm import LLMSettings
    base = dict(provider="openai", base_url="https://gw.example/v1", api_key="k", model="gemini-2.5-flash")
    base.update(kw)
    return LLMSettings(**base)


def test_gateway_checker_parses_grounded_answer(monkeypatch, tmp_path):
    from audit.llm import OpenAICompatChecker

    monkeypatch.setenv("AUDIT_CACHE_DIR", str(tmp_path))
    content = '```json\n{"event_date": "2026-09-21", "event_date_basis": "текст", "claims": [' \
              '{"claim": "a", "status": "confirmed", "source": null, "note": ""}]}\n```'
    payload = {"choices": [{"message": {"content": content, "annotations": [
        {"type": "url_citation", "url_citation": {"title": "atomic-energy.ru", "url": "https://x"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    g = OpenAICompatChecker(_settings())
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(json)
        return NS(status_code=200, json=lambda: payload, text="")

    monkeypatch.setattr(g.requests, "post", fake_post)
    res = g.check_news("t", "текст", datetime(2026, 9, 28), "школа")
    assert res["event_date"] == date(2026, 9, 21)
    assert res["claims"][0]["status"] == "confirmed" and "atomic-energy.ru" in res["claims"][0]["source"]
    assert sent["tools"] == [{"googleSearch": {}}]


def test_local_server_without_key_and_json_fallback(monkeypatch, tmp_path):
    """Локальный сервер (Ollama/LM Studio): без ключа, без веб-поиска; JSON-режим не поддержан -> повтор."""
    from audit.llm import OpenAICompatChecker

    monkeypatch.setenv("AUDIT_CACHE_DIR", str(tmp_path))
    g = OpenAICompatChecker(_settings(base_url="http://localhost:11434/v1", api_key="", model="qwen2.5:14b"))
    assert "Authorization" not in g.headers and not g.grounding
    assert g.url == "http://localhost:11434/v1/chat/completions"
    calls = []
    ok = {"choices": [{"message": {"content": '{"event_date": null, "event_date_basis": "", "claims": ['
                                              '{"claim": "c", "status": "contradiction", "source": "", "note": "n"}]}'}}]}

    def fake_post(url, headers, json, timeout):
        calls.append(dict(json))
        if "response_format" in json:
            return NS(status_code=400, json=lambda: {}, text="response_format not supported")
        return NS(status_code=200, json=lambda: ok, text="")

    monkeypatch.setattr(g.requests, "post", fake_post)
    res = g.check_news("t", "текст", datetime(2026, 9, 28), "школа")
    assert len(calls) == 2 and "response_format" not in calls[1]
    # без веб-поиска «расхождение» не засчитывается — это лишь мнение модели
    assert res["claims"][0]["status"] == "unverified" and "без веб-поиска" in res["claims"][0]["source"]


def test_settings_from_env(monkeypatch):
    from audit.llm import LLMSettings

    for k in ("AUDIT_LLM_PROVIDER", "AUDIT_LLM_BASE_URL", "AUDIT_LLM_MODEL", "AUDIT_LLM_WEB_SEARCH"):
        monkeypatch.delenv(k, raising=False)
    assert LLMSettings.from_env().provider == "anthropic"
    monkeypatch.setenv("AUDIT_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    s = LLMSettings.from_env(model="local-model")
    assert s.provider == "openai" and s.model == "local-model" and s.effective_web_search() == "off"
    monkeypatch.setenv("AUDIT_LLM_WEB_SEARCH", "google")
    assert LLMSettings.from_env().effective_web_search() == "google"
