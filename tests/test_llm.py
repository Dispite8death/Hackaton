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


def test_gateway_checker_parses_grounded_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDIT_CACHE_DIR", str(tmp_path))
    from audit.llm import GatewayChecker

    content = '```json\n{"event_date": "2026-09-21", "event_date_basis": "текст", "claims": [' \
              '{"claim": "a", "status": "confirmed", "source": null, "note": ""}]}\n```'
    payload = {"choices": [{"message": {"content": content, "annotations": [
        {"type": "url_citation", "url_citation": {"title": "atomic-energy.ru", "url": "https://x"}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    g = GatewayChecker("https://gw.example/v1", "k", "gemini-2.5-flash")
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(json)
        return NS(status_code=200, json=lambda: payload, text="")

    monkeypatch.setattr(g.requests, "post", fake_post)
    res = g.check_news("t", "текст", datetime(2026, 9, 28), "школа")
    assert res["event_date"] == date(2026, 9, 21)
    assert res["claims"][0]["status"] == "confirmed" and "atomic-energy.ru" in res["claims"][0]["source"]
    assert sent["tools"] == [{"googleSearch": {}}]
