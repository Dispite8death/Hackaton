import sqlite3
import unittest

from config import SCHEMA_SQL
from institution_audit import audit_run, institution_report, page_observations, reference_report, INSTITUTION_SCHEMA_SQL
from institution_contacts import extract_contacts
from schedule_evidence import extract_schedule


class ContactEvidenceTest(unittest.TestCase):
    def test_plain_header_values_without_anchors_do_not_require_template_classes(self):
        html="<header><p>+7 (4132) 12-34-56</p><span>school@one.test</span><div>ул. Лесная, 19</div></header>"
        v=extract_contacts(html,"https://a.example",section="home")
        self.assertEqual({i["field"] for i in v},{"address","phone","email"})
        self.assertTrue(all(i["location"]=="header" for i in v))

    def test_school_field_boundaries_and_combined_hours(self):
        school = """<main><h1>Контакты</h1><div>
            <div><div class="tpl-component-gw-base-title"><div class="tpl-text-header6">Часы приема граждан</div></div></div>
            <div><article></article></div>
            <div><div class="tpl-component-gw-base-title"><div class="tpl-text-header6">Режим работы</div></div></div>
            <div><article>Пн–пт 08:00–18:00</article></div></div>
            <p>Адрес: г. Магадан ул. Лесная, 1</p></main>"""
        values=extract_contacts(school,"https://a.example/contacts",section="contacts")
        self.assertTrue(any(v["field"]=="hours" and "18:00" in v["value"] for v in values))
        self.assertFalse(any(v["field"]=="reception" for v in values))
        combined="""<header><div class="intro-contacts"><p>Режим работы и часы приёма: по будням 09:00–17:00</p>
            </div></header><footer><a href="mailto:other@site.test">other@site.test</a></footer>"""
        values=extract_contacts(combined,"https://a.example",section="home")
        picked=[(v["field"],v["location"]) for v in values]
        self.assertIn(("hours","under_header"),picked)
        self.assertIn(("reception","under_header"),picked)
        self.assertIn(("email","footer"),picked)
        sources=[v for v in values if v["field"] in ("hours","reception")]
        self.assertEqual(len({v["selector"] for v in sources}),1)
        self.assertTrue(all("09:00" in v["block_text"] for v in sources))

    def test_reception_office_and_staff_hours_are_distinct(self):
        html="""<main><div><div class="gw-content-small-title">Часы работы приемной</div><div>среда 10:00–15:00</div></div>
            <div><div class="gw-content-small-title">Режим работы</div><div>Пн–пт 08:00–18:00</div>
                 <div>График работы специалистов</div><div>Вторник 13:00–18:00</div></div></main>"""
        v=extract_contacts(html,"https://a.example/contacts",section="contacts")
        self.assertTrue(any(i["field"]=="reception" and "15:00" in i["value"] for i in v))
        self.assertTrue(any(i["field"]=="hours" and "08:00" in i["value"] and "13:00" not in i["value"] for i in v))
        self.assertFalse(any(i["field"]=="hours" and "15:00" in i["value"] for i in v))


class ScheduleEvidenceTest(unittest.TestCase):
    def test_files_and_scans_are_candidates_even_with_empty_widget(self):
        doc="""<main><h1>Расписание</h1>
             <div class="document-object-item"><span class="document-caption">Расписание 5 класса</span>
               <a href="/r.pdf" download>Скачать</a><img src="/icon/docx.svg"></div>
             <figure><figcaption>Расписание для 6 класса</figcaption><img src="/scans/6.png"></figure>
             <img src="/banner.png"><p>Расписание на этот год не заполнено</p></main>"""
        r=extract_schedule(doc,"https://school.test/schedule/")
        self.assertEqual((r["status"],r["content_verdict"]),("material_candidate","pending_review"))
        self.assertEqual({m["kind"] for m in r["materials"]},{"file","image"})
        self.assertEqual(len(r["materials"]),2)
        self.assertTrue(r["empty_notice"])

    def test_calendar_table_and_class_selector_are_not_lesson_rows(self):
        base="<main><h1>Расписание</h1><div class='gw-schedule'><select name='class'></select></div>"
        r=extract_schedule(base+"<table><tr><td>Понедельник 17</td></tr></table></main>","https://school.test/schedule/")
        self.assertEqual(r["status"],"class_selection_required")
        r=extract_schedule(base+"<table><tr><td>Математика</td><td>9:10</td></tr></table></main>","https://school.test/schedule/")
        self.assertEqual(r["status"],"lesson_rows")


class InstitutionAuditTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL+INSTITUTION_SCHEMA_SQL)
        self.db.executescript("""
            INSERT INTO organization VALUES(1,'Школа А','https://one.test/','school'),
                  (2,'Нет страницы','https://two.test/','school'),(3,'Сад','https://three.test/','kindergarten');
            INSERT INTO crawl_run(id,id_organization,finished_at,state,purpose) VALUES
                 (1,1,100,'partial','audit'),(2,2,100,'failed','audit'),(3,3,100,'completed','audit'),
                 (4,1,101,'completed','sample');
        """)
        self.home='<header><a href="/schedule/">Расписание</a><a href="/contacts/">Контакты</a></header>'
        self.page(1,1,1,"home","https://one.test/",self.home)
        self.page(2,4,1,"schedule","https://one.test/schedule/","<main>Архив другого визита</main>")
        self.page(3,3,3,"home","https://three.test/","<header><address>ул. Лесная, д. 1</address></header>")
        self.db.execute("INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,http_status,error) VALUES(1,'https://one.test/contacts/','contacts',1,99,404,'404')")

    def page(self,pid,run,org,section,url,html,final=None):
        self.db.execute("INSERT INTO page(id,url,id_organization,text_page) VALUES(?,?,?,?)",(pid,final or url,org,html))
        self.db.execute("""INSERT INTO crawl_fetch(run_id,url,final_url,section,depth,fetched_at,page_id,http_status)
            VALUES(?,?,?,?,?,100,?,200)""",(run,url,final or url,section,0 if section=="home" else 1,pid))

    def test_scope_http_failures_unvisited_and_new_evidence(self):
        r=audit_run(self.db,1)
        self.assertEqual(r["contacts"]["routes"][0]["status"],"http_error")
        self.assertEqual(r["schedule"]["routes"][0]["status"],"unvisited")
        self.assertEqual(r["schedule"]["pages"],[])
        self.page(4,1,1,"schedule","https://one.test/schedule/","<main><h1>Расписание</h1><a href='/classes.pdf'>Расписание</a></main>")
        audit_run(self.db,1)
        r=institution_report(self.db,1)
        self.assertEqual(r["schedule"]["routes"][0]["status"],"page_saved")
        self.assertEqual(r["schedule"]["pages"][0]["status"],"material_candidate")
        self.assertEqual(page_observations(self.db,4)["schedule"]["materials"][0]["kind"],"file")
        self.assertFalse(page_observations(self.db,2))
        self.db.execute("UPDATE crawl_fetch SET final_url='https://one.test/' WHERE page_id=4")
        r=audit_run(self.db,1)
        self.assertEqual(r["schedule"]["routes"][0]["status"],"redirected_home")
        self.assertEqual(r["schedule"]["pages"],[])
        self.assertFalse(page_observations(self.db,4))

    def test_kind_benchmarks_exclude_unavailable_and_sample_sources(self):
        for run in (1,2,3,4):
            audit_run(self.db,run)
        schools=reference_report(self.db,"school",0.5)
        self.assertEqual((schools["assessed"],schools["registered"]),(1,2))
        self.assertEqual(schools["reference"]["organization_id"],1)
        self.assertEqual(schools["common_header_fields"],["contacts_link"])
        self.assertEqual(schools["contacts_section_assessed"],0)
        self.assertIsNone(schools["contact_section_fields"]["address"]["fraction"])
        self.assertEqual(schools["unassessed"][0]["organization_id"],2)
        gardens=reference_report(self.db,"kindergarten",0.5)
        self.assertEqual(gardens["reference"]["organization_id"],3)
        self.assertEqual(gardens["common_header_fields"],["address"])

    def test_camp_contact_link_is_recorded_without_supplying_institution_fields(self):
        self.db.execute("UPDATE page SET text_page=? WHERE id=1",
                        ('<header><a href="/organizatsiya-otdyha/kontakty/">Контакты</a></header>',))
        self.page(4,1,1,"contacts","https://one.test/organizatsiya-otdyha/kontakty/",
                  "<main><p>Телефон: 1234567</p></main>")
        r=audit_run(self.db,1)
        self.assertEqual(r["contacts"]["routes"][0]["scope"],"subdivision")
        self.assertNotIn("contacts_link",r["contacts"]["header_profile"])
        self.assertFalse(r["contacts"]["section_pages"])

    def test_configured_prevalence_applies_within_same_kind(self):
        from model_settings import set_setting
        self.page(4,2,2,"home","https://two.test/","<header><p>Режим работы: Пн–пт 08:00–18:00</p></header>")
        self.db.execute("UPDATE crawl_run SET state='partial' WHERE id=2")
        for run in (1,2):
            audit_run(self.db,run)
        set_setting(self.db,"reference_minimum",0.75)
        self.assertEqual(reference_report(self.db,"school")["common_header_fields"],[])
        self.assertEqual(reference_report(self.db,"school",0.5)["common_header_fields"],["hours","contacts_link"])


if __name__=="__main__":
    unittest.main()
