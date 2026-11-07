import unittest

from navigation import discover_links, find_section_headings, staff_pagination_controls


class NavigationTest(unittest.TestCase):
    site = "https://school.example"

    def discover(self, html, path="/", kind="school", section="home"):
        return discover_links(html, self.site + path, self.site, kind, section)

    def test_staff_pager_data_targets_unknown_and_inactive_controls(self):
        html = """<nav class="pagination">
            <a class="active" href="?page=1">1</a><span data-href="/directory/p2">2</span>
            <button data-url="/directory/p3" aria-label="Следующая страница"></button>
            <a href="javascript:more()">Далее</a><button disabled>Показать ещё</button></nav>
            <form action="/query"><button>Показать</button></form>
            <a rel="next" href="https://other.example/p4">Внешний переход</a>
            <button class="gw-js-show-more-button" data-action-url="/staff/more"
                    data-params="?curPos=12&amp;isNaked=1">Показать ещё</button>"""
        state = staff_pagination_controls(html, self.site + "/directory/", self.site, "staff")
        self.assertEqual(state["urls"], [self.site + "/directory/p2", self.site + "/directory/p3",
                                         self.site + "/staff/more?curPos=12&isNaked=1"])
        self.assertEqual(state["unresolved"], ["Далее", "Внешний переход"])

    def test_menu_cards_and_attachments(self):
        links = self.discover("""
            <div class="gw-mobile-menu-nav">
              <a href="/x">Сведения об образовательной организации</a>
              <a href="/map/">Навигатор по сайту</a>
              <a href="/fun/">Кружки</a>
              <a href="/nasha-shkola/profilnye-napravleniya/istoriya/">История</a>
            </div>
            <a href="/novosti/urok.html">Директор рассказал о школе</a>
            <a href="/docs/устав.pdf" download>Устав</a>
            <a href="https://example.org/novosti/">Новости</a>
            <a href="javascript:void(0)">Подробнее</a>
        """)
        self.assertEqual([(l.section, l.relation, l.follow) for l in links], [
            ("information", "section", True), ("hub", "navigation", True),
            ("unknown", "navigation", False), ("unknown", "navigation", False),
            ("news", "detail", True), ("documents", "document", False), ("news", "external", False),
        ])
        self.assertIn("%D1%83", links[5].url)

    def test_garden_separate_names_combined_fragment_and_photo_viewer(self):
        html = """<nav><a href="/sobytia/">Подробнее</a><a href="#local">Новости и мероприятия</a></nav>
            <article><a data-lightbox="a" href="/viewer">Гости праздника</a></article>
            <article><a href="/photo-gallery/">Наши фотографии</a><a href="/x.heic">Фотография</a></article>
            <article><a href="/publications/4">Новости с праздника</a></article>
            <nav class="pagination"><a aria-current="page" href="?page=1">1</a>
                <a href="/more/">Следующая страница</a></nav>"""
        links = self.discover(html,path="/lenta/",kind="kindergarten",section="news")
        self.assertEqual([(l.url.removeprefix(self.site),l.section,l.relation,l.follow) for l in links], [
            ("/sobytia/","events","section",True), ("/lenta/#local","news","section",False),
            ("/lenta/#local","events","section",False), ("/publications/4","news","detail",True),
            ("/more/","news","pagination",True)])
        self.assertEqual(find_section_headings("<h2>Новости и мероприятия</h2>","kindergarten","home"),
                         [("news","Новости и мероприятия"),("events","Новости и мероприятия")])

    def test_menu_after_redirect_to_www_is_still_internal(self):
        links = discover_links('<a href="/contacts/">Контакты</a>',
                               "https://www.school.example/", self.site, "school")
        self.assertEqual([(link.section, link.follow) for link in links], [("contacts", True)])

    def test_relative_links_base_fragments_and_full_labels(self):
        links = self.discover("""
            <base href="/nash-detskiy-sad/">
            <a href="nash-kollektiv/"><img alt="Наш коллектив">Наш коллектив</a>
            <a href="nash-kollektiv/#card">Все сотрудники</a>
            <a href="nash-kollektiv/">Педагогический состав</a>
            <a href="/#contacts">Контакты</a>
            <a href="/o-shkole/#history">Подробнее</a>
            <a href="/x#history">История школы</a>
            <a href="/pedagogam-i-sotrudnikam/" aria-label="Педагогам и сотрудникам"></a>
            """, kind="kindergarten")
        self.assertEqual([(l.url.removeprefix(self.site), l.section, l.follow) for l in links], [
            ("/nash-detskiy-sad/nash-kollektiv/", "team", True),
            ("/nash-detskiy-sad/nash-kollektiv/#card", "team", False),
            ("/nash-detskiy-sad/nash-kollektiv/", "staff", True),
            ("/#contacts", "contacts", False), ("/o-shkole/", "about", True),
            ("/o-shkole/#history", "history", False), ("/x", "hub", True), ("/x#history", "history", False),
            ("/pedagogam-i-sotrudnikam/", "staff_resources", True),
        ])

    def test_more_button_page_controls_filters_and_article_queries(self):
        links = self.discover("""
            <button class="gw-js-show-more-button" data-params="?curPos=5&amp;isNaked=1"
                    data-action-url="/novosti/novosti.html">Показать еще</button>
            <nav><a href="?cur_cc=10&amp;curPos=9">2</a></nav>
            <a href="?filter%5B1%5D=4">Тематика</a>
            <main><a href="?id=121">Карточка</a></main>
            <a href="?cur_cc=10">1</a>
            <article><a href="/click/?target=outside">Перейти</a></article>
            <main><a href="/golosovanie/">Проголосовать</a></main>
            """, path="/novosti/", section="news")
        self.assertEqual([(l.relation, l.follow) for l in links], [
            ("pagination", True), ("pagination", True), ("navigation", False), ("detail", True),
        ])
        self.assertEqual(links[0].url, self.site + "/novosti/novosti.html?curPos=5&isNaked=1")

    def test_history_is_an_unlinked_heading_on_the_engineering_school_about_page(self):
        # The two first headings reproduce the relevant fragments of its "О школе" page.
        html = """
            <h1 class="gw-page-header">О школе</h1>
            <div class="tpl-anchor" id="zagolovok"></div><h2 class="tpl-text-header2">История школы</h2>
            <h2 class="tpl-text-header2">Документы</h2>
            <nav><h2>Педагогический состав</h2></nav>
            <div aria-hidden="true"><h2>Новости</h2></div>
            <div class="gw-news-item"><h2>Контакты</h2></div>
            <h4><a href="/events/">Мероприятия</a></h4>
            <h5>Профильные направления</h5><h6>История</h6>
        """
        self.assertEqual(find_section_headings(html, "school", "about"), [
            ("about", "О школе"), ("history", "История школы"), ("documents", "Документы"),
        ])
        self.assertEqual(find_section_headings("<h3>История</h3>", "school", "news"), [])
        self.assertEqual(find_section_headings("<h3>История</h3>", "school", "about"), [("history", "История")])


if __name__ == "__main__":
    unittest.main()
