import unittest

from bs4 import BeautifulSoup

from home_content import extract_home_content


class HomeContentTest(unittest.TestCase):
    base = "https://school.example/"
    terms = {"main_photo": ["фасад школы"], "canteen_photo": ["столовая", "обеденный зал", "питание"]}

    def extract(self, source):
        return extract_home_content(source, self.base, self.terms)

    def test_upper_image_with_custom_alt_and_welcome_next_to_empty_school_text(self):
        source = """
          <header><img alt="Фасад школы" src="/wrong"></header><main>
            <div class="tpl-component-gw-base-image"><div class="tpl-anchor" id="izobrazheniya"></div>
              <a href="https://school.gosuslugi.ru"><img src="/72.jpg" alt="МБОУ НШ-ДС №72"></a></div>
            <div><div class="tpl-anchor" id="text"></div><article>
              <p>Рады приветствовать вас на сайте школы №72!</p>
              <p>Сейчас в школе учатся четыре начальных класса.</p>Дополнительное предложение.</article></div>
            <div><div class="tpl-anchor" id="tekst-o-shkole"></div></div>
            <div class="object-item-text">В 2025 школа выпустила 20 выпускников</div>
            <h2>Новости</h2><p>Новости нашей школы рассказывают о новых достижениях.</p></main>"""
        result = self.extract(source)
        photo = result["main_photo"]
        self.assertEqual((photo["status"], photo["image_url"], photo["alt"]),
                         ("found", self.base + "72.jpg", "МБОУ НШ-ДС №72"))
        self.assertEqual(BeautifulSoup(source, "html.parser").select_one(photo["evidence"]["selector"])["src"], "/72.jpg")
        desc = result["school_description"]
        self.assertEqual(desc["status"], "found")
        self.assertEqual(desc["evidence"]["paragraphs"], ["Рады приветствовать вас на сайте школы №72!",
                         "Сейчас в школе учатся четыре начальных класса.", "Дополнительное предложение."])
        self.assertEqual(desc["evidence"]["length"], len(desc["text"]))

    def test_generic_upper_text_without_anchor_and_missing_description(self):
        first = "<p>Наша школа открыта в 1989 году в районном центре.</p><p>Сейчас здесь учатся двести детей.</p>"
        news = "<h2>Новости школы</h2><p>Школа поздравляет всех победителей вчерашних соревнований.</p>"
        result = self.extract("<main>" + first + news + "</main>")["school_description"]
        self.assertEqual(result["status"], "found")
        self.assertEqual(len(result["evidence"]["paragraphs"]), 2)
        empty = self.extract("<main><div class='object-item-text'>В 2025 школа выпустила 20 выпускников</div>"
                             + news + "</main>")
        self.assertEqual(empty["school_description"]["status"], "not_found")

    def test_gym_canteen_conflict_and_local_library(self):
        source = """<main><section><div><div class="tpl-anchor" id="korotko-o-stolovoy-izobrazhenie"></div>
          <img src="/room.jpg" alt="Коротко о столовой: Изображение."></div>
          <div><p>Спортивный зал</p><p>Площадь: 120 м2</p></div></section>
          <section><img src="/library.jpg"><p>Библиотека</p></section></main>"""
        photo = self.extract(source)["canteen_photo"]
        self.assertEqual((photo["status"], photo["image_url"]), ("ambiguous", self.base + "room.jpg"))
        self.assertIn("conflicting_facility", photo["evidence"]["signals"])
        self.assertIn("Спортивный зал", photo["evidence"]["context"])
        self.assertEqual(photo["evidence"]["alternatives"], [])

    def test_custom_vocabulary_and_no_meal_plan_or_news_image(self):
        source = """<main>
          <section><h2>Организация питания</h2><a href="/menu/"><img src="/schedule.jpg" alt="Питание"></a></section>
          <article class="news"><img src="/news.jpg" alt="Обеденный зал"></article>
          <figure><img src="/hall.jpg"><figcaption>Трапезная</figcaption></figure></main>"""
        self.assertEqual(self.extract(source)["canteen_photo"]["status"], "not_found")
        configured = {**self.terms, "canteen_photo": ["трапезная", "питание"]}
        photo = extract_home_content(source, self.base, configured)["canteen_photo"]
        self.assertEqual((photo["status"], photo["image_url"]), ("found", self.base + "hall.jpg"))
        self.assertIn("трапезная", photo["evidence"]["signals"])

    def test_two_candidate_rooms_or_placeholder_remain_ambiguous(self):
        pair = """<main><figure><img src="/a.jpg" alt="Столовая"></figure>
                  <figure><img src="/b.jpg" alt="Столовая"></figure></main>"""
        photo = self.extract(pair)["canteen_photo"]
        self.assertEqual((photo["status"], photo["image_url"]), ("ambiguous", None))
        self.assertEqual(len(photo["evidence"]["alternatives"]), 1)
        hero = self.extract('<main><div class="hero"><img src="/placeholder.jpg" alt="Фасад школы"></div></main>')
        self.assertEqual(hero["main_photo"]["status"], "ambiguous")

    def test_main_photo_without_template_needs_local_institution_intro(self):
        intro = """<main><section><h1>Школа №72</h1><div><p>Здесь обучаются дети младших классов
          из трёх городских микрорайонов.</p></div><figure><img src="/building.jpg" alt=""></figure></section>
          <section><h2>Новости</h2><img src="/event.jpg"></section></main>"""
        photo = self.extract(intro)["main_photo"]
        self.assertEqual((photo["status"],photo["image_url"]), ("found", self.base + "building.jpg"))
        self.assertIn("school_heading_and_intro_with_single_image", photo["evidence"]["signals"])
        for heading, caption in [("Новости школы", "Встреча"), ("Школа №72", "Наша школьная столовая")]:
            source = f"""<main><section><h1>{heading}</h1><p>Здесь обучаются дети младших классов
              из трёх городских микрорайонов.</p><figure><img src="/table.jpg" alt="">
              <figcaption>{caption}</figcaption></figure></section></main>"""
            with self.subTest(heading=heading):
                self.assertEqual(self.extract(source)["main_photo"]["status"], "not_found")

    def test_responsive_candidates_record_html_attributes_and_keep_all_competing_images(self):
        source = """<main><section class="hero"><img alt="Фасад школы" src="/placeholder.jpg"
                    srcset="/small.jpg 480w, /large.jpg 1200w"></section></main>"""
        photo = self.extract(source)["main_photo"]
        self.assertEqual((photo["image_url"],photo["evidence"]["source_attribute"]), (self.base + "small.jpg", "srcset"))
        self.assertEqual([s["descriptor"] for s in photo["evidence"]["html_sources"]], [None,"480w","1200w"])
        bad = self.extract('<main><section class="hero"><img srcset="data:image/png;base64,AAAA 1x"></section></main>')
        self.assertEqual(bad["main_photo"]["status"], "not_found")
        gallery = "<main>" + "".join(f'<figure><img src="/{i}.jpg" alt="Столовая"></figure>' for i in range(8)) + "</main>"
        rooms = self.extract(gallery)["canteen_photo"]
        self.assertEqual((rooms["status"], rooms["image_url"]), ("ambiguous", None))
        values = [rooms["evidence"], *rooms["evidence"]["alternatives"]]
        self.assertEqual([v["image_url"] for v in values], [self.base + f"{i}.jpg" for i in range(8)])
