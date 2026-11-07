import unittest

from bs4 import BeautifulSoup
from home_events import extract_home_events


SITE = "https://school.example/"


class HomeEventsTest(unittest.TestCase):
    def test_nav_and_footer_are_not_home_content(self):
        result = extract_home_events("""
            <header><nav><a href="/events/">Мероприятия</a></nav></header><main>
            <aside class="promo"><a href="/events/?category=2">Все мероприятия</a></aside>
            <section class="news"><h2>Мероприятия</h2><article>Новость</article></section>
            <a href="https://other.example/events">Мероприятия</a></main>
            <footer><a href="/events/">Мероприятия</a></footer>""", SITE)
        self.assertEqual(result["block_status"], "not_found")
        self.assertEqual([(x["kind"],x["url"]) for x in result["links"]], [("navigation",SITE+"events/")])
        self.assertEqual(result["links"][0]["evidence"]["locations"], ["header","footer"])

    def test_home_slider_date_photo_and_template_hints(self):
        html = """<main><a href="/events/">Все мероприятия</a>
            <div class="tpl-component-gw-events" id="events-slider"><div data-object-count="2">
            <a class="object-index" href="/events/zagolovok-meropriyatiya-4.html">
              <div style="background-image:url('/images/garden.jpg')"></div>
              <h2 class="name">Сад школьников</h2><div class="object-index-date-info">14 октября</div></a>
            <a class="object-index" href="/events/first.html"><h2>Заголовок мероприятия №1</h2>
              <div class="description">Описание мероприятия</div><img src="/img_800h600_1.svg" alt="Плакат">
            </a></div></div></main>"""
        result = extract_home_events(html, SITE)
        self.assertEqual(result["block_status"], "cards")
        self.assertEqual([x["kind"] for x in result["links"]], ["section","event","event"])
        first, second = result["evidence"]["cards"]
        self.assertEqual(first["date_text"], "14 октября")
        self.assertIsNone(first["date_start"])
        self.assertEqual(first["date_evidence"]["reason"], "no_year_in_date_field")
        self.assertEqual(first["image_url"], SITE+"images/garden.jpg")
        self.assertIsNone(first["image_alt"])
        self.assertEqual(first["evidence"]["heuristics"], ["template_url_slug"])
        self.assertEqual(second["image_alt"], "Плакат")
        self.assertEqual(set(second["evidence"]["heuristics"]),
                         {"template_title","template_description","template_image_filename"})
        soup = BeautifulSoup(html, "html.parser")
        for card in (first, second):
            self.assertEqual(len(soup.select(card["selector"])), 1)

    def test_empty_and_ambiguous_are_distinct(self):
        fixtures = [
            ('<div class="tpl-component-gw-events"><div data-object-count="0"></div></div>', "empty"),
            ('<section><h2>Мероприятия</h2><p>Нет мероприятий</p></section>', "empty"),
            ('<div class="events"><script src="load.js"></script></div>', "ambiguous"),
            ('<h2>Мероприятия</h2>', "ambiguous"),
            ('<div class="events"><div data-object-count="4">Загрузка…</div></div>', "ambiguous")]
        for html, status in fixtures:
            with self.subTest(html=html):
                self.assertEqual(extract_home_events(html,SITE)["block_status"],status)

    def test_detail_can_corroborate_a_missing_year_on_an_explicit_calendar_day(self):
        html = """<main><div class="tpl-block-full tpl-component-gw-events"><div class="object-full">
            <h1>Экономический диктант 2026</h1>
            <div class="description"><time datetime="2026-10-14">14 октября 2026 года</time> состоится диктант.</div>
            <div class="object-date"><span class="day">14</span> <span class="month">октября</span></div>
            </div></div></main>"""
        result = extract_home_events(html,SITE+"events/dictation")
        card, = result["evidence"]["cards"]
        self.assertEqual(result["evidence"]["page_kind"],"detail")
        self.assertEqual(card["url"],SITE+"events/dictation")
        self.assertEqual(card["date_text"],"14 октября")
        self.assertEqual(card["date_start"],"2026-10-14")
        self.assertEqual(card["date_evidence"]["method"],"calendar_confirmed_by_card_text")

    def test_yearless_date_corroboration_uses_only_the_scoped_card_description(self):
        base = """<div class="tpl-block-full tpl-component-gw-events"><div class="object-full">
          <h1>Экономический диктант 2026</h1><div class="description" id="own">{}</div>
          <div class="object-date"><span class="day">14</span><span class="month">октября</span>
          <span class="week-time">среда</span></div></div></div>
          <aside><div class="description">Выпуск 9 ноября 2024</div></aside>"""
        fixtures = [
            ("14 октября 2026 года пройдёт диктант. Участие 14.10.2026 с 05:00 до 22:00.",
             "2026-10-14",None),
            ("Открытие 14 октября 2026 года, финал 15 октября 2026 года.",None,"multiple_distinct_card_text_dates"),
            ("Открытие 14 октября 2026 года, финал 15 октября.",None,"multiple_distinct_card_text_dates"),
            ("Встреча 15 октября 2026 года.",None,"card_text_day_month_mismatch"),
            ("14 октября в 10:00, итог 2:1. <a href='/14.10.2026'>14.10.2026</a>",None,"no_year_in_date_field"),
            ("https://agenda.example/14.10.2026 <span class='date'>14.10.2026</span>",None,"no_year_in_date_field"),
            ("14 октября 2025, 14 октября 2026.",None,"multiple_distinct_card_text_dates"),
        ]
        for text, expected, reason in fixtures:
            with self.subTest(text=text):
                html = base.format(text)
                card, = extract_home_events(html,SITE+"events/festival")["evidence"]["cards"]
                ev = card["date_evidence"]
                self.assertEqual((card["date_start"],card["date_end"]),(expected,expected))
                self.assertEqual(ev.get("conflict_reason") or ev["reason"],reason)
                if expected or reason not in ("no_year_in_date_field",):
                    body = [s for s in ev["sources"] if s.get("role")=="card_text"]
                    self.assertEqual(body[0]["selector"],"#own")
                    self.assertTrue(body[0]["quotes"])
                if ev.get("conflict_reason"):
                    self.assertEqual(ev["reason"],"possible_date_error")
                self.assertEqual(ev["calendar_parts"],[{"year":None,"month":10,"day":14}])

    def test_listing_card_with_list_in_description_retains_outer_fields(self):
        html = """<div class="tpl-component-gw-events"><div class="object">
             <h2 class="name"><a href="/events/seminar">Семинар</a></h2>
             <div class="description"><ul><li>Первая тема</li><li>Вторая тема</li></ul></div>
             <div class="object-date"><div class="object-date-item">
                <span class="day">24-27</span> <span class="month">января</span> <span class="year">2022</span>
             </div></div></div></div>"""
        result = extract_home_events(html,SITE+"events/")
        card, = result["evidence"]["cards"]
        self.assertEqual(result["evidence"]["page_kind"],"listing")
        self.assertEqual((card["title"],card["date_start"],card["date_end"]),
                         ("Семинар","2022-01-24","2022-01-27"))

    def test_generic_dates_across_quarters_and_new_year(self):
        fixtures = [
            ("<div class='date'>30 декабря — 2 января 2026</div>", "2025-12-30","2026-01-02"),
            ("<div class='date'>30 декабря 2025 – 2 января</div>", "2025-12-30","2026-01-02"),
            ("<div class='object-date'><div>30 сентября 2024</div><div>11 ноября 2024</div></div>",
             "2024-09-30","2024-11-11"),
            ("<time datetime='2026-01-02T16:30:00+11:00'>2 января</time>","2026-01-02","2026-01-02"),
            ("<time datetime='2026-10-14'>15 октября 2026</time>",None,None),
            ("<div class='date'>31 февраля 2026</div>",None,None),
            ("<div class='date'>30 декабря – 2 января</div>",None,None)]
        for markup, start, end in fixtures:
            html = f"<section><h2>Афиша</h2><article><h3><a href='/concert'>Концерт</a></h3>{markup}</article></section>"
            with self.subTest(markup=markup):
                card, = extract_home_events(html,SITE)["evidence"]["cards"]
                self.assertEqual((card["date_start"],card["date_end"]),(start,end))

    def test_listing_filters_pagination_and_two_gosweb_date_items(self):
        html = """<main><div class="tpl-component-gw-filter tpl-template-filter-event"><form>
            <input name="filter[545][Name]" value="">
            <select name="filter[545][Category]"><option value="">Все тематики</option>
            <option value="1">Искусство</option></select></form></div>
            <div class="tpl-component-gw-events"><div class="object"><h2 class="name">
            <a href="/events/club">Кружок</a></h2><div class="object-date">
            <div class="object-date-item"><span class="day">04</span><span class="month">декабря</span>
            <span class="year">2024</span></div><span class="delimiter"><svg></svg></span>
            <div class="object-date-item"><span class="day">09</span><span class="month">апреля</span>
            <span class="year">2025</span></div></div></div></div>
            <nav class="pagination"><a class="pagination-item active" href="?cur_cc=545">1</a>
            <a class="pagination-item" href="?cur_cc=545&amp;curPos=20">2</a></nav></main>"""
        url = SITE+"events/"
        result = extract_home_events(html,url)["evidence"]
        self.assertEqual(result["pagination_urls"],[url+"?cur_cc=545&curPos=20"])
        self.assertFalse(result["listing_filters_active"])
        card, = result["cards"]
        self.assertEqual((card["date_start"],card["date_end"]),("2024-12-04","2025-04-09"))
        marked = html.replace('option value="1"', 'option selected value="1"')
        self.assertTrue(extract_home_events(marked,url)["evidence"]["listing_filters_active"])
        self.assertTrue(extract_home_events(html,url+"?category=2")["evidence"]["listing_filters_active"])


if __name__ == "__main__":
    unittest.main()
