import unittest

from bs4 import BeautifulSoup

from staff_extract import extract_staff


class StaffExtractionTest(unittest.TestCase):
    site = "https://school.example/"

    def test_template_handles_incomplete_card_and_does_not_borrow_neighbor_photo(self):
        html = """
            <div class="tpl-component-gw-staff tpl-template-director">
                <div class="object-item">
                    <div class="contacts-object-info tpl-text-header3"><a href="/ivan">Иванов Иван Иванович</a></div>
                    <div class="contacts-object-info-subname">И.о. директора школы</div>
                </div>
                <div class="object-item">
                    <div class="contacts-object-info tpl-text-header3"><a href="/anna">Петрова Анна Алексеевна</a></div>
                    <img alt="Фото отсутствует" src="/placeholder.png">
                </div>
            </div>"""
        head, incomplete = extract_staff(html, self.site)
        self.assertTrue(head.is_head)
        self.assertEqual((head.photo_state, head.profile_url), ("absent", self.site + "ivan"))
        self.assertEqual((incomplete.role, incomplete.is_head, incomplete.photo_state), (None, False, "placeholder"))

    def test_semantic_fallback_in_known_wrapper_with_changed_descendants(self):
        html = """
            <a class="gw-person-card" href="/people/13">
                <img src="/faces/13.jpg" alt="Петрова Анна Алексеевна">
                <div class="new-job">Заведующая детским садом</div>
                <div class="new-name">Петрова Анна Алексеевна</div>
            </a>"""
        card, = extract_staff(html, self.site)
        self.assertTrue(card.is_head)
        self.assertEqual((card.name, card.role, card.method, card.photo_state),
                         ("Петрова Анна Алексеевна", "Заведующая детским садом", "semantic", "linked"))
        self.assertEqual(card.photo_url, self.site + "faces/13.jpg")
        soup = BeautifulSoup(html, "html.parser")
        for el in card.elements:
            self.assertEqual(len(soup.select(el.selector)), 1)

    def test_unfamiliar_markup_searches_shared_card_not_news_or_next_person(self):
        html = """
            <article class="news"><strong>Директор</strong><p>Сидоров Иван Петрович</p></article>
            <nav><b>Директор</b><p>Сидоров Иван Петрович</p></nav>
            <div class="card-footer"><b>Заведующий</b><p>Сидоров Иван Петрович</p></div>
            <section id="people"><div class="card"><img src="/head.jpg">
              <div><span>Директор:</span><p>Иванов Иван Иванович</p></div></div>
              <div class="card"><b>Заместитель директора</b><p>Петрова Анна Игоревна</p></div>
              <div class="card"><b>Заведующий хозяйством</b><p>Петров Виктор Игоревич</p></div>
            </section>"""
        cards = extract_staff(html, self.site)
        self.assertEqual([(c.role, c.is_head) for c in cards], [
            ("Директор", True), ("Заместитель директора", False), ("Заведующий хозяйством", False)])
        self.assertEqual(cards[0].photo_url, self.site + "head.jpg")
        self.assertIsNone(cards[1].photo_url)
        self.assertEqual(cards[2].method, "semantic")

    def test_named_columns_and_local_contact_fields(self):
        html = """
            <table><thead><tr><th>ФИО</th><th>Должность</th><th>Уровень образования</th></tr></thead>
            <tbody><tr><td>Петрова Анна Алексеевна</td><td>Воспитатель</td><td>Высшее</td></tr></tbody>
            </table><footer><a href="mailto:school@example.org">school@example.org</a></footer>
            <div class="gw-personal-card">
              <div class="gw-personal-card__name">Иванов Иван Иванович</div>
              <div class="gw-personal-card__name-subscription">Директор</div>
              <dl><dt>Телефон</dt><dd>2-33-44</dd></dl>
              <p><b>Образование:</b> Высшее педагогическое</p>
              <span itemprop="teachingLevel">Начальное общее образование</span>
            </div>"""
        cards = {c.name: c for c in extract_staff(html, self.site, "staff")}
        head = cards["Иванов Иван Иванович"]
        self.assertEqual((head.email, head.phone, head.education), (None, "2-33-44", "Высшее педагогическое"))
        self.assertEqual(cards["Петрова Анна Алексеевна"].education, "Высшее")

    def test_no_guess_when_only_other_staff_or_a_family_name_is_absent(self):
        html = """
            <div><strong>Директор</strong><span>Иванов Иван Иванович</span>
              <span>Петров Виктор Игоревич</span></div>
            <div class="gw-person-card"><p class="gw-person-card__job">Заведующий</p>
              <p class="gw-person-card__name">Фамилия Имя Отчество</p></div>"""
        card, = extract_staff(html, self.site)
        self.assertIsNone(card.name)
        self.assertTrue(card.is_head)

    def test_multiline_education_caption_and_punctuation_in_real_profile_patterns(self):
        html = """
            <div class="gw-employee-info">
              <h1 class="gw-employee-info__title">Петрова Анна Алексеевна.</h1>
              <p class="gw-employee-info__position">учитель физической культуры</p>
              <div><h5>Уровень (уровни) профессионального образования с указанием
                наименования направления подготовки и квалификации</h5>
                Средне-специальное образование, квалификация "Учитель физической культуры"</div>
            </div>"""
        person, = extract_staff(html, self.site + "profile", "staff")
        self.assertEqual(person.name, "Петрова Анна Алексеевна")
        self.assertEqual(person.education, 'Средне-специальное образование, квалификация "Учитель физической культуры"')

    def test_signature_tooltip_is_not_an_employee_card(self):
        html = """
          <div class="gw-person-card"><span class="gw-person-card__job">Заведующий</span>
            <span class="gw-person-card__name">Тупикова Анастасия Васильевна</span></div>
          <div class="gw-document-item-tooltip__content"><div>Тупикова А.В.</div><div>Заведующий</div></div>
          <div><div>Фамилия, имя и отчество подписавшего документ</div><div>Петрова Т.В.</div>
            <div>Должность лица, подписавшего документ</div><div>Заведующий</div></div>"""
        result = extract_staff(html, self.site, "management")
        self.assertEqual([(c.name, c.is_head) for c in result], [("Тупикова Анастасия Васильевна", True)])

    def test_governance_roles_keep_the_named_scope(self):
        html = """
          <section id="kollegialnye-organy"><div class="gw-filled-section_color_highlighted">
            <h6>Педагогический совет</h6><div><b>Петрова Анна Ивановна</b><i>Заведующий</i></div></div></section>
          <div class="subdivision-text"><div class="subdivision-name">Дошкольное отделение</div>
            <div><b>Директор</b><i>Акимова Оксана Николаева</i></div></div>
          <div class="subdivision-text"><div class="subdivision-name">МБОУ «СОШ п. Ягодное»</div>
            <div><b>Директор</b><i>Бачкова Евгения Владимировна</i></div></div>
          <table><tr><th>Название органа управления</th><th>Должность</th><th>ФИО</th></tr>
            <tr><td>Общее собрание работников</td><td>Заведующий</td><td>Семенова Кира Станиславовна</td></tr>
            <tr><td>Единоличный исполнительный орган</td><td>Заведующий</td><td>Тупикова Анастасия Васильевна</td></tr>
            <tr><td>Орган № 2</td><td>Заведующий</td><td>Котова Ирина Андреевна</td></tr></table>"""
        self.assertEqual([(c.name, c.role, c.is_head, c.method) for c in extract_staff(html, self.site, "management")], [
            ("Петрова Анна Ивановна", "Заведующий", False, "semantic_subunit"),
            ("Акимова Оксана Николаева", "Директор", False, "semantic_subunit"),
            ("Бачкова Евгения Владимировна", "Директор", True, "semantic"),
            ("Семенова Кира Станиславовна", "Заведующий", False, "semantic_subunit"),
            ("Тупикова Анастасия Васильевна", "Заведующий", True, "semantic"),
            ("Котова Ирина Андреевна", "Заведующий", False, "semantic_scope_review"),
        ])


if __name__ == "__main__":
    unittest.main()
