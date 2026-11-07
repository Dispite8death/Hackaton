from datetime import date, datetime

from audit.checks import _similar
from audit.crossval import compute_score
from audit.dates import find_dates, infer_year, next_quarter_bounds, parse_datetime, quarter_bounds
from audit.extract import extract_article_text, extract_documents, extract_events, extract_news, extract_persons, find_placeholders
from audit.factcheck import check_anniversaries, check_official_dates, check_weekdays, extract_event_date
from audit.models import Finding, SiteResult

BASE = "https://sh-test-r44.gosweb.gosuslugi.ru"


# ------------------------------------------------------------------ даты
def test_parse_datetime_formats():
    assert parse_datetime("25 мая 2026, 16:56") == datetime(2026, 5, 25, 16, 56)
    assert parse_datetime("26 октября 2026 г.") == datetime(2026, 10, 26)
    assert parse_datetime("2026-05-25 16:56:19") == datetime(2026, 5, 25, 16, 56)
    assert parse_datetime("01.12.2022, 15:00") == datetime(2022, 12, 1, 15, 0)


def test_infer_year_by_weekday():
    # 28 мая, четверг -> 2026 год
    assert infer_year(28, 5, date(2026, 11, 7), weekday=3) == 2026
    # 27 февраля, четверг -> 2025 год
    assert infer_year(27, 2, date(2026, 11, 7), weekday=3) == 2025


def test_find_dates_range_and_relative():
    fd = find_dates("21-25 сентября прошли занятия, а вчера — линейка", date(2026, 9, 26))
    assert fd[0].value == date(2026, 9, 21) and fd[0].end_value == date(2026, 9, 25)
    assert any(f.kind == "relative" and f.value == date(2026, 9, 25) for f in fd)


def test_time_is_not_a_date():
    assert find_dates("начало в 10.05", date(2026, 5, 1)) == []


def test_quarters():
    assert quarter_bounds(date(2026, 11, 7)) == (date(2026, 10, 1), date(2026, 12, 31))
    assert next_quarter_bounds(date(2026, 11, 7)) == (date(2027, 1, 1), date(2027, 3, 31))


# ------------------------------------------------------------------ дата события и факт-чекинг
def test_event_date_from_text():
    d, _ = extract_event_date("📍 23 октября дети посетили музей. Приём заявок до 30 октября.", datetime(2026, 10, 26))
    assert d == date(2026, 10, 23)


def test_event_date_ignores_unrelated_dates():
    d, _ = extract_event_date("Указ от 16 июня 2026 г. Выборы назначены на сентябрь", datetime(2026, 7, 9))
    assert d is None


def test_event_date_announcement_skipped():
    d, _ = extract_event_date("1 ноября состоится концерт, приглашаем всех", datetime(2026, 10, 20))
    assert d is None


def test_official_dates():
    ok = check_official_dates("24 мая – День славянской письменности и культуры", date(2026, 5, 24))
    bad = check_official_dates("4 ноября - День учителя", date(2026, 11, 4))
    assert ok[0]["status"] == "confirmed"
    assert bad[0]["status"] == "contradiction"


def test_anniversaries():
    res = check_anniversaries("посвящён 85-летию начала Великой Отечественной войны и 120-летию со дня рождения "
                              "маршала Жукова", date(2026, 4, 24))
    assert [r["status"] for r in res] == ["confirmed", "contradiction"]


def test_weekday_check():
    assert check_weekdays("в пятницу, 22 мая прошёл концерт", date(2026, 5, 24))[0]["status"] == "confirmed"
    assert check_weekdays("в среду, 22 мая прошёл концерт", date(2026, 5, 24))[0]["status"] == "contradiction"


# ------------------------------------------------------------------ парсеры
NEWS_SCHOOL = f"""<html><body><main><div class="gw-news__list">
<div class="gw-news__item" itemscope itemtype="https://schema.org/NewsArticle">
 <meta itemprop="datePublished" content="2026-05-25 16:56:19">
 <a href="/roditelyam-i-uchenikam/novosti/novosti_145.html"><img alt="x"></a>
 <span class="gw-news-item__date">25 мая 2026, 16:56</span>
 <div class="gw-news-item__title" itemprop="headline"><a href="/roditelyam-i-uchenikam/novosti/novosti_145.html">День открытых дверей</a></div>
 <p itemprop="description">В школе прошёл день открытых дверей</p></div>
<div class="gw-news__item"><span class="gw-news-item__date">20 мая 2026, 10:00</span>
 <div class="gw-news-item__title"><a href="/roditelyam-i-uchenikam/novosti/novosti_144.html">Зарница</a></div></div>
</div></main></body></html>"""

NEWS_DS = """<html><body><main><div class="row">
<div class="col"><a class="card" href="/nash-detskiy-sad/novosti-i-sobytiya/n_599.html">
 <div class="card-subtitle">26 октября 2026 г.</div><div class="h5">Ходили в музей</div><div>23 октября дети посетили музей</div></a></div>
<div class="col"><a class="card" href="/nash-detskiy-sad/novosti-i-sobytiya/n_598.html">
 <div class="card-subtitle">25 октября 2026 г.</div><div class="h5">Аппликация</div></a></div>
</div></main></body></html>"""


