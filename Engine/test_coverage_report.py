import sqlite3
import unittest

from coverage_report import coverage, coverage_counts
from crawl_store import initialize_db, record_fetch, start_run
from navigation import Link


class CoverageTest(unittest.TestCase):
    def test_only_this_runs_section_pages_headings_and_internal_links_count(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        initialize_db(db)
        site = "https://garden.example/"
        with db:
            db.execute("INSERT INTO organization(name, site_url, kind) VALUES ('Сад', ?, 'kindergarten')", (site,))
        previous = start_run(db, 1)
        record_fetch(db, previous, site, "home", 0, status=200, html="home", links=[
            Link(site + "contacts/", "Контакты", "contacts", "section", True),
        ])
        record_fetch(db, previous, site + "contacts/", "contacts", 1, status=200, html="contacts")

        run = start_run(db, 1)
        record_fetch(db, run, site, "home", 0, status=200, html="home", links=[
            Link(site + "#team", "Коллектив", "team", "section", False),
            Link(site + "#programs", "Программы", "programs", "section", False),
            Link(site + "contacts/", "Контакты", "contacts", "section", True),
            Link(site + "staff/", "Педагоги", "staff", "section", True),
            Link(site + "news/story", "Событие дня", "news", "detail", True),
            Link(site + "ustav.pdf", "Устав", "documents", "document", False),
            Link("https://elsewhere.example/infos", "Сведения", "information", "external", False),
        ], headings=[("programs", "Образовательные программы"), ("staff", "Педагоги")])
        record_fetch(db, run, site + "contacts/", "contacts", 1, status=404, error="not found")
        record_fetch(db, run, site + "staff/", "staff", 1, status=200, html="staff")
        record_fetch(db, run, site + "news/story", "news", 1, status=200, html="story")

        self.assertEqual(coverage(db, run, "kindergarten"), {
            "home": "page", "news": "unseen", "events": "unseen", "team": "link", "information": "unseen",
            "documents": "unseen", "programs": "heading", "staff": "page",
            "staff_resources": "unseen", "contacts": "link",
        })
        counts = coverage_counts(db, [(previous, "kindergarten"), (run, "kindergarten")])
        self.assertEqual(counts["contacts"], {"page": 1, "heading": 0, "link": 1, "unseen": 0})
        self.assertNotIn("schedule", counts)


if __name__ == "__main__":
    unittest.main()
