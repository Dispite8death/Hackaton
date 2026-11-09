import json
import sqlite3
import unittest

from config import SCHEMA_SQL
from garden_report import feed_discovery, garden_report, news_routes


class GardenReportTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript("""
            INSERT INTO organization VALUES(1,'Сад','https://garden.test/','kindergarten'),
                                           (2,'Другой сад','https://other.test/','kindergarten');
            INSERT INTO crawl_run(id,id_organization,finished_at) VALUES(1,1,20),(2,1,30),(3,2,40);
            INSERT INTO page(id,id_organization,url,text_page) VALUES
                (10,1,'https://garden.test/',''),(11,1,'https://garden.test/news',''),
                (12,1,'https://garden.test/',''),(21,1,'https://garden.test/old',''),
                (31,2,'https://garden.test/other','');
            INSERT INTO crawl_fetch(run_id,url,final_url,page_id,section,depth,fetched_at,http_status,error) VALUES
                (1,'https://garden.test/','https://garden.test/',10,'home',0,2,200,NULL),
                (1,'https://garden.test/go','https://garden.test/news',11,'news',1,3,200,NULL),
                (1,'https://garden.test/back','https://garden.test/',12,'news',1,3,200,NULL),
                (1,'https://garden.test/404','https://garden.test/404',NULL,'news',1,3,404,'Not Found'),
                (2,'https://garden.test/old','https://garden.test/old',21,'news',1,3,200,NULL),
                (3,'https://garden.test/other','https://garden.test/other',31,'news',1,3,200,NULL);
        """)
        def ref(path, title):
            return {"url":"https://garden.test/"+path,"title":title,"selector":"#"+path}
        combined = dict(ref("news","Новости и события"), combined=True)
        photo = {"selector":"#foto", "scope":"photo_gallery", "title":"Уголок", "image_url":"/photo.jpg","reasons":[]}
        meta = {"excluded_count":1, "excluded_examples":[photo]}
        for section, cards in (("news",["404","back","old","other"]), ("events",["404"])):
            data = {"listing_links":[combined], "navigation_links":[combined],
                    "cards":[ref(path,path) for path in cards], "evidence":meta}
            self.db.execute("INSERT INTO garden_home_section VALUES(10,?,1,'cards',1,1,?,?)",
                            (section,len(cards),json.dumps(data)))
        self.db.execute("""INSERT INTO garden_home_contact VALUES(10,'hours','found',?)""",
                        (json.dumps({"values":[{"value":"8:00–18:30"}]}),))

    def test_exact_same_run_routes_and_combined_articles(self):
        routes = {r["url"].rsplit("/",1)[1]:r for r in news_routes(self.db, 10)}
        self.assertEqual(len(routes), 5)
        self.assertEqual(routes["news"]["status"], "html_saved")
        self.assertEqual(routes["news"]["html_page_ids"], [11])
        self.assertEqual(routes["news"]["sections"], ["news","events"])
        self.assertEqual(set(routes["news"]["roles"]), {"listing","navigation"})
        self.assertEqual((routes["404"]["kind"],routes["404"]["http_status"],routes["404"]["status"]),
                         ("article",404,"http_error"))
        self.assertEqual(routes["404"]["sections"], ["news","events"])
        self.assertEqual((routes["back"]["status"],routes["back"]["page_ids"],routes["back"]["html_page_ids"]),
                         ("redirected_to_home",[12],[]))
        self.assertEqual([routes[p]["status"] for p in ("old","other")], ["unvisited","unvisited"])

    def test_gallery_count_and_contacts_are_separate(self):
        report = garden_report(self.db, 10)
        self.assertEqual(len(report["excluded"]["examples"]),1)
        self.assertTrue(report["excluded"]["examples_complete"])
        self.assertEqual(report["contacts"]["hours"]["values"],["8:00–18:30"])
        self.assertEqual(sum(r["kind"]=="article" for r in report["news_routes"]),4)
        with self.assertRaises(ValueError):
            news_routes(self.db, 31)

    def test_feed_discovery_follows_only_home_controls_and_preserves_each_url(self):
        site = "https://garden.test/"

        def link(path, section, selector, container):
            return dict(url=site+path, section=section, selector=selector, follow=True, relation="section",
                        label=section, panel_selector="#tree", container=container)

        def index(pid, is_map, entries=(), panel_links=(), panels=()):
            data = {"is_navigator_page":bool(is_map),"entries":entries,"panels":panels,"links":panel_links}
            self.db.execute("INSERT INTO site_navigator_page VALUES (?,?,?)", (pid,is_map,json.dumps(data)))

        for pid, run, path in ((13,1,"map"),(14,1,"unrelated-map"),(22,2,"prior-map"),(32,3,"other-map")):
            org = 2 if run==3 else 1
            self.db.execute("INSERT INTO page(id,id_organization,url,text_page) VALUES (?,?,?,'')",(pid,org,site+path))
            self.db.execute("""INSERT INTO crawl_fetch(run_id,url,final_url,page_id,section,depth,http_status,fetched_at)
                VALUES(?,?,?,?,'hub',1,200,30)""",(run,site+path,site+path,pid))
        index(13,True,panel_links=[link("news","news","#map1","map_page"),link("calendar","events","#map2","map_page")])
        for pid in (14,22,32):
            index(pid,True,panel_links=[link("leaked","events","#bad","map_page")])
        controls = [{"kind":"map_link","url":site+path,"selector":"#open","label":"Карта сайта"}
                    for path in ("map","prior-map","other-map")]
        index(10,False,controls,[link("inside","news","#panel-only","panel"),link("news","news","#in-panel","panel")],
              [{"kind":"panel","selector":"#tree"}])
        data = {"listing_links":[{"url":site+"news","title":"Новости","selectors":["#in-panel","#outside"]},
                                 {"url":site+"inside","title":"Новости","selectors":["#panel-only"]},
                                 {"url":site+"direct","title":"Новости","selector":"#main-news"}],
                "cards":[{"url":site+"individual","selector":"#article"}]}
        self.db.execute("UPDATE garden_home_section SET evidence=? WHERE page_id=10 AND section='news'",(json.dumps(data),))
        self.db.execute("UPDATE garden_home_section SET evidence='{}' WHERE page_id=10 AND section='events'")
        report = feed_discovery(self.db,10)
        rows = {r["url"].removeprefix(site):r for r in report["routes"]}
        self.assertEqual({p:r["status"] for p,r in rows.items()},
                         {"news":"both","inside":"navigator","direct":"direct","calendar":"navigator"})
        self.assertEqual([r["selector"] for r in rows["news"]["direct"]], ["#outside"])
        self.assertEqual({r["page_id"] for r in rows["news"]["navigator"]},{10,13})
        self.assertEqual({m["url"].removeprefix(site):m["status"] for m in report["maps"]},
                         {"map":"checked","prior-map":"not_saved","other-map":"not_saved"})

    def test_feed_discovery_keeps_missing_map_distinct_from_missing_index(self):
        self.db.execute("INSERT INTO site_navigator_page VALUES (10,0,?)",
                        (json.dumps({"entries":[{"kind":"map_link","url":"https://garden.test/map"}]}),))
        report = feed_discovery(self.db,10)
        self.assertEqual(report["navigator_status"],"not_saved")
        self.assertEqual(report["routes"][0]["status"],"direct")
        self.db.execute("DROP TABLE site_navigator_page")
        report = garden_report(self.db,10)["feed_discovery"]
        self.assertEqual((report["status"],report["navigator_status"]),("unresolved","pending_index"))
        self.assertEqual([r["label"] for r in report["routes"]],["путь не установлен"])


if __name__ == "__main__":
    unittest.main()
