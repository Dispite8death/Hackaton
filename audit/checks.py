"""Проверки одного сайта. Каждая проверка добавляет в SiteResult замечания (Finding) и метрики."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlparse

from . import config as C
from .dates import academic_year, fmt, next_quarter_bounds, quarter_bounds, quarter_label
from .discovery import discover, subpages
from .extract import (PHOTO_STUB_URL, extract_article_text, extract_documents, extract_events, extract_news,
                      extract_persons, find_placeholders, has_empty_docs_block, lines_of, main_content,
                      page_title, person_detail, soup_of, text_of)
from .factcheck import extract_event_date, offline_factcheck
from .fetcher import Fetcher, Page
from .models import Document, Finding, NewsItem, Person, SiteResult

log = logging.getLogger(__name__)


@dataclass
class Options:
    today: date
    max_news_details: int = 15
    max_persons: int = 40
    image_hashes: bool = True
    max_images: int = 25
    llm_news_per_site: int = 3


def _with_query(url: str, q: str) -> str:
    return url + ("&" if "?" in url else "?") + q


_STOP = set("который которая которые этот этого году года день дня для при над под или как так все всех наши наша наш "
            "нашей школы школе школа детский сада саду детей ребят".split())


def _tokens(s: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[а-яёa-z0-9]{4,}", s.lower()) if w not in _STOP}


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


class SiteAuditor:
    def __init__(self, fetcher: Fetcher, result: SiteResult, opts: Options, llm=None):
        self.f = fetcher
        self.r = result
        self.o = opts
        self.llm = llm
        self.base = result.base
        self.today = opts.today
        self.pages: dict[str, tuple[str, str, str]] = {}   # url -> (раздел, заголовок, текст)
        self.links: list[tuple[str, str]] = []

    # ------------------------------------------------------------------ helpers
    def get(self, url: str, section: str = "") -> Page:
        p = self.f.get(url)
        self.r.pages_fetched += 1
        if p.ok and url not in self.pages:
            soup = soup_of(p.text)
            self.pages[url.split("?")[0]] = (section, page_title(soup), text_of(main_content(soup), "\n"))
        return p

    def sec(self, key: str) -> str | None:
        return self.r.sections.get(key)

    def add(self, section: str, problem: str, severity: str, rec: str, url: str | None = None,
            evidence: list[str] | None = None, check: str = "") -> None:
        self.r.add(Finding(section, problem, severity, rec, url, evidence or [], check))

    # ------------------------------------------------------------------ main
    def run(self) -> SiteResult:
        r = self.r
        r.checked_at = datetime.now().isoformat(timespec="seconds")
        home = self.get(self.base + "/", "Главная страница")
        if not home.ok:
            r.status = "unavailable"
            r.error = home.error or f"HTTP {home.status}"
            self.add("Сайт", f"Сайт недоступен ({r.error}) — проверка не выполнена", "critical",
                     "Проверить доступность сайта и работу домена; обратиться в техподдержку платформы Госвеб.",
                     self.base, check="site_unavailable")
            return r
        final_host = urlparse(home.final_url or home.url).netloc
        if final_host and final_host != r.domain:
            self.add("Сайт", f"Главная страница перенаправляет на другой адрес: {final_host}", "high",
                     "Убедиться, что официальный адрес сайта указан верно.", home.final_url, check="redirect")

        r.sections, self.links = discover(self.f, self.base, home.text, r.kind)
        steps = [self.check_home, self.check_news, self.check_events, self.check_documents,
                 self.check_staff_all, self.check_contacts, self.check_programs, self.check_pedagogam]
        if r.kind == "school":
            steps += [self.check_about, self.check_structure, self.check_schedule]
        for step in steps:
            try:
                step(home)
            except Exception as e:  # noqa: BLE001 — одна упавшая проверка не должна ронять весь сайт
                log.exception("%s: проверка %s упала", r.domain, step.__name__)
                self.add("Служебное", f"Проверка «{step.__name__}» не выполнена: {type(e).__name__}", "low",
                         "Сообщить разработчикам краулера.", check="internal_error")
        self.check_placeholders()
        return r

    # ------------------------------------------------------------------ главная
    def check_home(self, home: Page) -> None:
        html = home.text
        if not re.search(r"pos\.gosuslugi|gosuslugi\.ru/pos|Решаем\s+вместе|pos-widget|data-pos|ПОС", html, re.I):
            self.add("Главная страница", "Не размещён баннер платформы обратной связи (ПОС «Решаем вместе»)", "medium",
                     "Указать идентификационный номер организации в настройках шаблона — баннер ПОС появится "
                     "автоматически (см. лекцию 1 / техподдержку).", self.base + "/", check="pos_banner")
        if self.r.kind == "kindergarten":
            txt = text_of(main_content(soup_of(html)), "\n")
            missing = []
            if not C.PHONE_RE.search(txt):
                missing.append("телефон")
            if not C.EMAIL_RE.search(txt):
                missing.append("электронная почта")
            if not C.ADDRESS_RE.search(txt):
                missing.append("адрес")
            if not (re.search(r"Режим\s+работы", txt, re.I) and C.TIME_RANGE_RE.search(txt)):
                missing.append("режим работы")
            self.r.metrics["home_contacts_missing"] = missing
            if missing:
                self.add("Главная страница", "На главной странице нет контактных данных: " + ", ".join(missing),
                         "high" if len(missing) > 1 else "medium",
                         "Заполнить блок контактов на главной странице (адрес, телефон, e-mail, режим работы).",
                         self.base + "/", check="home_contacts")

    # ------------------------------------------------------------------ новости
    def check_news(self, home: Page) -> None:
        S = "Новости"
        url = self.sec("news")
        if not url:
            self.add(S, "Раздел «Новости» не найден", "critical", "Создать и вести раздел «Новости».", check="news_missing")
            return
        page = self.get(_with_query(url, "recNum=100"), S)
        if not page.ok:
            page = self.get(url, S)
        items = extract_news(page.text, url) if page.ok else []
        dated = sorted([n for n in items if n.published], key=lambda n: n.published, reverse=True)
        m = self.r.metrics
        m["news_total"] = len(items)
        if not items:
            self.add(S, "В разделе «Новости» нет ни одной публикации", "critical",
                     "Публиковать новости учреждения не реже одного раза в 2 недели.", url, check="news_empty")
            return
        if not dated:
            self.add(S, "Не удалось определить даты публикации новостей", "medium",
                     "Проверить, что у новостей заполнена дата публикации.", url, check="news_no_dates")
            return
        last = dated[0].published
        days = (self.today - last.date()).days
        m.update(news_last=last.date().isoformat(), news_days_since_last=days)
        if days > C.NEWS_MAX_GAP_DAYS:
            self.add(S, f"Последняя новость опубликована {fmt(last)} — {days} дн. назад "
                        f"(норматив: не реже 1 раза в {C.NEWS_MAX_GAP_DAYS} дней)",
                     "critical" if days > 45 else "high",
                     "Опубликовать актуальные новости и далее размещать их не реже раза в 2 недели.",
                     url, [f"«{dated[0].title}» — {fmt(last)}"], "news_stale")

        # интервалы за последний год
        horizon = self.today - timedelta(days=C.NEWS_HISTORY_DAYS)
        asc = sorted({n.published.date() for n in dated if n.published.date() >= horizon})
        gaps, summer = [], []
        for a, b in zip(asc, asc[1:]):
            d = (b - a).days
            if d > C.NEWS_MAX_GAP_DAYS:
                span = [a + timedelta(days=i) for i in range(d)]
                in_summer = sum(1 for x in span if x.month in C.SUMMER_MONTHS) / d > 0.5
                (summer if in_summer else gaps).append((a, b, d))
        m["news_last_year"] = len(asc)
        m["news_gaps"] = len(gaps)
        m["news_max_gap"] = max([g[2] for g in gaps + summer], default=0)
        # если история короче года (пагинация) — отмечаем глубину
        m["news_oldest_loaded"] = dated[-1].published.date().isoformat()
        if gaps:
            ev = [f"с {fmt(a)} по {fmt(b)} — {d} дн. без новостей" for a, b, d in sorted(gaps, key=lambda g: -g[2])[:8]]
            self.add(S, f"За последний год {len(gaps)} раз(а) нарушен интервал публикации новостей "
                        f"(более {C.NEWS_MAX_GAP_DAYS} дней, максимум {max(g[2] for g in gaps)} дн.)",
                     "high" if len(gaps) >= 4 else "medium",
                     "Составить график публикаций: минимум одна новость в 2 недели.", url, ev, "news_gaps")
        if summer:
            ev = [f"с {fmt(a)} по {fmt(b)} — {d} дн." for a, b, d in summer[:4]]
            self.add(S, "Перерыв в публикации новостей в период летних каникул", "low",
                     "Даже летом публиковать новости (летний лагерь, ремонт, подготовка к учебному году).",
                     url, ev, "news_summer_gap")

        # своевременность и факт-чекинг
        recent = [n for n in dated if (self.today - n.published.date()).days <= C.NEWS_TIMELINESS_DAYS]
        recent = recent[: self.o.max_news_details]

        def load(n: NewsItem) -> NewsItem:
            p = self.get(n.url, S)
            if p.ok:
                _, n.text = extract_article_text(p.text)
            return n

        with ThreadPoolExecutor(4) as ex:
            list(ex.map(load, recent))
        late, with_date, contradictions = [], 0, []
        for i, n in enumerate(recent):
            body = f"{n.title}\n{n.text or n.snippet}"
            n.event_date, n.event_date_source = extract_event_date(body, n.published)
            n.factcheck = offline_factcheck(body, n.published)
            if self.llm and i < self.o.llm_news_per_site and n.text:
                res = self.llm.check_news(n.title, n.text, n.published, self.r.name)
                if res:
                    if res.get("event_date") and res["event_date"] <= n.published.date() + timedelta(days=1):
                        n.event_date, n.event_date_source = res["event_date"], "ИИ: " + res.get("event_date_basis", "")[:80]
                    n.factcheck += res.get("claims", [])
            if n.event_date:
                with_date += 1
                n.delay_days = (n.published.date() - n.event_date).days
                if n.delay_days > C.NEWS_PUBLISH_DELAY_DAYS:
                    late.append(n)
            contradictions += [(n, c) for c in n.factcheck if c["status"] == "contradiction"]
        m.update(news_checked=len(recent), news_with_event_date=with_date, news_late=len(late),
                 news_factchecked=sum(1 for n in recent if n.factcheck),
                 news_facts_confirmed=sum(1 for n in recent for c in n.factcheck if c["status"] == "confirmed"),
                 news_facts_contradiction=len(contradictions))
        if late:
            share = len(late) / max(with_date, 1)
            ev = [f"«{n.title[:80]}» — событие {fmt(n.event_date)}, опубликовано {fmt(n.published)} "
                  f"(+{n.delay_days} дн.)" for n in sorted(late, key=lambda n: -n.delay_days)[:8]]
            self.add(S, f"{len(late)} из {with_date} проверенных новостей опубликованы позже 24 часов после события",
                     "high" if share >= 0.5 else "medium",
                     "Размещать новость в течение суток после мероприятия.", url, ev, "news_late")
        if len(recent) >= 3 and with_date / len(recent) < 0.3:
            self.add(S, f"В {len(recent) - with_date} из {len(recent)} свежих новостей не указана дата события",
                     "low", "Указывать в тексте новости дату проведения мероприятия — это помогает читателям "
                            "и позволяет контролировать срок публикации.", url, check="news_no_event_date")
        if contradictions:
            ev = [f"«{n.title[:60]}»: {c['claim']} — {c['note']} [{c['source'][:80]}]" for n, c in contradictions[:8]]
            self.add(S, f"Факт-чекинг: найдено {len(contradictions)} расхождений с открытыми источниками", "medium",
                     "Проверить и исправить даты/факты в указанных новостях.", url, ev, "news_factcheck")
        self.r.news = dated[:100]

    # ------------------------------------------------------------------ мероприятия
    def check_events(self, home: Page) -> None:
        S = "Мероприятия"
        url = self.sec("events")
        school = self.r.kind == "school"
        if not url:
            if school:
                self.add(S, "Раздел «Мероприятия» не найден", "high",
                         "Создать раздел «Мероприятия» и публиковать план мероприятий на квартал.", check="events_missing")
            return
        page = self.get(_with_query(url, "recNum=100"), S)
        if not page.ok:
            page = self.get(url, S)
        events = extract_events(page.text, url, self.today) if page.ok else []
        self.r.events = events
        cq, nq = quarter_bounds(self.today), next_quarter_bounds(self.today)

        def overlaps(e, a, b):
            return e.start and e.start <= b and (e.end or e.start) >= a

        in_cq = [e for e in events if overlaps(e, *cq)]
        in_nq = [e for e in events if overlaps(e, *nq)]
        m = self.r.metrics
        m.update(events_total=len(events), events_current_q=len(in_cq), events_next_q=len(in_nq))
        sev_main = "high" if school else "medium"
        if not events:
            self.add(S, "Раздел «Мероприятия» пуст", sev_main,
                     "Разместить план мероприятий на текущий и следующий квартал.", url, check="events_empty")
            return
        if not in_cq:
            last = max((e.start for e in events if e.start), default=None)
            self.add(S, f"Нет мероприятий на текущий квартал ({quarter_label(cq[0])}: {fmt(cq[0])}–{fmt(cq[1])})",
                     sev_main, "Добавить мероприятия текущего квартала (собрания, праздники, конкурсы, олимпиады).",
                     url, [f"Самое позднее мероприятие в разделе: {fmt(last)}"] if last else [], "events_current_q")
        if not in_nq:
            self.add(S, f"Нет анонсов мероприятий на следующий квартал ({quarter_label(nq[0])})", "medium",
                     "Заранее публиковать план мероприятий на предстоящий квартал.", url, check="events_next_q")
        # сопоставление с новостями
        past = [e for e in events if e.start and e.start <= self.today
                and (self.today - e.start).days <= C.NEWS_TIMELINESS_DAYS]
        unmatched, matched = [], 0
        for e in past:
            end = (e.end or e.start) + timedelta(days=C.EVENT_NEWS_WINDOW_DAYS)
            cands = [n for n in self.r.news if n.published and e.start - timedelta(days=2) <= n.published.date() <= end]
            if any(_similar(e.title, f"{n.title} {n.snippet} {n.text[:500]}") >= 0.5 for n in cands):
                matched += 1
            else:
                unmatched.append(e)
        m.update(events_past_checked=len(past), events_with_news=matched)
        if unmatched:
            self.add(S, f"{len(unmatched)} из {len(past)} прошедших мероприятий не отражены в новостях",
                     "medium", "После проведения мероприятия публиковать новость с итогами и фотографиями.", url,
                     [f"«{e.title[:90]}» ({fmt(e.start)})" for e in unmatched[:8]], "events_vs_news")

    # ------------------------------------------------------------------ документы
    def check_documents(self, home: Page) -> None:
        S = "Документы"
        docs: list[Document] = []
        texts: list[str] = []
        url = self.sec("documents")
        if url:
            p = self.get(_with_query(url, "recNum=500"), S)
            if not p.ok:
                p = self.get(url, S)
            if p.ok:
                docs += extract_documents(p.text, url)
                texts.append("\n".join(text_of(a) for a in main_content(soup_of(p.text)).find_all("a")))
        else:
            self.add(S, "Подраздел «Документы» раздела «Сведения об образовательной организации» не найден", "critical",
                     "Создать подраздел «Документы» согласно Приказу Рособрнадзора № 1493.", check="docs_missing")
        sv = self.sec("svedeniya")
        empty_pages = []
        subs = subpages(self.links, sv, 25) if sv else []
        for u, t in subs:
            p = self.get(u, "Сведения об образовательной организации")
            if not p.ok:
                if p.status == 404:
                    empty_pages.append(f"{t or u} — страница не открывается (404)")
                continue
            docs += extract_documents(p.text, u)
            body = self.pages.get(u, ("", "", ""))[2]
            # тексты ссылок (документ может быть ссылкой на bus.gov.ru или карточку без файла)
            texts.append("\n".join(text_of(a) for a in main_content(soup_of(p.text)).find_all("a")))
            if len(body) < 250 and has_empty_docs_block(p.text):
                empty_pages.append(f"{t or u}: только заголовок и «Документы не выбраны»")
        uniq: dict[str, Document] = {}
        for d in docs:
            uniq.setdefault(d.title.lower(), d)
        docs = list(uniq.values())
        self.r.documents = docs
        titles = "\n".join(d.title for d in docs)
        alltext = titles + "\n" + "\n".join(texts)
        present, missing = [], []
        for key, (label, rx, sev, who) in C.REQUIRED_DOCS.items():
            if who != "both" and who != self.r.kind:
                continue
            if re.search(rx, titles, re.I) or re.search(rx, alltext, re.I):
                present.append(key)
            else:
                missing.append((key, label, sev))
        # перепроверка встроенным поиском сайта: документ мог быть размещён в другом разделе
        still_missing = []
        for key, label, sev in missing:
            q = C.DOC_SEARCH_QUERIES.get(key)
            found = False
            if q:
                sp = self.f.get(f"{self.base}/search/?search_query={quote(q)}")
                if sp.ok:
                    stext = text_of(main_content(soup_of(sp.text)))
                    found = bool(re.search(C.REQUIRED_DOCS[key][1], stext, re.I))
            if found:
                present.append(key)
                self.r.metrics.setdefault("docs_found_by_search", []).append(label)
            else:
                still_missing.append((key, label, sev))
        missing = still_missing
        self.r.doc_categories = present
        self.r.metrics.update(docs_total=len(docs), docs_required_present=len(present),
                              docs_required_total=len(present) + len(missing))
        for key, label, sev in missing:
            self.add(S, f"Не найден обязательный документ: {label}", sev,
                     "Разместить документ (скан с подписью или файл с ЭП) в подразделе «Документы».",
                     url, check=f"doc_{key}")
        # актуальность отчёта о самообследовании (срок — до 20 апреля следующего года)
        years = [int(y) for d in docs if re.search(C.REQUIRED_DOCS["samoobsl"][1], d.title, re.I)
                 for y in re.findall(r"20\d\d", d.title)]
        need = self.today.year - 1 if self.today >= date(self.today.year, 4, 20) else self.today.year - 2
        if years and max(years) < need:
            self.add(S, f"Отчёт о самообследовании устарел: последний — за {max(years)} г., требуется за {need} г.",
                     "high", "Разместить отчёт о самообследовании за прошедший год (срок — до 20 апреля).",
                     url, check="doc_samoobsl_old")
        if empty_pages:
            self.add("Сведения об образовательной организации",
                     f"{len(empty_pages)} подразделов не заполнены или не открываются", "high" if len(empty_pages) > 3 else "medium",
                     "Заполнить подразделы либо указать «информация отсутствует / не предоставляется» "
                     "(не оставлять заголовки без текста).", sv, empty_pages[:12], "svedeniya_empty")

    # ------------------------------------------------------------------ педагоги
    def check_staff_all(self, home: Page) -> None:
        if self.r.kind == "school":
            self._check_staff("ped_staff", "Педагогический состав", contacts_required=False, critical_if_missing=True)
        else:
            self._check_staff("kollektiv", "Наш коллектив", contacts_required=True, critical_if_missing=False)
            self._check_staff("ped_staff", "Педагогический состав", contacts_required=True, critical_if_missing=True)

    def _check_staff(self, key: str, S: str, contacts_required: bool, critical_if_missing: bool) -> None:
        url = self.sec(key)
        if not url:
            self.add(S, f"Раздел «{S}» не найден", "critical" if critical_if_missing else "high",
                     f"Создать раздел «{S}» и заполнить карточки сотрудников.", check=f"{key}_missing")
            return
        p = self.get(_with_query(url, "recNum=500"), S)
        if not p.ok:
            p = self.get(url, S)
        persons = extract_persons(p.text, url) if p.ok else []
        if not persons:
            self.add(S, f"В разделе «{S}» нет сведений о сотрудниках", "critical" if critical_if_missing else "high",
                     "Заполнить карточки: ФИО, должность, фото, уровень образования, квалификация, стаж, контакты.",
                     url, check=f"{key}_empty")
            return
        sample = [x for x in persons if x.url][: self.o.max_persons]

        def detail(person: Person) -> None:
            dp = self.get(person.url, S)
            if not dp.ok:
                return
            d = person_detail(dp.text)
            person.detail_checked = True
            person.has_education |= d["has_education"]
            person.has_phone |= d["has_phone"]
            person.has_email |= d["has_email"]

        def img(person: Person) -> None:
            if person.photo and not PHOTO_STUB_URL.search(person.photo):
                person.photo_hash = self.f.image_hash(person.photo)

        with ThreadPoolExecutor(4) as ex:
            list(ex.map(detail, sample))
            if self.o.image_hashes:
                list(ex.map(img, persons[: self.o.max_images]))
        for x in persons:
            x.list_page = url
        self.r.persons += persons
        n = len(persons)
        no_photo = [x for x in persons if not x.photo or PHOTO_STUB_URL.search(x.photo or "")]
        checked = [x for x in persons if x.detail_checked] or persons
        no_edu = [x for x in checked if not x.has_education]
        no_contact = [x for x in checked if not (x.has_phone or x.has_email)]
        self.r.metrics[f"{key}_count"] = n
        self.r.metrics[f"{key}_no_photo"] = len(no_photo)
        self.r.metrics[f"{key}_detail_checked"] = len(checked)
        self.r.metrics[f"{key}_no_education"] = len(no_edu)
        self.r.metrics[f"{key}_no_contacts"] = len(no_contact)

        def sev(share: float, hi: float = 0.3) -> str:
            return "high" if share >= hi else "medium"

        if no_photo:
            self.add(S, f"Нет фотографии у {len(no_photo)} из {n} сотрудников", sev(len(no_photo) / n, 0.5),
                     "Добавить портретные фото в деловом стиле на нейтральном одинаковом фоне.", url,
                     [x.name for x in no_photo[:12]], f"{key}_photo")
        if no_edu:
            self.add(S, f"Не указан уровень образования у {len(no_edu)} из {len(checked)} проверенных сотрудников",
                     sev(len(no_edu) / len(checked)),
                     "Заполнить в карточке: уровень образования, квалификация, направление подготовки "
                     "(обязательно по Приказу № 1493).", url, [x.name for x in no_edu[:12]], f"{key}_education")
        if contacts_required and no_contact:
            self.add(S, f"Нет контактных данных (телефон/e-mail) у {len(no_contact)} из {len(checked)} сотрудников",
                     sev(len(no_contact) / len(checked), 0.5),
                     "Указать рабочий телефон или e-mail в карточке сотрудника.", url,
                     [x.name for x in no_contact[:12]], f"{key}_contacts")

    # ------------------------------------------------------------------ контакты
    def check_contacts(self, home: Page) -> None:
        S = "Контакты"
        url = self.sec("contacts")
        if not url:
            self.add(S, "Раздел «Контакты» не найден", "high", "Создать раздел «Контакты».", check="contacts_missing")
            return
        p = self.get(url, S)
        if not p.ok:
            self.add(S, "Раздел «Контакты» не открывается", "high", "Проверить страницу.", url, check="contacts_404")
            return
        main = main_content(soup_of(p.text))
        txt = text_of(main, "\n")
        lines = lines_of(main)
        missing = []
        if not (C.PHONE_RE.search(txt) or main.select_one("a[href^='tel:']")):
            missing.append("телефон")
        if not C.EMAIL_RE.search(txt):
            missing.append("электронная почта")
        if not C.ADDRESS_RE.search(txt):
            missing.append("адрес")
        labels = {"режим работы": r"^Режим\s+работы"}
        if self.r.kind == "school":
            labels["часы приёма граждан"] = r"^(Часы|График)\s+при[её]ма"
        label_rx = re.compile(r"^(Телефон|Электронная почта|Адрес|Режим работы|Часы при[её]ма|График при[её]ма|"
                              r"Начало занятий|Продолжительность|Схема проезда|Как добраться|Официальные данные|"
                              r"Структура|Документы|Наименование)", re.I)
        for name, rx in labels.items():
            idx = next((i for i, ln in enumerate(lines) if re.search(rx, ln, re.I)), None)
            if idx is None:
                if name == "режим работы" and C.TIME_RANGE_RE.search(txt):
                    continue
                missing.append(name)
                continue
            inline = re.sub(rx, "", lines[idx], flags=re.I).strip(" :—-")
            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if not inline and (not nxt or (label_rx.match(nxt) and not C.TIME_RANGE_RE.search(nxt))):
                missing.append(f"{name} (заголовок без содержания)")
        self.r.metrics["contacts_missing"] = missing
        if missing:
            crit = any(x in ("телефон", "адрес") for x in missing)
            self.add(S, "Неполные контактные данные: не указаны " + ", ".join(missing),
                     "high" if crit or len(missing) >= 2 else "medium",
                     "Заполнить все поля раздела «Контакты»: адрес, телефон, e-mail, режим работы"
                     + (", часы приёма граждан." if self.r.kind == "school" else "."), url, check="contacts_incomplete")

    # ------------------------------------------------------------------ О школе / структура
    def check_about(self, home: Page) -> None:
        S = "О школе"
        url = self.sec("about")
        if not url:
            self.add(S, "Раздел «О школе» не найден", "high", "Создать раздел «О школе» с историей учреждения.",
                     check="about_missing")
            return
        p = self.get(url, S)
        txt = self.pages.get(url, ("", "", ""))[2] if p.ok else ""
        self.r.metrics["about_chars"] = len(txt)
        if not re.search(r"истори|основан|открыт\w*\s+в\s+\d{4}|\b(19|20)\d\d\s+год", txt, re.I):
            self.add(S, "Не заполнена история учреждения", "medium",
                     "Описать историю школы (1000–1200 символов) и добавить фотографии.", url, check="about_history")
        elif len(txt) < 600:
            self.add(S, f"Раздел «О школе» заполнен минимально ({len(txt)} символов)", "low",
                     "Дополнить сведения об истории, традициях и достижениях школы.", url, check="about_short")

    def check_structure(self, home: Page) -> None:
        S = "Структура и органы управления"
        url = self.sec("struktura")
        if not url:
            self.add(S, "Подраздел «Структура и органы управления» не найден", "high",
                     "Заполнить подраздел: структурные подразделения, руководители, положения.", check="structure_missing")
            return
        p = self.get(url, S)
        if not p.ok:
            return
        txt = self.pages.get(url, ("", "", ""))[2]
        persons = extract_persons(p.text, url)
        docs = extract_documents(p.text, url)
        self.r.metrics.update(structure_chars=len(txt), structure_persons=len(persons), structure_docs=len(docs))
        if len(txt) < 300 and not persons and not docs:
            self.add(S, "Подраздел «Структура и органы управления» практически не заполнен", "high",
                     "Указать органы управления, их руководителей (ФИО, должность, фото), положения об органах.",
                     url, check="structure_empty")
        no_photo = [x.name for x in persons if not x.photo]
        if no_photo:
            self.add(S, f"Нет фото у {len(no_photo)} руководителей органов управления", "medium",
                     "Добавить портретные фото руководителей.", url, no_photo[:10], "structure_photo")
        empty = []
        for u, t in subpages(self.links, url, 10):
            sp = self.get(u, S)
            body = self.pages.get(u, ("", "", ""))[2] if sp.ok else ""
            if sp.ok and len(body) < 200 and not extract_documents(sp.text, u) and not extract_persons(sp.text, u):
                empty.append(t or u)
        if empty:
            self.add(S, f"Не заполнены страницы органов управления: {len(empty)}", "medium",
                     "Заполнить или скрыть пустые страницы (состав, положение, контакты).", url, empty, "structure_sub_empty")

    # ------------------------------------------------------------------ расписание
    def check_schedule(self, home: Page) -> None:
        S = "Расписание"
        url = self.sec("schedule")
        if not url:
            self.add(S, "Раздел «Расписание» не найден", "high", "Разместить расписание уроков на текущий учебный год.",
                     check="schedule_missing")
            return
        p = self.get(url, S)
        if not p.ok:
            self.add(S, "Раздел «Расписание» не открывается", "high", "Проверить страницу.", url, check="schedule_404")
            return
        main = main_content(soup_of(p.text))
        txt = text_of(main, "\n")
        docs = extract_documents(p.text, url)
        rows = sum(len(t.find_all("tr")) for t in main.find_all("table"))
        links = [a for a in main.find_all("a", href=True) if re.search(r"расписан", text_of(a), re.I)]
        images = [i for i in main.find_all("img") if "/netcat_files/" in (i.get("src") or "")]
        image_links = [a for a in main.find_all("a", href=True)
                       if re.search(r"\.(jpe?g|png|gif|webp)$", a["href"], re.I)]
        # модуль «Расписание» шаблона: список классов в data-schedule-numbers / <option>
        widget = "gw-schedule" in p.text or 'name="class_number"' in p.text
        nums = re.search(r"data-schedule-numbers=['\"]([^'\"]*)['\"]", p.text)
        sel = re.search(r'name="class_number"[^>]*>(.*?)</select>', p.text, re.S)
        widget_filled = bool(nums and nums[1].strip()) or bool(sel and re.search(r"<option[^>]*value=\"[^\"]+\"", sel[1]))
        files_or_media = bool(docs) or rows > 3 or bool(images) or bool(image_links)
        filled = widget_filled or files_or_media or bool(links) or len(txt) > 600
        self.r.metrics.update(schedule_docs=len(docs), schedule_table_rows=rows, schedule_images=len(images) + len(image_links),
                              schedule_widget=widget, schedule_widget_filled=widget_filled)
        if not filled:
            self.add(S, "Расписание не заполнено", "high",
                     "Заполнить расписание уроков (модуль «Расписание» шаблона или файл) на текущий учебный год.",
                     url, check="schedule_empty")
            return
        if widget and not widget_filled:
            self.add(S, "Модуль «Расписание» шаблона не заполнен — расписание размещено только файлами/картинками",
                     "low", "Заполнить модуль «Расписание» (по классам), чтобы родители видели расписание на неделю "
                            "без скачивания файлов.", url, check="schedule_widget_empty")
        ay = academic_year(self.today)
        years = [(int(a), int(b)) for a, b in re.findall(r"(20\d\d)\s*[-–/]\s*(20\d\d)", txt + " ".join(d.title for d in docs))]
        if years and max(years) < ay:
            self.add(S, f"Расписание за прошлый учебный год ({max(years)[0]}–{max(years)[1]}), "
                        f"текущий — {ay[0]}–{ay[1]}", "high",
                     "Обновить расписание на текущий учебный год.", url, check="schedule_old")

    # ------------------------------------------------------------------ педагогам и сотрудникам
    def check_pedagogam(self, home: Page) -> None:
        S = "Педагогам и сотрудникам"
        url = self.sec("pedagogam")
        ds = self.r.kind == "kindergarten"
        if not url:
            self.add(S, "Раздел «Педагогам и сотрудникам» не найден", "high" if ds else "medium",
                     "Создать и заполнить раздел (методические материалы, аттестация, повышение квалификации).",
                     check="pedagogam_missing")
            return
        p = self.get(url, S)
        root_txt = self.pages.get(url, ("", "", ""))[2] if p.ok else ""
        root_docs = extract_documents(p.text, url) if p.ok else []
        subs = subpages(self.links, url, 15)
        empty, filled = [], 0
        for u, t in subs:
            sp = self.get(u, S)
            if not sp.ok:
                empty.append(f"{t or u} — не открывается")
                continue
            body = self.pages.get(u, ("", "", ""))[2]
            if len(body) < 150 and not extract_documents(sp.text, u):
                empty.append(t or u)
            elif len(body) < 300 and has_empty_docs_block(sp.text) and not extract_documents(sp.text, u):
                empty.append(f"{t or u} (только заголовки, «Документы не выбраны»)")
            else:
                filled += 1
        self.r.metrics.update(pedagogam_subsections=len(subs), pedagogam_empty=len(empty))
        if not subs and len(root_txt) < 200 and not root_docs:
            self.add(S, "Раздел «Педагогам и сотрудникам» не заполнен", "high" if ds else "medium",
                     "Разместить материалы для педагогов: аттестация, повышение квалификации, методические материалы.",
                     url, check="pedagogam_empty")
        elif empty:
            self.add(S, f"Не заполнено {len(empty)} из {len(subs)} подразделов", "medium",
                     "Заполнить подразделы или скрыть ненужные.", url, empty[:12], "pedagogam_sub_empty")

    # ------------------------------------------------------------------ образовательные программы
    def check_programs(self, home: Page) -> None:
        S = "Реализуемые образовательные программы"
        url = self.sec("obrazovanie")
        if not url:
            self.add(S, "Подраздел «Образование» не найден", "high",
                     "Заполнить подраздел «Образование» (Приказ № 1493).", check="programs_missing")
            return
        p = self.get(url, S)
        if not p.ok:
            return
        txt = self.pages.get(url, ("", "", ""))[2]
        docs = extract_documents(p.text, url)
        elems = {
            "наименование образовательной программы": r"образовательн\w+\s+программ",
            "форма обучения": r"форм\w+\s+обучени|очн\w+",
            "нормативный срок обучения": r"(нормативн\w+\s+)?срок\w*\s+(обучени|освоени|получени)",
            "язык обучения": r"язык",
            "численность обучающихся": r"численност|количеств\w+\s+(обучающ|воспитанник|детей)",
            "электронное обучение / ДОТ": r"электронн\w+\s+обучени|дистанционн",
        }
        missing = [k for k, rx in elems.items() if not re.search(rx, txt, re.I)]
        self.r.metrics.update(programs_docs=len(docs), programs_missing=missing)
        if not re.search(elems["наименование образовательной программы"], txt, re.I) and not docs:
            self.add(S, "Не указаны реализуемые образовательные программы", "high",
                     "Указать программы, сроки, форму и язык обучения, разместить тексты программ.", url,
                     check="programs_empty")
        elif missing:
            self.add(S, "Неполные сведения о реализуемых программах: нет — " + ", ".join(missing),
                     "medium" if len(missing) <= 2 else "high",
                     "Дополнить подраздел «Образование» недостающими сведениями.", url, check="programs_incomplete")
        if not docs:
            self.add(S, "В подразделе «Образование» не размещены тексты образовательных программ", "medium",
                     "Прикрепить файлы образовательных программ и учебных планов.", url, check="programs_no_docs")

    # ------------------------------------------------------------------ заглушки
    def check_placeholders(self) -> None:
        by_page = []
        for url, (section, title, text) in self.pages.items():
            hits = find_placeholders(text)
            if hits:
                by_page.append((url, section or title, hits))
        self.r.metrics["placeholder_pages"] = len(by_page)
        for url, section, hits in by_page[:12]:
            labels = sorted({h[0] for h in hits})
            self.add(section or "Сайт", "Не убран демонстрационный контент шаблона: " + "; ".join(labels), "high",
                     "Заменить заглушки реальной информацией или скрыть незаполненные блоки.", url,
                     [f"…{h[1]}…" for h in hits[:4]], "placeholders")
        if len(by_page) > 12:
            self.add("Сайт", f"Заглушки шаблона найдены ещё на {len(by_page) - 12} страницах", "high",
                     "Проверить сайт целиком и убрать демонстрационный контент.", self.base,
                     [u for u, _, _ in by_page[12:30]], "placeholders_more")
