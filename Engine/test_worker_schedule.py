from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

import worker_schedule as schedule


class WorkerScheduleTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory()))/"мой проект & архив"
        self.temp.mkdir()
        self.db = sqlite3.connect(self.temp/"архив сайта.sqlite3")
        self.addCleanup(self.db.close)
        self.enterContext(patch.object(schedule,"_ROOT",self.temp))
        self.enterContext(patch.object(schedule,"_WINDOWS",True))
        self.sid = "S-1-5-21-1111-222-3333-1001"
        self.tasks = {}
        self.commands = []
        self.enterContext(patch.object(schedule,"_system",side_effect=self.system))
        self.installation = schedule._installation(self.db)
        for p in (self.installation.python,self.installation.script):
            p.parent.mkdir(parents=True)
            p.touch()

    def system(self,program,*argv):
        def result(code=0,data=b""):
            return subprocess.CompletedProcess(argv,code,data,b"")
        if program=="whoami.exe":
            return result(data=f'"account","{self.sid}"'.encode())
        self.assertEqual(program,"schtasks.exe")
        self.commands.append(list(argv))
        key = argv[argv.index("/TN")+1]
        if argv[0]=="/Query":
            return result(data=self.tasks[key]) if key in self.tasks else result(0x80070002)
        if argv[0]=="/Create":
            self.tasks[key] = Path(argv[argv.index("/XML")+1]).read_bytes()
            return result()
        if argv[0]=="/Delete":
            del self.tasks[key]
            return result()
        self.fail(str(argv))

    def test_enable_repeat_update_disable(self):
        self.assertFalse(schedule.schedule_info(self.db)["registered"])
        first = schedule.enable_schedule(self.db,15)
        self.assertEqual((first["enabled"],first["minutes"]),(True,15))
        self.assertEqual(schedule.enable_schedule(self.db,15),first)
        second = schedule.enable_schedule(self.db,90)
        self.assertEqual(second["minutes"],90)
        creates = [c for c in self.commands if c[0]=="/Create"]
        self.assertEqual(len(creates),2)
        self.assertNotIn("/F",creates[0])
        self.assertIn("/F",creates[1])
        self.assertFalse(schedule.disable_schedule(self.db)["registered"])
        self.assertFalse(schedule.disable_schedule(self.db)["enabled"])
        self.assertEqual(sum(c[0]=="/Delete" for c in self.commands),1)

    def test_definition_is_limited_to_current_logon_user_and_hidden_interpreter(self):
        schedule.enable_schedule(self.db,120)
        task = ET.fromstring(self.tasks[self.installation.name])
        ns={"t":schedule._NS}
        get=lambda path:task.findtext(path,namespaces=ns)
        self.assertEqual(get("t:Triggers/t:LogonTrigger/t:UserId"),self.sid)
        self.assertEqual(get("t:Principals/t:Principal/t:UserId"),self.sid)
        self.assertEqual(get("t:Principals/t:Principal/t:RunLevel"),"LeastPrivilege")
        self.assertEqual(get("t:Principals/t:Principal/t:LogonType"),"InteractiveToken")
        self.assertEqual(get("t:Settings/t:MultipleInstancesPolicy"),"IgnoreNew")
        self.assertEqual(get("t:Actions/t:Exec/t:Command"),str(self.installation.python))
        self.assertEqual(get("t:Actions/t:Exec/t:WorkingDirectory"),str(self.temp))
        self.assertEqual(get("t:Actions/t:Exec/t:Arguments"),
            f'"{self.installation.script}" --db "{self.installation.db}" --once')
        self.assertEqual(schedule._minutes("P1D"),1440)
        self.assertEqual(schedule._minutes("PT1H30M"),90)

    def test_daily_trigger_once_at_nine_uses_local_time_and_catches_missed_start(self):
        info = schedule.enable_schedule(self.db,1440)
        task = ET.fromstring(self.tasks[self.installation.name])
        ns={"t":schedule._NS}
        self.assertEqual((info["minutes"],info["schedule_type"]),(1440,"daily"))
        self.assertIsNone(task.find("t:Triggers/t:LogonTrigger",ns))
        trigger=task.find("t:Triggers/t:CalendarTrigger",ns)
        self.assertEqual(trigger.findtext("t:ScheduleByDay/t:DaysInterval",namespaces=ns),"1")
        self.assertRegex(trigger.findtext("t:StartBoundary",namespaces=ns),r"T09:00:00$")
        self.assertEqual(task.findtext("t:Settings/t:StartWhenAvailable",namespaces=ns),"true")
        self.assertEqual(task.findtext("t:Principals/t:Principal/t:LogonType",namespaces=ns),"InteractiveToken")

    def test_refuses_foreign_action_and_access_errors_are_not_reported_as_disabled(self):
        xml = schedule._definition(self.installation,15).decode("utf-16")
        self.tasks[self.installation.name] = xml.replace("pythonw.exe","foreign.exe").encode("utf-16")
        for action in (schedule.schedule_info, schedule.disable_schedule, lambda c:schedule.enable_schedule(c,2)):
            with self.assertRaises(RuntimeError):
                action(self.db)
        self.assertTrue(all(c[0]=="/Query" for c in self.commands))
        with patch.object(schedule,"_system",return_value=subprocess.CompletedProcess([],0x80070005,b"",b"denied")):
            with self.assertRaisesRegex(RuntimeError,"80070005"):
                schedule._read(self.installation)

    def test_intervals_identity_and_ephemeral_database(self):
        for value in (0,1441,True,"5",1.5):
            with self.assertRaises(ValueError):
                schedule.enable_schedule(self.db,value)
        other=sqlite3.connect(self.temp/"other.db")
        self.addCleanup(other.close)
        self.assertNotEqual(self.installation.name,schedule._installation(other).name)
        self.sid="S-1-5-21-999-1002"
        self.assertNotEqual(self.installation.name,schedule._installation(self.db).name)
        mem=sqlite3.connect(":memory:")
        self.addCleanup(mem.close)
        with self.assertRaises(ValueError):
            schedule.schedule_info(mem)
