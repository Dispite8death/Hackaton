import io
import json
import os
import sqlite3
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image

import admin
from crawl_store import initialize_db


class VisualLabelsTest(unittest.TestCase):
    def test_labels_and_missing_metadata_in_old_history(self):
        self.assertEqual(admin._decision("not_applicable",None),"Неприменимо")
        self.assertEqual(admin._check_label({"id":"a","visual":{"target":"staff","observation_ids":[8]}}),"a · № 8")
        self.assertIn("вне оценки",admin._decision("absent",{"rule_scope":"advisory"}))
        self.assertIn("стр. 13",admin._preview_source({"kind":"guide","source_document":"Памятка","page_number":13}))


@unittest.skipUnless(os.environ.get("HACKATON_TEST_ADMIN")=="1","explicit local Tk test")
class VisualAdminTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        initialize_db(self.db)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа','https://school.test','school')")
        self.db.execute("INSERT INTO page VALUES(11,'https://school.test',1,1,'<main>Карточки</main>')")
        self.db.execute("INSERT INTO page_passport VALUES(11,'staff_directory','Педагоги','[]','','[]')")
        self.db.execute("""INSERT INTO staff_observation(id,page_id,card_selector,full_name,is_head,
                        photo_state,method) VALUES(70,11,'#head','Петрова Анна Ивановна',1,'linked','card')""")
        def photo(qid,target,label,ids,pages,scope="school_guide"):
            return {"id":qid,"instruction":"Полный текст для "+qid, "material_kinds":["image"],
                    "visual":{"target":target,"label":label,"observation_ids":ids,"reference_pages":pages,
                              "rule_scope":scope,"manual_only":scope=="advisory"}}
        self.checks=[
            {"id":"text","instruction":"Видно ли имя?","material_kinds":["html"]},
            photo("staff","staff","Лицо",[70],[],"case"),
            photo("series","staff_series","Фон",[70,71],[13,14]),
            photo("flag","staff","Флаг",[70],[3],"advisory"),
            photo("group","school_main","Групповой портрет",[],[11]),
        ]
        self.enterContext(patch.object(admin,"page_checks",return_value=self.checks))
        self.enterContext(patch.object(admin,"guide_illustration",
                                      side_effect=lambda c:object() if c["id"] in ("series","flag") else None))
        self.root=tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.ui=admin.Admin(self.root,self.db)

    def select(self,group):
        self.ui.check_filter.set(group)
        self.ui._filter_checks()

    def test_storage_uses_separate_policy_without_registering_a_windows_task(self):
        from retention import get_policy
        self.ui.keep_days.set("240")
        self.ui.clean_replaced.set(True)
        self.ui._storage(True)
        self.assertEqual(get_policy(self.db),{"days":240,"enabled":True})
        with patch.object(self.ui,"_job",side_effect=lambda msg,work,done:done(work(self.db))):
            self.ui._storage(False)
        self.assertIn("240 дней",self.ui.worker_info.get("1.0","end"))

    def test_filtered_choice_and_guide_preference(self):
        u=self.ui
        self.assertIn("Карточка №70 · Петрова Анна Ивановна",u.filter_box["values"])
        self.assertTrue(u.preview_button.instate(["disabled"]))
        self.select("Карточка №70 · Петрова Анна Ивановна")
        self.assertEqual(u.check_box["values"],("Лицо · № 70","Флаг · № 70"))
        self.assertEqual(u.check_description.get(),"Полный текст для staff")
        self.assertTrue(u.guide_box.instate(["disabled"]))
        self.select(admin.SERIES_CHECKS)
        self.assertEqual(u._current_check()["id"],"series")
        self.assertTrue(u.include_guide.get())
        self.assertFalse(u.guide_box.instate(["disabled"]))
        u.guide_box.invoke()
        self.select(admin.ADVISORY_CHECKS)
        self.assertFalse(u.include_guide.get())
        self.assertIn("Полный текст для flag",u.check_description.get())
        self.select(admin.VISUAL_CHECKS)
        self.assertEqual(u._current_check()["id"],"flag")
        u.check_box.current(len(u.visible_checks)-1)
        u._question()
        self.assertEqual(u._current_check()["id"],"group")
        self.assertTrue(u.guide_box.instate(["disabled"]))

    def test_preview_uses_exact_bundle_and_handles_empty(self):
        self.select(admin.SERIES_CHECKS)
        u=self.ui
        with io.BytesIO() as out:
            Image.new("RGB",(30,20),"navy").save(out,format="PNG")
            image=out.getvalue()
        items=[({"source":"original","photo_url":"https://school.test/p.png","kind":"image"},image,"Карточка №70"),
               ({"kind":"guide","source_document":"Памятка","page_number":13},image,"Памятка")]
        dialogs=[]
        real_dialog=u._preview_dialog
        def show(*args):
            dialog=real_dialog(*args)
            dialog.withdraw()
            dialogs.append(dialog)
            return dialog
        with patch.object(u,"_job",side_effect=lambda _,work,done:done(work(self.db))), \
             patch.object(admin,"visual_bundle",side_effect=[items,[]]) as bundle, \
             patch.object(u,"_preview_dialog",side_effect=show):
            u._preview()
            bundle.assert_called_with(self.db,11,self.checks[2],include_guide=True)
            widgets=dialogs[0].winfo_children()[0].winfo_children()
            picker=next(w for w in widgets if w.winfo_class()=="TCombobox")
            self.assertEqual(picker["values"],("1. Карточка №70","2. Памятка"))
            picker.current(1)
            picker.event_generate("<<ComboboxSelected>>")
            self.root.update()
            label=next(w for w in widgets if w.winfo_class()=="TLabel")
            self.assertIn("стр. 13",str(self.root.getvar(label.cget("textvariable"))))
            u.guide_box.invoke()
            u._preview()
            bundle.assert_called_with(self.db,11,self.checks[2],include_guide=False)
            children=dialogs[1].winfo_children()[0].winfo_children()
            self.assertIn("нет подходящего изображения",children[0].cget("text"))

    def test_review_and_history_read_the_recorded_scope(self):
        self.select(admin.ADVISORY_CHECKS)
        u=self.ui
        u.guide_box.invoke()
        result={"assessment_id":31,"outcome":"not_applicable","response":{"results":[{"evidence":"Полотнище не видно"}]},
                "error":None,"seconds":1.0}
        self.db.execute("""INSERT INTO model_assessment(id,page_id,check_id,provider,endpoint,model,outcome,
                         request,response,elapsed_seconds) VALUES(31,11,'flag','lmstudio','local','model',
                         'not_applicable',?,?,1)""",(json.dumps({"checks":[self.checks[3]]}),json.dumps(result["response"])))
        with patch.object(u,"_job",side_effect=lambda _,work,done:done(work(self.db))), \
             patch.object(admin,"assess",return_value=result) as assess:
            u._review()
        self.assertEqual(assess.call_args.args,(self.db,11,"flag"))
        self.assertFalse(assess.call_args.kwargs["include_guide"])
        self.assertIn("Справочное наблюдение · Неприменимо",u.result.get("1.0","end"))
        # Current question metadata may have changed since the assessment was written.
        self.checks[3]["visual"]["rule_scope"]="case"
        self.checks[3]["visual"]["manual_only"]=False
        u._history()
        self.assertIn("Справочное наблюдение · Неприменимо",u.result.get("1.0","end"))


if __name__=="__main__":
    unittest.main()
