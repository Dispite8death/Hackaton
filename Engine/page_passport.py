"""Index the observed text and applicable review checks for one archived page."""

import argparse
import json
import sqlite3
from contextlib import closing
from html.parser import HTMLParser
from urllib.parse import urlsplit

if __package__:
    from .attached_files import attachment_questions
    from .deferred_checks import deferred_for
    from .publication_review import publication_questions
    from .visual_criteria import visual_questions
else:
    from attached_files import attachment_questions
    from deferred_checks import deferred_for
    from publication_review import publication_questions
    from visual_criteria import visual_questions


_VOID = set("area base br col embed hr img input link meta param source track wbr".split())
_SKIP = {"nav", "footer", "aside", "form", "script", "style", "template", "noscript", "svg"}
_SCREEN = ("viewport", "full")
_TEXT = ("html", "dom")
_SUMMARY_LIMIT = 480


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.title = []
        self.headings = [[], []]
        self.fragments = [[], []]
        self.lengths = [0, 0]
        self.has_main = False
        self.flags = set()

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        _, hidden, main, title, heading = self.stack[-1] if self.stack else ("", False, False, False, None)
        classes = (attrs.get("class") or "").lower().split()
        role = (attrs.get("role") or "").lower()
        main = main or tag == "main" or role == "main"
        self.has_main |= main
        hidden = hidden or tag in _SKIP or (tag == "header" and not main) or role in {"navigation","menu","banner","contentinfo"} or (
            "hidden" in attrs or "inert" in attrs or attrs.get("aria-hidden") == "true" or
            any(c in {"breadcrumb","breadcrumbs","gw-breadcrumbs","d-none","sr-only","visually-hidden"}
                or c.startswith(("gw-footer","gw-complex-nav","gw-mobile-menu","gw-breadcrumb")) for c in classes))
        if not hidden:
            if tag in {"h1","h2","h3","h4","h5","h6"}:
                heading = {"level": int(tag[1]), "pieces": []}
                self.headings[int(main)].append(heading)
            schema_type = (attrs.get("itemtype") or "").rstrip("/").rsplit("/", 1)[-1]
            if schema_type in {"NewsArticle", "Event"}:
                self.flags.add(schema_type)
            if any(c in {"gw-person-detail","gw-employee-info"} for c in classes):
                self.flags.add("profile")
            if any(c in {"gw-person-card","gw-person-info-card","gw-personal-card","gw-teachers"} for c in classes):
                self.flags.add("directory")
            if "tpl-block-full" in classes and any(c in {"tpl-component-gw-news","tpl-component-gw-events"} for c in classes):
                self.flags.add("detail")
        if tag not in _VOID:
            self.stack.append((tag, hidden, main, title or tag == "title", heading))

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.stack:
            return
        tag, hidden, main, title, heading = self.stack[-1]
        if title:
            self.title.append(data)
        if hidden or title or tag == "head":
            return
        text = " ".join(data.split())
        if not text:
            return
        if heading is not None:
            heading["pieces"].append(text)
        scope = int(main)
        if self.lengths[scope] <= _SUMMARY_LIMIT:
            self.fragments[scope].append(text[:_SUMMARY_LIMIT+1])
            self.lengths[scope] += 1 + len(text)

    def observed(self):
        scope = int(self.has_main)
        headings = [{"level": h["level"], "text": " ".join(h["pieces"])} for h in self.headings[scope] if h["pieces"]]
        title = " ".join("".join(self.title).split()) or next((h["text"] for h in headings if h["level"] == 1), "")
        text = " ".join(self.fragments[scope])
        summary = text if len(text) <= _SUMMARY_LIMIT else text[:_SUMMARY_LIMIT-1].rstrip() + "…"
        return title, headings, summary


