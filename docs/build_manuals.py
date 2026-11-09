"""Run from the project root: python -m docs.build_manuals."""
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, KeepTogether
from Engine import crawl_store, operation_log
from Engine.dashboard_pdf import _fonts, _REGULAR, _BOLD
from ai_audit_web import db

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/pdf"
TEAL = colors.HexColor("#13665d")
INK = colors.HexColor("#18353e")
GRAY = colors.HexColor("#51636c")


def p(text, size=10.5, bold=False, color=INK, after=7):
    style = ParagraphStyle("text",fontName=_BOLD if bold else _REGULAR,fontSize=size,leading=size*1.42,
                           textColor=color,spaceAfter=after)
    return Paragraph(escape(text),style)


def heading(title):
    return p(title,22,True,TEAL,14)


def item(title, body):
    return KeepTogether([p(title,12,True,INK,3),p(body)])


def section(story, title, pairs):
    if story:
        story.append(PageBreak())
    story.append(heading(title))
    story.extend(item(*pair) for pair in pairs)


def write(title, story):
    OUT.mkdir(parents=True,exist_ok=True)
    doc=SimpleDocTemplate(str(OUT/(title+".pdf")),pagesize=A4,leftMargin=47,rightMargin=47,
                          topMargin=56,bottomMargin=48,title="Контур | "+title,author="Одинокие одиночки")
    def frame(canvas, doc):
        w,h=A4
        canvas.setFillColor(TEAL);canvas.rect(0,h-10,w,10,fill=1,stroke=0)
        canvas.setFont(_BOLD,9);canvas.drawString(47,h-33,"КОНТУР   /   "+title.upper())
        canvas.setStrokeColor(colors.HexColor("#bccfc9"));canvas.line(47,38,w-47,38)
        canvas.setFillColor(GRAY);canvas.setFont(_REGULAR,8)
        canvas.drawString(47,24,"Одинокие одиночки   •   Аудит сайтов образовательных учреждений")
        canvas.drawRightString(w-47,24,str(doc.page))
    doc.build(story,onFirstPage=frame,onLaterPages=frame)


