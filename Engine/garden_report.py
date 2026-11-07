"""Показать проверяемые результаты главной страницы детского сада из архива."""

import argparse
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from urllib.parse import urldefrag


_SECTIONS = {"news":"новости", "events":"события", "team":"коллектив"}
_STATUS = {"html_saved":"HTML сохранён", "redirected_to_home":"переход на главную",
           "http_error":"ошибка запроса", "unvisited":"переход не сохранён"}
_KINDS = {"article":"публикация", "listing":"каталог", "navigation":"навигация"}


def _rows(conn, sql, args):
    cursor = conn.execute(sql, args)
    fields = [column[0] for column in cursor.description]
    return [dict(zip(fields, row)) for row in cursor]


def _source(conn, page_id):
    rows = _rows(conn, """
        SELECT p.id AS page_id,p.url AS home_url,p.date_loaded,f.url AS requested_home_url,
               r.id AS run_id,r.purpose,o.id AS org_id,o.name,o.site_url
        FROM crawl_fetch f JOIN page p ON p.id=f.page_id JOIN crawl_run r ON r.id=f.run_id
        JOIN organization o ON o.id=p.id_organization AND o.id=r.id_organization
        WHERE p.id=? AND f.depth=0 AND o.kind='kindergarten'
    """, (page_id,))
    if not rows:
        raise ValueError(f"Нет главной страницы детского сада с page_id={page_id}")
    return rows[0]


def _sections(conn, page_id):
    return {section: json.loads(evidence) for section, evidence in conn.execute(
        "SELECT section,evidence FROM garden_home_section WHERE page_id=?", (page_id,))}


def _attempts(conn, source):
    by_url = defaultdict(list)
    homes = {source["home_url"], source["requested_home_url"], source["site_url"]}
    for attempt in _rows(conn, """
        SELECT f.id AS fetch_id,f.url,f.final_url,f.page_id,f.section,f.http_status,f.error,p.url AS saved_url
        FROM crawl_fetch f JOIN crawl_run r ON r.id=f.run_id LEFT JOIN page p ON p.id=f.page_id
        WHERE f.run_id=? AND r.id_organization=? AND (f.page_id IS NULL OR p.id_organization=r.id_organization)
        ORDER BY f.id
    """, (source["run_id"],source["org_id"])):
        code = attempt["http_status"]
        if attempt["error"] or not attempt["page_id"] or code is not None and not 200 <= code < 300:
            status = "http_error"
        elif (attempt["final_url"] or attempt["saved_url"]) in homes and attempt["url"] not in homes:
            status = "redirected_to_home"
        else:
            status = "html_saved"
        attempt["status"] = status
        for key in {attempt["url"], attempt["final_url"]} - {None, ""}:
            by_url[key].append(attempt)
    return by_url


def _routes(sections, attempts, selection):
    groups = {}
    for section in selection:
        evidence = sections.get(section, {})
        for bucket, role in (("navigation_links","navigation"), ("listing_links","listing"), ("cards","article")):
            if section == "team" and role == "article":
                continue
            for item in evidence.get(bucket, []):
                url = item.get("url")
                if not url:
                    continue
                row = groups.setdefault(url, {"url":url,"sections":[],"roles":[],"references":[]})
                if section not in row["sections"]:
                    row["sections"].append(section)
                if role not in row["roles"]:
                    row["roles"].append(role)
                row["references"].append({"section":section,"role":role,"title":item.get("title"),
                                          "selector":item.get("selector"),"date_text":item.get("date_text"),
                                          "combined":item.get("combined", False)})
    rank = {"http_error":0, "redirected_to_home":1, "html_saved":2}
    for row in groups.values():
        row["kind"] = next(r for r in ("article","listing","navigation") if r in row["roles"])
        row["title"] = next((ref["title"] for ref in row["references"] if ref["role"] == row["kind"] and ref["title"]), "")
        visits = attempts.get(row["url"], [])
        best = max(visits, key=lambda v: (rank[v["status"]], v["fetch_id"])) if visits else None
        row.update(status=best["status"] if best else "unvisited", http_status=best["http_status"] if best else None,
                   page_ids=sorted({v["page_id"] for v in visits if v["page_id"] is not None}),
                   html_page_ids=sorted({v["page_id"] for v in visits if v["status"] == "html_saved"}),
                   attempts=visits)
    return list(groups.values())


