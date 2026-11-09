import httpx
from pathlib import Path

from .paths import DATA_DIR as ROOT

SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}

SEVERITY_RU = {
    "CRITICAL": "критично",
    "HIGH": "высокая важность",
    "MEDIUM": "заметно",
    "LOW": "мелочь",
    "INFO": "информационно",
}


def _enabled(settings):
    return settings.get("llm_enabled") == "1"


async def _request(settings, prompt, timeout=300):
    """Единая точка обращения к LLM (Ollama или OpenAI-совместимый API)"""
    mode = settings.get("llm_mode", "ollama")
    model = settings.get("llm_model", "qwen2.5:7b-instruct")

    async with httpx.AsyncClient(timeout=timeout) as client:
        if mode == "openai":
            base = (settings.get("llm_base_url") or "http://127.0.0.1:11434/v1").rstrip("/")
            url = base + "/chat/completions"
            headers = {"Content-Type": "application/json"}
            key = settings.get("llm_api_key", "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "stream": False,
            }
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        else:
            url = settings.get("llm_url") or "http://127.0.0.1:11434/api/generate"
            payload = {"model": model, "prompt": prompt, "stream": False}
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json().get("response", "")


def _findings_text(findings, limit=25):
    lines = []
    ordered = sorted(
        findings,
        key=lambda f: SEVERITY_ORDER.get(f.get("severity", "LOW"), 0),
        reverse=True,
    )
    for i, f in enumerate(ordered[:limit], 1):
        sev = SEVERITY_RU.get(f.get("severity", ""), f.get("severity", ""))
        lines.append(f"{i}. [{sev}] {f.get('message', '')} (блок: {f.get('agent', '')})")
    return "\n".join(lines) if lines else "Ошибок не найдено."


# ============================================================
# 1. Краткая сводка (используется при аудите)
# ============================================================

async def summarize(site, findings, settings):
    if not _enabled(settings):
        return ""
    if not findings:
        return "Критичных проблем не найдено."
    prompt = f"""Ты — помощник по аудиту сайтов школ и детских садов.
Сайт: {site.get('name')} (тип: {site.get('type')})

Найдены проблемы:
{_findings_text(findings)}

Составь краткую сводку (3–5 предложений) для администратора: общее состояние сайта и на что обратить внимание в первую очередь. На русском языке, без технического жаргона."""
    try:
        return await _request(settings, prompt)
    except Exception as e:
        return f"LLM недоступен или ошибка запроса: {e}"


# ============================================================
# 2. Подробное объяснение ошибок простым языком (НОВАЯ функция)
# ============================================================

async def explain(site, findings, settings):
    """Возвращает текст объяснения или None, если LLM выключен"""
    if not _enabled(settings):
        return None
    prompt = f"""Ты — методист-консультант, который помогает учителю вести сайт школы или детского сада.
Учитель — НЕ программист. Объясняй всё простыми, человеческими словами, как в памятке.

Сайт: {site.get('name')} (тип: {site.get('type')})
Аудит обнаружил следующие проблемы:
{_findings_text(findings)}

Составь ответ строго в такой структуре:

1. «ЧТО ОЗНАЧАЮТ ПРОБЛЕМЫ» — по каждой проблеме (похожие объедини) объясни в 1–2 предложения: что не так и почему это важно для школы, родителей и проверяющих.
2. «ПЛАН ИСПРАВЛЕНИЯ» — топ-3–5 действий по порядку важности. Для каждого действия подскажи конкретно: в какой раздел зайти, что добавить или загрузить.
3. «ЧТО МОЖНО НЕ ТРОГАТЬ ПОКА» — мелкие замечания, не требующие срочного вмешательства.

Пиши на русском языке, без технического жаргона."""
    return await _request(settings, prompt)


# ============================================================
# 3. Проверка соединения (кнопка в настройках)
# ============================================================

async def test(settings):
    """Возвращает (ok: bool, message: str)"""
    if not _enabled(settings):
        return False, "LLM выключен в настройках. Включите переключатель «Использовать LLM»."
    try:
        answer = await _request(settings, "Ответь ровно одним словом: работает.", timeout=60)
        return True, f"Соединение есть! Ответ модели: {answer.strip()[:100]}"
    except Exception as e:
        return False, f"Не удалось подключиться к LLM: {e}"


# ============================================================
# Сохранение объяснений в файл (чтобы не пропадали)
# ============================================================

def _explanation_path(site_id):
    return ROOT / "storage" / str(site_id) / "llm_explanation.txt"


def save_explanation(site_id, text):
    p = _explanation_path(site_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def load_explanation(site_id):
    p = _explanation_path(site_id)
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None
