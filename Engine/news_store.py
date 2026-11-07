"""Persist preview-card evidence and index the archived material available for news reviews."""

import argparse
import json
from contextlib import closing

if __package__:
    from .home_store import encoded
    from .render_store import incomplete_document
else:
    from home_store import encoded
    from render_store import incomplete_document


def index_news_page(conn, page_id):
    row = conn.execute("""
        SELECT p.url,p.text_page,r.rendered_html,r.details,b.page_kind,f.section
        FROM page_passport b JOIN page p ON p.id=b.page_id JOIN crawl_fetch f ON f.page_id=b.page_id
        LEFT JOIN page_render r ON r.page_id=b.page_id
        WHERE b.page_id=? AND b.page_kind IN ('home','news_listing','news_detail')
    """, (page_id,)).fetchone()
    if row is None:
        return False
    if __package__:
        from .news_cards import extract_news_page
    else:
        from news_cards import extract_news_page
    url, html, dom, details, kind, section = row
    use_dom = bool(dom) and not incomplete_document(json.loads(details))
    data = extract_news_page(dom if use_dom else html, url, section=section, page_kind=kind)
    cards = data.pop("cards")
    conn.execute("""
        INSERT INTO news_page VALUES(?,?,?,?,?,?)
        ON CONFLICT(page_id) DO UPDATE SET page_kind=excluded.page_kind,source=excluded.source,
            order_status=excluded.order_status,card_count=excluded.card_count,evidence=excluded.evidence
    """, (page_id, kind, "rendered_dom" if use_dom else "response_html", data["order_status"], len(cards), encoded(data)))
    conn.execute("DELETE FROM news_preview WHERE page_id=?", (page_id,))
    conn.executemany("""
        INSERT INTO news_preview(page_id,ordinal,url,title,date_status,date_text,date_iso,evidence)
        VALUES(?,?,?,?,?,?,?,?)
    """, ((page_id, card["ordinal"], card["url"], card["title"], card["date_status"], card["date_text"],
           card["date_iso"], encoded(card)) for card in cards))
    return True


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser = argparse.ArgumentParser(description="Повторно разобрать карточки новостей из сохранённых снимков")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--index-all", action="store_true")
    scope.add_argument("--run", type=int, help="ID выбранного обхода")
    args = parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        rows = conn.execute("""
            SELECT b.page_id FROM page_passport b JOIN crawl_fetch f ON f.page_id=b.page_id
            WHERE b.page_kind IN ('home','news_listing','news_detail')
                AND (? IS NULL OR f.run_id=?) ORDER BY b.page_id
        """, (args.run, args.run)).fetchall()
        for row in rows:
            with conn:
                index_news_page(conn, row[0])
        print(f"Снимков в реестре новостей обработано: {len(rows)}")


if __name__ == "__main__":
    main()