def news_routes(conn: sqlite3.Connection, page_id: int) -> list[dict]:
    """One entry per exact URL on this homepage, retaining every news/events section and role."""
    source = _source(conn, page_id)
    return _routes(_sections(conn, page_id), _attempts(conn, source), ("news","events"))


_PATH_LABELS = {"direct":"с главной", "navigator":"через навигатор", "both":"оба пути",
                "unresolved":"путь не установлен"}
_NAV_LABELS = {"checked":"навигатор проверен", "not_saved":"навигатор не сохранён",
               "pending_index":"сведения о навигаторе ещё не сохранены", "no_entry":"вход в навигатор не найден",
               "unresolved":"переход в навигатор не подтверждён"}


def feed_discovery(conn, page_id):
    """Source paths to each feed index, using snapshots from this homepage's visit only."""
    home = _source(conn,page_id)
    sections = _sections(conn,page_id)
    attempts = _attempts(conn,home)
    indexed = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='site_navigator_page'").fetchone())

    def navigator(pid):
        row = conn.execute("SELECT is_navigator_page,evidence FROM site_navigator_page WHERE page_id=?",
                           (pid,)).fetchone() if indexed else None
        return (bool(row[0]), json.loads(row[1])) if row else (False, None)

    def listing(url):
        if url:
            url = urldefrag(url).url
            if url not in {home["home_url"],home["site_url"],home["requested_home_url"]}:
                return url
        return None

    routes = {}

    def add(url, section, source=None, reference=None):
        url = listing(url)
        if not url or section not in {"news","events"}:
            return
        route = routes.setdefault(url, {"url":url, "sections":[], "direct":[], "navigator":[]})
        if section not in route["sections"]:
            route["sections"].append(section)
        if source and reference not in route[source]:
            route[source].append(reference)

    def add_nav_links(doc, container, pid, map_url=None):
        for link in doc.get("links",[]):
            if link.get("container") == container and link.get("follow") and link.get("relation") == "section":
                add(link["url"],link["section"],"navigator",{
                    "kind":"inline_panel" if container=="panel" else "map_page","page_id":pid,"map_url":map_url,
                    "selector":link.get("selector"),"panel_selector":link.get("panel_selector"),"label":link.get("label")})

    _, home_nav = navigator(page_id)
    doc = home_nav or {}
    panel_selectors = {link["selector"] for link in doc.get("links",[]) if link.get("container")=="panel" and link.get("selector")}
    panel_roots = [p["selector"]+" > " for p in doc.get("panels",[]) if p.get("kind")=="panel" and p.get("selector")]
    for section in ("news","events"):
        for bucket in ("listing_links","navigation_links"):
            for link in sections.get(section,{}).get(bucket,[]):
                url = link.get("url")
                add(url,section)
                for selector in link.get("selectors") or [link.get("selector")]:
                    if (home_nav is not None and selector and selector not in panel_selectors
                            and not any(selector.startswith(root) for root in panel_roots)):
                        add(url,section,"direct",{"kind":bucket,"page_id":page_id,"selector":selector,
                                                 "label":link.get("title"),"locations":link.get("locations",[])})
    add_nav_links(doc,"panel",page_id)
    maps = []
    entries = defaultdict(list)
    for entry in doc.get("entries",[]):
        if entry.get("kind")=="map_link" and entry.get("url"):
            entries[entry["url"]].append(entry)
    for url, controls in entries.items():
        visits = attempts.get(url,[])
        saved = sorted({v["page_id"] for v in visits if v["status"]=="html_saved"})
        verified = []
        pending = False
        for pid in saved:
            actual_map, map_doc = navigator(pid)
            if map_doc is None:
                pending = True
            elif actual_map and map_doc.get("is_navigator_page"):
                verified.append(pid)
                add_nav_links(map_doc,"map_page",pid,url)
        state = "checked" if verified else "pending_index" if pending else "unresolved" if saved else "not_saved"
        maps.append({"url":url,"status":state,"controls":controls,"page_ids":saved,"verified_page_ids":verified})
    inline = any(panel.get("kind")=="panel" for panel in doc.get("panels",[]))
    if home_nav is None:
        nav_status = "pending_index"
    elif inline or any(m["status"]=="checked" for m in maps):
        nav_status = "checked"
    elif maps:
        nav_status = next(state for state in ("pending_index","not_saved","unresolved") if any(m["status"]==state for m in maps))
    else:
        nav_status = "unresolved" if doc.get("entries") else "no_entry"
    # A feed reached e.g. from «Наш детский сад» may have neither of the two requested entrance paths.
    for url, section in conn.execute("""
        SELECT DISTINCT l.url,l.section FROM crawl_link l JOIN crawl_fetch f ON f.id=l.fetch_id
        JOIN page p ON p.id=f.page_id WHERE f.run_id=? AND p.id_organization=?
          AND l.follow=1 AND l.relation='section' AND l.section IN ('news','events')
    """,(home["run_id"],home["org_id"])):
        if any(v["status"]=="html_saved" for v in attempts.get(url,[])):
            add(url,section)
    measurements = defaultdict(list)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='page_render' AND type='table'").fetchone():
        row = conn.execute("SELECT json_extract(details,'$.entry_links') FROM page_render WHERE page_id=?",(page_id,)).fetchone()
        for mark in json.loads(row[0]) if row and row[0] else []:
            if isinstance(mark,dict) and mark.get("url"):
                measurements[urldefrag(mark["url"]).url].append(mark)
    for entry in maps:
        entry["home_browser_links"] = measurements[entry["url"]]
    for route in routes.values():
        direct, via_nav = bool(route["direct"]), bool(route["navigator"])
        route["status"] = "both" if direct and via_nav else "direct" if direct else "navigator" if via_nav else "unresolved"
        route["label"] = _PATH_LABELS[route["status"]]
        # Geometry belongs to all measured homepage controls for this URL; it need not identify a panel vs a footer.
        route["home_browser_links"] = measurements[route["url"]]
    return {"status":"found" if any(r["direct"] or r["navigator"] for r in routes.values()) else "unresolved",
            "navigator_status":nav_status,"navigator_label":_NAV_LABELS[nav_status],"maps":maps,"routes":list(routes.values())}


