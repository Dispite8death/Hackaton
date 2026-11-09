import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from functools import partial
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import loadPage
from crawl_store import initialize_db
from http_page import PageResponse


SITE = "https://school.example/"
HOME = """
    <nav><a href="/news/">Новости</a><a href="/contacts/">Контакты</a></nav>
    <a href="/docs/ustav.pdf" download>Устав</a>
"""
NEWS = '<main><a href="/news/meeting.html">Собрание родителей</a></main>'


class CrawlerTest(unittest.TestCase):
    def setUp(self):
        uri = f"file:{uuid4().hex}?mode=memory&cache=shared"

        def connect_memory():
            db = sqlite3.connect(uri, uri=True)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            return db

        self.db = connect_memory()
        self.addCleanup(self.db.close)
        self.enterContext(patch.object(loadPage, "connect_db", connect_memory))
        self.enterContext(patch.object(loadPage.time, "sleep"))
        initialize_db(self.db)
        with self.db:
            self.db.executemany(
                "INSERT INTO organization (name, site_url, kind) VALUES (?, ?, ?)",
                [("Школа", SITE, "school"), ("Детский сад", "https://garden.example/", "kindergarten")],
            )
        self.org = loadPage.organizations(["1"])[0]
        self.requested = []
        self.responses = {
            SITE: PageResponse(SITE, 200, HOME, loaded_at=100),
            SITE + "news/": PageResponse(SITE + "news/", 200, NEWS),
            SITE + "news/meeting.html": PageResponse(SITE + "news/meeting.html", 200, "Статья"),
            SITE + "contacts/": PageResponse(SITE + "contacts/", 404, None, "HTTP 404: Not Found"),
        }

    def fetch(self, url, site, timeout):
        self.assertEqual(site, SITE)
        self.requested.append(url)
        return self.responses[url]

    def rows(self, sql, args=()):
        return [tuple(row) for row in self.db.execute(sql, args)]

    def test_default_transport_captures_home_and_subpage_with_pdf(self):
        from browser_capture import RenderEvidence

        def page(url, site, timeout, *, section):
            html = '<a href="/news/">Новости</a>' if section == "home" else "<main>Новости</main>"
            render = RenderEvidence(101,url,1280,800,html,b"viewport",b"full",b"%PDF-test",True,{})
            return PageResponse(url,200,html,render=render)

        with patch("browser_capture.BrowserCapture") as capture:
            capture.return_value.__enter__.return_value.fetch.side_effect = page
            result = loadPage.crawl_organization(self.org,max_pages=2)
        capture.assert_called_once_with(pdf=True,ready_timeout=45)
        self.assertEqual(result.loaded, 2)
        self.assertEqual(self.rows("""
            SELECT f.section,r.pdf IS NOT NULL,r.screenshot IS NOT NULL,r.full_screenshot IS NOT NULL
            FROM crawl_fetch f JOIN page_render r ON r.page_id=f.page_id ORDER BY f.id
        """), [("home",1,1,1),("news",1,1,1)])

    def test_garden_combined_feed_and_actual_articles(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        listing = "/nash-detskiy-sad/novosti-i-sobytiya/"
        article = listing + "novosti-i-sobytiya_95.html"
        routes = {
            "/": f'<a href="/contacts/">Контакты</a><nav><a href="{listing}">Новости и события</a></nav>',
            listing: f"""<a href="#">Подробнее</a><a href="javascript:open()">Открыть</a>
                <img src="/media/photo.jpg"><a href="{listing}photo.avif">Фотография</a>
                <article><a href="/gallery/">Альбом</a></article>
                <a data-fancybox href="{listing}large">Увеличить</a>
                <a class="card" href="{article}">Консультации для родителей</a>""",
            article: "<h1>Консультации для родителей</h1>",
        }
        seen = []

        def fetch(url, *_):
            path = "/" + url.removeprefix(site)
            seen.append(path)
            return PageResponse(url, 200, routes[path])

        result = loadPage.crawl_organization(org, max_pages=3, fetch=fetch)
        self.assertEqual(seen, ["/", listing, article])
        self.assertCountEqual(self.rows(
            "SELECT section, follow FROM crawl_link WHERE url=?", (site.rstrip("/") + listing,)),
            [("news", 1), ("events", 1)])
        self.assertEqual(self.rows("""SELECT f.depth,l.section,l.relation FROM crawl_link l
            JOIN crawl_fetch f ON f.id=l.fetch_id WHERE f.run_id=? AND l.url=?""",
            (result.run_id, site.rstrip("/") + article)), [(1,"news","detail")])

    def test_garden_separate_feed_indexes_precede_articles_and_known_pagination_is_reported(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        routes = {
            "/": """<nav><a href="/contacts/">Контакты</a><a href="/ann/">Новости</a>
                <a href="/calendar/">События</a></nav><h2>Новости и события</h2>""",
            "/ann/": """<article><a href="/p/1">Открытый урок</a></article>
                <nav class="pagination"><a href="/p2/">2</a></nav>""",
            "/calendar/": '<article><a href="/p/2">Утренник</a></article>',
            "/p2/": '<article><a href="/p/3">Субботник</a></article>',
            "/p/1": "<h1>Открытый урок</h1>", "/p/2": "<h1>Утренник</h1>", "/p/3": "<h1>Субботник</h1>",
        }
        seen = []

        def fetch(url, *_):
            path = "/" + url.removeprefix(site)
            seen.append(path)
            return PageResponse(url, 200, routes[path])

        short = loadPage.crawl_organization(org, max_pages=3, fetch=fetch)
        self.assertEqual(seen, ["/", "/ann/", "/calendar/"])
        self.assertIn("news_pagination_unvisited", short.reason)
        self.assertEqual(self.rows("""SELECT f.section,l.relation,l.follow FROM crawl_fetch f
            JOIN crawl_link l ON l.fetch_id=f.id WHERE f.run_id=? AND l.url=?""",
            (short.run_id, site+"p2/")), [("news","pagination",1)])
        seen.clear()
        with patch.object(loadPage, "crawl_organization", partial(loadPage.crawl_organization, fetch=fetch)):
            with patch.object(sys, "argv", ["loadPage.py","--org","2","--news-only","--max-pages","20"]):
                with redirect_stdout(StringIO()):
                    self.assertEqual(loadPage.main(), 0)
        self.assertEqual(seen[:3], ["/", "/ann/", "/calendar/"])
        self.assertCountEqual(seen, routes)
        self.assertEqual(self.rows("SELECT purpose,stop_reason FROM crawl_run ORDER BY id DESC LIMIT 1"),
                         [("sample","frontier_exhausted")])
        self.assertEqual(self.rows("SELECT depth,section FROM crawl_fetch WHERE url=?", (site+"p/3",)), [(3,"news")])

    def test_garden_without_publication_targets_never_generates_an_article_url(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        pages = {site:'<a href="/lenta/">Новости и мероприятия</a>',
                 site+"lenta/":'<article><a href="#"><img src="/photo.png"></a></article><button>Показать ещё</button>'}
        result = loadPage.crawl_organization(org, news_only=True, fetch=lambda u,*_: PageResponse(u,200,pages[u]))
        self.assertEqual((result.loaded,result.reason), (2,"news_pagination_unknown"))
        self.assertEqual(self.rows("SELECT count(*) FROM crawl_link WHERE relation='detail'"), [(0,)])

    def test_news_only_reaches_publication_through_unique_real_site_navigator(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        routes = {
            site: """<nav><a href="/about/">Наш детский сад</a><a href="/guide">Навигатор по сайту</a></nav>
                <footer><a href="/guide">Навигатор по сайту</a></footer>""",
            site+"guide": '<main><h1>Навигатор по сайту</h1><a href="/press/">Новости и события</a></main>',
            site+"press/": '<article><a href="/post/1">Спектакль</a></article>',
            site+"post/1": '<h1>Спектакль</h1>',
        }
        result = loadPage.crawl_organization(org, max_depth=3, news_only=True,
                                             fetch=lambda u,*_: PageResponse(u,200,routes[u]))
        self.assertEqual((result.loaded,result.reason), (4,"frontier_exhausted"))
        self.assertEqual(self.rows("SELECT url,depth,section FROM crawl_fetch ORDER BY id"),
                         [(site,0,"home"), (site+"guide",1,"site_navigator"), (site+"press/",2,"news"),
                          (site+"post/1",3,"news")])
        self.assertEqual(self.rows("SELECT DISTINCT section FROM crawl_link WHERE url=?", (site+"about/",)), [("about",)])

    def test_news_window_retains_old_link_evidence_without_revisiting_dated_article(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        def card(slug,day):
            return (f'<a class="card" href="/news/{slug}.html"><h3>Новость {slug}</h3>'
                    f'<div class="card-subtitle">{day}</div></a>')
        listing = ('<main><section class="gw-news">'+card("start","08 ноября 2025 г.")+
                   card("old","07 ноября 2025 г.")+card("unclear","06 ноября")+'</section></main>')
        seen = []
        routes = {site:'<nav><a href="/news/">Новости</a></nav>',site+"news/":listing,
                  site+"news/start.html":"<h1>Граница периода</h1>",
                  site+"news/unclear.html":"<h1>Уточнение даты</h1>"}
        def fetch(url,*_):
            seen.append(url)
            return PageResponse(url,200,routes[url])
        result = loadPage.crawl_organization(org,news_only=True,news_as_of=date(2026,11,8),fetch=fetch)
        self.assertCountEqual(seen,routes.keys())
        self.assertEqual(result.errors,0)
        old = site+"news/old.html"
        self.assertTrue(self.rows("SELECT url FROM crawl_link WHERE url=?",(old,)))
        self.assertFalse(self.rows("SELECT url FROM crawl_fetch WHERE url=?",(old,)))

    def test_news_only_can_recover_index_link_from_an_actual_article(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        routes = {site: '<article><a href="/novosti/otkrytie.html">Мы открыли выставку</a></article>',
                  site+"novosti/otkrytie.html": '<nav><a href="/press/">Новости</a></nav>',
                  site+"press/": '<article><a href="/another">День семьи</a></article>',
                  site+"another": '<h1>День семьи</h1>'}
        result = loadPage.crawl_organization(org, news_only=True, fetch=lambda u,*_: PageResponse(u,200,routes[u]))
        self.assertEqual((result.loaded,result.reason), (4,"frontier_exhausted"))
        self.assertEqual(self.rows("SELECT url FROM crawl_fetch ORDER BY id"), [(url,) for url in routes])

    def test_inline_navigator_uses_its_panel_without_an_extra_request(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        routes = {
            site: """<button aria-controls="panel">Навигатор по сайту</button>
                <span id="label">События</span><div id="panel" hidden>
                <a href="/af/" aria-labelledby="label"><svg></svg></a></div>""",
            site+"af/": '<article><a href="/p/1">Экскурсия</a></article>',
            site+"p/1": '<h1>Экскурсия</h1>',
        }
        result = loadPage.crawl_organization(org, news_only=True, fetch=lambda u,*_: PageResponse(u,200,routes[u]))
        self.assertEqual((result.loaded,result.reason), (3,"frontier_exhausted"))
        self.assertEqual(self.rows("SELECT url,section FROM crawl_fetch ORDER BY id"),
                         [(site,"home"), (site+"af/","events"), (site+"p/1","events")])

    def test_map_can_reveal_second_feed_before_budget_is_spent_on_articles(self):
        org = loadPage.organizations(["2"])[0]
        site = org["site_url"]
        routes = {site:'<a href="/n/">Новости</a><a href="/guide/">Карта сайта</a>',
                  site+"n/":'<article><a href="/post1">Встреча выпускников</a></article>',
                  site+"guide/":'<main><h1>Карта сайта</h1><a href="/e/">События</a></main>',
                  site+"e/":'<article><a href="/post2">День матери</a></article>'}
        result = loadPage.crawl_organization(org, max_pages=4, fetch=lambda u,*_: PageResponse(u,200,routes[u]))
        self.assertEqual(result.reason, "page_limit")
        self.assertEqual(self.rows("SELECT url,section FROM crawl_fetch ORDER BY id"),
                         [(site,"home"), (site+"n/","news"), (site+"guide/","site_navigator"), (site+"e/","events")])

    def test_staff_pages_and_profiles_both_kinds_precede_individual_articles(self):
        for org in loadPage.organizations():
            with self.subTest(kind=org["kind"]):
                site = org["site_url"]
                listing = "pedagogicheskiy-sostav/" if org["kind"] == "school" else "nash-kollektiv/"
                next_url = site + listing + "?cur_cc=10&curPos=8"
                profile = site + "persony/petrova.html"
                if org["kind"] == "school":
                    pager = '<div class="gw-pagination"><a href="?cur_cc=10&amp;curPos=8">2</a></div>'
                    card = """<div class="gw-person-info-card"><a class="gw-person-info-card__name"
                        href="/persony/petrova.html">Петрова Анна Ивановна</a>
                        <p class="gw-person-info-card__job">Учитель</p></div>"""
                else:
                    pager = '<ul class="pagination"><li><a href="?cur_cc=10&amp;curPos=8">2</a></li></ul>'
                    card = """<a class="gw-person-card" href="/persony/petrova.html">
                        <b class="gw-person-card__name">Петрова Анна Ивановна</b>
                        <div class="gw-person-card__job">Воспитатель</div></a>"""
                routes = {
                    site: f'<a href="/{listing}">Все сотрудники</a><a href="/contacts/">Контакты</a>',
                    site + "contacts/": "<main>Контакты</main>",
                    site + listing: pager + '<article><a href="/persony/other.html">Иной сотрудник</a></article>',
                    next_url: card + f'<nav class="pagination"><a href="/{listing}">1</a></nav>',
                    profile: '<div class="gw-person-detail"><p class="gw-person-detail__name">Петрова Анна Ивановна</p></div>',
                    site + "persony/other.html": "<main>Профиль</main>",
                }
                seen = []

                def fetch(url, _site, timeout):
                    seen.append(url)
                    return PageResponse(url, 200, routes[url])

                run = loadPage.crawl_organization(org, max_pages=4, fetch=fetch)
                self.assertEqual(set(seen[:3]), {site,site+"contacts/",site+listing})
                self.assertEqual(seen[3], next_url)
                self.assertIn("page_limit", run.reason)
                self.assertEqual(self.rows("""
                    SELECT s.full_name FROM staff_observation s JOIN crawl_fetch f ON f.page_id=s.page_id
                    WHERE f.run_id=? AND f.url=?
                """, (run.run_id,next_url)), [("Петрова Анна Ивановна",)])
                seen.clear()
                run = loadPage.crawl_organization(org, max_pages=12, fetch=fetch)
                self.assertEqual(seen.count(site + listing), 1)
                self.assertIn(profile, seen)
                self.assertEqual(self.rows("""
                    SELECT count(DISTINCT s.page_id) FROM staff_observation s JOIN crawl_fetch f ON f.page_id=s.page_id
                    WHERE f.url=?
                """, (next_url,)), [(2,)])

    def test_unresolved_staff_button_is_reported_without_requesting_script(self):
        pages = {SITE:'<a href="/team/">Все сотрудники</a>',
                 SITE+"team/":'<button onclick="loadMore()">Показать ещё</button>'}
        result = loadPage.crawl_organization(self.org, fetch=lambda url,*_: PageResponse(url, 200, pages[url]))
        self.assertEqual((result.state,result.reason), ("partial","staff_pagination_unknown"))
        self.assertEqual(self.rows("SELECT url,follow FROM crawl_link WHERE relation='pagination_unresolved'"),
                         [(SITE+"team/",0)])

    def test_select_one_multiple_or_all(self):
        self.assertEqual(self.org["site_url"], SITE)
        self.assertEqual([row["id"] for row in loadPage.organizations(["Детский сад", "1", "2"])], [2, 1])
        self.assertEqual([row["id"] for row in loadPage.organizations()], [1, 2])
        with self.assertRaises(ValueError):
            loadPage.organizations(["99"])

    def test_failed_page_and_links_keep_their_provenance(self):
        result = loadPage.crawl_organization(self.org, fetch=self.fetch)
        self.assertEqual(self.requested[0], SITE)
        self.assertCountEqual(self.requested, self.responses)
        self.assertEqual((result.loaded, result.errors, result.state), (3, 1, "partial"))
        self.assertEqual(self.rows(
            "SELECT http_status, error, page_id FROM crawl_fetch WHERE url = ?", (SITE + "contacts/",)
        ), [(404, "HTTP 404: Not Found", None)])
        self.assertCountEqual(self.rows("""
            SELECT f.url, l.url, l.label, l.follow
            FROM crawl_fetch AS f JOIN crawl_link AS l ON f.id = l.fetch_id
            WHERE f.run_id = ?
        """, (result.run_id,)), [
            (SITE, SITE + "news/", "Новости", 1),
            (SITE, SITE + "contacts/", "Контакты", 1),
            (SITE, SITE + "docs/ustav.pdf", "Устав", 0),
            (SITE + "news/", SITE + "news/meeting.html", "Собрание родителей", 1),
        ])
        self.assertEqual(self.rows("SELECT depth, section FROM crawl_fetch WHERE url = ?",
                                   (SITE + "news/meeting.html",)), [(2, "news")])

    def test_depth_and_page_limits_keep_unvisited_links(self):
        for depth, budget, reason in [(0, 10, "depth_limit"), (3, 1, "page_limit")]:
            with self.subTest(depth=depth, budget=budget):
                self.requested.clear()
                result = loadPage.crawl_organization(self.org, max_depth=depth, max_pages=budget, fetch=self.fetch)
                self.assertEqual(self.requested, [SITE])
                self.assertEqual((result.loaded, result.state, result.reason), (1, "partial", reason))
                self.assertEqual(self.rows("""
                    SELECT count(*) FROM crawl_link JOIN crawl_fetch ON crawl_fetch.id = crawl_link.fetch_id
                    WHERE run_id = ? AND follow = 1
                """, (result.run_id,)), [(2,)])

    def test_repeat_runs_keep_both_versions_at_the_same_url(self):
        first = loadPage.crawl_organization(self.org, max_depth=0, fetch=self.fetch)
        self.responses[SITE] = PageResponse(SITE, 200, "<h1>Обновлённая главная</h1>", loaded_at=200)
        second = loadPage.crawl_organization(self.org, max_depth=0, fetch=self.fetch)
        self.assertEqual((second.state, second.reason), ("completed", "frontier_exhausted"))
        self.assertEqual(self.rows("""
            SELECT f.run_id, p.date_loaded, p.text_page FROM page AS p
            JOIN crawl_fetch AS f ON f.page_id = p.id
            WHERE p.url = ? AND p.id_organization = ? ORDER BY p.date_loaded
        """, (SITE, self.org["id"])), [
            (first.run_id, 100, HOME), (second.run_id, 200, self.responses[SITE].html),
        ])

    def test_sample_cli_keeps_last_audit_for_coverage(self):
        audit = loadPage.crawl_organization(self.org, fetch=self.fetch)
        self.responses[SITE] = PageResponse(SITE, 200, "<h1>Контрольная загрузка</h1>")
        with patch.object(loadPage, "crawl_organization", partial(loadPage.crawl_organization, fetch=self.fetch)):
            with patch.object(sys, "argv", ["loadPage.py", "--org", "1", "--sample", "--max-pages", "1"]):
                with redirect_stdout(StringIO()):
                    self.assertEqual(loadPage.main(), 0)
        self.assertEqual(self.rows("SELECT purpose FROM crawl_run ORDER BY id"), [("audit",), ("sample",)])
        for selector in (["--org", "1"], ["--all"]):
            output = StringIO()
            with patch.object(sys, "argv", ["loadPage.py", *selector, "--coverage"]), redirect_stdout(output):
                self.assertEqual(loadPage.main(), 0)
            self.assertIn("Новости — сохранена страница", output.getvalue())
            if selector == ["--org", "1"]:
                self.assertIn(f"обход {audit.run_id},", output.getvalue())
            else:
                self.assertIn("Новости — сохранена страница 1", output.getvalue())

    def test_required_section_behind_menu_precedes_a_news_card(self):
        hub = SITE + "info/"
        docs = SITE + "docs/"
        self.responses = {
            SITE: PageResponse(SITE, 200, """
                <a href="/news/story.html">Вчерашняя встреча</a>
                <nav><a href="/info/">Сведения об образовательной организации</a></nav>
            """),
            hub: PageResponse(hub, 200, '<a href="/docs/">Документы</a>'),
            docs: PageResponse(docs, 200, '<h1>Документы</h1>'),
        }
        result = loadPage.crawl_organization(self.org, fetch=self.fetch, max_pages=3)
        self.assertEqual(self.requested, [SITE, hub, docs])
        self.assertEqual(self.rows("""
            SELECT h.section, h.label, f.depth FROM crawl_heading AS h
            JOIN crawl_fetch AS f ON f.id = h.fetch_id WHERE f.run_id = ?
        """, (result.run_id,)), [("documents", "Документы", 2)])


if __name__ == "__main__":
    unittest.main()
