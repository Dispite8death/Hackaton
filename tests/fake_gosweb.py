"""Офлайн-сайт школы в разметке шаблона Госвеб и подменная ИИ-модель — для проверок интерфейса без сети
и без расхода агрегатора. Даты строятся от текущего дня, чтобы правила audit/ давали предсказуемый результат."""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta

from audit.fetcher import Page
from sever.runner import Cancelled, CancellableFetcher

DOMAIN = "sh-demo-r44.gosweb.gosuslugi.ru"
BASE = f"https://{DOMAIN}"
NEWS = "/roditelyam-i-uchenikam/novosti/"
EVENTS = "/roditelyam-i-uchenikam/meropriyatiya/"
SVED = "/svedeniya-ob-obrazovatelnoy-organizatsii/"


def _page(title: str, body: str, menu: str = "") -> str:
    return f"<html><body><header>{menu}</header><main><h1>{title}</h1>{body}</main><footer>© 2026</footer></body></html>"


def build_site(today: date) -> dict[str, str]:
    d = lambda n: today - timedelta(days=n)  # noqa: E731
    menu = "".join(f'<a href="{u}">{t}</a>' for u, t in [
        (NEWS, "Новости"), (EVENTS, "Мероприятия"), (SVED, "Сведения об образовательной организации"),
        ("/nasha-shkola/o-shkole/", "О школе"), ("/glavnoe/kontakty/", "Контакты"), ("/glavnoe/raspisanie/", "Расписание"),
        ("/pedagogam-i-sotrudnikam/", "Педагогам и сотрудникам"),
        (SVED + "dokumenty/", "Документы"), (SVED + "obrazovanie/", "Образование"),
        (SVED + "pedagogicheskiy-sostav/", "Педагогический состав"),
        (SVED + "struktura-i-organy-upravleniya/", "Структура и органы управления")])
    news = [("koncert", "Концерт для мам", d(2), f"{d(3):%d.%m.%Y} в школе прошёл концерт, ребята пели песни."),
            ("olimpiada", "Школьная олимпиада по физике", d(9), f"{d(10):%d.%m.%Y} состоялась олимпиада по физике."),
            ("sbor", "Сбор макулатуры", d(45), f"{d(46):%d.%m.%Y} прошёл сбор макулатуры.")]
    cards = "".join(f'<div class="card"><span itemprop="datePublished" content="{p:%Y-%m-%d}T10:00:00"></span>'
                    f'<h3 class="card-title"><a href="{NEWS}{slug}.html">{t}</a></h3></div>' for slug, t, p, _ in news)
    site = {
        "/": _page("Средняя школа «Демо»", '<p>Добро пожаловать.</p><a href="https://pos.gosuslugi.ru/x">ПОС</a>', menu),
        NEWS: _page("Новости", cards, menu),
        EVENTS: _page("Мероприятия", f'<div class="object"><div class="object-date">{d(-5):%d.%m.%Y}</div>'
                                    f'<h2 class="name"><a href="{EVENTS}sobranie.html">Родительское собрание</a></h2></div>', menu),
        SVED: _page("Сведения об образовательной организации", "<p>Основные сведения об учреждении, учредитель, "
                    "место нахождения, режим и график работы, контактные телефоны и адреса электронной почты.</p>", menu),
        SVED + "dokumenty/": _page("Документы", "".join(
            f'<div class="gw-document-item"><a class="gw-document-item__title" href="/upload/{i}.pdf">{t}</a></div>'
            for i, t in enumerate(["Устав образовательной организации", "Выписка из реестра лицензий",
                                   "Правила внутреннего распорядка обучающихся", "Коллективный договор",
                                   "Отчёт о результатах самообследования за 2025 год", "Учебный план 2026-2027"]))),
        SVED + "obrazovanie/": _page("Образование", "<p>Основная образовательная программа основного общего образования. "
                                    "Форма обучения: очная. Нормативный срок обучения 5 лет. Язык обучения русский. "
                                    "Численность обучающихся 350. Электронное обучение не применяется.</p>"
                                    '<a href="/upload/oop.pdf">Образовательная программа ООО</a>'),
        SVED + "pedagogicheskiy-sostav/": _page("Педагогический состав", "".join(
            f'<div class="gw-person-info-card"><a href="/p/{i}.html"><img src="/netcat_files/{i}.jpg"></a>'
            f'<div class="name">{n}</div><div class="position">Учитель</div></div>'
            for i, n in enumerate(["Иванова Мария Петровна", "Петров Иван Сергеевич", "Сидорова Анна Олеговна"]))),
        SVED + "struktura-i-organy-upravleniya/": _page("Структура и органы управления", "<p>" + "Органы управления: "
                                                         "общее собрание, педагогический совет, управляющий совет. " * 6 + "</p>"),
        "/nasha-shkola/o-shkole/": _page("О школе", "<p>" + "История школы: основана в 1965 году. " * 20 + "</p>"),
        "/glavnoe/kontakty/": _page("Контакты", "<p>Адрес</p><p>685000, г. Магадан, ул. Пушкина, д. 1</p>"
                                    "<p>Телефон</p><p>8 (4132) 62-00-00</p><p>Электронная почта</p><p>demo@school.ru</p>"
                                    "<p>Режим работы</p><p>с 08:00 до 18:00</p><p>Часы приёма</p><p>Пн 14:00–16:00</p>"),
        "/glavnoe/raspisanie/": _page("Расписание", "<table>" + "<tr><td>Математика</td></tr>" * 6 + "</table>"),
        "/pedagogam-i-sotrudnikam/": _page("Педагогам и сотрудникам",
                                           "<p>Разместите текст анонса. Примерно от 100 до 200 символов.</p>"),
    }
    for slug, title, _p, text in news:
        site[f"{NEWS}{slug}.html"] = _page(title, f"<p>{text}</p>")
    for i in range(3):
        site[f"/p/{i}.html"] = _page("Карточка", "<p>Уровень образования: высшее. Телефон 8 (4132) 62-00-0{i}</p>")
    return site