def _excluded(sections):
    entries = {}
    counts = {}
    for name in ("news", "events"):
        evidence = sections.get(name, {}).get("evidence", {})
        if "excluded_count" in evidence:
            examples = evidence.get("excluded_examples", [])
            counts[name] = {"count":evidence["excluded_count"], "examples":len(examples)}
            for item in examples:
                key = item["selector"]
                entries.setdefault(key, item)
    return {"per_section":counts, "examples":list(entries.values()),
            "examples_complete":len(counts)==2 and all(x["count"]==x["examples"] for x in counts.values())}


def garden_report(conn, page_id):
    source = _source(conn, page_id)
    sections = _sections(conn, page_id)
    attempts = _attempts(conn, source)
    contacts = {}
    for field, status, evidence in conn.execute("SELECT field,status,evidence FROM garden_home_contact WHERE page_id=?", (page_id,)):
        contacts[field] = {"status":status, "values":[v["value"] for v in json.loads(evidence).get("values", [])]}
    return {"source":source,"contacts":contacts,"news_routes":_routes(sections, attempts, ("news","events")),
            "feed_discovery":feed_discovery(conn,page_id),
            "excluded":_excluded(sections), "staff_lists":_routes(sections, attempts, ("team",)),
            "news_analysis_available":all(s in sections for s in ("news","events"))}