def _page_kind(section, depth, url, flags):
    if depth == 0 or section == "home":
        return "home"
    path = urlsplit(url).path.lower()
    article_url = path.rstrip("/").endswith((".html", ".htm", ".aspx"))
    if section in {"team", "staff", "management"} or "profile" in flags:
        if "profile" in flags or (article_url and "directory" not in flags and section in {"team","staff"}):
            return "staff_profile"
        if section in {"staff","team"} or "directory" in flags:
            return "staff_directory"
    if section == "events":
        return "event_detail" if article_url or "detail" in flags else "events_listing"
    if section == "news":
        return "news_detail" if article_url or "detail" in flags else "news_listing"
    return section or "unclassified"


def _checks(kind, institution):
    checks = []

    def check(key, instruction, materials):
        checks.append({"id": key, "instruction": instruction, "material_kinds": list(materials)})

    garden = institution == "kindergarten"
    if kind == "home":
        menu = {"news":"Новости", "documents":"Документы", "contacts":"Контакты"}
        if garden:
            menu.update(team="Наш коллектив", information="Сведения об образовательной организации",
                        programs="Образование", staff="Педагогический состав", staff_resources="Педагогам и сотрудникам")
        else:
            menu.update(events="Мероприятия", schedule="Расписание", history="История", about="О школе",
                        management="Руководство", staff="Педагогический состав")
        for key, label in menu.items():
            check("nav_" + key, f"Есть ли в меню ссылка с адресом раздела «{label}» или его смыслового аналога?", _TEXT)
        check("site_navigator_entry", "Есть ли на странице ссылка или элемент управления с названием «Навигатор по сайту» или «Карта сайта»?", _TEXT)
        check("feedback_banner", "Виден ли на первом экране баннер платформы обратной связи?", ("viewport",))
        check("feedback_banner_overlap", "Закрывает ли баннер обратной связи хотя бы один пункт меню на снимке экрана?", _SCREEN)
        if garden:
            check("garden_building_photo", "Видно ли на главной фотографии здание детского сада?", ("image", *_SCREEN))
            check("garden_team_cards", "Есть ли на главной хотя бы одна персональная карточка сотрудника детского сада?", _TEXT)
            check("garden_staff_list_link", "Есть ли в блоке сотрудников прямая ссылка на полный список коллектива?", _TEXT)
        else:
            check("school_main_photo", "Показывает ли главное изображение здание, логотип школы или групповой портрет коллектива?", ("image", *_SCREEN))
            check("main_photo_original_size", "Составляет ли размер оригинального файла главного изображения ровно 1200 × 900 пикселей? Без размеров исходного файла указать uncertain.", ("image",))
            check("school_description", "Есть ли на странице связное описание именно этой школы?", _TEXT)
            check("description_length", "Укладывается ли выделенный текст описания школы в 300–400 символов включительно?", _TEXT)
            check("canteen_image_markup", "Есть ли изображение в локальном блоке, посвящённом помещению столовой?", _TEXT)
            check("canteen_photo", "Видно ли на относящемся к столовой изображении помещение для питания школьников?", ("image", *_SCREEN))
        check("head_card", "Есть ли на странице персональная карточка с ФИО руководителя и его должностью в одном блоке?", _TEXT)
    if kind in {"staff_directory","staff_profile","management"}:
        check("staff_identity", "Указана ли должность в одной карточке с ФИО сотрудника?", _TEXT)
        check("staff_education", "Есть ли сведения об образовании в карточке идентифицируемого сотрудника?", _TEXT)
        check("staff_portrait", "Виден ли портрет человека в изображении карточки сотрудника?", ("image", *_SCREEN))
        if kind != "staff_profile":
            check("staff_directory_coverage", "Есть ли в списке реальный адрес перехода на следующую страницу сотрудников?", _TEXT)
            check("staff_profile_link", "Есть ли у карточки сотрудника ссылка на его индивидуальный профиль?", _TEXT)
    if kind == "contacts" or (garden and kind in {"home","information"}):
        target = "детского сада" if garden else "школы"
        for field, label in (("address","почтовый адрес"), ("phone","контактный телефон"), ("email","электронная почта")):
            check(("garden_" if garden else "school_") + field, f"Опубликован ли контакт учреждения «{label}» для {target}?", _TEXT)
        if garden:
            check("garden_hours", "Опубликован ли режим работы самого детского сада? Расписание занятий и часы приёма заведующего считать отдельными сведениями.", _TEXT)
    if kind in {"home","events_listing","event_detail"}:
        check("garden_event_cards" if garden else "events_evidence",
              "Содержит ли страница хотя бы одну именованную запись о конкретном мероприятии? Ссылка на общий каталог ответа не даёт.", _TEXT)
        if not garden:
            for period, label in (("current","текущий"), ("next","следующий")):
                check("events_quarter_" + period, f"Есть ли среди записей этой страницы именованное мероприятие с явно установленной датой за {label} квартал относительно даты снимка? При неясном годе указать uncertain.", _TEXT)
        if kind == "event_detail":
            check("event_template_title", "Содержит ли заголовок события явный шаблонный текст вроде «Заголовок мероприятия №1»?", _TEXT)
    if kind in {"home","news_listing","news_detail"}:
        if kind == "news_detail":
            check("news_evidence", "Опубликован ли здесь собственный текст конкретной новости?", _TEXT)
            check("news_date", "Указана ли в полной публикации дата её размещения?", _TEXT)
        else:
            check("news_evidence", "Есть ли в новостном блоке превью с заголовком и ссылкой на отдельную публикацию?", _TEXT)
            check("news_date", "У каждого новостного превью видна ровно одна полная дата? Если превью нет или они обрезаны, укажите uncertain.", _TEXT)
            check("news_order", "Расположены ли новости от новых к старым? Сравните даты превью в порядке чтения. "
                  "При равных датах порядок допустим. Если для сравнения не хватает дат, укажите uncertain.", _TEXT)
    section_checks = {
        "documents":"Есть ли ссылка на конкретный документ с названием?",
        "schedule":"Есть ли на странице ссылка на файл расписания, подписанный скан или таблица занятий?",
        "programs":"Указана ли хотя бы одна образовательная программа?",
        "history":"Содержит ли основной текст сведения об истории самого учреждения?",
        "about":"Содержит ли основной текст собственное описание учреждения?",
        "information":"Есть ли в основном содержимом сведения о конкретной образовательной организации?",
        "staff_resources":"Опубликован ли материал, адресованный педагогам или сотрудникам?",
    }
    if kind in section_checks:
        check("section_content", section_checks[kind], (*_TEXT,"pdf"))
    if kind == "schedule":
        check("schedule_visible_lessons", "Можно ли прочитать расписание на самом снимке: класс или группу, "
              "время и занятия? Если показано только название вложения или изображение нечёткое, ответьте uncertain. "
              "Содержимое файла проверяется по его оригиналу.", ("full","viewport","pdf"))
    if kind == "site_navigator":
        check("map_news_link", "Есть ли в списке самой карты сайта ссылка на ленту новостей или общий раздел новостей и событий?", _TEXT)
        check("map_events_link", "Есть ли в самой карте ссылка, явно обозначенная как события или мероприятия, включая совмещённый раздел?", _TEXT)
    if not checks:
        check("page_content", "Есть ли на странице самостоятельный содержательный материал за пределами навигации?", _TEXT)
    check("screen_layout", "Видно ли на экранном снимке наложение другого элемента, закрывающее текст страницы или меню? При обрезке нужной области указать uncertain.", _SCREEN)
    return checks