def user():
    s=[]
    section(s,"С чего начать",[
        ("Если вы получили архив жюри","Распакуйте ZIP целиком. Откройте «Открыть Контур.html» двойным щелчком. В браузере переключите вид аудита, выберите школу или сад и откройте строку учреждения. Мини-версия работает без установки и подключения к сети. Папки «Отчёты», «Документация» и «Материалы защиты» доступны рядом с HTML."),
        ("Если нужен новый анализ","Для полной версии на Windows запустите «Запустить аудит.bat». Для Linux из папки проекта выполните sh \"Запустить аудит.sh\". При первой установке библиотек нужен интернет. Для снятия экранных снимков Linux поддерживает sh start.sh --install-browser. На Windows подходят Chromium, Chrome и Edge."),
        ("Какой инструмент выбрать","В «Сводном аудите» доступны доказательства по критериям, охват, рейтинг и PDF. «Алгоритмический аудит» быстро проверяет сайт по правилам Ильи, сохраняет замечания и позволяет получить отдельное пояснение ИИ."),
        ("Три шага для первого просмотра","На главной выберите «Сводный аудит». Откройте «Учреждения», введите часть названия или домена и нажмите на учреждение. Откройте замечание, прочитайте основание и перейдите к источнику. Нажатие «Контур» возвращает к выбору режимов."),
        ("Где работают настройки","Полная административная панель открывается на компьютере с приложением. В публичном показе отдельные настройки демонстрационной модели позволяют попробовать ответ и восстановить начальный профиль. Комплект HTML из ZIP хранит только готовые результаты.")])
    section(s,"Сводный аудит и PDF",[
        ("Обзор","Карточки и диаграммы показывают выбранную группу. Нажмите на интересующий сегмент для перехода к результатам. Переключатель подробности меняет представление отчёта."),
        ("Учреждения","Выберите школу, садик или весь реестр. Ищите по названию или адресу, сортируйте по баллу, охвату, замечаниям или дате. Отметьте несколько учреждений для общего PDF."),
        ("Все результаты и карточка","В дополнительных фильтрах можно объединить учреждения, тип замечания, категорию, группу, статус, критичность, уверенность, уровень оценки и исполнителя (ИИ, алгоритм, ещё не проверено). Укажите даты при необходимости. В карточке доступны критерии, основание вывода, рекомендации, история обходов и переход в другой инструмент, если учреждение сопоставлено."),
        ("Как читать балл","Охват показывает долю применимых критериев, по которым получен определённый результат. При охвате ниже 50% или менее трёх оценённых критериев класс качества отсутствует. «Недостаточно данных» означает, что ещё нужны материалы. Фильтр по замечаниям меняет строки отчёта, общий балл за выбранный период сохраняется."),
        ("Скачать PDF","В меню выберите текущие фильтры, отмеченные учреждения или весь реестр. Для отдельной карточки доступен PDF учреждения. В дополнительных фильтрах отметьте «Включить в PDF таблицу…», если нужен список учреждений с оценками «Хорошо» и «Отлично». По умолчанию эта таблица скрыта. Вариант «Все учреждения без фильтров» экспортирует весь реестр.")])
    section(s,"Быстрая проверка и отчёты",[
        ("Обзор и запуск","В разделе «Запуск» найдите учреждения по названию или адресу. «Проверить сайты» получает свежие страницы. «Проверить сохранённые страницы» повторяет анализ имеющейся копии. По пустому кэшу выводится пояснение. Алгоритмы работают при выключенном ИИ."),
        ("Учреждения","В разделе «Учреждения» выберите сайт. В карточке доступны запуск проверки, обновление отображения, замечания с рекомендациями, история, контактный e-mail и действия с отчётом. Почтовая ссылка открывает почтовый клиент пользователя."),
        ("Отчёты","Фильтруйте по учреждению, типу, разделу, коду замечания, серьёзности, периоду и результату. Сводка показывает размер выборки, таблица ниже перечисляет проблемы. В настройке состава HTML и CSV выберите нужные блоки, при необходимости сохраните состав по умолчанию и скачайте файл."),
        ("Разбор с ИИ","Выберите готовый отчёт. «Краткая сводка с ИИ» и «План исправления с ИИ» передают модели наблюдения алгоритма. Ответ сохраняется в исходном отчёте. При недоступном профиле откройте общие настройки подключения."),
        ("Правила и параметры обхода","В «Правилах» редактируются значения правил содержания и цели поиска фотографий. В «Параметрах обхода» задайте дополнительные домены, лимит страниц, допустимый возраст новостей, автоопределение типа учреждения и эталонные размеры фото. После сохранения повторите проверку кэша для применения новых правил.")])
    section(s,"Как обновить или проверить выборку",[
        ("Одна организация или несколько","Откройте «Настройки → Проверка выбранных учреждений». Выберите режим, введите часть названия или адреса и отметьте строки. Счётчик сохраняет выбор при поиске другого учреждения."),
        ("Выбор операции","Алгоритмы анализируют сохранённые страницы. Режим ИИ проверяет только вопросы, для которых подготовлены материалы. Загрузка получает страницы и вложения. Два совмещённых режима сначала загружают, затем проверяют алгоритмами, при выборе ИИ отправляют готовые вопросы модели."),
        ("Оценка и запуск","Нажмите «Проверить материалы и оценить время». Посмотрите число страниц, недостающие разделы и объём готовых вопросов, затем запустите. Оценка ИИ строится по пробе текущего профиля. Большой PDF или фото могут потребовать больше времени. Во время работы показываются этап и прошедшее время."),
        ("Что делать при остановке","Вкладку можно закрыть. Ссылка на состояние задания доступна на главной. Если закрыли консоль, заново запустите приложение и продолжите последнее задание в соответствующем инструменте. Завершённые этапы сохраняются. Незаконченный запрос ИИ может быть отправлен снова. Новый ручной запуск заменяет предыдущее задание для продолжения."),
        ("Когда сайт доступен не полностью","Раскройте список материалов, оставшихся без проверки. Убедитесь, что источник открывается и план предусматривает нужную глубину. Запустите совмещённый режим. Последние доступные копии используются при неудачной загрузке.")])
    section(s,"Как настроить систему",[
        ("Реестр учреждений","Общий список для обоих инструментов находится в настройках. Добавьте название, тип и URL. Импорт UTF-8 принимает JSON с полями name, kind, site_url, CSV с одноимёнными заголовками и TXT с одной строкой «название & тип & ссылка». Типы school и kindergarten, также школа и детский сад. Удаление из реестра сохраняет историю."),
        ("Фоновое обновление","Интервал запуска задаёт частоту партий, интервал повторного обхода определяет, когда сайт снова доступен очереди. Задайте число сайтов за партию, лимит страниц на сайт, количество переходов от главной, дополнительные страницы, вложения, вопросы модели, сверяемые публикации и период новостей. Сохраните. Windows позволяет отдельно включить или отключить расписание. В Linux используйте sh start.sh --once либо --loop."),
        ("Подключение модели","Выберите LM Studio, Ollama или совместимый API. Укажите базовый адрес /v1, точный идентификатор модели, формат ответа, тайм-аут и при необходимости ключ. Включите ИИ и сохраните профиль. Проверка ответа и замер скорости запускаются отдельно. Ошибка соединения сохраняет введённый профиль. При отказе проверьте порт, загруженную модель, права ключа и формат JSON."),
        ("Хранение и независимые источники","Сначала используйте предпросмотр очистки. Задайте возраст заменённых файлов и разрешите очистку во время фонового цикла. Для фактчекинга включите проверку публикаций и отредактируйте перечень базовых URL независимых издателей."),
        ("Словарь","Откройте словарь в настройках, выберите категорию фото, добавьте фразу или удалите лишнюю. Длинные фразы переносятся. Повторите алгоритмический анализ по сохранённым страницам для применения изменений.")])
    write("Руководство пользователя",s)


