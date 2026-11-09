import json
import sqlite3
import unittest
from datetime import datetime

from crawl_store import initialize_db
from event_quarters import refresh_quarters
from home_store import analyze_saved_page


def card(title, url, dates):
    return f'<a class="event-card" href="{url}"><h3>{title}</h3>{dates}</a>'


def date_field(text):
    return f'<div class="event-date">{text}</div>'


class EventQuartersTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize_db(self.db)
        for org in (1, 2):
            self.db.execute("INSERT INTO organization(id,name,site_url,kind) VALUES (?,?,?,'school')",
                            (org, str(org), f"https://school{org}.example/"))
        self.run = self.make_run()

    def make_run(self, org=1, when="2025-12-25T12:00:00+00:00", state="completed"):
        start = int(datetime.fromisoformat(when).timestamp())
        row = self.db.execute("""
            INSERT INTO crawl_run(id_organization,started_at,finished_at,state) VALUES (?,?,?,?)""",
            (org, start, start + 60, state))
        return row.lastrowid

    def page(self, path, html, *, run=None, org=1, depth=1, requested=None):
        run = self.run if run is None else run
        base = f"https://school{org}.example"
        pid = self.db.execute("INSERT INTO page(url,id_organization,text_page) VALUES (?,?,?)",
                              (base + path, org, html)).lastrowid
        fid = self.db.execute("""
            INSERT INTO crawl_fetch(run_id,url,final_url,section,depth,fetched_at,http_status,page_id)
            VALUES (?,?,?,?,?,1,200,?)
        """, (run, base + (requested or path), base + path, "events" if depth else "home", depth, pid)).lastrowid
        self.assertTrue(analyze_saved_page(self.db, pid))
        return fid, pid

    def quarters(self, run=None):
        run = self.run if run is None else run
        refresh_quarters(self.db, run)
        return {period: (start, end, status, count, json.loads(detail)) for period, start, end, status, count, detail
                in self.db.execute("SELECT period,start_date,end_date,status,dated_events,evidence "
                                   "FROM school_event_quarter WHERE run_id=?", (run,))}

    def test_magadan_year_rollover_and_inclusive_intersection(self):
        before = self.make_run(when="2025-12-31T12:59:59+00:00")
        after = self.make_run(when="2025-12-31T13:00:00+00:00")
        for run in (before, after):
            markup = card("Экскурсия", "/events/trip", date_field("30 декабря 2025 — 1 января 2026"))
            self.page("/events/", f'<div class="events">{markup}</div>', run=run)
        old, new = self.quarters(before), self.quarters(after)
        self.assertEqual(old["current"][:4], ("2025-10-01","2025-12-31","dated_candidates",1))
        self.assertEqual(old["next"][:4], ("2026-01-01","2026-03-31","dated_candidates",1))
        self.assertEqual(new["current"][:4], ("2026-01-01","2026-03-31","dated_candidates",1))
        self.assertEqual(new["next"][:4], ("2026-04-01","2026-06-30","none_in_saved_pages",0))
        self.assertEqual(new["current"][4]["reference_date"], "2026-01-01")

    def test_home_listing_and_detail_share_one_url_but_runs_and_schools_do_not(self):
        home = card("Экскурсия", "/events/old#card", date_field("24 декабря, среда"))
        self.page("/", f'<main><div class="events">{home}</div></main>', depth=0)
        current = card("Экскурсия", "/events/trip", date_field("24 декабря 2025"))
        self.page("/events/", f'<div class="events">{current}</div>')
        self.page("/events/trip", '<div class="tpl-component-gw-events tpl-block-full"><h1>Экскурсия</h1>'
                  '<time datetime="2025-12-24T14:00:00+11:00"></time></div>', requested="/events/old")
        other = card("Другая экскурсия", "/events/other", date_field("25 декабря 2025"))
        for org in (1, 2):
            past_run = self.make_run(org=org)
            self.page("/events/", f'<div class="events">{other}</div>', org=org, run=past_run)
        result = self.quarters()
        self.assertEqual(result["current"][2:4], ("dated_candidates",1))
        evidence = result["current"][4]["dated_candidates"][0]
        self.assertEqual(evidence["event"], "https://school1.example/events/trip")
        self.assertEqual(len(evidence["sources"]), 3)
        self.assertEqual(result, self.quarters())

    def test_unknown_year_conflicting_sources_and_templates(self):
        unknown = card("Фестиваль 2025", "/events/fest", date_field("24 декабря, среда"))
        demo = card("Заголовок мероприятия №2", "/events/demo", date_field("24 декабря 2025"))
        slug = card("Театральный вечер", "/events/zagolovok-meropriyatiya-3.html", date_field("2 апреля 2026"))
        wrong = card("Лекция", "/events/lecture", date_field("23 декабря 2025"))
        partial = card("Кружок", "/events/club", date_field("10 декабря"))
        self.page("/", f'<div class="events">{unknown}{demo}{wrong}{partial}</div>', depth=0)
        second = card("Лекция", "/events/lecture", date_field("24 декабря 2025"))
        full = card("Кружок", "/events/club", date_field("11 декабря 2025"))
        self.page("/events/", f'<div class="events">{second}{full}{slug}</div>')
        data = self.quarters()["current"]
        self.assertEqual(data[2:4], ("dates_unresolved",0))
        reasons = {x["title"]: x["reason"] for x in data[4]["unresolved"]}
        self.assertEqual(reasons, {"Фестиваль 2025": "no_complete_calendar_date", "Лекция": "conflicting_dates",
                                   "Кружок": "conflicting_dates"})
        self.assertEqual([x["title"] for x in data[4]["template_signals"]], ["Заголовок мероприятия №2"])

    def test_listing_missing_empty_unrecognized_and_partial(self):
        self.page("/", '<nav><a href="/events/">Мероприятия</a></nav>', depth=0)
        self.assertEqual(self.quarters()["current"][2], "listing_not_captured")
        for html, returned in [
                ('<div class="events">Нет мероприятий</div>', "none_in_saved_pages"),
                ('<div class="events">Ошибка загрузки календаря</div>', "listing_unresolved"),
                ('<main><form><input name="filter"></form></main>', "listing_unresolved")]:
            with self.subTest(html=html):
                run = self.make_run()
                _, pid = self.page("/events/", html, run=run)
                data = self.quarters(run)["current"]
                self.assertEqual(data[2], returned)
                self.assertEqual(data[4]["listings"][0]["page_id"], pid)
                self.assertEqual(data[4]["coverage_incomplete"], returned == "listing_unresolved")
        limited = self.make_run(state="partial")
        self.page("/events/", '<div class="events">Нет мероприятий</div>', run=limited)
        self.assertEqual(self.quarters(limited)["current"][2], "listing_unresolved")

    def test_filtered_or_missing_pagination_keeps_scope_in_evidence(self):
        run = self.make_run()
        self.page("/events/?filter[date]=", '<div class="events">Ничего не найдено</div>', run=run)
        result = self.quarters(run)["current"]
        self.assertEqual(result[2], "listing_unresolved")
        self.assertIn("filtered_listing", result[4]["listings"][0]["reasons"])
        fid, _ = self.page("/events/", '<div class="events">Нет мероприятий</div>')
        next_page = "https://school1.example/events/?page=2"
        self.db.execute("INSERT INTO crawl_link(fetch_id,url,label,section,relation,follow) "
                        "VALUES (?,?,'Ещё','events','pagination',1)", (fid,next_page))
        result = self.quarters()["current"]
        self.assertEqual(result[2], "listing_unresolved")
        self.assertEqual(result[4]["listings"][0]["unresolved_pagination"], [next_page])
        self.page("/events/?page=2", '<div class="events">Нет мероприятий</div>')
        self.assertEqual(self.quarters()["current"][2], "none_in_saved_pages")

    def test_active_filter_and_second_uncaptured_listing_do_not_hide_an_actual_match(self):
        home = '<a href="/calendar-school/">План мероприятий</a><a href="/events/">Мероприятия</a>'
        self.page("/", '<main>' + home + '</main>', depth=0)
        listing = card("Турнир", "/events/tourney", date_field("24 декабря 2025"))
        selected = '<form><select name="filter[Category]"><option value="sport" selected>Спорт</option></select></form>'
        self.page("/events/", '<main>' + selected + f'<div class="events">{listing}</div></main>')
        quarters = self.quarters()
        self.assertEqual(quarters["current"][2:4], ("dated_candidates",1))
        self.assertEqual(quarters["next"][2], "listing_unresolved")
        evidence = quarters["next"][4]
        self.assertTrue(evidence["coverage_incomplete"])
        self.assertEqual(evidence["missing_listing_urls"], ["https://school1.example/calendar-school/"])
        self.assertIn("filtered_listing", evidence["listings"][0]["reasons"])
        _, alternate = self.page("/calendar-school/", "<main>Неразобранный календарь</main>")
        evidence = self.quarters()["next"][4]
        self.assertEqual(evidence["missing_listing_urls"], [])
        self.assertIn(alternate, [x["page_id"] for x in evidence["listings"] if
                                  "unrecognized_listing_content" in x["reasons"]])

    def test_card_text_can_supply_a_year_but_an_explicit_date_error_survives_other_sources(self):
        date_only = date_field("24 декабря")
        home_or_listing = card("Концерт", "/events/concert", date_only)
        precise = card("Лекция", "/events/lecture", date_field("23 декабря 2025"))
        self.page("/events/", f'<div class="events">{home_or_listing}{precise}</div>')

        def detail(title, day_month, body):
            return (f'<div class="tpl-component-gw-events tpl-block-full"><div class="object-full"><h1>{title}</h1>'
                    f'{date_field(day_month)}<div class="description">{body}</div></div></div>')

        _, concert_id = self.page("/events/concert", detail("Концерт", "24 декабря",
                                   "Встреча состоится 24 декабря 2025 года."))
        _, lecture_id = self.page("/events/lecture", detail("Лекция", "23 декабря",
                                   "Лекции состоятся 23 декабря 2025 года и 25 декабря 2025 года."))
        result = self.quarters()["current"]
        self.assertEqual(result[2:4], ("dated_candidates", 1))
        concert, = result[4]["dated_candidates"]
        self.assertEqual((concert["title"],concert["start"]), ("Концерт", "2025-12-24"))
        source = next(s["date_evidence"] for s in concert["sources"] if s["page_id"] == concert_id)
        self.assertEqual(source["method"], "calendar_confirmed_by_card_text")
        self.assertTrue(source["sources"])
        lecture, = result[4]["unresolved"]
        self.assertEqual((lecture["title"],lecture["reason"]), ("Лекция","possible_date_error"))
        suspect = next(s["date_evidence"] for s in lecture["sources"] if s["page_id"] == lecture_id)
        self.assertTrue(suspect["conflict_reason"])
