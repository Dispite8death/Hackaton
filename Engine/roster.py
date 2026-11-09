"""Manage the active roster while retaining evidence and stable institution IDs."""
import csv
import io
import json
from urllib.parse import urlsplit, urlunsplit

from ai_audit_web import db as quick_db

FIELDS = ("name", "kind", "site_url")
KINDS = {"school":"school", "школа":"school", "kindergarten":"kindergarten", "детский сад":"kindergarten", "садик":"kindergarten"}


def address_key(url):
    parsed = urlsplit(url.strip())
    return (parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query)


def parse_rows(fmt, content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Файл пуст.")
    content = content.lstrip("\ufeff")
    if not content.strip():
        raise ValueError("Файл пуст.")
    if fmt == "json":
        try:
            records = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(f"Некорректный JSON. Строка {error.lineno}, позиция {error.colno}.") from error
        if not isinstance(records, list):
            raise ValueError("JSON должен содержать массив учреждений.")
    elif fmt == "csv":
        first = content.splitlines()[0]
        reader = csv.DictReader(io.StringIO(content, newline=""), delimiter=";" if ";" in first else ",", strict=True)
        try:
            if set(reader.fieldnames or ()) != set(FIELDS) or len(reader.fieldnames) != 3:
                raise ValueError("Нужен заголовок CSV: name,kind,site_url")
            records = list(reader)
        except csv.Error as exc:
            raise ValueError(f"Ошибка CSV в строке {reader.line_num}: {exc}") from exc
    elif fmt == "txt":
        records = []
        for n, line in enumerate(content.splitlines(), 1):
            if line.strip():
                fields = line.split("&", 2)
                if len(fields) != 3:
                    raise ValueError(f"Строка {n}: ожидается название & тип & ссылка.")
                records.append(dict(zip(FIELDS, fields)))
    else:
        raise ValueError("Поддерживаются JSON, CSV и TXT.")
    if not 0 < len(records) <= 5000:
        raise ValueError("За один импорт принимается от 1 до 5000 учреждений.")
    unique = {}
    for n, row in enumerate(records, 1):
        if not isinstance(row, dict) or set(row) != set(FIELDS) or not all(isinstance(v, str) for v in row.values()):
            raise ValueError(f"Запись {n}: нужны три текстовых поля name, kind, site_url.")
        name, kind, url = (row[k].strip() for k in FIELDS)
        if not name or len(name) > 600 or kind.casefold() not in KINDS:
            raise ValueError(f"Запись {n}: укажите название и тип school или kindergarten.")
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname and parsed.port != 0
                     and parsed.username is None and not parsed.fragment and not any(c.isspace() or ord(c)<32 for c in url)
                     and "\\" not in url and len(url) <= 2048)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f"Запись {n}: требуется адрес сайта с http:// или https:// без пароля и якоря.")
        url = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))
        key = address_key(url)
        record = (name, KINDS[kind.casefold()], url)
        if key in unique and unique[key][:2] != record[:2]:
            raise ValueError(f"Запись {n}: тот же сайт указан с другим названием или типом.")
        unique[key] = record
    return list(unique.values())


def organizations(conn):
    return [dict(row) for row in conn.execute("SELECT id,name,kind,site_url FROM active_organization ORDER BY name,id")]


def change(conn, data):
    operation = data.get("operation")
    if operation not in ("import", "add", "clear", "remove"):
        raise ValueError("Неизвестная операция с реестром.")
    rows = []
    if operation == "import":
        rows = parse_rows(data.get("format"), data.get("content"))
    elif operation == "add":
        rows = parse_rows("json", json.dumps([data.get("record")]))
    elif operation == "clear" and data.get("confirmed") is not True:
        raise ValueError("Подтвердите очистку списка учреждений.")
    conn.execute("ATTACH DATABASE ? AS quick", (str(quick_db.DB_PATH),))
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = updated = 0
            if operation == "clear":
                conn.execute("INSERT OR IGNORE INTO inactive_organization SELECT id FROM organization")
                conn.execute("UPDATE quick.sites SET active=0")
            elif operation == "remove":
                row = conn.execute("SELECT site_url FROM active_organization WHERE id=?", (data.get("id"),)).fetchone()
                if row is None:
                    raise ValueError("Учреждение отсутствует в списке.")
                conn.execute("INSERT INTO inactive_organization VALUES(?)", (data["id"],))
                ids = [(i,) for i,u in conn.execute("SELECT id,url FROM quick.sites") if address_key(u)==address_key(row[0])]
                conn.executemany("UPDATE quick.sites SET active=0 WHERE id=?", ids)
            else:
                archive = {address_key(u): i for i,u in conn.execute("SELECT id,site_url FROM organization")}
                quick = {address_key(u): i for i,u in conn.execute("SELECT id,url FROM quick.sites")}
                for name, kind, url in rows:
                    key = address_key(url)
                    if key in archive:
                        oid = archive[key]
                        conn.execute("UPDATE organization SET name=?,kind=?,site_url=? WHERE id=?", (name,kind,url,oid))
                        conn.execute("DELETE FROM inactive_organization WHERE organization_id=?", (oid,))
                        updated += 1
                    else:
                        conn.execute("INSERT INTO organization(name,kind,site_url) VALUES(?,?,?)", (name,kind,url))
                        inserted += 1
                    if key in quick:
                        conn.execute("UPDATE quick.sites SET name=?,type=?,url=?,active=1 WHERE id=?", (name,kind,url,quick[key]))
                    else:
                        conn.execute("INSERT INTO quick.sites(name,type,url) VALUES(?,?,?)", (name,kind,url))
        return {"organizations":organizations(conn), "inserted":inserted, "matched":updated}
    finally:
        conn.execute("DETACH DATABASE quick")
