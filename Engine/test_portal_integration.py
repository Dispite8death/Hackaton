import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from Engine import config, model_settings, site_server
from Engine.importer import SOURCE, read_organizations
from ai_audit_web import db, integration


class PortalIntegrationTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, "DB_PATH", str(directory / "archive.db")))
        self.enterContext(patch.object(integration, "DATA_DIR", directory))
        self.enterContext(patch.object(db, "DB_PATH", directory / "quick.db"))
        site_server.prepare_database()

    def test_import_preserves_existing_institution_and_maps_independent_ids(self):
        db.init()
        name, url, kind = read_organizations(SOURCE)[0]
        with closing(db.conn()) as conn, conn:
            conn.execute("INSERT INTO sites(id,name,url,type,email) VALUES(?,?,?,?,?)",
                         (101, name, url + "/", kind, "contact@example.org"))
        integration.initialize()
        integration.initialize()
        self.assertEqual(integration.quick_id(url), 101)
        self.assertEqual(integration.archive_id(url + "/"), 1)
        self.assertEqual(db.get_site(101)["email"], "contact@example.org")
        roster = {(urlsplit(url).hostname, kind) for _, url, kind in read_organizations(SOURCE)}
        sites = db.get_sites()
        self.assertEqual(len(sites), 80)
        self.assertEqual({(urlsplit(s["url"]).hostname, s["type"]) for s in sites}, roster)

    def test_disabled_and_missing_key_profiles_cannot_enable_model_actions(self):
        integration.initialize()
        with closing(config.connect_db()) as conn, conn:
            model_settings.save_profile(conn, model_settings.Profile("compatible", "https://example.org/v1", "test"))
        self.assertEqual(integration.audit_settings()["llm_enabled"], "0")
        with closing(config.connect_db()) as conn, conn:
            model_settings.set_setting(conn, "portal_ai_enabled", True)
        self.assertEqual(integration.ai_status(), "Для ИИ нужен API-ключ")
        self.assertEqual(integration.audit_settings()["llm_enabled"], "0")
        with closing(config.connect_db()) as conn, conn:
            model_settings.save_profile(conn, model_settings.DEFAULTS["ollama"])
        self.assertEqual(integration.audit_settings()["llm_enabled"], "1")
