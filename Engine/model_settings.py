"""Application-wide model profiles. Secrets are stored in the user's OS credential store."""

import ipaddress
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit, urlunsplit


PROVIDERS = {"lmstudio": "LM Studio", "ollama": "Ollama", "compatible": "OpenAI-совместимый API"}
MODEL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_setting (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_profile (provider TEXT PRIMARY KEY, settings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_assessment (
    id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES page(id), check_id TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT(unixepoch()), provider TEXT NOT NULL, endpoint TEXT NOT NULL,
    model TEXT NOT NULL, outcome TEXT NOT NULL, request TEXT NOT NULL, response TEXT,
    error TEXT, elapsed_seconds REAL NOT NULL, usage_json TEXT
);
CREATE INDEX IF NOT EXISTS model_assessment_page ON model_assessment(page_id,check_id,id);
CREATE TABLE IF NOT EXISTS model_batch_result (
    page_id INTEGER NOT NULL REFERENCES page(id),check_id TEXT NOT NULL,
    profile_key TEXT NOT NULL,input_key TEXT NOT NULL,assessment_id INTEGER NOT NULL REFERENCES model_assessment(id),
    PRIMARY KEY(page_id,check_id,profile_key,input_key)
);
CREATE TABLE IF NOT EXISTS model_review_guard (
    assessment_id INTEGER PRIMARY KEY REFERENCES model_assessment(id),
    original_response TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_visual_verification (
    assessment_id INTEGER PRIMARY KEY REFERENCES model_assessment(id) ON DELETE CASCADE,
    status TEXT NOT NULL, guided_pass TEXT NOT NULL, target_pass TEXT
);
CREATE TABLE IF NOT EXISTS model_evidence_pass (
    assessment_id INTEGER NOT NULL REFERENCES model_assessment(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, material_ids TEXT NOT NULL, outcome TEXT, evidence TEXT,
    error TEXT, usage_json TEXT, raw_response TEXT,
    PRIMARY KEY(assessment_id,ordinal)
);
"""


@dataclass(frozen=True)
class Profile:
    provider: str
    endpoint: str
    model: str
    timeout: int = 120
    response_mode: str = "json_schema"
    disable_thinking: bool = False


DEFAULTS = {
    "lmstudio": Profile("lmstudio", "http://127.0.0.1:1234/v1", "qwen/qwen3.5-9b",disable_thinking=True),
    "ollama": Profile("ollama", "http://127.0.0.1:11434/v1", "qwen3.5:9b-q4_K_M",disable_thinking=True),
    "compatible": Profile("compatible", "", ""),
}


def local_host(host):
    if host == "localhost" or (host or "").endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private and not (address.is_unspecified or address.is_multicast)


def checked(profile, *, require_model=True):
    if profile.provider not in DEFAULTS:
        raise ValueError("Неизвестный тип подключения")
    url = urlsplit(profile.endpoint.strip())
    if (url.scheme not in ("http","https") or not url.hostname or url.username is not None
            or url.password is not None or url.fragment or url.query or url.port == 0):
        raise ValueError("Укажите базовый URL API с http:// или https:// без ключа и параметров")
    if profile.provider != "compatible" and not local_host(url.hostname):
        raise ValueError("Для локального сервера укажите localhost или IP локальной сети")
    if url.scheme == "http" and not local_host(url.hostname):
        raise ValueError("Внешний API требует HTTPS")
    if require_model and not profile.model.strip():
        raise ValueError("Выберите модель или введите её идентификатор")
    if not 5 <= profile.timeout <= 900:
        raise ValueError("Тайм-аут должен быть от 5 до 900 секунд")
    if profile.response_mode not in ("json_schema","json_object","prompt"):
        raise ValueError("Неизвестный формат генерации")
    path = url.path.rstrip("/") or "/v1"
    return Profile(profile.provider, urlunsplit((url.scheme,url.netloc,path,"","")),
                   profile.model.strip(), profile.timeout, profile.response_mode, profile.disable_thinking)


def setting(conn, name, default):
    row = conn.execute("SELECT value FROM app_setting WHERE name=?", (name,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(conn, name, value):
    conn.execute("""INSERT INTO app_setting VALUES(?,?) ON CONFLICT(name)
                    DO UPDATE SET value=excluded.value""", (name,json.dumps(value,ensure_ascii=False)))


def get_profile(conn, provider=None):
    provider = provider or setting(conn, "active_provider", "lmstudio")
    row = conn.execute("SELECT settings FROM model_profile WHERE provider=?", (provider,)).fetchone()
    return Profile(**json.loads(row[0])) if row else DEFAULTS[provider]


def save_profile(conn, profile):
    profile = checked(profile)
    conn.execute("""INSERT INTO model_profile VALUES(?,?) ON CONFLICT(provider)
                    DO UPDATE SET settings=excluded.settings""", (profile.provider,json.dumps(asdict(profile))))
    set_setting(conn, "active_provider", profile.provider)


def _credential_name(profile):
    return f"{profile.provider} {checked(profile, require_model=False).endpoint}"


def get_secret(profile):
    import keyring
    return keyring.get_password("Hackaton site audit", _credential_name(profile)) or ""


def active_secret(conn, profile):
    return get_secret(profile) if setting(conn,"credential:"+_credential_name(profile),False) else ""


def set_secret(profile, secret):
    import keyring
    name = _credential_name(profile)
    if secret:
        keyring.set_password("Hackaton site audit", name, secret)
    elif keyring.get_password("Hackaton site audit", name) is not None:
        keyring.delete_password("Hackaton site audit", name)


def save_secret(conn, profile, secret):
    set_secret(profile, secret)
    set_setting(conn, "credential:"+_credential_name(profile), bool(secret))


def portal_connection(conn):
    """Opt-in is specific to this installation; no credential is bundled with the portal."""
    if not setting(conn, "portal_ai_enabled", False):
        return "ИИ выключен", None, ""
    try:
        profile = checked(get_profile(conn))
        key = active_secret(conn, profile)
    except Exception:
        return "Проверьте настройки подключения ИИ", None, ""
    if profile.provider == "compatible" and not local_host(urlsplit(profile.endpoint).hostname) and not key.strip():
        return "Для ИИ нужен API-ключ", None, ""
    return "ИИ настроен", profile, key
