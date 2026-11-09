import socket
import sqlite3
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import factcheck as fc
import factcheck_sources as policy
from config import SCHEMA_SQL
from model_settings import set_setting

TITLE="Областной фестиваль осенних открытий"
FIRST="5 октября 2026 года в Магадане прошел областной фестиваль осенних открытий. В нём участвовала школа №7."
SECOND=("Городская школа №7 в Магадане провела 5 октября 2026 года встречу школьных исследователей. "
        "Жюри отметило практические наблюдения о природе и вручило ученикам призы.")


class FactcheckTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа №7 города Магадана','https://school.example.ru','school')")
        self.db.execute("INSERT INTO organization VALUES(2,'Детский сад','https://ds-other.gosweb.gosuslugi.ru','kindergarten')")
        self.db.execute("INSERT INTO page VALUES(1,'https://school.example.ru/events/1',1,1,?)",
                        ("<main><h1>"+TITLE+"</h1><p>Школьное мероприятие из архива</p></main>",))
        self.db.execute("INSERT INTO page_passport VALUES(1,'event_detail',?,'[]','','[]')",(TITLE,))
        self.db.execute("""INSERT INTO home_event_link(page_id,ordinal,kind,title,date_start,evidence)
                           VALUES(1,0,'event',?,'2026-10-05','{}')""",(TITLE,))
        self.targets={}
        self.calls=[]
        self.db.commit()

    def article(self,url,body,*,name=None,title=TITLE):
        target=urlsplit(url)
        policy.add_allowed_source(self.db, f"{target.scheme}://{target.netloc}/")
        meta=f'<meta property="og:site_name" content="{name}">' if name else ""
        self.targets[url]=(url,"text/html",(meta+f"<article><h1>{title}</h1><p>{body}</p></article>").encode())
        return url

    def fetch(self,url,blocked,timeout,accept):
        self.calls.append(url)
        result=self.targets.get(url)
        if result is None:
            raise ValueError("unavailable in fixture")
        return result

    def check(self,urls,**kw):
        return fc.run_factcheck(self.db,1,candidate_urls=urls,fetch=self.fetch,search=False,**kw)

    def test_distinct_publishers_and_matching_local_event(self):
        a=self.article("https://culture.journal.co.uk/a",FIRST)
        b=self.article("https://media-other.ru/festival",SECOND)
        report=self.check([a,b])
        self.assertEqual((report["status"],report["source_count"]),("corroborated",2))
        self.assertEqual(report["sources"][0]["publisher"],"journal.co.uk")
        self.assertTrue(all(s["signals"]["event_date_match"] for s in report["sources"]))
        self.assertEqual(self.db.execute("SELECT count(*) FROM factcheck_source").fetchone()[0],2)
        set_setting(self.db,"factcheck",{"minimum_sources":3,"limit":3})
        self.assertEqual(self.check([a,b])["status"],"review")

    def test_no_double_count_for_same_publisher_or_copied_article(self):
        a=self.article("https://culture.journal.co.uk/a",FIRST,name="Культура города")
        b=self.article("https://news.journal.co.uk/b",SECOND)
        c=self.article("https://mirror.ru/story",FIRST)
        d=self.article("https://sameagency.ru/story",SECOND,name="Культура города")
        e=self.article("https://credit.ru/story",SECOND+" Источник: пресс-служба министерства")
        result=self.check([a,b,c,d,e])
        self.assertEqual(result["source_count"],1)
        self.assertEqual([r["status"] for r in result["sources"]],[
            "matching_report","same_publisher","duplicate_text","same_publisher","attributed_or_syndicated"])

    def test_schools_redirects_unknown_social_and_incomplete_evidence(self):
        school="https://ds-other.gosweb.gosuslugi.ru/news/1"
        redirect="https://media.ru/redirect"
        policy.add_allowed_source(self.db, "https://media.ru/")
        self.targets[redirect]=(school,"text/html",b"school")
        wrong=self.article("https://press.ru/wrong",FIRST.replace("№7","№71"))
        notice=self.article("https://notice.ru/1",FIRST.replace("прошел","состоится").replace("участвовала","будет участвовать"))
        different=self.article("https://third.ru/1",FIRST.replace("5 октября","6 октября"))
        social="https://t.me/news_channel/1"
        report=self.check([school,redirect,wrong,notice,different,social])
        self.assertEqual([r["status"] for r in report["sources"]],
                         ["school_resource","school_resource","related_unverified","announcement","related_unverified","social_owner_unverified"])
        self.assertEqual(report["source_count"],0)
        self.assertNotIn(school,self.calls)

    def test_diagnostics_preserve_empty_search_and_network_error(self):
        with patch.object(fc,"_search",wraps=fc._search):
            report=fc.run_factcheck(self.db,1,search_sites=["https://news.ru"],fetch=self.fetch)
        self.assertEqual(report["status"],"retryable")
        self.assertTrue(report["retryable"])
        self.assertEqual(self.db.execute("SELECT status FROM factcheck_run WHERE id=?", (report["check_id"],)).fetchone()[0], "retryable")
        self.assertEqual([s["status"] for s in report["searches"]],["error","error","error"])
        for url in self.calls:
            if "bing.com/" in url:
                self.targets[url]=(url,"application/xml",b"<rss><channel/></rss>")
            else:
                self.targets[url]=(url,"application/json",b'{"articles":[]}' if "gdelt" in url else b"[]")
        report=fc.run_factcheck(self.db,1,search_sites=["https://news.ru"],fetch=self.fetch)
        self.assertEqual([s["status"] for s in report["searches"]],["no_results","no_results","no_results"])
        self.assertEqual(report["status"],"insufficient")
        self.assertFalse(report["retryable"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM factcheck_run").fetchone()[0],2)

    def test_retry_when_every_article_is_unreadable(self):
        policy.add_allowed_source(self.db, "https://unavailable.ru/")
        report = self.check(["https://unavailable.ru/1", "https://unavailable.ru/2"])
        self.assertTrue(report["retryable"])
        self.assertEqual((report["status"], report["event_status"]), ("retryable", "retryable"))
        unrelated = self.article("https://regional.ru/2", "Рецепты яблочного пирога", title="Выпечка")
        mixed = self.check(["https://unavailable.ru/1", unrelated])
        self.assertEqual((mixed["status"], mixed["retryable"]), ("insufficient", False))

    def test_national_event_corroboration_does_not_establish_local_participation(self):
        title = "Всероссийский экономический диктант"
        self.db.execute("UPDATE page_passport SET title=?", (title,))
        self.db.execute("UPDATE page SET text_page=?", ("<main>По всей России пройдёт юбилейный диктант</main>",))
        a = self.article("https://university.ru/one", "5 октября 2026 года в Иркутске прошёл экономический диктант.", title=title)
        b = self.article("https://academy.ru/two", "Студенты нашего университета 5 октября 2026 года приняли участие в диктанте.", title=title)
        denial = self.article("https://press.ru/story", FIRST, title="Отмена акции «"+title+"»")
        report = self.check([a,b,denial])
        self.assertEqual((report["status"],report["source_count"]),("review",0))
        self.assertEqual((report["event_status"],report["event_source_count"]),("corroborated",2))
        self.assertFalse(report["sources"][2]["signals"]["event_supported"])

    def test_mentioned_school_did_not_participate(self):
        article = FIRST.replace("участвовала школа №7", "школа №7 не участвовала")
        report = self.check([self.article("https://region.ru/report", article)])
        self.assertEqual(report["source_count"], 0)
        self.assertFalse(report["sources"][0]["signals"]["participation_match"])

    def test_fetch_rejects_local_targets_and_preserves_distinct_gosweb_sites(self):
        private=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]
        with patch.object(fc.socket,"getaddrinfo",return_value=private),patch.object(fc.socket,"socket") as connect:
            with self.assertRaisesRegex(ValueError,"nonpublic"):
                fc._fetch("https://redirect.example/article",set(),2,"text/html")
            connect.assert_not_called()
        self.assertFalse(fc._school("ministry.gosweb.gosuslugi.ru",fc._school_hosts(self.db)))

    def test_policy_normalizes_precise_origins_and_rejects_ambiguous_urls(self):
        base="https://пример.рф:443/новости"
        self.assertTrue(policy.add_allowed_source(self.db, base))
        rule=policy.canonical_source_url(base)
        self.assertEqual(rule,"https://xn--e1afmkfd.xn--p1ai/%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D0%B8/")
        self.assertFalse(policy.add_allowed_source(self.db,rule))
        self.assertEqual(policy.match_publisher(base+"/апрель?id=1#result",[rule]),rule)
        self.assertEqual(policy.match_publisher(base,[rule]),rule)
        for url in (base+"года",base.replace("https","http"),base.replace("пример","www.пример")):
            self.assertIsNone(policy.match_publisher(url,[rule]))
        for url in ("https://co.uk/","https://github.io/","https://gosweb.gosuslugi.ru/",
                    "https://m.vk.com/","https://127.0.0.1/","https://localhost/","https://name:secret@abc.ru/",
                    "https://example.ru/path?key=secret","https://example.ru/path#section","https://example.ru/../",
                    "https://example.ru/path/%2E%2e/story","https://example.ru/x%2fy/","https://example.ru/x%5Cy/",
                    "https://example.ru/x%252Fy/","https://example.ru/foo\\bar","https://example.ru//path",
                    "https://school.example.ru/","https://ds-other.gosweb.gosuslugi.ru/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                policy.add_allowed_source(self.db,url)

    def test_empty_policy_survives_restart_and_saves_its_snapshot(self):
        original=policy.allowlist_fingerprint(self.db)
        for base in policy.get_allowed_sources(self.db):
            self.assertTrue(policy.remove_allowed_source(self.db,base))
        self.db.executescript(SCHEMA_SQL)
        self.assertEqual(policy.get_allowed_sources(self.db),[])
        out=fc.run_factcheck(self.db,1,fetch=self.fetch,candidate_urls=["https://lengu.ru/news/1"])
        self.assertEqual((out["status"],out["retryable"]),("insufficient",False))
        self.assertEqual(self.calls,[])
        self.assertEqual(out["sources"][0]["status"],"source_not_allowed")
        saved=json.loads(self.db.execute("SELECT claim FROM factcheck_run WHERE id=?",(out["check_id"],)).fetchone()[0])
        self.assertEqual(saved["allowlist_fingerprint"],policy.allowlist_fingerprint(self.db))
        self.assertNotEqual(original,saved["allowlist_fingerprint"])
        self.assertEqual((saved["allowed_sources"],saved["allowlist_status"]),([],"empty"))

    def test_rejected_urls_do_not_consume_budget_and_redirect_and_canonical_are_checked(self):
        rule="https://news-site.ru/local/"
        policy.add_allowed_source(self.db,rule)
        urls=["https://excluded.ru/first","https://news-site.ru/national/second",rule+"one",rule+"two"]
        self.targets[urls[2]]=("https://news-site.ru/other/redirect","text/html",b"<article>story</article>")
        self.targets[urls[3]]=(urls[3],"text/html",b'<link rel="canonical" href="/outside/copy"><article>story</article>')
        out=self.check(urls,limit=2)
        self.assertEqual(self.calls,urls[2:])
        self.assertEqual([r["status"] for r in out["sources"]],["source_not_allowed"]*4)
        self.assertEqual(out["sources"][2]["blocked_url"],"https://news-site.ru/other/redirect")
        self.assertEqual(out["sources"][3]["policy_status"],"blocked")
        self.assertFalse(out["retryable"])

    def test_live_transport_stops_before_resolving_an_unlisted_redirect(self):
        start="http://news-site.ru/local/go"
        policy.add_allowed_source(self.db,"http://news-site.ru/local/")
        address=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("1.1.1.1",80))]
        response=MagicMock()
        response.status=302
        response.getheader.return_value="http://foreign.ru/redirect"
        with patch.object(fc.socket,"getaddrinfo",return_value=address) as lookup, patch.object(fc.socket,"socket"), patch.object(fc.http.client,"HTTPConnection") as connect:
            connect.return_value.getresponse.return_value.__enter__.return_value=response
            out=fc.run_factcheck(self.db,1,search=False,candidate_urls=[start])
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[:2],("news-site.ru",80))
        self.assertEqual(out["sources"][0]["status"],"source_not_allowed")

    def test_bing_search_includes_scoped_site_filter(self):
        for base in policy.get_allowed_sources(self.db):
            policy.remove_allowed_source(self.db,base)
        policy.add_allowed_source(self.db,"https://region.ru/news/")
        out=fc.run_factcheck(self.db,1,fetch=self.fetch,search_sites=[])
        from urllib.parse import parse_qs
        bing=[item for item in out["searches"] if item["provider"]=="Bing News RSS"]
        self.assertEqual(len(bing),1)
        self.assertIn("site:region.ru/news",parse_qs(urlsplit(bing[0]["request_url"]).query)["q"][0])

    def test_account_from_school_home_and_bing_results_are_checked_at_origin(self):
        self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(1,1)")
        self.db.execute("INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at) VALUES(1,1,?,'home',0,1)",
                        ("https://school.example.ru",))
        self.db.execute("INSERT INTO crawl_link VALUES(1,'https://vk.com/public738','ВКонтакте','contacts','external',0)")
        r=self.check(["https://vk.ru/wall-738_1"])
        self.assertEqual(r["sources"][0]["status"],"school_linked_social")
        a=self.article("https://radio.ru/one",FIRST)
        b=self.article("https://culture.ru/two",SECOND)
        def search_fetch(url,*args):
            if "bing.com/news/search" in url:
                rss=f'<rss><channel><item><link>https://www.bing.com/news/apiclick.aspx?url={a}</link></item></channel></rss>'
                return url,"application/xml",rss.encode()
            if "gdeltproject.org" in url:
                return url,"application/json",('{"articles":[{"url":"'+b+'"}]}').encode()
            return self.fetch(url,*args)
        out=fc.run_factcheck(self.db,1,fetch=search_fetch,search_sites=[])
        self.assertEqual((out["status"],out["source_count"],out["related_groups"]),("corroborated",2,2))
        self.assertEqual([s["url"] for s in out["sources"]],[a,b])