def relations(rows):
    height = len(rows)*42+8
    d = Drawing(490,height)
    for i,(source,link,target) in enumerate(rows):
        y=height-33-i*42
        for x,label in [(0,source),(305,target)]:
            d.add(Rect(x,y,175,26,rx=4,ry=4,fillColor=colors.HexColor("#e6f2ef"),strokeColor=TEAL))
            d.add(String(x+8,y+9,label,fontName=_BOLD,fontSize=9,fillColor=INK))
        d.add(Line(177,y+11,301,y+11,strokeColor=TEAL))
        d.add(Polygon([302,y+11,296,y+14,296,y+8],fillColor=TEAL,strokeColor=TEAL))
        d.add(String(181,y+16,link,fontName=_REGULAR,fontSize=8,fillColor=GRAY))
    return d


def describe_schema(s, conn, name):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    edges = [(table,fk[3],fk[2]) for table in tables for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")')]
    for start in range(0,len(edges),14):
        s.extend([PageBreak(),heading(name+" • карта связей"),
                  p(f"Объявленные внешние ключи, связи {start+1}–{min(start+14,len(edges))} из {len(edges)}. Слева дочерняя таблица, над стрелкой поле ссылки, справа таблица-владелец.",9),
                  relations(edges[start:start+14])])
    section(s,name+" • физическая схема",[
        ("Как читать справочник",f"Таблиц: {len(tables)}. PK обозначает позицию столбца в первичном ключе. ! обозначает обязательное значение NOT NULL. Отдельная строка перечисляет внешние ключи и их целевые поля. Источник описания — PRAGMA свежей базы, создаваемой кодом проекта.")])
    for name in tables:
        cols = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
        fks = conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        body = "  ·  ".join(f"{r[1]} {r[2]}"+(f" PK{r[5]}" if r[5] else "")+(" !" if r[3] else "") for r in cols)
        parts=[p(name,11,True,TEAL,2),p(body,8.5,after=4)]
        if fks:
            parts.append(p("FK   "+"  ·  ".join(f"{r[3]} → {r[2]}.{r[4] or 'PK'}" for r in fks),8.5,color=GRAY,after=5))
        s.append(KeepTogether(parts+[Spacer(1,4)]))
    views = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='view' ORDER BY name")]
    if views: s.append(item("Представления",", ".join(views)))


def developer():
    s=[]
    section(s,"Архитектура и запуск",[
        ("Две подсистемы в одном процессе","Engine.portal.create_app создаёт FastAPI, применяет LocalAccess, регистрирует страницы сводного аудита и подключает NiceGUI через ui.run_with. Engine.portal.launch запускает Uvicorn на loopback. Windows начинает с start.cmd и Engine/start-portal.ps1, Linux с start.sh."),
        ("Границы хранения","Engine/config.py выбирает search.sqlite3 из HACKATON_DB_PATH. Engine/crawl_store.py: initialize_db создаёт схему и выполняет совместимые миграции, Engine/importer.py: seed_if_empty импортирует учреждения из задания. ai_audit_web/paths.py определяет HACKATON_QUICK_DIR, db.init создаёт audit.db. Реестры сопоставляются по URL через integration и roster, числовые ID двух баз независимы."),
        ("Архив и повторяемость","Снимки page включают дату и HTML. page_render хранит данные браузерного захвата. Индексы, вложения и оценки ссылаются на конкретный снимок. Часть исходных файлов хранится в соседней .audit-assets. Для резервной копии используйте SQLite backup и копию каталогов материалов при остановленных сборщиках."),
        ("Запуск в разработке","Установите requirements.txt в виртуальное окружение, выполните python -m Engine.site_server --port 8765. Для изолированного теста задайте отдельные HACKATON_DB_PATH и HACKATON_QUICK_DIR. python -m unittest discover -s Engine запускает тесты. HACKATON_TEST_BROWSER=1 включает браузерный сценарий, HACKATON_TEST_LOCAL_QWEN=1 требует живую модель на loopback.")])
    section(s,"Сбор и доказательства",[
        ("Маршрут страницы","Engine/loadPage.py: crawl_organization отвечает за начальный обход организации. navigation.py обнаруживает ссылки и категории, site_navigator.py ищет навигатор, news_window.py ограничивает период новостей. browser_capture.py: BrowserCapture сохраняет HTML после исполнения сценариев, DOM, экранные снимки и PDF. crawl_store.record_fetch записывает попытку HTTP, успешный page и обнаруженные ссылки."),
        ("Дополнительные документы","backfill_materials.seed и process_org ведут очередь material_backfill. attached_files.download_pending получает вложения, проверяет повторное использование и учитывает временную блокировку. document_links.py, document_inventory.py, schedule_evidence.py описывают каталог и доступные материалы расписания."),
        ("Правила и сотрудники","home_content.py, home_events.py, home_styles.py и garden_home.py разбирают главную. home_terms.py управляет словарём. staff_extract.py и staff_store.py извлекают и записывают карточки, staff_render.py оценивает видимость, staff_duplicates.py и staff_cross_org.py связывают похожие наблюдения. Совпадение имени между учреждениями остаётся поводом для сверки."),
        ("Даты и источники","news_cards.py и news_order.py разбирают превью и порядок. event_quarters.py и timeline_audit.py оценивают кварталы и даты, factcheck_sources.py ограничивает список независимых источников, factcheck_batch.py ведёт очередь сверки."),
        ("Автоматизация","audit_worker.run_once выполняет ограниченную партию, записывает audit_worker_cycle и освобождает межпроцессную блокировку. worker_schedule.py управляет заданием Windows. retention.py и retention_records.py применяют срок хранения заменённых данных. Ручные выборки проходят через portal_jobs.preview/start и operation_log.")])
    section(s,"Модель, отчёты и интерфейс",[
        ("Модель сводного аудита","model_settings.py хранит профили и флаг разрешения, ключ передаётся в системное хранилище keyring. model_gateway.py формирует совместимый запрос и проверяет ответ. deferred_checks.py задаёт вопросы, page_evidence.py готовит материал, review_contract.py задаёт контракт. model_batch.batch_assess планирует и запускает вопросы. Уже сохранённый результат привязан к снимку, вопросу, профилю и отпечатку входа."),
        ("Экспресс-проверка","ai_audit_web/core.py: audit_site выбирает сеть или кэш и выполняет правила; vision.py отвечает за фото. operations.start создаёт задание, сохраняет этапы в своей базе и записывает результат учреждения. reports.latest и select выбирают последние результаты и фильтруют замечания. llm.summarize и explain отправляют замечания на разбор через общий профиль."),
        ("Сводный интерфейс","web/home.html отвечает за выбор инструмента и статус, web/index.html и web/app.js — за обзор, учреждения и результаты. URL хранит фильтры, hash выбирает вкладку или карточку. dashboard_data.dashboard и institution формируют JSON из SQLite, dashboard_pdf.build_pdf получает ту же выборку. include_quality управляет только дополнительной таблицей PDF."),
        ("Администрирование и NiceGUI","web/admin.html и admin.js получают состояние и токен, отправляют команды, опрашивают прогресс. site_server.update_settings проверяет действие и вызывает модуль сервера. ai_audit_web/app.py регистрирует /quick, /quick/run, /sites, /site/{id}, /report, /quick/ai, /rules, /settings. События кнопок NiceGUI выполняются на сервере."),
        ("Переносная версия","Engine.jury_export.write_snapshot переносит разрешённые поля уже вычисленных результатов в web/jury.html. JSON вшит в HTML, текст выводится через DOM textContent. Страница может открываться с диска без API и Python. Локальные токены, файлы SQLite и архивные документы для этого не нужны.")])
    section(s,"Технологии и назначение",[
        ("Python и SQLite","Python 3.11+ исполняет сервер, обработчики, задания и CLI. sqlite3 хранит историю, связи и транзакции. OS file locks и SQL-блокировки защищают записи между процессами. asyncio и потоки разделяют обслуживание интерфейса и долгие операции."),
        ("FastAPI, Uvicorn, NiceGUI","FastAPI обслуживает JSON и PDF. Uvicorn исполняет ASGI. NiceGUI реализует быстрый инструмент на серверных компонентах и WebSocket. Страницы сводного аудита используют HTML, CSS, JavaScript и Fetch API."),
        ("Получение материалов","urllib и HTTP-клиенты получают сайты и API, Beautiful Soup и lxml разбирают HTML/XML. Playwright запускает Chromium для вычисленного CSS и печати. tldextract используется в проверках доменов. defusedxml обрабатывает XML ограниченным парсером."),
        ("Файлы и изображения","pypdfium2 читает и растеризует PDF. openpyxl и xlrd читают электронные таблицы, legacy-doc помогает с устаревшим форматом Word. Pillow, NumPy и OpenCV используются для декодирования, размеров, сопоставлений и анализа изображений. ReportLab собирает PDF и встраивает кириллические шрифты."),
        ("Локальная и внешняя генерация","LM Studio и Ollama предоставляют локальный API. Совместимый внешний сервис настраивается через base URL и свой ключ. Для /chat/completions доступны JSON по схеме, JSON-объект и текстовая инструкция. Результат зависит от модели и доступности материала. Списки пакетов приведены в requirements.txt и requirements-render.txt.")])
    section(s,"HTTP API • чтение",[
        ("GET /healthz и /api/access","Ответ содержит ok, service, admin, ai_status. Эти маршруты подтверждают доступность портала и установленный статус профиля. Для реального запроса к модели используйте POST verify."),
        ("GET /api/dashboard","Возвращает as_of, filters, options, grading, summary, institutions, findings. Например /api/dashboard?kind=school&status=issue&detector=ai. Повторяющиеся параметры и списки через запятую поддерживаются для многозначных фильтров. q, date_from и date_to принимают одно значение; формат даты YYYY-MM-DD."),
        ("GET /api/institution/{id}","Возвращает подробности по архивному ID, категории и историю. Несуществующее активное учреждение получает HTTP 404. quick_id при наличии указывает на карточку экспресс-проверки."),
        ("GET /report.pdf","Возвращает application/pdf и Content-Disposition: attachment. Пример /report.pdf?organization_id=1,2&status=issue&include_quality=1. По умолчанию дополнительная таблица отключена. Ограничение параллельной генерации может вернуть HTTP 503."),
        ("GET /api/admin/state, /api/admin/progress, /api/operations","state возвращает реестр, профили без ключей, настройки и токен команд. progress включает job, cycle и manual_cycle. operations отдаёт сокращённое состояние каждого режима для главной страницы. Отображаемые состояния ручного задания: idle, running, completed, error, interrupted.")])
    section(s,"HTTP API • команды",[
        ("Правила запроса","POST /api/admin/{action} принимает объект JSON, заголовки Content-Type: application/json, Origin с адресом портала и X-Audit-Token из state. Ошибки параметров дают 400 с полем error, проверка происхождения — 403. В обычном режиме LocalAccess разрешает локальные Host и Origin для HTTP и WebSocket."),
        ("plan → analyze → resume",'plan: {"ids":[1,2],"mode":"download_algorithm"}. Ответ содержит ticket, pages, empty, gaps и показатели ИИ. analyze получает те же ids, mode и ticket, возвращает id задания. Допустимы algorithm, ai, download, download_algorithm, download_ai. resume получает {"id":1}. Клиент опрашивает progress до завершения.'),
        ("profile, disable-ai, verify, benchmark","profile получает profile (provider, endpoint, model, timeout, response_mode, disable_thinking), enabled и необязательный secret. disable-ai и verify получают {}. benchmark с decline:true фиксирует отказ, иначе делает замер. Валидность формата проверяется по реальному ответу."),
        ("settings, cycle, schedule, retention","settings принимает объект settings с числовыми лимитами. cycle с {} запускает одну партию без ИИ. schedule принимает operation: status, enable или disable. retention принимает preview:true для оценки либо enabled и days для сохранения политики."),
        ("roster, words, sources, factcheck","roster поддерживает add c record, import с format и content, remove с id, clear с confirmed:true. words принимает operation (add/remove), category и word. sources получает operation и url. factcheck принимает enabled. POST /api/run и /api/run_cache запускают быстрые сетевые или кэшированные проверки и возвращают статус запуска.")])
    section(s,"Показ и восстановление",[
        ("Изоляция","Engine/jury_portal.py запускает копию портала только на 127.0.0.1:8766. KONTUR_DEMO_DIR и HACKATON_DB_PATH должны указывать на изолированный каталог и его search.sqlite3, HACKATON_QUICK_DIR — на data/quick внутри копии. Исходный локальный портал сохраняет проверки Host и Origin."),
        ("Вход и транспорт","JuryEntry проверяет Host, Origin, приглашение и сессионную cookie. POST /demo-login получает invite. Приватная ссылка из архива передаёт код во фрагменте URL. Сквозь SSH публикуется только демонстрационный веб-портал; модель отвечает локальному серверу. При завершении процесса сессии просмотра истекают."),
        ("Возврат настроек","demo_settings.baseline сохраняет настройки копии перед первым показом. POST /api/admin/restore-demo восстанавливает профиль ИИ, настройки цикла, словарь, разрешённые источники, хранение и настройки правил экспресс-аудита. Быстрые и сводные отчёты сохраняются. Операция требует токен и свободные блокировки заданий."),
        ("Сопровождение","Секрет владельца, приглашения и локальные резервные копии хранятся вне репозитория. Для повторного выпуска документации выполните python -m docs.build_manuals. Физическая схема извлекается из новой тестовой базы без чтения персональных настроек.")])
    section(s,"Устройство баз данных",[
        ("Как читать стрелки","Ребро ведёт от дочерней записи к владельцу или объекту ссылки. Верхняя схема показывает основные связи архива. В нижней схеме логические ссылки экспресс-базы реализованы запросами приложения. Связь реестров определяется адресом сайта.")])
    s.append(relations([("page","id_organization","organization"),("crawl_run","id_organization","organization"),
        ("crawl_fetch","run_id","crawl_run"),("crawl_fetch","page_id","page"),("crawl_link","fetch_id","crawl_fetch"),
        ("page_render","page_id","page"),("model_assessment","page_id","page"),("page_attachment","page_id","page")]))
    s.append(p("Экспресс-аудит",13,True))
    s.append(relations([("run_sites","run_id","runs"),("run_sites","site_id","sites"),("findings","run_id + site_id","результат учреждения")]))
    s.append(p("В таблицах portal_operation обеих баз независимо хранятся JSON-записи очереди. Время и текущий этап обновляются в контрольных точках. Полный перечень таблиц, полей и физических FK приведён далее.",9))
    with closing(sqlite3.connect(":memory:")) as a:
        a.row_factory=sqlite3.Row
        crawl_store.initialize_db(a);operation_log.initialize(a)
        describe_schema(s,a,"Сводный архив")
    with TemporaryDirectory() as tmp:
        old=db.DB_PATH
        try:
            db.DB_PATH=Path(tmp)/"quick.db";db.init()
            with closing(db.conn()) as q: describe_schema(s,q,"Экспресс-аудит")
        finally: db.DB_PATH=old
    write("Руководство разработчика",s)


if __name__=="__main__":
    _fonts()
    user()
    developer()
