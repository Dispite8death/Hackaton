import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from home_terms import HOME_TERMS_SCHEMA_SQL, add_term, get_terms, initialize_terms, remove_term


class HomeTermsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.conn.close)

    def test_schema_inside_existing_project_transaction(self):
        self.conn.executescript("BEGIN IMMEDIATE;\n" + HOME_TERMS_SCHEMA_SQL)
        initialize_terms(self.conn)
        self.assertIn("зал для приема пищи", get_terms(self.conn)["canteen_photo"])
        self.assertIn("фасад", get_terms(self.conn)["main_photo"])
        self.conn.rollback()
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='home_term'").fetchone())

    def test_removed_defaults_and_added_phrases_survive_reinitialization(self):
        initialize_terms(self.conn)
        self.assertTrue(remove_term(self.conn, "canteen_photo", "  ЗАЛ  для ПРИЁМА  пищи "))
        self.assertFalse(remove_term(self.conn, "canteen_photo", "зал для приема пищи"))
        self.assertTrue(add_term(self.conn, "main_photo", "  Аллёя\tшколы "))
        self.assertFalse(add_term(self.conn, "main_photo", "аллея школы"))
        with self.assertRaises(ValueError):
            add_term(self.conn, "canteen_photo", " \t ")
        with self.assertRaises(ValueError):
            remove_term(self.conn, "unknown", "главная")
        initialize_terms(self.conn)
        terms = get_terms(self.conn)
        self.assertNotIn("зал для приема пищи", terms["canteen_photo"])
        self.assertIn("аллея школы", terms["main_photo"])
        self.assertNotIn("аллея школы", terms["canteen_photo"])

    def test_cli_clear_all_add_list_remove_in_persistent_database(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as temporary:
            path = Path(temporary.name)
        try:
            env = dict(os.environ, HACKATON_DB_PATH=str(path), PYTHONIOENCODING="utf-8")

            def run(*args):
                return subprocess.check_output(
                    [sys.executable, "-B", str(Path(__file__).with_name("home_terms.py")), *args],
                    env=env, encoding="utf-8",
                )

            run("remove", "canteen_photo", "--all")
            self.assertIn("Словарь пуст", run("list", "canteen_photo"))
            self.assertNotIn("Словарь пуст", run("list", "main_photo"))
            self.assertIn("Добавлено", run("add", "canteen_photo", "Зал", "для", "обедов"))
            self.assertIn("зал для обедов", run("list"))
            self.assertIn("Удалено", run("remove", "canteen_photo", "зал для обедов"))
            self.assertIn("Словарь пуст", run("list", "canteen_photo"))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
