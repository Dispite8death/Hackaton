"""Download HTML snapshots and record navigation for one or more institutions."""

import argparse
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from dataclasses import dataclass

if __package__:
    from .config import connect_db
    from .attached_files import download_pending
    from .coverage_report import SECTION_LABELS, STATES, coverage, coverage_counts
    from .crawl_store import finish_run, initialize_db, record_fetch, select_organizations, start_run
    from .factcheck_batch import batch_check
    from .model_settings import setting
    from .news_window import filter_publication_links,skip_queued_url
    from .navigation import FEEDS, REQUIRED_SECTIONS, STAFF_DIRECTORIES, discover_links, find_section_headings, resolve_url, with_navigator_links
    from .scan_staff import save_snapshot_staff
    from .site_navigator import discover_site_navigator
else:
    from config import connect_db
    from attached_files import download_pending
    from coverage_report import SECTION_LABELS, STATES, coverage, coverage_counts
    from crawl_store import finish_run, initialize_db, record_fetch, select_organizations, start_run
    from factcheck_batch import batch_check
    from model_settings import setting
    from news_window import filter_publication_links,skip_queued_url
    from navigation import FEEDS, REQUIRED_SECTIONS, STAFF_DIRECTORIES, discover_links, find_section_headings, resolve_url, with_navigator_links
    from scan_staff import save_snapshot_staff
    from site_navigator import discover_site_navigator


@dataclass(frozen=True)
class Target:
    url: str
    section: str
    relation: str
    depth: int


@dataclass(frozen=True)
class Result:
    run_id: int
    loaded: int
    errors: int
    state: str
    reason: str


def crawl_organization(org, *, max_pages=32, max_depth=3, timeout=12, fetch=None, capture=None,
                       purpose=None, archive_photos=None, render_wait=45, news_only=False, archive_staff=None,
                       archive_files=None, db=None, news_months=12, news_as_of=None, before_page=None):
    """Normal visits capture PDF, both screenshots and DOM. Tests can supply a transport."""
    purpose = purpose or ("sample" if news_only else "audit")
    if fetch is None and capture is None:
        if __package__:
            from .browser_capture import BrowserCapture
            from .home_image_files import archive_home_images
            from .staff_photo_assets import archive_staff_photos
        else:
            from browser_capture import BrowserCapture
            from home_image_files import archive_home_images
            from staff_photo_assets import archive_staff_photos
        with BrowserCapture(pdf=True, ready_timeout=render_wait) as browser:
            return _crawl(org, max_pages, max_depth, timeout, None, browser, purpose,
                          archive_photos or archive_home_images, news_only, archive_staff or archive_staff_photos,
                          download_pending if archive_files is None else archive_files, db,news_months,news_as_of,before_page)
    return _crawl(org, max_pages, max_depth, timeout, fetch, capture, purpose, archive_photos, news_only, archive_staff,
                  archive_files, db,news_months,news_as_of,before_page)


