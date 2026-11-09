

from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

if __package__:
    from .config import PROJECT_DIR, SCHEMA_SQL, connect_db
else:
    from config import PROJECT_DIR, SCHEMA_SQL, connect_db


SOURCE = Path(PROJECT_DIR) / "Задание" / "Описание проблемы.docx"
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SECTION_KINDS = {"Школы": "school", "Детские сады": "kindergarten"}


def word_text(element: ElementTree.Element) -> str:
    return "".join(node.text or "" for node in element.iter(WORD + "t")).strip()


def read_organizations(path: Path) -> list[tuple[str, str, str]]:
    with ZipFile(path) as document:
        root = ElementTree.fromstring(document.read("word/document.xml"))

    organizations = []
    kind = None
    for block in root.find(WORD + "body"):
        if block.tag == WORD + "p":
            heading = word_text(block).rstrip(":")
            if heading:
                kind = SECTION_KINDS.get(heading)
        elif block.tag == WORD + "tbl" and kind:
            for row in block.findall(WORD + "tr")[1:]:  # Первая строка — заголовки столбцов.
                name, domain = map(word_text, row.findall(WORD + "tc"))
                if not name or not domain:
                    raise ValueError(f"Пустое название или домен в строке: {name!r}, {domain!r}")
                organizations.append((name, "https://" + domain.lower(), kind))
            kind = None

    if {row[2] for row in organizations} != set(SECTION_KINDS.values()):
        raise ValueError("В документе нужны обе заполненные таблицы: школы и детские сады")
    return organizations


def seed_if_empty(conn) -> None:
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM organization LIMIT 1").fetchone():
            conn.executemany("INSERT OR IGNORE INTO organization(name,site_url,kind) VALUES(?,?,?)",
                             read_organizations(SOURCE))


def import_organizations(path: Path = SOURCE) -> int:
    organizations = read_organizations(path)
    with closing(connect_db()) as conn, conn:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
        conn.executemany(
            """
            INSERT INTO organization (name, site_url, kind) VALUES (?, ?, ?)
            ON CONFLICT(site_url) DO UPDATE SET name = excluded.name, kind = excluded.kind
            """,
            organizations,
        )
    return len(organizations)


if __name__ == "__main__":
    print(f"Обработано учреждений: {import_organizations()}")