def _show_route(row):
    sections = "/".join(_SECTIONS[s] for s in row["sections"])
    code = f", HTTP {row['http_status']}" if row["http_status"] is not None else ""
    pages = ", page_id " + ", ".join(map(str,row["page_ids"])) if row["page_ids"] else ""
    print(f"  {sections} · {_KINDS[row['kind']]} · {row['title']} · {_STATUS[row['status']]}{code}{pages}")
    print(f"    {row['url']}")


def _display(report):
    s = report["source"]
    print(f"{s['name']} · учреждение {s['org_id']}, снимок {s['page_id']}, обход {s['run_id']} ({s['purpose']})")
    for field, label in (("hours","Режим работы"),("address","Адрес"),("phone","Телефон"),("email","Почта")):
        entry = report["contacts"].get(field)
        value = "нет результата разбора" if entry is None else " | ".join(entry["values"]) or entry["status"]
        if entry and entry["values"] and entry["status"] != "found":
            value += f" ({entry['status']})"
        print(f"{label}: {value}")
    if report["news_analysis_available"]:
        print("Новости и события · уникальных ссылок на публикации:", sum(r["kind"]=="article" for r in report["news_routes"]))
    else:
        print("Разбор одного или обоих разделов новостей и событий отсутствует")
    discovery = report["feed_discovery"]
    print("Путь к ленте ·",discovery["navigator_label"])
    if not discovery["routes"]:
        print("  Путь не установлен")
    for route in discovery["routes"]:
        print(f"  {'/'.join(_SECTIONS[s] for s in route['sections'])} · {route['label']} · {route['url']}")
    for row in report["news_routes"]:
        _show_route(row)
    excluded = report["excluded"]
    if excluded["per_section"]:
        summarised = ", ".join(f"{_SECTIONS[s]} {v['count']}" for s,v in excluded["per_section"].items())
        sample = "" if excluded["examples_complete"] else " (по сохранённым примерам)"
        print(f"Исключено из разделов: {summarised}")
        print(f"Фотоплитки галереи: {sum(i['scope']=='photo_gallery' for i in excluded['examples'])}{sample}")
        print(f"Прочие исключённые плитки: {sum(i['scope']!='photo_gallery' for i in excluded['examples'])}{sample}")
    else:
        print("Сведения об исключённых плитках в этом разборе отсутствуют")
    for row in report["staff_lists"]:
        _show_route(row)


def main(argv=None):
    if __package__:
        from .config import DB_PATH
    else:
        from config import DB_PATH
    parser = argparse.ArgumentParser(description="Новости, события и контакты на главной странице детского сада")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--org", type=int, help="ID детсада, последний завершённый основной обход")
    scope.add_argument("--page", type=int, help="точный ID снимка главной страницы")
    parser.add_argument("--json", action="store_true", help="вывести структурированный отчёт")
    args = parser.parse_args(argv)
    try:
        with closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
            conn.execute("BEGIN")
            page = args.page
            if page is None:
                row = conn.execute("""
                    SELECT f.page_id FROM crawl_fetch f WHERE f.depth=0 AND f.page_id IS NOT NULL
                    AND f.run_id=(SELECT max(id) FROM crawl_run WHERE id_organization=? AND purpose='audit'
                                  AND finished_at IS NOT NULL) ORDER BY f.id LIMIT 1
                """, (args.org,)).fetchone()
                if row is None:
                    raise ValueError("В последнем завершённом основном обходе нет снимка главной")
                page = row[0]
            report = garden_report(conn, page)
        if args.json:
            print(json.dumps(report,ensure_ascii=False,indent=2))
        else:
            _display(report)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
