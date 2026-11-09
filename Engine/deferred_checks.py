"""Page-scoped questions that use documents, rendered pages and published text."""

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import unquote, urlsplit


TEXT = ("page_text", "page_pdf")
SCREEN = ("page_screen",)
KINDS = {"page_text": ("html", "dom"), "page_pdf": ("pdf",),
         "page_screen": ("viewport", "full"), "print_comparison": ("pdf", "full", "viewport")}
GROUPS = {"content":"Содержание разделов", "publication":"Новости и мероприятия",
          "layout":"Экран и сохранённый PDF", "attachments":"Оригиналы вложений","photos":"Фотографии учреждений"}


def review_group(check):
    key = check["id"]
    if check.get("visual") or key in ("garden_building_photo","staff_portrait","main_photo_original_size"):
        return "photos"
    if check.get("attachment"):
        return "attachments"
    if key.startswith(("news_date_card_","event_plan_","publication_")) or key == "event_date_in_text":
        return "publication"
    if key in ("layout_peer_difference","pdf_missing_screen_content","home_building_caption_conflict",
               "feedback_banner","feedback_banner_overlap","screen_layout"):
        return "layout"
    if check.get("evidence_policy"):
        return "content"
    return None


def deferred_for(page_kind, institution, captured_at, *, title=None, headings=(), url=""):
    questions = []
    labels = " ".join([title or "",*(heading.get("text","") for heading in headings)]).casefold()
    slug = unquote(urlsplit(url).path).casefold().rstrip("/").rsplit("/",1)[-1]
    about = (slug in {"o-shkole","about","ob-uchrezhdenii"} or (title or "").casefold().strip() in
             {"о школе","об учреждении","наша школа"})
    history = (page_kind=="history" or page_kind=="about" and
               (title is None or about or re.search(r"истори[яию]|историческ|летопис",labels)))
    structure = (page_kind=="management" or page_kind in ("about","information") and
                 (title is None or about or slug in {"struct","struktura"} or
                  re.search(r"орган[ыа] управлен|структур.{0,22}управлен|управлени[ея] школ",labels)))
    programs = (page_kind=="programs" or page_kind=="information" and
                (title is None or slug in {"education","obrazovanie"} or
                 re.search(r"образовательн\w* программ|реализуем\w* программ",labels)))

    def add(key, question, sources=TEXT, aggregation="exists"):
        kinds = list(dict.fromkeys(kind for source in sources for kind in KINDS[source]))
        questions.append({"id":key, "instruction":question, "material_kinds":kinds,
                          "evidence_policy":{"sources":list(sources),"aggregation":aggregation}})

    if page_kind == "home":
        if institution == "school":
            add("school_description_own", "Есть ли во вводном блоке связный текст о конкретной школе? "
                "Приведите факт о ней. Название в шапке, приветствие без сведений и текст о платформе недостаточны.")
        add("layout_peer_difference", "Видно ли различие оформления однотипных подписей в одном ряду карточек "
            "(должности сотрудников или заголовки событий)? Укажите обе подписи и различие. "
            "При отсутствии двух видимых однотипных элементов выберите uncertain.", SCREEN)
        add("home_building_caption_conflict", "Противоречит ли видимое содержимое главного изображения собственной "
            "подписи в том же блоке? Укажите подпись и что именно видно. Если подпись или изображение "
            "не видны вместе, выберите uncertain.", SCREEN)

    if history and institution == "school":
        add("school_history_facts", "Приведены ли факты истории этой школы — основание, преобразование, "
            "этап развития или датированное событие из её прошлого? Процитируйте факт. "
            "Пункт меню и пустой заголовок сами по себе недостаточны. Вывод относится только к этой странице.")
    if structure and institution == "school":
        add("management_structure_details", "Описаны ли в основном содержимом структура или органы управления именно "
            "этой школой? Приведите название органа, подразделения или ответственного и связанную с ним функцию "
            "или контакт. Не используйте соседние ссылки общего меню.")
    if page_kind in ("staff_profile","management","staff_directory"):
        add("staff_named_contact", "Указан ли телефон или электронная почта, явно отнесённые к конкретному "
            "названному сотруднику в том же блоке? Укажите имя и вид контакта. Общие контакты из шапки "
            "не подтверждают персональный контакт. Вывод относится к показанным карточкам.")
        add("staff_named_education", "Опубликован ли уровень основного профессионального образования конкретного "
            "названного сотрудника? Процитируйте имя и указание на уровень (например, высшее или среднее "
            "профессиональное образование) либо диплом с полученной специальностью. Должность, курсы "
            "повышения квалификации, часовые программы, сертификаты и место прохождения курсов этого не доказывают.")
    if page_kind == "contacts" or (institution == "kindergarten" and page_kind == "home"):
        for key, question in (
            ("contact_reception_hours", "Указаны ли дни или часы личного приёма руководителя либо администрации? "
             "Процитируйте, кто принимает и когда. Общий режим работы учреждения недостаточен."),
            ("contact_institution_hours", "Указаны ли рабочие дни или часы работы самого учреждения? Процитируйте "
             "период и его назначение. Отличайте режим работы от личного приёма и расписания занятий."),
        ):
            add(key,question)
    if programs and institution == "kindergarten":
        add("garden_program_details", "Есть ли содержательные сведения хотя бы об одной реализуемой программе "
            "этого сада? Укажите название и конкретную характеристику, например возраст, срок, направленность "
            "или форму обучения. Общий заголовок и ссылка на файл не раскрывают его содержание.")
    if page_kind == "staff_resources":
        add("staff_resources_substantive", "Есть ли материал для педагогов с конкретной темой и полезным содержимым "
            "или доступным именованным ресурсом? Укажите тему и что опубликовано. Пункт меню и пустая рубрика "
            "сами по себе недостаточны. Доступность содержимого вложения подтверждает проверка его оригинала.")
    if page_kind in ("news_detail","event_detail","events_listing","news_listing"):
        add("publication_filler", "Есть ли в заголовке, анонсе или основном тексте явная не заменённая подсказка "
            "автору, пример заполнения или нумерованный шаблон публикации? Процитируйте фрагмент. "
            "Краткость текста, старый год, опечатка и повтор изображения сами по себе этого не доказывают.")
        add("publication_stock_image", "Видно ли в области карточки новости или мероприятия изображение с явной "
            "надписью-заполнителем или типовым силуэтом? Назовите видимый признак и заголовок карточки. "
            "Повтор изображения или обычная иллюстрация сами по себе недостаточны. Если нельзя связать их, "
            "выберите uncertain.", SCREEN)
    if page_kind in ("news_detail","event_detail"):
        add("event_date_in_text", "Указывает ли основной текст этой публикации дату описанного события с однозначным "
            "годом? Процитируйте дату и действие, уточните план это или сообщение о состоявшемся событии. "
            "Дата размещения и соседние новости не подходят. При неясном годе или противоречии дат — uncertain.")
    if page_kind == "news_detail":
        add("publication_time_evidence", "Указаны ли прямо в этой публикации дата и время её размещения? "
            "Процитируйте отметку и связанные с ней слова. При наличии лишь календарной даты укажите, что часы "
            "не опубликованы. По одной дате нельзя установить точный интервал в 24 часа.")
    if page_kind in ("events_listing","event_detail") and institution == "school" and captured_at is not None:
        date = datetime.fromtimestamp(captured_at,timezone(timedelta(hours=11))).date()
        q = (date.month - 1)//3
        for shift, label in ((0,"current"),(1,"next")):
            number = (q+shift)%4+1
            year = date.year+(q+shift)//4
            add("event_plan_"+label, f"Есть ли именованное мероприятие, запланированное на {number}-й квартал "
                f"{year} года? Дата сохранения страницы {date:%d.%m.%Y} (Магадан). Процитируйте название, дату "
                "и связь с планом. Общая ссылка и ретроспектива сами по себе недостаточны. "
                "Если указан только день и месяц и год установить нельзя, выберите uncertain.")
    if page_kind == "schedule":
        add("schedule_body_lessons", "Видно ли на самой странице расписание учебных занятий с классом или группой "
            "и привязкой занятия к дню или времени? Назовите одну строку. Расписание звонков и список "
            "файлов сами по себе недостаточны.", ("page_text","page_pdf","page_screen"))
    add("pdf_missing_screen_content", "Видно ли конкретное содержимое экранной версии, пропавшее в сохранённом "
        "PDF этой же страницы? Для present укажите, что именно пропало и где оно на экране. Перенос на другой лист "
        "и повтор баннера сами по себе недостаточны. Если сравнение неполно, выберите uncertain.",
        ("print_comparison",))
    return questions