_UPSERT = """
INSERT INTO page_passport(page_id,page_kind,title,headings,summary,checks) VALUES(?,?,?,?,?,?)
ON CONFLICT(page_id) DO UPDATE SET page_kind=excluded.page_kind,title=excluded.title,headings=excluded.headings,
                                  summary=excluded.summary,checks=excluded.checks
"""


def questions_for(conn, page_id, *, include_photos=True):
    row = conn.execute("""SELECT b.checks,b.page_kind,o.kind,p.date_loaded,b.title,b.headings,p.url FROM page_passport b
        JOIN page p ON p.id=b.page_id JOIN organization o ON o.id=p.id_organization WHERE b.page_id=?""",
        (page_id,)).fetchone()
    if row is None:
        raise ValueError(f"Паспорт снимка {page_id} не найден")
    checks = json.loads(row[0])
    photographic = visual_questions(conn,page_id) if include_photos else []
    replaced = {c["id"] for c in photographic}
    checks = [c for c in checks if c["id"] not in replaced]
    for check in checks:
        if check["id"]=="staff_portrait":
            check.update(instruction="Видна ли хотя бы одна фотография человека в карточках коллектива на снимке страницы?",
                         material_kinds=["full","viewport"])
    checks.extend(photographic)
    checks.extend(deferred_for(row[1],row[2],row[3],title=row[4],headings=json.loads(row[5]),url=row[6]))
    checks.extend(attachment_questions(conn,page_id))
    checks.extend(publication_questions(conn,page_id))
    for ordinal,title in conn.execute("""
        SELECT ordinal,title FROM news_preview WHERE page_id=? AND date_status!='valid' ORDER BY ordinal
    """,(page_id,)):
        checks.append({
            "id":f"news_date_card_{ordinal+1}",
            "instruction":f"В превью № {ordinal+1} «{title[:150]}» встречается только одна дата и она полная? "
                          "Вторая дата, даже без года или в названии акции, означает absent. "
                          "Дословно процитируйте каждую дату из превью, включая его анонс. Проверяется только эта карточка.",
            "material_kinds":["html","dom","full","pdf"],
        })
    return checks