class FakeFetcher(CancellableFetcher):
    """Fetcher audit/ без сети: отдаёт страницы офлайн-сайта; delay замедляет ответы для теста остановки."""

    def __init__(self, sites: dict[str, dict[str, str]], cancelled=lambda: False, delay: dict[str, float] | None = None):
        super().__init__("/tmp/sever-test-cache", use_cache=False, cancelled=cancelled)
        self.sites, self.delay, self.calls = sites, delay or {}, 0
        self._lock = threading.Lock()

    def get(self, url):
        if self.cancelled():
            raise Cancelled()
        with self._lock:
            self.calls += 1
        host, _, rest = url.partition("://")[2].partition("/")
        if self.delay.get(host):
            time.sleep(self.delay[host])
        path = "/" + rest.split("?")[0]
        html = self.sites.get(host, {}).get(path)
        if html is None:
            return Page(url=url, status=404, text="", final_url=url)
        return Page(url=url, status=200, text=html, final_url=url)

    def image_hash(self, url, max_bytes=3_000_000):
        return "d:" + str(abs(hash(url)) % 10 ** 12)


class FakeLLM:
    """Подменная модель: всегда «видит» дату события на день раньше алгоритма — расхождение уходит инспектору."""

    def __init__(self, settings=None):
        self.model = "fake-model"
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0, "web_searches": 0}
        self.calls = []

    def check_news(self, title: str, text: str, published: datetime, site_name: str):
        self.calls.append(title)
        self.usage["requests"] += 1
        return {"event_date": published.date() - timedelta(days=30), "event_date_basis": "подмена",
                "claims": [{"claim": title, "status": "unverified", "source": "ИИ (fake-model), без веб-поиска: внешняя проверка не выполнялась",
                            "note": "Внешняя проверка не выполнялась"}]}
