"""ИИ-агент для факт-чекинга новостей по открытым источникам.

Два бэкенда (включаются флагом --llm):
  * OpenAI-совместимый шлюз (AUDIT_LLM_BASE_URL + AUDIT_LLM_API_KEY, модель AUDIT_LLM_MODEL, по умолчанию
    gemini-2.5-flash) — проверка через Google Search grounding, источники берутся из цитат ответа;
  * Anthropic API (ANTHROPIC_API_KEY или профиль `ant auth login`) — Claude с серверным веб-поиском.
Для каждой выбранной новости агент:
  * определяет дату события, о котором говорится в публикации;
  * выделяет проверяемые утверждения (даты, названия конкурсов, места, результаты);
  * проверяет их через веб-поиск (официальные сайты министерств, муниципалитетов, СМИ);
  * возвращает строгий JSON.
Без ключа краулер работает полностью офлайн-эвристиками (factcheck.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_GATEWAY_MODEL = "gemini-2.5-flash"

SCHEMA = {
    "type": "object",
    "properties": {
        "event_date": {"type": ["string", "null"], "description": "YYYY-MM-DD или null"},
        "event_date_basis": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "status": {"type": "string", "enum": ["confirmed", "contradiction", "unverified"]},
                    "source": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["claim", "status", "source", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["event_date", "event_date_basis", "claims"],
    "additionalProperties": False,
}

SYSTEM = (
    "Ты — аудитор официальных сайтов образовательных учреждений Магаданской области. "
    "Тебе дают новость с сайта школы или детского сада. Задачи: "
    "1) определить дату события, о котором рассказывает новость (не дату публикации); если в тексте её нет "
    "и вывести нельзя — null; "
    "2) выделить до 4 проверяемых фактических утверждений (даты праздников и всероссийских/региональных "
    "мероприятий, названия и итоги конкурсов, юбилеи, упомянутые организации); "
    "3) проверить каждое веб-поиском по открытым источникам (приоритет — официальные сайты органов власти, "
    "организаторов, региональные СМИ). Статус contradiction ставь только при явном расхождении с источником, "
    "unverified — если подтверждения не нашлось. В поле source укажи URL или название источника. "
    "Отвечай по-русски, кратко."
)


class LLMChecker:
    def __init__(self, model: str = DEFAULT_MODEL, max_searches: int = 3):
        import anthropic  # импорт здесь: без флага --llm пакет не нужен

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.max_searches = max_searches
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0, "web_searches": 0}

    def check_news(self, title: str, text: str, published: datetime, site_name: str) -> dict | None:
        prompt = (f"Учреждение: {site_name}\nДата публикации: {published:%Y-%m-%d %H:%M}\n"
                  f"Заголовок: {title}\n\nТекст новости:\n{text[:6000]}")
        messages = [{"role": "user", "content": prompt}]
        try:
            for _ in range(4):  # pause_turn у серверного веб-поиска — продолжаем
                resp = self.client.beta.messages.create(
                    model=self.model,
                    max_tokens=4000,
                    system=SYSTEM,
                    messages=messages,
                    tools=[{"type": "web_search_20260209", "name": "web_search",
                            "max_uses": self.max_searches, "user_location": {
                                "type": "approximate", "country": "RU", "region": "Magadan Oblast",
                                "timezone": "Asia/Magadan"}}],
                    output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
                self._account(resp)
                if resp.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                break
        except self.anthropic.RateLimitError as e:
            log.warning("LLM rate limit: %s", e)
            return None
        except self.anthropic.APIStatusError as e:
            log.warning("LLM API error %s: %s", e.status_code, e.message)
            return None
        except self.anthropic.APIConnectionError as e:
            log.warning("LLM connection error: %s", e)
            return None
        if resp.stop_reason == "refusal":
            return None
        text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        if not text_blocks:
            return None
        raw = text_blocks[-1]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return None
            try:
                data = json.loads(m[0])
            except json.JSONDecodeError:
                return None
        ev = data.get("event_date")
        try:
            data["event_date"] = date.fromisoformat(ev) if ev else None
        except ValueError:
            data["event_date"] = None
        for c in data.get("claims", []):
            c["source"] = f"ИИ-агент + веб-поиск: {c.get('source', '')}".strip()
        return data

    def _account(self, resp) -> None:
        u = resp.usage
        self.usage["requests"] += 1
        self.usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
        self.usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        if stu is not None:
            self.usage["web_searches"] += getattr(stu, "web_search_requests", 0) or 0


class GatewayChecker:
    """OpenAI-совместимый /chat/completions. Для моделей Gemini включается Google Search grounding."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120):
        import requests

        self.requests = requests
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.model = model
        self.timeout = timeout
        self.grounding = "gemini" in model.lower()
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0, "web_searches": 0}

    def check_news(self, title: str, text: str, published: datetime, site_name: str) -> dict | None:
        prompt = (f"Учреждение: {site_name}\nДата публикации: {published:%Y-%m-%d %H:%M}\n"
                  f"Заголовок: {title}\n\nТекст новости:\n{text[:6000]}\n\n"
                  "Верни ТОЛЬКО JSON без пояснений по схеме: "
                  + json.dumps(SCHEMA["properties"], ensure_ascii=False))
        body = {"model": self.model, "max_tokens": 4000, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
        if self.grounding:
            body["tools"] = [{"googleSearch": {}}]
        else:
            body["response_format"] = {"type": "json_object"}
        cache = _cache_file(self.model, prompt)
        if cache.exists():
            self.usage["cached"] = self.usage.get("cached", 0) + 1
            return _normalize(json.loads(cache.read_text(encoding="utf-8")))
        for attempt in range(3):
            try:
                r = self.requests.post(self.url, headers=self.headers, json=body, timeout=self.timeout)
            except self.requests.RequestException as e:
                log.warning("LLM-шлюз недоступен: %s", e)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                import time
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code != 200:
                log.warning("LLM-шлюз HTTP %s: %s", r.status_code, r.text[:200])
                return None
            break
        else:
            return None
        d = r.json()
        self.usage["requests"] += 1
        u = d.get("usage") or {}
        self.usage["input_tokens"] += u.get("prompt_tokens", 0) or 0
        self.usage["output_tokens"] += u.get("completion_tokens", 0) or 0
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        cites = [a["url_citation"] for a in msg.get("annotations") or [] if a.get("type") == "url_citation"]
        if cites:
            self.usage["web_searches"] += 1
        data = _parse_json(msg.get("content") or "")
        if data is None:
            return None
        sources = ", ".join(dict.fromkeys(c.get("title", "") for c in cites if c.get("title")))[:200]
        for c in data.get("claims", []):
            src = str(c.get("source") or "").strip()
            if src.lower() in ("", "null", "none", "-"):
                src = sources
            c["source"] = f"ИИ ({self.model}) + Google Search: {src[:200] or 'источник не найден'}"
            if c.get("status") not in ("confirmed", "contradiction", "unverified"):
                c["status"] = "unverified"
            if c["status"] == "confirmed" and not cites:
                c["status"] = "unverified"   # без найденных источников «подтверждено» не засчитываем
            c.setdefault("note", "")
            c.setdefault("claim", "")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return _normalize(data)


def _cache_file(model: str, prompt: str):
    """Ответы ИИ кэшируются по (модель, текст запроса): повторный прогон не тратит токены."""
    import hashlib
    from pathlib import Path

    h = hashlib.sha1(f"{model}\n{SYSTEM}\n{prompt}".encode("utf-8")).hexdigest()
    return Path(os.environ.get("AUDIT_CACHE_DIR", ".cache")) / "llm" / f"{h}.json"


def _parse_json(raw: str) -> dict | None:
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    for cand in (raw, (re.search(r"\{.*\}", raw, re.S) or [None])[0]):
        if not cand:
            continue
        try:
            d = json.loads(cand)
            return d if isinstance(d, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def _normalize(data: dict) -> dict:
    ev = data.get("event_date")
    try:
        data["event_date"] = date.fromisoformat(str(ev)[:10]) if ev else None
    except ValueError:
        data["event_date"] = None
    data.setdefault("event_date_basis", "")
    data["claims"] = [c for c in data.get("claims") or [] if isinstance(c, dict)][:6]
    return data


def make_checker(enabled: bool, model: str | None = None):
    if not enabled:
        return None
    base, key = os.environ.get("AUDIT_LLM_BASE_URL"), os.environ.get("AUDIT_LLM_API_KEY")
    try:
        if base and key:
            return GatewayChecker(base, key, model or os.environ.get("AUDIT_LLM_MODEL") or DEFAULT_GATEWAY_MODEL)
        return LLMChecker(model or os.environ.get("AUDIT_LLM_MODEL") or DEFAULT_MODEL)
    except Exception as e:  # noqa: BLE001 — нет пакета/ключа: работаем без ИИ
        log.warning("ИИ-проверка отключена: %s", e)
        return None