def test_extract_news_school_and_kindergarten_templates():
    n = extract_news(NEWS_SCHOOL, BASE + "/roditelyam-i-uchenikam/novosti/")
    assert [x.title for x in n] == ["День открытых дверей", "Зарница"]
    assert n[0].published == datetime(2026, 5, 25, 16, 56) and n[1].published == datetime(2026, 5, 20, 10, 0)
    d = extract_news(NEWS_DS, BASE + "/nash-detskiy-sad/novosti-i-sobytiya/")
    assert [x.title for x in d] == ["Ходили в музей", "Аппликация"]
    assert d[0].published == datetime(2026, 10, 26)


def test_article_text_drops_publication_date():
    html = """<main><h1>Ходили в музей</h1><div class="card-subtitle">26 октября 2026 г.</div>
              <p>📍 23 октября дети посетили музей</p></main>"""
    title, text = extract_article_text(html)
    assert title == "Ходили в музей" and "26 октября" not in text and "23 октября" in text


def test_extract_events_with_weekday_year_inference():
    html = """<main><div class="object"><h2 class="name"><a href="/meropriyatiya/sobranie.html">Собрание</a></h2>
      <div class="object-date"><span class="day">28</span> <span class="month">мая</span> <span>четверг, 18:00</span></div></div>
      <div class="object"><h2 class="name"><a href="/meropriyatiya/bitva.html">Битва талантов</a></h2>
      <div class="object-date">24-28 ноября 2025</div></div></main>"""
    ev = extract_events(html, BASE + "/meropriyatiya/", date(2026, 11, 7))
    assert ev[0].start == date(2026, 5, 28)
    assert ev[1].start == date(2025, 11, 24) and ev[1].end == date(2025, 11, 28)


def test_extract_documents_skips_signature_noise():
    html = """<main><div class="gw-document-item"><a class="gw-document-item__title" href="/d/1.html">Устав образовательной организации</a>
      <a href="/netcat_files/1/USTAV.pdf">Скачать 4,4 МБ</a><span>Документ подписан электронной подписью</span></div>
      <div><span>Коллективный договор</span><a href="/netcat_files/2/kd.pdf">Скачать</a></div></main>"""
    titles = [d.title for d in extract_documents(html, BASE + "/svedeniya-ob-obrazovatelnoy-organizatsii/dokumenty/")]
    assert titles == ["Устав образовательной организации", "Коллективный договор"]


def test_extract_persons():
    html = """<main><div class="gw-person-info-card"><a href="/p/1.html"><img src="/netcat_files/a.jpg"></a>
      <a class="gw-person-info-card__name" href="/p/1.html">Иванова Мария Петровна</a>
      <div class="gw-person-info-card__position">учитель</div><a href="tel:+74132000000">+7 413 200-00-00</a></div>
      <div class="gw-person-info-card"><a class="gw-person-info-card__name" href="/p/2.html">Петров Иван Сергеевич</a></div></main>"""
    p = extract_persons(html, BASE + "/svedeniya-ob-obrazovatelnoy-organizatsii/pedagogicheskiy-sostav/")
    assert [x.name for x in p] == ["Иванова Мария Петровна", "Петров Иван Сергеевич"]
    assert p[0].photo and p[0].has_phone and not p[1].photo


def test_placeholders():
    labels = [h[0] for h in find_placeholders("Актуальный состав Укажите ФИО. Заголовок мероприятия №3. "
                                              "Опишите историю школы. Примерно от 1000 до 1200 символов.")]
    assert len(labels) == 4


# ------------------------------------------------------------------ прочее
def test_similarity_events_vs_news():
    assert _similar("Общешкольное родительское собрание", "Прошло общешкольное родительское собрание") >= 0.5
    assert _similar("Диктант Победы", "Осенняя ярмарка") == 0


def test_score_bounds():
    r = SiteResult(domain="x", name="x", kind="school")
    compute_score(r)
    assert r.score == 100
    r.findings = [Finding("Новости", "p", "critical", "r")] * 10
    compute_score(r)
    assert 70 < r.score < 80   # штраф ограничен 25 баллами на раздел


def test_education_field_variants():
    from audit.config import EDUCATION_RE
    assert EDUCATION_RE.search("Уровень образования: высшее")
    assert EDUCATION_RE.search("Сведения об уровне (уровнях) профессионального образования с указанием направления")
    assert not EDUCATION_RE.search("Сведения о повышении квалификации (за последние 3 года)")


def test_temporal_consistency_ignores_periods_and_nouns():
    from audit.factcheck import temporal_consistency
    t1 = "Дата проведения: с 26 августа по 02.09.2026 в рамках проведения акции"
    t2 = "в рамках проведения Всероссийского дня правовой помощи детям 20.11.2026"
    assert temporal_consistency(t1, datetime(2026, 8, 26)) == []
    assert temporal_consistency(t2, datetime(2026, 10, 27)) == []
    assert temporal_consistency("15.12.2026 в школе прошёл концерт", datetime(2026, 11, 1))
