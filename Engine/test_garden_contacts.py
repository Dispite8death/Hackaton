import unittest

from bs4 import BeautifulSoup

from garden_contacts import extract_garden_contacts


class GardenContactsTest(unittest.TestCase):
    site = "https://garden.example/"

    def test_template_mixed_hours_and_actual_value_selectors(self):
        def field(label, value):
            return (f'<div class="gw-gradient-block__contact-item-block">'
                    f'<div class="gw-gradient-block__contact-item-block-caption">{label}</div>'
                    f'<div class="gw-gradient-block__contact-item-block-text">{value}</div></div>')

        html = '<main>' + field("Адрес", '<a href="/kontakty/">г. Магадан, ул. Кольцевая, д. 32 А</a>') + field(
            "Контактный телефон", '<a href="tel:+74132653364">8 (4132) 65-33-64</a>') + field(
            "Электронная почта", '<a href="mailto:garden@example.org">Написать в детский сад</a>') + field(
            "Режим работы", '<p>Адрес места нахождения детского сада: ул. Садовая, д. 5</p>'
            '<p>пн — пт: 07:30—19:30</p><p>сб — вс: выходной</p><p>График работы специалистов:</p>'
            '<p>пн — пт: 9:00—17:30</p>') + '</main>'
        soup = BeautifulSoup(html, "html.parser")
        result = extract_garden_contacts(soup, self.site)
        self.assertEqual({v["status"] for v in result.values()}, {"found"})
        self.assertEqual(result["hours"]["values"][0]["value"], "пн — пт: 07:30—19:30 сб — вс: выходной")
        specialists, = result["hours"]["other_schedules"]
        self.assertEqual(specialists["value"], "График работы специалистов: пн — пт: 9:00—17:30")
        self.assertEqual(specialists["location"], "under_header")
        self.assertEqual(len(soup.select(specialists["selector"])), 1)
        self.assertEqual(result["email"]["values"][0]["value"], "garden@example.org")
        self.assertEqual(result["phone"]["values"][0]["value"], "8 (4132) 65-33-64")
        for entry in result.values():
            value, = entry["values"]
            self.assertEqual(value["location"], "under_header")
            self.assertEqual(len(soup.select(value["selector"])), 1)

    def test_responsive_phone_aliases_and_official_footer_are_attributed(self):
        source = """<section><h2>Контактная информация</h2><dl><dt>Адрес</dt><dd>г. Магадан, ул. Садовая, 8</dd>
            <dt>Телефон</dt><dd>8 (4132) 60-20-40</dd></dl><div><a href="tel:+74132602040">Позвонить</a></div></section>
            <footer><a href="mailto:garden@example.org">Написать</a><div><b>Телефон ЕДДС</b>
            <a href="tel:+74000000000">+7 400 000 0000</a></div></footer>"""
        result = extract_garden_contacts(source, self.site)
        self.assertEqual(result["phone"]["status"], "found")
        phone, = result["phone"]["values"]
        self.assertGreaterEqual(len(phone["sources"]), 2)
        self.assertEqual(result["email"]["values"][0]["location"], "footer")
        self.assertEqual(result["address"]["status"], "found")
        self.assertEqual(result["hours"]["status"], "not_found")

    def test_staff_news_outside_services_reception_and_an_empty_link(self):
        source = """<header><a href="/contacts">Контакты</a></header>
            <article class="news"><h2>Новость</h2><p>Электронная почта учреждения: author@example.org</p></article>
            <section><h2>Контакты</h2><div><h3>Заведующая</h3><p>Телефон: +7 (400) 000 0007</p></div>
            <div><h3>Горячая линия</h3><a href="mailto:operator@example.org">Написать</a></div>
            <dl><dt>Режим работы</dt><dd>Часы приёма заведующей: понедельник с 15:00 до 17:00</dd></dl>
            <div class="gw-person-card"><a href="mailto:teacher@example.org">Почта сотрудника</a></div></section>"""
        result = extract_garden_contacts(source, self.site)
        self.assertEqual({v["status"] for v in result.values()}, {"not_found"})
        reception, = result["hours"]["other_schedules"]
        self.assertIn("15:00", reception["value"])

    def test_two_distinct_institutional_numbers_survive_for_review(self):
        source = """<footer><p>Телефон: +7 (4132) 60-20-40</p><p>Телефон: +7 (4132) 60-20-42</p></footer>"""
        result = extract_garden_contacts(source, self.site)["phone"]
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["values"]), 2)
