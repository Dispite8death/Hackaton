import unittest

from bs4 import BeautifulSoup
from news_cards import extract_news_page


PAGE="https://school.example/news/"


def card(slug,title,field="",extra=""):
    return (f'<a class="card" href="{slug}"><div class="card-subtitle">{field}</div>'
            f'<h3>{title}</h3><p>{extra}</p></a>')


def scan(*cards):
    return extract_news_page('<main><section class="gw-news">'+''.join(cards)+'</section></main>',PAGE)


class NewsPreviewTests(unittest.TestCase):
    def test_one_visible_date_source_does_not_double_count_datetime_or_hidden_text(self):
        markup=card("first","Первый",'<time datetime="2026-11-04">04 ноября 2026 г.</time>',
                    '<span hidden>03 ноября 2026</span>')
        row,=scan(markup)["cards"]
        self.assertEqual((row["ordinal"],row["date_status"],row["date_iso"]),(0,"valid","2026-11-04"))
        proof=row["date_evidence"]
        self.assertEqual(len(proof["occurrences"]),1)
        self.assertEqual(proof["sources"][0]["technical_datetimes"],["2026-11-04"])
        self.assertEqual(proof["sources"][0]["text"][proof["occurrences"][0]["start"]:proof["occurrences"][0]["end"]],
                         row["date_text"])
        self.assertEqual(len(BeautifulSoup('<main><section class="gw-news">'+markup+'</section></main>',
                                           "html.parser").select(row["selector"])),1)
        incomplete,=scan(card("x","Заметка",'<time datetime="2026-11-04">4 ноября</time>'))["cards"]
        self.assertEqual((incomplete["date_status"],incomplete["date_iso"]),("incomplete_date",None))

    def test_independent_mentions_including_unrelated_holiday_are_all_recorded(self):
        r=scan(card("holiday","Письмо солдату","05 февраля 2025 г.","Участие в акции к 23 февраля"),
               card("second","Дубль","04.11.2026", "04.11.2026"),
               card("third","Без года","04.11"),
               card("fourth","Без даты","", "Время встречи 18:00"),
               card("fifth","Ошибка","31 февраля 2026"))
        self.assertEqual([c["date_status"] for c in r["cards"]],
                         ["multiple_dates","multiple_dates","incomplete_date","missing_date","invalid_date"])
        self.assertTrue(all(c["date_iso"] is None for c in r["cards"]))
        first=r["cards"][0]["date_evidence"]
        self.assertEqual([v["role"] for v in first["sources"]],["date_field","preview_text"])
        self.assertEqual([v["date_iso"] for v in first["occurrences"]],["2025-02-05",None])
        self.assertEqual([v["position"] for v in r["cards"][1]["date_evidence"]["occurrences"]],[0,1])

    def test_bare_day_month_or_year_never_supply_a_full_preview_date(self):
        for snippet in ("7","ноябрь","ноября 2026","10.11","2026"):
            with self.subTest(snippet=snippet):
                preview, = scan(card("a","Объявление",snippet))["cards"]
                self.assertEqual((preview["date_status"],preview["date_iso"]),("incomplete_date",None))

    def test_stable_order_ties_gaps_and_explicit_inversions(self):
        one=card("a","Один","2026-11-04")
        same=card("b","Два","04.11.2026")
        later=card("c","Три","5 ноября 2026")
        no=card("d","Четыре")
        self.assertEqual(scan()["order_status"],"insufficient_cards")
        self.assertEqual(scan(one)["order_status"],"insufficient_cards")
        self.assertEqual(scan(one,same)["order_status"],"newest_first")
        wrong=scan(one,later)
        self.assertEqual(wrong["order_status"],"out_of_order")
        self.assertEqual(wrong["inversions"][0]["newer_ordinal"],1)
        gaps=scan(one,no,later)
        self.assertEqual(gaps["order_status"],"partial")
        self.assertEqual(gaps["order_evidence"]["unresolved_ordinals"],[1])
        self.assertEqual(gaps["inversions"][0]["previous_ordinal"],0)
        self.assertEqual(gaps["inversions"][0]["current_ordinal"],2)

    def test_only_linked_local_article_and_no_detail_body_preview(self):
        html="""<main><h1>Новости</h1><div class="gw-news"><div class="gw-news-item">
          <div class="gw-news-item__image"><a href="/photo.jpg"><img src="/photo.jpg"></a></div>
          <span class="gw-news-item__date">03 июня 2026, 01:18</span>
          <div class="gw-news-item__title"><a class="gw-news-item__title-link" href="/news/one.html">
            Объявление</a></div></div>
          <figure><img src="/slide.jpg"><figcaption>8 марта</figcaption></figure></div>
          <section><article><h3>Жизнь школы 1 апреля 2026</h3></article></section></main>"""
        r=extract_news_page(html,PAGE)
        self.assertEqual([(c["url"],c["date_iso"]) for c in r["cards"]],[(PAGE+"one.html","2026-06-03")])
        self.assertEqual(r["excluded"][0]["reasons"][0]["reason"],"no_detail_link")
        detail=extract_news_page(html,PAGE+"one.html",page_kind="news_detail")
        self.assertEqual((detail["cards"],detail["order_status"]),([],"not_applicable"))

    def test_photo_album_and_link_back_to_listing_are_not_previews(self):
        html="""<main><div class="news-list">
          <a class="card" href="/news/"><img alt="Новости" src="/pic.jpg"></a>
          <figure><a href="/gallery/may"><img src="/photo.jpg"></a><figcaption>9 мая!</figcaption></figure>
          <a class="card" href="/photo.jpg"><img alt="Фото группы" src="/photo.jpg"></a>
          </div></main>"""
        data=extract_news_page(html,PAGE)
        self.assertEqual(data["cards"],[])
        self.assertEqual(len(data["excluded"]),3)


if __name__=="__main__":
    unittest.main()
