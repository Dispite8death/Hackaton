import unittest

from bs4 import BeautifulSoup

from document_links import extract_documents


ROOT = "https://school.example/docs/"


def extract(html, url=ROOT):
    return extract_documents(BeautifulSoup(html, "html.parser"), url)


class DocumentLinksTest(unittest.TestCase):
    def test_repeated_controls_slider_and_catalogue_share_one_file_and_detail(self):
        result = extract("""
            <main><h1>Документы</h1>
            <div class="gw-document-item"><a class="gw-document-item__title"
                 href="../files/a.PDF?version=4&amp;key=2#page=1">Устав школы</a></div>
            <div class="gw-document-item"><a class="gw-document-item__title" href="ustav.html">Устав школы</a>
                 <a download href="../files/a.PDF?version=4&amp;key=2">Скачать 10 КБ</a>
                 <a href="../files/a.PDF?version=4&amp;key=2">Открыть в браузере</a>
                 <a download href="../files/a.PDF.sig">Скачать</a>
                 <div class="gw-icon-tooltip"><a download href="/signature">Электронная подпись</a></div></div>
            <div class="document-object-item"><a class="document-object-text" href="rules.html">
                 <span class="document-caption">Правила приёма</span></a>
                 <a href="get?key=1" download="rules.docx">Скачать</a></div>
            <div class="gw-document-item"><a class="gw-document-item__title" href="later.html">Отчёт за год</a></div>
            <aside><a href="/other.pdf">Меню</a></aside><div class="sidebar"><a href="/x.pdf">Боковой</a></div>
            <nav><a href="/menu.pdf">Файл меню</a></nav></main><footer><a href="/f.pdf">Контакты</a></footer>""")
        self.assertEqual(result["documents"], [
            {"url":"https://school.example/files/a.PDF?version=4&key=2", "title":"Устав школы",
             "title_source":"link", "format":"pdf", "detail_url":ROOT+"ustav.html"},
            {"url":ROOT+"get?key=1", "title":"Правила приёма", "title_source":"card",
             "format":"docx", "detail_url":ROOT+"rules.html"}])
        self.assertEqual(result["detail_urls"], [ROOT+"ustav.html",ROOT+"rules.html",ROOT+"later.html"])

    def test_netcat_pager_relative_paths_fragments_and_first_page(self):
        html = """<main><nav class="pagination">
             <li class="active"><a href="?cur_cc=9">1</a></li>
             <a href="?cur_cc=9&amp;curPos=20#documents">2</a>
             <a href="?cur_cc=9&amp;curPos=20">Следующая</a>
             <a aria-disabled="true" href="disabled/">3</a>
             <a href="?sort=title">Сортировка</a><a href="https://other.example/page/2">Чужой раздел</a>
             </nav><button class="gw-js-show-more-button" data-action-url="blocks/list.html"
             data-params="?curPos=40&amp;isNaked=1">Показать ещё</button>
             <aside><a href="?curPos=80">Архив сайта</a></aside></main>"""
        self.assertEqual(extract(html)["pagination_urls"],
                         [ROOT+"?cur_cc=9&curPos=20", ROOT+"blocks/list.html?curPos=40&isNaked=1"])

    def test_detail_page_uses_its_title_and_own_url_for_multiple_controls(self):
        result = extract("""<main>
            <h1>1 <span class="gw-title-with-icon-description__title">Устав МКОУ</span></h1>
            <div class="gw-current-document"><a download href="../files/scan.pdf">Скачать 1,2 МБ</a>
            <a href="../files/scan.pdf">Открыть в браузере</a></div></main>""", ROOT+"ustav.html")
        self.assertEqual(result["documents"], [
            {"url":"https://school.example/files/scan.pdf","format":"pdf","title":"Устав МКОУ",
             "title_source":"page_title","detail_url":ROOT+"ustav.html"}])
        self.assertEqual(result["detail_urls"], [])

    def test_uncaptioned_controls_do_not_borrow_other_documents_title(self):
        result = extract("""<main><h1>Документы</h1>
            <table><tr><td>Режим занятий</td><td><a href="regime.pdf">Скачать</a></td></tr></table>
            <p><a href="report.pdf">Скачать</a><a href="signed.p7s" download>Скачать</a></p>
            <li><a href="detail.html">Годовой отчёт</a></li>
            <a href="https://archive.example/a.xlsx">Бюджет школы</a></main>""")
        self.assertEqual([(d["title"],d["title_source"]) for d in result["documents"]],
                         [("Режим занятий","card"),("report.pdf","filename"),("Бюджет школы","link")])
        self.assertEqual(result["detail_urls"], [ROOT+"detail.html"])


if __name__=="__main__":
    unittest.main()
