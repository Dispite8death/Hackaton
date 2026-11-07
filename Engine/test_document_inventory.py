import sqlite3
import unittest

from config import SCHEMA_SQL
from document_inventory import _classify, document_inventory
from institution_audit import audit_run, page_observations, reference_report


class DocumentInventoryTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        for org,kind in ((1,"school"),(2,"school"),(3,"kindergarten")):
            self.db.execute("INSERT INTO organization VALUES(?,?,?,?)", (org,"Учреждение "+str(org),self.url(org),kind))
            self.db.execute("""INSERT INTO crawl_run(id,id_organization,state,finished_at,stop_reason)
                               VALUES(?,?,'partial',1,'page_limit')""",(org,org))
            self.page(org,org,"home","",'<header><a href="documents/">Документы</a></header>')
        self.page(11,1,"documents","documents/", """
            <main><h1>Документы</h1><a href="a.pdf">Устав школы №1</a>
            <a href="r.docx">ПРАВИЛА внутреннего трудового распорядка</a>
            <a href="p.pdf">Положение о кабинете химии</a>
            <a href="https://files.example/law.pdf">Федеральный закон</a>
            <nav class="pagination"><a href="?page=2">2</a></nav></main>""")
        self.page(12,1,"staff_resources","teachers/", """
            <main><a href="../documents/a.pdf">Устав школы №1</a>
            <a href="handbook.pdf">Памятка для сотрудников</a></main>""")
        self.db.execute("INSERT INTO crawl_link VALUES(12,?,'Скачать','staff_resources','document',0)",
                        (self.url(1)+"teachers/handbook.pdf",))
        self.page(21,2,"documents","documents/", """
            <main><a href="/charter.pdf">Устав школы №2</a><a href="rules.pdf">Правила внутреннего трудового распорядка.</a>
            <a href="/p2.pdf">Положение о премировании</a><a href="https://files.example/law.pdf">Общий закон</a></main>""")
        self.db.execute("""INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,http_status,error)
                           VALUES(31,3,?,'documents',1,1,404,'404')""",(self.url(3)+"documents/",))
        self.db.execute("INSERT INTO crawl_link VALUES(3,?,'Документы','documents','section',1)",
                        (self.url(3)+"documents/",))
        for run in (1,2,3):
            audit_run(self.db,run)

    def url(self, org):
        return f"https://institution{org}.example/"

    def page(self, pid, org, section, path, html):
        url = self.url(org)+path
        self.db.execute("INSERT INTO page VALUES(?,?,?,1,?)", (pid,url,org,html))
        self.db.execute("""INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,http_status,page_id)
                           VALUES(?,?,?,?,?,1,200,?)""",(pid,org,url,section,int(bool(path)),pid))

    def test_cross_type_reference_and_match_reasons(self):
        report = reference_report(self.db,"school")
        self.assertEqual(report["documents"]["reference_organization_id"],1)
        self.assertEqual(report["documents"]["catalog_files"],8)
        self.assertEqual(report["documents"]["other_files"],1)
        candidate = next(row for row in report["candidates"] if row["organization_id"]==2)["documents"]
        self.assertEqual({g["group"] for g in candidate["matched_groups"]},{"charter","employee_rules"})
        self.assertEqual(len(candidate["matching_titles"]),1)
        self.assertEqual(candidate["matching_titles"][0]["institution"]["title"],"Правила внутреннего трудового распорядка.")
        self.assertEqual(candidate["matching_titles"][0]["reference"]["format"],"docx")
        self.assertEqual(candidate["matching_urls"][0]["institution"]["url"],"https://files.example/law.pdf")
        self.assertIn("Положение о кабинете химии",[d["title"] for d in candidate["reference_titles_unseen"]])
        self.assertIn("Положение о премировании",[d["title"] for d in candidate["additional_titles"]])
        garden = reference_report(self.db,"kindergarten")
        self.assertEqual(garden["documents"]["reference_items"],[])
        cov = garden["reference"]["documents"]["coverage"]
        self.assertEqual((cov["status"],cov["catalog_routes"][0]["http_status"]),("unseen",404))

    def test_inventory_sources_scope_and_unvisited_navigation(self):
        inv = document_inventory(self.db,1)
        self.assertEqual((inv["catalog_files"],inv["other_files"]),(4,1))
        charter = next(d for d in inv["items"] if d["group"]=="charter")
        self.assertEqual(charter["source_page_ids"],[11,12])
        self.assertEqual(charter["type"],"устав")
        self.assertEqual(inv["coverage"]["unvisited_pagination"],[self.url(1)+"documents/?page=2"])
        self.assertEqual(inv["coverage"]["status"],"partial")
        self.assertFalse(page_observations(self.db,12)["documents"]["catalog"])

    def test_accredited_experts_are_not_school_accreditation(self):
        title = "Реестр независимых экспертов, получивших аккредитацию на проведение антикоррупционной экспертизы"
        self.assertEqual(_classify({"title":title})["group"],"anti_corruption")
        self.assertEqual(_classify({"title":"Свидетельство о государственной аккредитации"})["group"],"accreditation")

    def test_reindex_removes_stale_file_and_distinguishes_new_snapshots(self):
        self.db.execute("UPDATE page SET text_page='<main>Переехали</main>' WHERE id=11")
        audit_run(self.db,1)
        first = document_inventory(self.db,1)
        second = document_inventory(self.db,2)
        self.assertEqual(first["catalog_files"],0)
        self.assertEqual(second["catalog_files"],4)
        self.assertEqual(first["other_files"],2)


if __name__=="__main__":
    unittest.main()