def _crawl(org, max_pages, max_depth, timeout, fetch, capture, purpose, archive_photos, news_only, archive_staff,
           archive_files=None, db=None, news_months=12,news_as_of=None,before_page=None):
    site = resolve_url(org["site_url"], "")
    if not site:
        raise ValueError(f"Некорректный адрес учреждения {org['id']}")
    pending = {site: Target(site, "home", "section", 0)}
    visited, beyond_depth = set(), set()
    required = set(REQUIRED_SECTIONS[org["kind"]])
    reached_sections = set()
    section_aliases = defaultdict(set)
    loaded_urls = set()
    section_visits = Counter()
    prioritize_feeds = news_only or org["kind"] == "kindergarten"
    listed_articles, sampled_feeds, listing_pages = set(), set(), set()
    loaded_feeds = set()
    loaded = errors = 0
    reasons = []

    def priority(target):
        missing = required - reached_sections
        if prioritize_feeds and target.section in FEEDS and target.relation == "section":
            rank = -3
        elif prioritize_feeds and target.section == "site_navigator" and FEEDS - loaded_feeds:
            rank = -2
        elif (prioritize_feeds and target.url in listed_articles and target.relation == "detail"
              and target.section not in sampled_feeds):
            rank = -2
        elif news_only and (target.relation == "pagination" or target.section == "site_navigator"):
            rank = -1
        elif target.relation == "section" and target.section in missing:
            rank = 0
        elif missing and target.section in {"information", "about", "hub", "site_navigator"} and target.relation != "detail":
            rank = 1
        elif (target.section in STAFF_DIRECTORIES or (prioritize_feeds and target.section in FEEDS)) and target.relation == "pagination":
            rank = 3
        else:
            rank = {"section": 2, "navigation": 3, "detail": 4, "pagination": 5}[target.relation]
        return rank, section_visits[target.section], target.depth

    with (closing(connect_db()) if db is None else nullcontext(db)) as conn:
        run = start_run(conn, org["id"], max_depth=max_depth, max_pages=max_pages, purpose=purpose)
        try:
            while pending and loaded + errors < max_pages:
                if before_page:
                    before_page(run)
                current = min(pending.values(), key=priority)
                del pending[current.url]
                visited.add(current.url)
                if current.section=="news" and current.relation in ("pagination","detail") and skip_queued_url(
                        conn,org["id"],current.url,current.section,months=news_months,as_of=news_as_of):
                    continue
                section_visits[current.section] += 1
                if current.depth:
                    time.sleep(0.15)
                if capture is None:
                    response = fetch(current.url, site, timeout)
                else:
                    response = capture.fetch(current.url, site, timeout, section=current.section)

                links, headings = [], []
                if response.html is not None:
                    render = response.render
                    rendered = (render and render.details.get("document_ready_state") != "loading" and
                                render.details.get("document_complete_for_extraction") is not False)
                    document = (render.rendered_html if rendered else None) or response.html
                    links = discover_links(document, response.final_url, site, org["kind"], current.section)
                    navigator = discover_site_navigator(document, response.final_url, site, org["kind"],
                                                        is_map_page=current.section == "site_navigator")
                    links = with_navigator_links(links, navigator)
                    for link in links:
                        if link.relation == "pagination_unresolved":
                            reason = "news_pagination_unknown" if link.section in FEEDS else "staff_pagination_unknown"
                            if reason not in reasons:
                                reasons.append(reason)
                    headings = find_section_headings(document, org["kind"], current.section)
                    reached_sections.update(section for section, _ in headings)
                    loaded += 1
                    if current.relation == "section":
                        reached_sections.add(current.section)
                    if current.relation == "detail" and current.url in listed_articles:
                        sampled_feeds.add(current.section)
                    final_url = resolve_url(response.final_url, "")
                    visited.add(final_url)
                    loaded_urls.update((current.url, final_url))
                    aliases = section_aliases[current.url] | section_aliases[final_url]
                    reached_sections.update(aliases)
                    if current.relation == "section":
                        loaded_feeds.update(FEEDS.intersection(aliases | {current.section}))
                    pending.pop(final_url, None)
                else:
                    errors += 1
                fetch_id = record_fetch(conn, run, current.url, current.section, current.depth,
                             final_url=response.final_url, status=response.status, error=response.error,
                             html=response.html, links=links, headings=headings, loaded_at=response.loaded_at,
                             render=response.render)
                if response.html:
                    snapshot = conn.execute("SELECT page_id FROM crawl_fetch WHERE id=?", (fetch_id,)).fetchone()[0]
                    if any(link.section=="news" and link.relation in ("pagination","detail") for link in links):
                        links = filter_publication_links(conn,source_page_id=snapshot,links=links,
                            organization_id=org["id"],months=news_months,as_of=news_as_of)
                    if ((current.section in STAFF_DIRECTORIES or current.section == "home")
                            and getattr(response.render, "staff_cards", None) is None):
                        save_snapshot_staff(conn, snapshot, response.final_url, current.section)
                    if not news_only:
                        if archive_photos and current.depth == 0:
                            archive_photos(conn, snapshot, timeout=timeout)
                        if archive_staff:
                            archive_staff(conn, snapshot, timeout=timeout)
                if response.status == 429:
                    reasons.append("rate_limited")
                    break
                for link in links:
                    if (news_only and link.section != "site_navigator" and
                            (link.section not in FEEDS or link.relation not in {"section", "detail", "pagination"})):
                        continue
                    if current.relation == "detail":
                        staff_directory = link.section in STAFF_DIRECTORIES and link.relation in {"section", "pagination"}
                        feed_index = prioritize_feeds and current.section in FEEDS and link.section in FEEDS and link.relation == "section"
                        if not (staff_directory or feed_index or (prioritize_feeds and link.section == "site_navigator")):
                            continue
                    if prioritize_feeds and link.follow and link.section in FEEDS:
                        if link.relation == "detail" and current.section in FEEDS:
                            listed_articles.add(link.url)
                        elif link.relation == "pagination":
                            listing_pages.add(link.url)
                    if link.relation == "section" and link.follow:
                        section_aliases[link.url].add(link.section)
                        if link.url in loaded_urls:
                            reached_sections.add(link.section)
                            if link.section in FEEDS:
                                loaded_feeds.add(link.section)
                    if not link.follow or link.url in visited:
                        continue
                    if current.depth >= max_depth:
                        beyond_depth.add(link.url)
                        continue
                    candidate = Target(link.url, link.section, link.relation, current.depth + 1)
                    previous = pending.get(link.url)
                    if previous is None or priority(candidate) < priority(previous):
                        pending[link.url] = candidate
            if pending and loaded + errors >= max_pages:
                reasons.append("page_limit")
            if beyond_depth - visited:
                reasons.append("depth_limit")
            if listing_pages - visited:
                reasons.append("news_pagination_unvisited")
            if news_only and not loaded_feeds:
                reasons.append("news_listing_unreached")
            if errors:
                reasons.append("fetch_errors")
            state = "failed" if not loaded else ("partial" if reasons else "completed")
            reason = ",".join(reasons) or "frontier_exhausted"
            finish_run(conn, run, state, reason=reason)
        except BaseException as error:
            finish_run(conn, run, "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                       reason=type(error).__name__)
            raise
        if archive_files:
            archive_files(conn,run_id=run)
        return Result(run, loaded, errors, state, reason)


def organizations(selectors=None):
    with closing(connect_db()) as conn:
        initialize_db(conn)
        return select_organizations(conn, selectors)


def LoadOnePage(name: str) -> bool:
    """Keep the original single-organization entry point."""
    result = crawl_organization(organizations([name])[0])
    return result.loaded > 0


def print_coverage(selected, summary):
    labels = {"page": "сохранена страница", "heading": "найден заголовок",
              "link": "найдена ссылка", "unseen": "пока не найдено"}
    runs = []
    with closing(connect_db()) as conn:
        for org in selected:
            run = conn.execute(
                "SELECT id, state FROM crawl_run WHERE id_organization = ? AND purpose='audit' ORDER BY id DESC LIMIT 1",
                (org["id"],),
            ).fetchone()
            if run is None:
                print(f"ID {org['id']}: ещё не загружали")
                continue
            runs.append((run["id"], org["kind"]))
            if not summary:
                print(f"{org['name']} (обход {run['id']}, {run['state']})")
                for role, state in coverage(conn, run["id"], org["kind"]).items():
                    print(f"  {SECTION_LABELS[role]} — {labels[state]}")
        if summary:
            for role, counts in coverage_counts(conn, runs).items():
                evidence = ", ".join(f"{labels[state]} {counts[state]}" for state in STATES)
                print(f"{SECTION_LABELS[role]} — {evidence}")


def main(*, factcheck_fetch=None) -> int:
    parser = argparse.ArgumentParser(description="Загрузить главные страницы и разделы сайтов учреждений.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--org", nargs="+", metavar="ID", help="ID или полные названия одного или нескольких учреждений")
    mode.add_argument("--all", action="store_true", help="обойти все учреждения")
    mode.add_argument("--list", action="store_true", help="показать ID, тип и название учреждений")
    parser.add_argument("--coverage", action="store_true", help="показать признаки разделов по последнему обходу из БД")
    parser.add_argument("--render", action="store_true", help="совместимость: браузерный захват уже включён")
    parser.add_argument("--render-wait", type=float, default=45, help="сколько секунд ждать построения DOM в браузере")
    parser.add_argument("--pdf", action="store_true", help="совместимость: PDF уже включён для каждой HTML-страницы")
    parser.add_argument("--sample", action="store_true", help="пометить пробный обход отдельно от основных отчётов")
    parser.add_argument("--news-only", action="store_true",
                        help="обойти главную, навигатор сайта, разделы новостей и событий, страницы лент и публикации; сохранить как пробный обход")
    parser.add_argument("--factcheck", action="store_true",
                        help="после обхода запустить внешнюю проверку публикаций из этого обхода. "
                             "Размер партии задаётся в окне администратора (изначально 3). "
                             "Без флага проверка запускается только при включённой автонастройке")
    parser.add_argument("--max-pages", type=int, default=32, help="лимит запросов страниц на учреждение, включая главную")
    parser.add_argument("--max-depth", type=int, default=3, help="предельное число переходов от главной")
    parser.add_argument("--workers", type=int, default=3, help="число одновременно обрабатываемых учреждений")
    parser.add_argument("--news-months",type=int,default=12,help="сколько календарных месяцев новостей загружать")
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_depth < 0 or args.workers < 1:
        parser.error("Лимиты страниц и потоков должны быть положительными, глубина — от нуля")
    if not 1<=args.news_months<=1200:
        parser.error("Период новостей должен быть от 1 до 1200 месяцев")
    if not 0 < args.render_wait <= 300:
        parser.error("--render-wait должен быть больше 0 и не более 300 секунд")
    if args.coverage and args.list:
        parser.error("Для покрытия выберите --org или --all")
    if (args.list or args.coverage) and (args.render or args.pdf or args.sample or args.news_only or args.factcheck):
        parser.error("Параметры захвата применяются только при загрузке страниц")
    try:
        selected = organizations(args.org)
    except ValueError as error:
        parser.error(str(error))
    if args.list:
        for org in selected:
            print(f"{org['id']:3}  {org['kind']:12}  {org['name']}")
        return 0
    if args.coverage:
        print_coverage(selected, summary=args.all)
        return 0

    def run(org):
        result = crawl_organization(org, max_pages=args.max_pages, max_depth=args.max_depth,
                                    purpose="sample" if args.sample or args.news_only else "audit",
                                    render_wait=args.render_wait, news_only=args.news_only,news_months=args.news_months)
        print(f"ID {org['id']}, обход {result.run_id}: HTML {result.loaded}, ошибок {result.errors}, {result.reason}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=min(args.workers, len(selected))) as pool:
        results = list(pool.map(run, selected))
    with closing(connect_db()) as conn:
        enabled = args.factcheck or setting(conn, "factcheck_auto", False)
        checks = batch_check(conn, [r.run_id for r in results], limit=None if enabled else 0, fetch=factcheck_fetch)
    print(f"Фактчекинг публикаций: добавлено {checks['queued']}, проверено {checks['processed']}, "
          f"ошибок {checks['errors']}, осталось {checks['remaining']}", flush=True)
    return int(any(result.errors for result in results) or bool(checks["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
