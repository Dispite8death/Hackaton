import hashlib
import sqlite3
import unittest

from config import SCHEMA_SQL


class HomeImageReuseTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA_SQL)
        self.db.executemany("INSERT INTO organization(id,name,site_url,kind) VALUES(?,?,?,'school')",
                            ((i, f"School {i}", f"https://school{i}.example/") for i in (1,2)))
        self.db.executemany("""
            INSERT INTO crawl_run(id,id_organization,purpose,finished_at) VALUES(?,?,?,?)
        """, [(1,1,"audit",1),(2,1,"audit",2),(3,2,"audit",2),(4,2,"sample",3),(5,1,"audit",None)])
        self.asset_url = "https://cdn.example/same.jpg"

    def page(self, run, path, *, requested=None):
        org = self.db.execute("SELECT id_organization FROM crawl_run WHERE id=?", (run,)).fetchone()[0]
        url = f"https://school{org}.example" + path
        pid = self.db.execute("INSERT INTO page(url,id_organization,text_page) VALUES(?,?,'') RETURNING id",
                              (url,org)).fetchone()[0]
        self.db.execute("INSERT INTO crawl_fetch(run_id,url,final_url,section,depth,fetched_at,page_id) "
                        "VALUES(?,?,?,'events',1,100,?)", (run,requested or url,url,pid))
        return pid

    def event(self, page_id, page_kind, card_url, *, ordinal=0):
        self.db.execute("INSERT OR IGNORE INTO home_event_block VALUES(?,?,'cards','{}')", (page_id,page_kind))
        self.db.execute("""
            INSERT INTO home_event_link(page_id,ordinal,kind,url,title,image_url,evidence)
            VALUES(?,?,'event',?,'Концерт',?,'{}')
        """, (page_id,ordinal,card_url,self.asset_url))

    def photo(self, pid, table, url):
        self.assertIn(table, ("home_main_photo","home_canteen_photo"))
        self.db.execute(f"INSERT INTO {table} VALUES(?,'found',?,NULL,'{{}}')",(pid,url))

    def saved(self, pid, url, data):
        digest = hashlib.sha256(data).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO home_image_asset VALUES(?,'image/png',1,1,?)",(digest,data))
        self.db.execute("INSERT INTO home_image_download VALUES(?,?,1,?,200,'saved',NULL,?)",
                        (pid,url,url,digest))
        return digest

    def test_event_across_home_listing_detail_is_one_use_even_with_redirect_or_fragment(self):
        alias, canonical = "https://school1.example/spring", "https://school1.example/events/spring"
        self.event(self.page(2,"/"),"home",alias+"#program")
        listing = self.page(2,"/events/")
        self.event(listing,"listing",canonical)
        self.event(self.page(2,"/events/spring",requested=alias),"detail",canonical)
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_image").fetchall(), [])
        self.event(listing,"listing","https://school1.example/events/second",ordinal=1)
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_image").fetchall(),[(self.asset_url,2,1,1)])

    def test_same_url_between_categories_excludes_old_sample_and_unfinished_runs(self):
        self.photo(self.page(1,"/"),"home_main_photo",self.asset_url)
        self.photo(self.page(4,"/"),"home_canteen_photo",self.asset_url)
        self.photo(self.page(5,"/"),"home_canteen_photo",self.asset_url)
        first = self.page(2,"/")
        self.photo(first,"home_main_photo",self.asset_url)
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_image").fetchall(), [])
        self.photo(first,"home_canteen_photo",self.asset_url)
        self.photo(self.page(3,"/"),"home_main_photo",self.asset_url)
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_image").fetchall(),[(self.asset_url,3,2,2)])

    def test_matching_bytes_on_different_urls_require_distinct_current_photo_uses(self):
        for run, category, file in ((1,"canteen","old.png"), (4,"canteen","sample.png"),
                                    (5,"canteen","running.png"), (2,"main","first.png"), (2,"main","second.png")):
            pid, url = self.page(run,"/"), "https://cdn.example/"+file
            self.photo(pid,f"home_{category}_photo",url)
            digest = self.saved(pid,url,b"same image bytes")
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_asset").fetchall(),[])
        other = self.page(3,"/")
        self.photo(other,"home_canteen_photo",self.asset_url)
        self.saved(other,self.asset_url,b"same image bytes")
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_asset").fetchall(),
                         [(digest,2,2,2,3,"same_bytes_multiple_uses")])
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_image").fetchall(),[])
        # A download row remains historical if the corrected criterion now points elsewhere.
        self.db.execute("UPDATE home_canteen_photo SET image_url=? WHERE page_id=?",
                        ("https://cdn.example/revised.png",other))
        self.assertEqual(self.db.execute("SELECT * FROM home_reused_asset").fetchall(),[])


if __name__ == "__main__":
    unittest.main()
