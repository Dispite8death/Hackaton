"""Manage one current-user Windows task for the audit worker and this SQLite file."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET


_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_WINDOWS = os.name=="nt"
_MISSING = {2,3,0x80070002,0x80070003}
_ROOT = Path(__file__).resolve().parents[1]


def _system(command,*arguments):
    executable = Path(os.environ["SystemRoot"])/"System32"/command
    try:
        return subprocess.run([str(executable),*arguments],stdin=subprocess.DEVNULL,capture_output=True,
                              timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Не удалось выполнить {command}: {exc}") from exc


def _decode(raw):
    if raw.startswith((b"\xff\xfe",b"\xfe\xff")):
        return raw.decode("utf-16").strip()
    if b"\0" in raw[:80]:
        return raw.decode("utf-16-le").strip()
    try:
        return raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return raw.decode("oem",errors="replace").strip()


def _check(result):
    if result.returncode:
        message = _decode(result.stderr or result.stdout)[:550]
        raise RuntimeError(f"Планировщик Windows, код 0x{result.returncode & 0xffffffff:08X}. {message}")


@dataclass(frozen=True)
class _Installation:
    root: Path
    db: Path
    sid: str
    name: str

    @property
    def python(self):
        return self.root/".venv"/"Scripts"/"pythonw.exe"

    @property
    def script(self):
        return self.root/"Engine"/"audit_worker.py"

    @property
    def args(self):
        return subprocess.list2cmdline([str(self.script),"--db",str(self.db),"--once"])

    @property
    def marker(self):
        return "urn:gosweb-audit:"+self.name.lstrip("\\")


def _installation(conn):
    if not _WINDOWS:
        raise ValueError("Для регистрации задачи требуется Windows.")
    filename = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1]=="main"),"")
    if not filename:
        raise ValueError("Расписание доступно для базы, сохранённой в файл.")
    db = Path(filename).resolve()
    user = _system("whoami.exe","/user","/fo","csv","/nh")
    _check(user)
    sid = re.search(rb'\bS-1-[0-9]+(?:-[0-9]+)+\b',user.stdout)
    if not sid:
        raise RuntimeError("Windows не вернула SID текущего пользователя.")
    sid = sid.group().decode("ascii")
    key = "\0".join([os.path.normcase(str(_ROOT)),os.path.normcase(str(db)),sid])
    return _Installation(_ROOT,db,sid,"\\GoswebAudit-"+sha256(key.encode("utf-8")).hexdigest()[:24])


def _definition(install,minutes):
    task = ET.Element("Task",{"version":"1.2","xmlns":_NS})

    def child(parent,tag,value):
        ET.SubElement(parent,tag).text = value

    registration = ET.SubElement(task,"RegistrationInfo")
    child(registration,"Description","Проверка сайтов образовательных учреждений по сохранённой очереди.")
    child(registration,"Source",install.marker)
    triggers = ET.SubElement(task,"Triggers")
    if minutes==1440:
        now = datetime.now()
        start = now.replace(hour=9,minute=0,second=0,microsecond=0)
        if start<=now:
            start += timedelta(days=1)
        trigger = ET.SubElement(triggers,"CalendarTrigger")
        child(trigger,"Enabled","true")
        child(trigger,"StartBoundary",start.isoformat(timespec="seconds"))
        child(ET.SubElement(trigger,"ScheduleByDay"),"DaysInterval","1")
    else:
        trigger = ET.SubElement(triggers,"LogonTrigger")
        child(trigger,"Enabled","true")
        repeat = ET.SubElement(trigger,"Repetition")
        child(repeat,"Interval",f"PT{minutes}M")
        child(repeat,"StopAtDurationEnd","false")
        child(trigger,"UserId",install.sid)
    principal = ET.SubElement(ET.SubElement(task,"Principals"),"Principal",id="Author")
    child(principal,"UserId",install.sid)
    child(principal,"LogonType","InteractiveToken")
    child(principal,"RunLevel","LeastPrivilege")
    settings = ET.SubElement(task,"Settings")
    for tag, value in (("MultipleInstancesPolicy","IgnoreNew"),("DisallowStartIfOnBatteries","false"),
                       ("StopIfGoingOnBatteries","false"),("StartWhenAvailable","true"),
                       ("Enabled","true"),("ExecutionTimeLimit","PT0S")):
        child(settings,tag,value)
    action = ET.SubElement(ET.SubElement(task,"Actions",Context="Author"),"Exec")
    child(action,"Command",str(install.python))
    child(action,"Arguments",install.args)
    child(action,"WorkingDirectory",str(install.root))
    return ET.tostring(task,encoding="utf-16",xml_declaration=True)


def _minutes(value):
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:0S)?)?",value)
    if match:
        days,hours,minutes = (int(n or 0) for n in match.groups())
        return days*1440+hours*60+minutes or None
    return None


def _read(install):
    result = _system("schtasks.exe","/Query","/TN",install.name,"/XML","/HRESULT")
    info = dict(supported=True,registered=False,enabled=False,minutes=None,name=install.name,
                detail="Расписание не зарегистрировано.")
    if result.returncode & 0xffffffff in _MISSING:
        return info
    _check(result)
    try:
        xml = ET.fromstring(_decode(result.stdout))
    except (ET.ParseError,UnicodeError) as exc:
        raise RuntimeError("Не удалось прочитать описание задачи из Планировщика Windows.") from exc
    ns = {"t":_NS}
    def get(path,default=""):
        return xml.findtext(path,default,namespaces=ns)
    if (get("t:RegistrationInfo/t:Source")!=install.marker or
        os.path.normcase(get("t:Actions/t:Exec/t:Command"))!=os.path.normcase(str(install.python)) or
        get("t:Actions/t:Exec/t:Arguments")!=install.args):
        raise RuntimeError("Задача с таким именем имеет другое назначение. Проверьте её в Планировщике Windows.")
    daily = "t:Triggers/t:CalendarTrigger"
    at_nine = (get(daily+"/t:ScheduleByDay/t:DaysInterval")=="1" and
               re.search(r"T09:00:00(?:[.]0+)?$",get(daily+"/t:StartBoundary")) is not None)
    trigger = daily if at_nine else "t:Triggers/t:LogonTrigger"
    enabled = (get("t:Settings/t:Enabled","true")=="true" and get(trigger+"/t:Enabled","true")=="true")
    minutes = 1440 if at_nine else _minutes(get(trigger+"/t:Repetition/t:Interval"))
    detail = ("Ежедневно в 09:00 по местному времени." if at_nine else "Работает после входа пользователя в Windows.")
    return dict(info,registered=True,enabled=enabled,minutes=minutes,schedule_type="daily" if at_nine else "logon",
                detail=detail if enabled else "Задача отключена в Windows.")


def schedule_info(conn):
    if not _WINDOWS:
        return dict(supported=False,registered=False,enabled=False,minutes=None,name="",
                    detail="Для регистрации задачи требуется Windows.")
    return _read(_installation(conn))


def enable_schedule(conn,minutes):
    if type(minutes) is not int or not 1<=minutes<=1440:
        raise ValueError("Интервал должен быть целым числом от 1 до 1440 минут.")
    install = _installation(conn)
    if not install.python.is_file() or not install.script.is_file():
        raise ValueError("Для запуска нужны .venv/Scripts/pythonw.exe и Engine/audit_worker.py в каталоге проекта.")
    previous = _read(install)
    kind = "daily" if minutes==1440 else "logon"
    if previous["enabled"] and previous["minutes"]==minutes and previous["schedule_type"]==kind:
        return previous
    with TemporaryDirectory() as temp:
        definition = Path(temp)/"audit-task.xml"
        definition.write_bytes(_definition(install,minutes))
        args = ["/Create","/TN",install.name,"/XML",str(definition),"/HRESULT"]
        if previous["registered"]:
            args.append("/F")
        result = _system("schtasks.exe",*args)
    if result.returncode:
        concurrent = _read(install)
        if concurrent["enabled"] and concurrent["minutes"]==minutes and concurrent["schedule_type"]==kind:
            return concurrent
        _check(result)
    return _read(install)


def disable_schedule(conn):
    install = _installation(conn)
    previous = _read(install)
    if previous["registered"]:
        result = _system("schtasks.exe","/Delete","/TN",install.name,"/F","/HRESULT")
        if result.returncode & 0xffffffff not in _MISSING:
            _check(result)
        return _read(install)
    return previous
