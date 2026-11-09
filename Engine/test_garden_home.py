import unittest

from bs4 import BeautifulSoup
from garden_home import extract_garden_home


SITE = "https://garden.example/"
NAME = "Детский сад № 42"


def scan(html):
    return extract_garden_home(html, SITE, NAME, {})


class GardenHomeTest(unittest.TestCase):
    def test_team_navigation_listing_profiles_and_responsive_copies(self):
        html = """<header><nav><a href="/team/">Наш коллектив</a>
          <a href="/pedagogam-i-sotrudnikam/">Педагогам и сотрудникам</a></nav></header>
          <main><section><h2>Педагогический коллектив</h2>
          <a href="/team/">Все сотрудники</a><div class="gw-person-card">
          <a href="/team/ivanova"><img src="profile.jpg"></a><div class="gw-person-card__text">
          <div class="gw-person-card__job">Заведующий</div>
          <a class="gw-person-card__nametext-break" href="/team/ivanova">Иванова Анна Сергеевна</a>
          </div></div><a href="/team/">Все сотрудники</a></section></main>"""
        item = scan(html)["team"]
        self.assertTrue(item["heading_found"])
        self.assertEqual(item["block_status"],"cards")
        self.assertEqual(len(item["navigation_links"]),1)
        self.assertEqual(len(item["listing_links"]),1)
        self.assertEqual(len(item["listing_links"][0]["selectors"]),2)
        card, = item["cards"]
        self.assertEqual((card["url"],card["title"],card["role"]),
                         (SITE+"team/ivanova","Иванова Анна Сергеевна","Заведующий"))
        self.assertEqual(len(BeautifulSoup(html,"html.parser").select(card["selector"])),1)

    def test_combined_heading_and_news_dates_do_not_invent_events(self):
        html = """<main><section><h2>Новости и события</h2>
          <a href="/novosti-i-sobytiya/">Все новости и события</a>
          <div class="gw-news"><a class="card" href="/novosti-i-sobytiya/news1.html">
          <time>26 октября 2026 г.</time><div class="h5">Ходили в музей</div></a></div></section>
          </main><footer><h2>Мероприятия</h2></footer>"""
        result=scan(html)
        self.assertEqual(result["news"]["block_status"],"cards")
        self.assertEqual(result["news"]["cards"][0]["title"],"Ходили в музей")
        events=result["events"]
        self.assertEqual(events["headings"][0]["combined"],True)
        self.assertTrue(events["listing_links"][0]["combined"])
        self.assertEqual(events["block_status"],"ambiguous")
        self.assertEqual(events["cards"],[])
        self.assertEqual(events["evidence"]["unclassified_combined_urls"],[SITE+"novosti-i-sobytiya/news1.html"])

    def test_clearly_typed_event_in_shared_section(self):
        result=scan("""<main><section><h2>Новости и события</h2><div class="gw-news">
          <a class="card event-card" href="/events/festival"><h3>Праздник</h3></a>
          <a class="card" href="/news/first"><h3>Новости группы</h3></a></div></section></main>""")
        self.assertEqual([c["title"] for c in result["events"]["cards"]],["Праздник"])
        self.assertEqual([c["title"] for c in result["news"]["cards"]],["Новости группы"])

    def test_logo_is_distinct_from_a_facade_and_gallery_labels_are_not_proof(self):
        html = """<main><section class="gw-gradient-block"><h1>Детский сад № 42</h1>
          <img class="gw-gradient-block__logo-image" alt="Детский сад № 42" src="/logo.jpg"></section>
          <div class="gw-slider"><a data-description="Памятка об опасности заброшенных зданий">
          <img src="/warning.jpg" alt="."></a><a data-description="Проект об экологии">
          <img src="/project.jpg" alt="."></a></div></main>"""
        p=scan(html)["main_photo"]
        self.assertEqual((p["status"],p["image_url"],p["evidence"]["source_kind"]),
                         ("found",SITE+"logo.jpg","institution_logo"))
        self.assertEqual(p["evidence"]["building_candidate"]["status"],"not_identified")
        self.assertFalse(p["evidence"]["building_verified"])
        strong=html.replace("Памятка об опасности заброшенных зданий","Фасад здания детского сада № 42")
        p=scan(strong)["main_photo"]
        self.assertEqual(p["evidence"]["building_candidate"]["status"],"candidate")
        self.assertEqual(p["image_url"],SITE+"warning.jpg")
        tied=strong.replace("Проект об экологии","Главный вход в здание детского сада")
        p=scan(tied)["main_photo"]
        self.assertEqual(p["evidence"]["building_candidate"]["status"],"ambiguous")
        self.assertIsNone(p["evidence"]["building_candidate"]["image_url"])

    def test_template_logo_and_unrelated_news_photo(self):
        p=scan("""<main><section class="gw-gradient-block"><h1>Детский сад № 42</h1>
          <img class="gw-gradient-block__logo-image" alt="Детский сад № 42" src="/kinder/img/cube.svg"></section>
          <section class="gw-news"><a class="card"><img alt="Фото здания детского сада" src="news.jpg">
          </a></section></main>""")["main_photo"]
        self.assertEqual(p["status"],"ambiguous")
        self.assertEqual(p["evidence"]["source_kind"],"template_logo")
        self.assertEqual(p["evidence"]["building_candidate"]["status"],"not_identified")

    def test_publications_need_a_detail_link_and_media_remain_in_evidence(self):
        html = """<main><div class="gw-slider"><a data-gallery href="/files/day.jpg"
                  data-description="Великий май — Победный май!"><img src="/files/day.jpg"></a></div>
        <section><h2>Новости и события</h2><a href="/news/">Все новости</a><div class="gw-news">
        <a href="/news/first"><article class="card"><h3>Отдельная публикация</h3></article></a>
        <article class="card"><a href="/preview.png"><img src="/preview.png"></a>
          <h3><a href="/news/second">Вторая публикация</a></h3></article>
        <figure><img src="/files/holiday.jpg"><figcaption>8 марта!</figcaption></figure>
        <a class="card" href="#only"><h3>Якорь</h3></a><a class="card" href="/"><h3>Главная</h3></a>
        <a class="card" href="/news/?category=7"><h3>Фильтр ленты</h3></a>
        <a class="card" href="/memo.pdf"><h3>Памятка</h3></a>
        <a class="card" href="javascript:void(0)"><h3>Кнопка</h3></a>
        <a class="card" href="https://outside.example/story"><h3>Сторонний ресурс</h3></a>
        </div></section></main>"""
        result=scan(html)
        self.assertEqual([v["url"] for v in result["news"]["cards"]],[SITE+"news/first",SITE+"news/second"])
        self.assertEqual(result["events"]["cards"],[])
        excluded=result["news"]["evidence"]["excluded_examples"]
        reasons={v["title"]:v["reasons"][0]["reason"] for v in excluded}
        self.assertEqual(reasons["8 марта!"],"no_detail_link")
        self.assertEqual(reasons["Великий май — Победный май!"],"media_or_file")
        self.assertEqual(reasons["Фильтр ленты"],"section_listing")
        self.assertEqual(reasons["Кнопка"],"non_http_link")
        self.assertEqual(reasons["Главная"],"same_page")
        self.assertEqual(reasons["Сторонний ресурс"],"external_url")
        self.assertEqual(reasons["Памятка"],"media_or_file")
        self.assertEqual(reasons["Якорь"],"fragment_only")


if __name__ == "__main__":
    unittest.main()
