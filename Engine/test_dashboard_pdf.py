from unittest.mock import patch
import unittest

import pypdfium2 as pdfium
from dashboard_pdf import build_pdf
import test_dashboard_data as fixtures


class FilteredPDFTest(unittest.TestCase):
    def test_selected_cyrillic_report_offline(self):
        fixture = fixtures.DashboardCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with patch("socket.socket.connect", side_effect=AssertionError("network call")):
            data = build_pdf(fixture.conn, {"kind":"kindergarten"}, organization_ids=[2])
        self.assertTrue(data.startswith(b"%PDF-"))
        with pdfium.PdfDocument(data) as doc:
            pages = [doc[i].get_textpage().get_text_bounded() for i in range(len(doc))]
        text = " ".join(pages)
        self.assertIn("Детский сад Ландыш",text)
        self.assertIn("Недостаточно данных",text)
        self.assertIn("персональные профили",text)
        self.assertNotIn("Школа Олимп",text)
        self.assertTrue(all(len(t)>150 for t in pages))
