"""Editable publisher policy used for external event evidence."""

import hashlib
import ipaddress
import json
import re
import unicodedata
from functools import lru_cache
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import idna
import tldextract


DEFAULT_ALLOWED_SOURCES = (
    "https://www.iro-49.ru/",
    "https://kolyma.ru/",
    "https://kolymaplus.ru/",
    "https://lengu.ru/",
    "https://indo.usue.ru/",
    "https://vesti-magadan.ru/",
)

SOCIAL_DOMAINS = frozenset({
    "vk.com", "vk.ru", "t.me", "telegram.me", "telegram.org", "telegra.ph", "ok.ru",
    "max.ru", "rutube.ru", "youtube.com", "youtu.be", "facebook.com", "fb.com",
    "instagram.com", "twitter.com", "x.com", "tiktok.com", "dzen.ru", "zen.yandex.ru",
    "vkvideo.ru", "m.vk.com",
})
SHARED_ROOTS = {
    "amazonaws.com", "google.com", "googleapis.com", "gosuslugi.ru", "gosweb.gosuslugi.ru",
    "yandex.ru", "narod.ru", "ucoz.ru", "ucoz.com", "tilda.ws", "netlify.com", "wix.com",
    "wordpress.com", "livejournal.com", "sites.google.com",
}

# The marker survives removal of the final publisher and prevents reseeding an empty policy.
_seeds = " UNION ALL ".join(f"SELECT '{url}' AS base_url" for url in DEFAULT_ALLOWED_SOURCES)
SOURCE_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS factcheck_allowed_source (base_url TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS factcheck_source_bootstrap (id INTEGER PRIMARY KEY CHECK(id=1));
INSERT OR IGNORE INTO factcheck_allowed_source(base_url)
SELECT base_url FROM ({_seeds}) WHERE NOT EXISTS (SELECT 1 FROM factcheck_source_bootstrap);
INSERT OR IGNORE INTO factcheck_source_bootstrap(id) VALUES(1);
"""


@lru_cache(maxsize=1)
def _suffixes():
    return tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True, cache_dir=None)


@lru_cache(maxsize=1)
def _shared_parents():
    # A publisher cannot be the parent of a public or a private hosting suffix.
    parents = set(SHARED_ROOTS)
    for suffix in _suffixes().tlds:
        parts = suffix.lstrip("*.!").split(".")
        parents.update(".".join(parts[i:]) for i in range(1, len(parts)-1))
    return parents


def normalize_domain(value):
    if not isinstance(value, str):
        raise ValueError("Введите домен издателя")
    domain = value.strip().removesuffix(".")
    try:
        domain = idna.encode(domain, uts46=True, std3_rules=True).decode("ascii").lower()
    except (idna.IDNAError, UnicodeError):
        raise ValueError("Введите только домен издателя, без протокола, пути, порта и ключей") from None
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise ValueError("Укажите доменное имя издателя вместо IP-адреса")
    parts = _suffixes()(domain)
    if not parts.suffix or not parts.domain or domain in _shared_parents():
        raise ValueError("Нужен домен отдельного издателя с общедоступной доменной зоной")
    if any(domain == root or domain.endswith("."+root) for root in SOCIAL_DOMAINS):
        raise ValueError("Домен общей социальной платформы нельзя добавить как издателя")
    return domain


def _safe_path(raw):
    if re.search(r"%(?![\da-fA-F]{2})|%(?:2f|5c|25)", raw, re.I):
        raise ValueError("Недопустимое кодирование пути URL")
    decoded = unquote(raw, errors="strict")
    normalized = unicodedata.normalize("NFKC", decoded)
    if (re.search(r"[\\;#?\x00-\x1f\x7f∕⁄]", normalized) or "//" in normalized or
            normalized.count("/") != decoded.count("/") or
            any(part in (".", "..") for part in normalized.split("/"))):
        raise ValueError("В пути URL обнаружен небезопасный сегмент")
    return quote(decoded, safe="/:@!$&'()*+,-._~")


def canonical_source_url(value, *, base=True):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Введите базовый URL издателя с http:// или https://")
    value = value.strip()
    if re.search(r"[\\\x00-\x1f\x7f]", value):
        raise ValueError("Недопустимый символ в URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("Введите URL с http:// или https:// без логина и пароля")
    if base and ("?" in value or "#" in value):
        raise ValueError("Базовый URL не должен содержать параметры запроса или фрагмент")
    host = normalize_domain(parts.hostname)
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    if port not in (None, 80, 443):
        raise ValueError("Доступны стандартные HTTP(S) порты 80 и 443")
    authority = host if port in (None, default_port) else f"{host}:{port}"
    path = _safe_path(parts.path or "/")
    if base and not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme, authority, path, "", ""))


def checked_redirect(location):
    if re.search(r"[\\\x00-\x1f\x7f]", location):
        raise ValueError("Недопустимый символ в адресе перенаправления")
    _safe_path(urlsplit(location).path)


def get_allowed_sources(conn):
    return [row[0] for row in conn.execute("SELECT base_url FROM factcheck_allowed_source ORDER BY base_url")]


def school_hosts(conn):
    rows = conn.execute("""SELECT site_url FROM organization UNION SELECT final_url FROM crawl_fetch
                            WHERE depth=0 AND page_id IS NOT NULL AND final_url IS NOT NULL""")
    hosts = set()
    for (url,) in rows:
        try:
            host = urlsplit(url if "://" in url else "https://"+url).hostname
            if host:
                hosts.add(idna.encode(host.removesuffix("."), uts46=True, std3_rules=True).decode("ascii").lower().removeprefix("www."))
        except (ValueError, UnicodeError):
            continue
    return hosts


def is_school_host(host, hosts):
    return any(host==name or host.endswith("."+name) for name in hosts)


def add_allowed_source(conn, domain):
    normalized = canonical_source_url(domain)
    if is_school_host(urlsplit(normalized).hostname, school_hosts(conn)):
        raise ValueError("Сайт образовательной организации из базы нельзя добавить как независимый источник")
    return conn.execute("INSERT OR IGNORE INTO factcheck_allowed_source(base_url) VALUES(?)", (normalized,)).rowcount == 1


def remove_allowed_source(conn, domain):
    normalized = canonical_source_url(domain)
    return conn.execute("DELETE FROM factcheck_allowed_source WHERE base_url=?", (normalized,)).rowcount == 1


def snapshot_fingerprint(domains):
    payload = json.dumps(sorted(set(domains)), separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def allowlist_fingerprint(conn):
    return snapshot_fingerprint(get_allowed_sources(conn))


def match_publisher(url, rules):
    candidate = canonical_source_url(url, base=False)
    # Every rule ends in a slash: /news/ covers /news/item and /news, never /newsroom.
    return max((rule for rule in rules if candidate.startswith(rule) or candidate + "/" == rule),
               key=len, default=None)
