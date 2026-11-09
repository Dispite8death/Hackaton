import json
import sqlite3
from contextlib import closing
import unittest

from config import SCHEMA_SQL
from site_navigator import discover_site_navigator, index_navigator


SITE = "https://garden.test/"


class SiteNavigatorTest(unittest.TestCase):
    def discover(self, html, path="", requested=False):
        return discover_site_navigator(html,SITE+path,SITE,"kindergarten",is_map_page=requested)

    def test_mobile_header_footer_entries_do_not_promote_entire_menu(self):
        html = """<header><div id="offcanvasNavLeft" class="offcanvas"><a href="/map/">Навигатор по сайту</a>
                    <a href="/irrelevant/">Сторонний пункт меню</a></div>
                  <div class="gw-top-nav"><a href="/map/">Навигатор по сайту</a></div></header>
                  <nav aria-label="breadcrumb"><a href="/fake">Карта сайта</a></nav>
                  <footer><a href="/map/">Навигатор по сайту</a></footer>"""
        r = self.discover(html)
        self.assertEqual([e["location"] for e in r["entries"]], ["mobile_menu","header","footer"])
        self.assertEqual({e["url"] for e in r["entries"]}, {SITE+"map/"})
        self.assertFalse(r["is_navigator_page"])
        self.assertFalse(r["links"])

    def test_named_sidebar_with_fragment_only_collects_its_own_real_internal_anchors(self):
        html = """<button aria-controls="left-nav" aria-label="Открыть навигатор по сайту"></button>
                  <button onclick="run()">Карта сайта</button>
                  <aside id="left-nav"><a href="/team/">Наш коллектив</a><a href="https://other.test/novosti">Новости</a>
                  <a href="javascript:run()">Новости</a><nav class="breadcrumb"><a href="/x/">Мероприятия</a></nav>
                  </aside><section><a href="/novosti/">Новости</a></section>"""
        r = self.discover(html)
        self.assertEqual([e["kind"] for e in r["entries"]], ["panel_control","unresolved_control"])
        self.assertEqual(r["entries"][0]["panel_selector"],"#left-nav")
        self.assertEqual([(l["section"],l["href"],l["source"],l["container"]) for l in r["links"]],
                         [("team","/team/","navigator","panel")])

    def test_map_lists_are_distinct_from_breadcrumbs_and_keep_both_feed_roles(self):
        html = """<main><ol class="breadcrumb"><li><a href="/bad/">Новости</a></li></ol>
              <h1>Навигатор по сайту</h1><ul class="gw-site-map-list"><li><a href="/feeds/">Новости и события</a>
              <ul class="gw-site-map-list"><li><a href="/contacts/">Контакты</a></li></ul></li></ul>
              <p><a href="/unrelated/">Не пункт карты</a></p></main>"""
        r = self.discover(html,"map/")
        self.assertTrue(r["is_navigator_page"])
        self.assertEqual(len(r["panels"]), 1)
        self.assertEqual([(x["href"],x["section"]) for x in r["links"]],
                         [("/feeds/","news"),("/feeds/","events"),("/contacts/","contacts")])
        redirect = self.discover('<main><h1>Главная</h1><a href="/news">Новости</a></main>', requested=True)
        self.assertFalse(redirect["is_navigator_page"])
        self.assertFalse(redirect["links"])
        fragment = self.discover('<ul class="gw-site-map-list"><li><a href="/news/">Новости</a></li></ul>',"map/")
        self.assertTrue(fragment["is_navigator_page"])
        self.assertEqual(fragment["links"][0]["section"],"news")

    def test_storage_uses_the_individual_snapshot_and_outer_transaction(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.executescript(SCHEMA_SQL)
            db.execute("INSERT INTO organization VALUES(1,'Сад',?,'kindergarten')",(SITE,))
            for page_id, suffix in [(1,"a"),(2,"b")]:
                db.execute("INSERT INTO page(id,url,id_organization,text_page) VALUES(?,?,1,?)",
                           (page_id,SITE,f'<main><button aria-controls="links">Карта сайта</button><nav id="links"><a href="/{suffix}">Новости</a></nav></main>'))
            db.commit()
            db.execute("BEGIN")
            self.assertTrue(index_navigator(db,1))
            self.assertTrue(index_navigator(db,2))
            self.assertTrue(index_navigator(db,2))
            records = [json.loads(text)["links"][0]["href"] for text, in db.execute(
                "SELECT evidence FROM site_navigator_page ORDER BY page_id")]
            self.assertEqual(records,["/a","/b"])
            db.rollback()
            self.assertEqual(db.execute("SELECT count(*) FROM site_navigator_page").fetchone()[0],0)


if __name__=="__main__":
    unittest.main()
