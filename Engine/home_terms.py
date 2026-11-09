"""Manage the institution home-page photo vocabulary in the project's SQLite database."""

import argparse
import sqlite3
import unicodedata
from contextlib import closing


CATEGORY_LABELS = {
    "canteen_photo": "Фото столовой",
    "main_photo": "Главное фото или логотип школы",
}

# These phrases alone can describe a menu or a meal plan. A detector needs local
# evidence that the image represents the dining room before treating it as a candidate.
WEAK_CANTEEN_TERMS = frozenset({
    "питание", "питания", "питании", "питанием", "организация питания", "школьное питание", "горячее питание",
})

_DEFAULTS = {
    "canteen_photo": (
        "столовая", "столовой", "столовую", "столовые", "школьная столовая", "наша школьная столовая",
        "обеденный зал", "обеденного зала", "обеденном зале", "зал столовой", "обеденная зона",
        "зал для приёма пищи", "помещение для приёма пищи", "помещение столовой", "обеденные столы",
        "зона раздачи", "зоны раздачи", "линия раздачи", "линии раздачи", "пищеблок", "пищеблока",
        *sorted(WEAK_CANTEEN_TERMS),
    ),
    "main_photo": (
        "главное фото", "главное изображение", "главная фотография", "основное фото",
        "фото школы", "фотография школы", "изображение школы", "здание школы", "здания школы",
        "здание гимназии", "здание лицея", "фасад", "фасада", "фасад школы", "главный корпус",
        "центральный вход", "главный вход", "логотип школы", "эмблема школы", "герб школы",
        "логотип гимназии", "логотип лицея", "эмблема гимназии", "эмблема лицея",
    ),
}

_CATEGORY_TABLE = """
CREATE TABLE IF NOT EXISTS home_term_category (
    category TEXT PRIMARY KEY CHECK (category IN ('canteen_photo', 'main_photo'))
)"""
_TERM_TABLE = """
CREATE TABLE IF NOT EXISTS home_term (
    category TEXT NOT NULL REFERENCES home_term_category(category),
    term TEXT NOT NULL CHECK (length(term) > 0),
    PRIMARY KEY (category, term)
)"""
HOME_TERMS_SCHEMA_SQL = _CATEGORY_TABLE + ";\n" + _TERM_TABLE + ";\n"


def normalize_term(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


def initialize_terms(conn: sqlite3.Connection) -> None:
    """Create and seed each category once, preserving a deliberately empty vocabulary."""
    conn.execute("SAVEPOINT initialize_home_terms")
    try:
        conn.execute(_CATEGORY_TABLE)
        conn.execute(_TERM_TABLE)
        for category, defaults in _DEFAULTS.items():
            created = conn.execute(
                "INSERT INTO home_term_category(category) VALUES (?) ON CONFLICT(category) DO NOTHING",
                (category,),
            ).rowcount
            if created:
                conn.executemany("INSERT INTO home_term(category, term) VALUES (?, ?)",
                                 ((category, normalize_term(term)) for term in defaults))
    except BaseException:
        conn.execute("ROLLBACK TO initialize_home_terms")
        raise
    finally:
        conn.execute("RELEASE initialize_home_terms")


def get_terms(conn: sqlite3.Connection) -> dict[str, list[str]]:
    terms = {category: [] for category in CATEGORY_LABELS}
    for category, term in conn.execute("SELECT category, term FROM home_term ORDER BY category, term"):
        terms[category].append(term)
    return terms


def _key(category: str, text: str) -> tuple[str, str]:
    if category not in CATEGORY_LABELS:
        raise ValueError("Неизвестная категория")
    term = normalize_term(text)
    if not term:
        raise ValueError("Укажите непустое слово или фразу")
    return category, term


def add_term(conn: sqlite3.Connection, category: str, term: str) -> bool:
    return bool(conn.execute(
        "INSERT INTO home_term(category, term) VALUES (?, ?) ON CONFLICT(category, term) DO NOTHING",
        _key(category, term),
    ).rowcount)


def remove_term(conn: sqlite3.Connection, category: str, term: str) -> bool:
    return bool(conn.execute("DELETE FROM home_term WHERE category=? AND term=?", _key(category, term)).rowcount)


def main(argv=None) -> None:
    if __package__:
        from .config import connect_db
    else:
        from config import connect_db
    parser = argparse.ArgumentParser(description="Словарь поиска изображений на главной странице учреждения")
    actions = parser.add_subparsers(dest="action", required=True, title="Команды")
    show = actions.add_parser("list", help="показать слова одной или обеих категорий")
    show.add_argument("category", nargs="?", choices=CATEGORY_LABELS, help="категория поиска")
    add = actions.add_parser("add", help="добавить слово или фразу")
    add.add_argument("category", choices=CATEGORY_LABELS, help="категория поиска")
    add.add_argument("words", nargs="+", metavar="слово", help="слово или фраза")
    remove = actions.add_parser("remove", help="удалить слово или фразу")
    remove.add_argument("category", choices=CATEGORY_LABELS, help="категория поиска")
    remove.add_argument("words", nargs="*", metavar="слово", help="слово или фраза")
    remove.add_argument("--all", "--все", action="store_true", help="удалить все слова выбранной категории")
    args = parser.parse_args(argv)
    if args.action == "remove" and bool(args.words) == args.all:
        parser.error("Для удаления укажите либо фразу, либо --all")
    term = normalize_term(" ".join(getattr(args, "words", [])))
    if args.action != "list" and not getattr(args, "all", False) and not term:
        parser.error("Укажите непустое слово или фразу")
    with closing(connect_db()) as conn, conn:
        initialize_terms(conn)
        if args.action == "list":
            for category, words in get_terms(conn).items():
                if args.category and category != args.category:
                    continue
                print(f"{CATEGORY_LABELS[category]} ({category})")
                for word in words:
                    print("  " + word)
                if not words:
                    print("  Словарь пуст")
        elif args.action == "add":
            print("Добавлено" if add_term(conn, args.category, term) else "Уже есть в словаре")
        elif args.all:
            count = conn.execute("DELETE FROM home_term WHERE category=?", (args.category,)).rowcount
            print(f"Удалено записей: {count}")
        else:
            print("Удалено" if remove_term(conn, args.category, term) else "Фраза отсутствует в словаре")


if __name__ == "__main__":
    main()