def _values(conn, page_id):
    row = conn.execute("""
        SELECT p.url,p.text_page,o.kind,f.depth,f.section FROM page p
        JOIN organization o ON o.id=p.id_organization LEFT JOIN crawl_fetch f ON f.page_id=p.id WHERE p.id=?
    """, (page_id,)).fetchone()
    if row is None:
        return None
    url, html, institution, depth, section = row
    parser = _PageText()
    parser.feed(html)
    parser.close()
    title, headings, summary = parser.observed()
    kind = _page_kind(section, depth, url, parser.flags)
    if section == "hub" and (
            title.strip().casefold() in {"карта сайта", "навигатор по сайту"}
            or any(h["level"] == 1 and h["text"].casefold() in {"карта сайта", "навигатор по сайту"} for h in headings)):
        kind = "site_navigator"
    return (page_id,kind,title,json.dumps(headings, ensure_ascii=False),summary,
            json.dumps(_checks(kind, institution), ensure_ascii=False))


def index_page(conn: sqlite3.Connection, page_id: int) -> bool:
    """Index the saved HTTP response, sharing the caller's transaction."""
    values = _values(conn, page_id)
    if values is None:
        return False
    conn.execute(_UPSERT, values)
    return True


def index_all(conn: sqlite3.Connection) -> int:
    page_ids = [row[0] for row in conn.execute("SELECT id FROM page ORDER BY id")]
    count = 0
    for i in range(0, len(page_ids), 25):
        rows = [_values(conn, page_id) for page_id in page_ids[i:i+25]]
        rows = [row for row in rows if row is not None]
        with conn:
            conn.executemany(_UPSERT, rows)
        count += len(rows)
    return count


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser = argparse.ArgumentParser(description="Подготовить паспорта сохранённых страниц без повторной загрузки сайтов")
    parser.add_argument("--index-all", required=True, action="store_true", help="обработать все доступные снимки")
    parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        print(f"Составлено паспортов страниц: {index_all(conn)}")


if __name__ == "__main__":
    main()
