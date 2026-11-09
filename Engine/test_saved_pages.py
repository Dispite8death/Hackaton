import sqlite3
import unittest

from crawl_store import initialize_db, record_fetch, start_run
from navigation import Link
from saved_pages import get_homepage_html, get_organization_id, get_snapshot_html, section_pages


class SavedPagesTest(unittest.TestCase):
    def test_lookup_by_name_handles_unknown_and_ambiguous_names(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        initialize_db(db)
        name = "Школа 'Восток'"
        db.execute("INSERT INTO organization (name, site_url, kind) VALUES (?, ?, 'school')",
                   (name, "https://one.example"))
        self.assertEqual(get_organization_id(db, name), 1)
        self.assertIsNone(get_organization_id(db, "Школа"))
        db.execute("INSERT INTO organization (name, site_url, kind) VALUES (?, ?, 'school')",
                   (name, "https://two.example"))
        with self.assertRaisesRegex(ValueError, "1, 2"):
            get_organization_id(db, name)

    def test_latest_home_and_section_indexes_across_runs(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        initialize_db(db)
        site = "https://school.example/"
        with db:
            db.execute("INSERT INTO organization (name, site_url, kind) VALUES ('Школа', ?, 'school')", (site,))
        links = [Link(site + "news/", "Новости", "news", "section", True),
                 Link(site + "archive/", "Архив новостей", "news", "section", True),
                 Link(site + "story/", "Событие", "news", "detail", True)]
        for version in ("первый", "второй"):
            run = start_run(db, 1)
            record_fetch(db, run, site, "home", 0, status=200, html=version, links=links, loaded_at=100)
            record_fetch(db, run, site + "news/", "news", 1, status=200, html=version, loaded_at=100)
            record_fetch(db, run, site + "archive/", "news", 1, final_url=site + "old/",
                         status=200, html="Архив")
            record_fetch(db, run, site + "story/", "news", 2, status=200, html="Статья")
        run = start_run(db, 1)
        record_fetch(db, run, site, "home", 0, status=404, error="not found", loaded_at=110)
        self.assertEqual(get_homepage_html(db, 1), "второй")
        self.assertIsNone(get_homepage_html(db, 2))
        pages = section_pages(db, 1, "news")
        self.assertEqual([p.url for p in pages], [site + "news/", site + "old/"])
        self.assertEqual(get_snapshot_html(db, pages[0].id), "второй")


if __name__ == "__main__":
    unittest.main()
