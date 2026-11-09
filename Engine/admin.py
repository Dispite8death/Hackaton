"""Desktop administration of the audit database and model connections."""

from contextlib import closing
from collections import Counter
from io import BytesIO
import json
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from urllib.parse import urlsplit

if __package__:
    from .retention import retired_for_page
    from .deferred_checks import GROUPS, review_group
    from .model_batch import batch_assess
    from .config import connect_db
    from .crawl_store import initialize_db
    from .document_inventory import document_inventory
    from .factcheck import DEFAULT_SEARCH_SITES, run_factcheck
    from .factcheck_batch import batch_check
    from .factcheck_sources import add_allowed_source, allowlist_fingerprint, get_allowed_sources, remove_allowed_source
    from .home_terms import CATEGORY_LABELS, add_term, get_terms, remove_term
    from .institution_audit import reference_report
    from .model_gateway import assess, list_models, page_checks
    from .visual_guide import guide_illustration
    from .visual_materials import visual_bundle
    from .model_settings import (PROVIDERS, Profile, active_secret, checked, get_profile, save_profile,
                                 save_secret, set_setting, setting)
else:
    from retention import retired_for_page
    from deferred_checks import GROUPS, review_group
    from model_batch import batch_assess
    from config import connect_db
    from crawl_store import initialize_db
    from document_inventory import document_inventory
    from factcheck import DEFAULT_SEARCH_SITES, run_factcheck
    from factcheck_batch import batch_check
    from factcheck_sources import add_allowed_source, allowlist_fingerprint, get_allowed_sources, remove_allowed_source
    from home_terms import CATEGORY_LABELS, add_term, get_terms, remove_term
    from institution_audit import reference_report
    from model_gateway import assess, list_models, page_checks
    from visual_guide import guide_illustration
    from visual_materials import visual_bundle
    from model_settings import (PROVIDERS, Profile, active_secret, checked, get_profile, save_profile,
                                save_secret, set_setting, setting)


FORMATS = {"По JSON-схеме": "json_schema", "JSON-объект": "json_object", "По инструкции": "prompt"}
OUTCOMES = {"present":"Да","absent":"Нет","uncertain":"Требует проверки","not_applicable":"Неприменимо","error":"Ошибка"}
FACT_OUTCOMES = {"corroborated":"подтверждено","insufficient":"недостаточно источников",
                 "review":"требует сверки","retryable":"источники недоступны, можно повторить"}
VISION_RECHECK = {"agreed":"Проверка только по снимку сайта дала тот же ответ.",
                  "disagreed":"Ответы с примером и только по снимку разошлись.",
                  "target_error":"Проверку только по снимку сайта выполнить не удалось.",
                  "guided_error":"Запрос с примером завершился ошибкой.",
                  "target_guarded":"Контрольный ответ по снимку содержит наблюдение, требующее сверки.",
                  "guided_uncertain":"Модель сочла сведения недостаточными.",
                  "guard_skipped":"Связанные наблюдения потребовали сверки."}
FIELDS = {"address":"Адрес","phone":"Телефон","email":"Электронная почта","hours":"Режим работы",
          "reception":"Часы приёма","reception_hours":"Часы приёма","contacts_link":"Ссылка на контакты"}

ALL_CHECKS = "Все вопросы"
VISUAL_CHECKS = "Все фотопроверки"
SERIES_CHECKS = "Сравнение серии карточек"
ADVISORY_CHECKS = "Справочные наблюдения"


def _check_label(check):
    if check.get("attachment"):
        return check["attachment"]["label"]
    visual = check.get("visual")
    if not visual:
        text = check["instruction"]
        return text if len(text)<=100 else text[:99]+"…"
    label = visual.get("label") or check.get("id","Фотопроверка")
    ids = visual.get("observation_ids", [])
    if ids:
        label += " · № "+", ".join(map(str, ids))
    return label


def _decision(outcome, visual):
    label = OUTCOMES.get(outcome,outcome)
    if visual and (visual.get("manual_only") or visual.get("rule_scope")=="advisory"):
        return f"Справочное наблюдение · {label} · вне оценки учреждения"
    return label


def _preview_source(meta):
    if meta.get("kind")=="guide":
        return f"Памятка · {meta.get('source_document','')} · стр. {meta.get('page_number','?')}"
    source = {"original":"Исходная фотография","viewport_crop":"Фрагмент первого экрана",
              "document_crop":"Фрагмент полного снимка страницы"}.get(meta.get("source"),"Снимок учреждения")
    where = meta.get("photo_url") or meta.get("path","")
    if meta.get("partial"):
        source += " · материал с ограниченным охватом"
    return f"{source} · {where}" if where else source


def _field(parent, row, label, widget):
    ttk.Label(parent,text=label).grid(row=row,column=0,sticky="w",padx=(0,18),pady=7)
    widget.grid(row=row,column=1,sticky="ew",pady=7)
    return widget


def _text_output(parent, height=12):
    return ScrolledText(parent,height=height,wrap="word",font="TkTextFont",padx=10,pady=8,state="disabled")


def _display(widget, text):
    widget.configure(state="normal")
    widget.delete("1.0","end")
    widget.insert("1.0",text)
    widget.configure(state="disabled")


def _cycle_summary(result):
    states = {"completed":"завершён","partial":"частично завершён","running":"выполняется",
              "busy":"уже выполняется","interrupted":"прерван"}
    stages = {"crawl":("Основной обход","processed"),"frontier":("Догрузка страниц","processed"),
              "attachments":("Оригиналы вложений","saved"),"models":("Вопросы модели","processed"),
              "factcheck":("Внешние источники","processed"),"retention":("Очистка заменённых материалов","files")}
    lines = [f"Цикл {result.get('id','')} · {states.get(result.get('state'),result.get('state',''))}"]
    for key,item in result.get("stages",{}).items():
        label,field = stages.get(key,(key,"processed"))
        if item.get("state")=="disabled" or item.get("enabled") is False:
            lines.append(f"{label} · отключено")
            continue
        detail = item.get("error") or item.get("stop_reason")
        count = item.get(field,0)
        errors = item.get("errors") or 0
        lines.append(f"{label} · {count}" + (f" · ошибок {errors}" if errors else "") +
                     (f" · {detail}" if detail else ""))
    return "\n".join(lines)


class Admin:
    def __init__(self, root, conn):
        self.root, self.db = root, conn
        self.queue = Queue()
        self.busy = False
        root.title("Аудит сайтов • Администрирование")
        root.geometry("960x695")
        root.minsize(820,610)
        outer = ttk.Frame(root,padding=(24,18,24,10))
        outer.pack(fill="both",expand=True)
        top = ttk.Frame(outer)
        top.pack(fill="x",pady=(0,12))
        ttk.Label(top,text="Администрирование",font=("Segoe UI",19,"bold")).pack(side="left")
        self.active = tk.StringVar()
        ttk.Label(top,textvariable=self.active).pack(side="right")
        book = ttk.Notebook(outer)
        book.pack(fill="both",expand=True)
        self.tab = {}
        for name in ("Подключение к ИИ","Проверка страницы","Очередь ИИ","Автоматизация","Источники","Правила и эталоны","Поисковые слова"):
            frame = ttk.Frame(book,padding=(20,15))
            self.tab[name] = frame
            book.add(frame,text=name)
        self.status = tk.StringVar(value="Готово")
        ttk.Label(outer,textvariable=self.status,wraplength=860).pack(fill="x",pady=(10,0))
        self._models_tab()
        self._checks_tab()
        self._model_batch_tab()
        self._worker_tab()
        self._sources_tab()
        self._rules_tab()
        self._words_tab()
        self._show_profile(get_profile(conn).provider)
        self._active_label()
        root.report_callback_exception = lambda typ,err,tb: self.status.set(str(err))
        self._poll_timer = root.after(100,self._poll)
        root.bind("<Destroy>",self._stop_poll,add="+")

    def _active_label(self):
        p = get_profile(self.db)
        self.active.set(f"Активно: {PROVIDERS[p.provider]}  ·  {p.model}")

    def _job(self, message, work, done):
        if self.busy:
            self.status.set("Дождитесь завершения текущего запроса.")
            return
        self.busy = True
        self.status.set(message)
        def run():
            try:
                with closing(connect_db()) as conn:
                    value = work(conn)
            except Exception as exc:
                self.queue.put((None,str(exc)))
            else:
                self.queue.put((done,value))
        Thread(target=run,daemon=True).start()

    def _poll(self):
        try:
            callback,value = self.queue.get_nowait()
        except Empty:
            pass
        else:
            self.busy = False
            if callback is None:
                self.status.set("Ошибка. " + value)
            else:
                self.status.set("Готово")
                try:
                    callback(value)
                except Exception as exc:
                    self.status.set(str(exc))
        if self._poll_timer is not None:
            self._poll_timer = self.root.after(100,self._poll)

    def _stop_poll(self,event):
        if event.widget is self.root and self._poll_timer is not None:
            self.root.after_cancel(self._poll_timer)
            self._poll_timer = None

    def _models_tab(self):
        frame = self.tab["Подключение к ИИ"]
        frame.columnconfigure(1,weight=1)
        self.provider = tk.StringVar()
        modes = ttk.Frame(frame)
        modes.grid(row=0,column=0,columnspan=2,sticky="w",pady=(0,12))
        for name,label in PROVIDERS.items():
            ttk.Radiobutton(modes, text=label,variable=self.provider,value=name,
                            command=lambda n=name:self._show_profile(n)).pack(side="left",padx=(0,20))
        self.endpoint,self.model,self.key,self.timeout,self.format = [tk.StringVar() for _ in range(5)]
        _field(frame,1,"Адрес API",ttk.Entry(frame,textvariable=self.endpoint))
        choices = ttk.Frame(frame)
        choices.columnconfigure(0,weight=1)
        self.model_box = ttk.Combobox(choices,textvariable=self.model)
        self.model_box.grid(row=0,column=0,sticky="ew")
        ttk.Button(choices,text="Получить модели",command=self._models).grid(row=0,column=1,padx=(8,0))
        _field(frame,2,"Модель",choices)
        keys = ttk.Frame(frame)
        keys.columnconfigure(0,weight=1)
        ttk.Entry(keys,textvariable=self.key,show="•").grid(row=0,column=0,sticky="ew")
        ttk.Button(keys,text="Удалить ключ",command=self._clear_key).grid(row=0,column=1,padx=(8,0))
        _field(frame,3,"Ключ API",keys)
        ttk.Label(frame,text="Пустое поле сохраняет прежний ключ для этого адреса.").grid(row=4,column=1,sticky="w")
        _field(frame,5,"Формат ответа",ttk.Combobox(frame,textvariable=self.format,values=list(FORMATS),state="readonly"))
        _field(frame,6,"Тайм-аут, секунд",ttk.Spinbox(frame,textvariable=self.timeout,from_=5,to=900))
        self.thinking_off = tk.BooleanVar()
        ttk.Checkbutton(frame,text="Отключить размышления модели (если API поддерживает)",
                        variable=self.thinking_off).grid(row=7,column=1,sticky="w")
        ttk.Button(frame,text="Сохранить и использовать",command=self._save).grid(row=8,column=1,sticky="w",pady=(14,10))
        self.model_hint = tk.StringVar()
        ttk.Label(frame,textvariable=self.model_hint,wraplength=690,justify="left").grid(row=9,column=0,columnspan=2,sticky="w")

    def _show_profile(self,name):
        p = get_profile(self.db,name)
        self.provider.set(name)
        self.endpoint.set(p.endpoint)
        self.model.set(p.model)
        self.model_box.configure(values=())
        self.key.set("")
        self.timeout.set(str(p.timeout))
        self.format.set(next(k for k,v in FORMATS.items() if v==p.response_mode))
        self.thinking_off.set(p.disable_thinking)
        self.model_hint.set({
            "lmstudio":"В LM Studio включите Local Server. Для анализа скриншотов выберите модель с поддержкой изображений.",
            "ollama":"Запустите Ollama. В список попадут модели, установленные на выбранном сервере.",
            "compatible":"Укажите базовый адрес сервера Chat Completions и идентификатор модели. "
                         "При запуске проверки выбранный фрагмент текста или изображение будет передано этому серверу.",
        }[name])

    def _profile(self, require_model=True):
        return checked(Profile(self.provider.get(), self.endpoint.get(), self.model.get(),
                               int(self.timeout.get()),FORMATS[self.format.get()],self.thinking_off.get()),
                       require_model=require_model)

    def _models(self):
        p = self._profile(False)
        typed = self.key.get()
        def done(items):
            if self.provider.get()!=p.provider or self._profile(False).endpoint!=p.endpoint:
                return
            self.model_box.configure(values=items)
            if not self.model.get() and items:
                self.model.set(items[0])
            self.status.set(f"Сервер ответил. Доступно моделей: {len(items)}.")
        self._job("Запрашиваю модели…",lambda c:list_models(p,typed or active_secret(c,p)),done)

    def _save(self):
        p = self._profile()
        with self.db:
            if self.key.get().strip():
                save_secret(self.db,p,self.key.get().strip())
            save_profile(self.db,p)
        self._show_profile(p.provider)
        self._active_label()
        self.status.set(f"Для новых проверок выбран профиль {PROVIDERS[p.provider]}.")

    def _clear_key(self):
        with self.db:
            save_secret(self.db,self._profile(False),"")
        self.key.set("")
        self.status.set("Ключ для этого адреса удалён.")

    def _checks_tab(self):
        frame = self.tab["Проверка страницы"]
        frame.columnconfigure(1,weight=1)
        frame.rowconfigure(7,weight=1)
        self.pid,self.check_title,self.page_info = tk.StringVar(),tk.StringVar(),tk.StringVar()
        ids = ttk.Frame(frame)
        ttk.Entry(ids,textvariable=self.pid,width=12).pack(side="left")
        ttk.Button(ids,text="Открыть снимок",command=self._load_checks).pack(side="left",padx=8)
        ttk.Button(ids,text="Найти в архиве…",command=self._browse).pack(side="left",padx=(0,8))
        ttk.Label(ids,textvariable=self.page_info,wraplength=480).pack(side="left")
        _field(frame,0,"ID снимка в базе",ids)
        self.check_filter = tk.StringVar(value=ALL_CHECKS)
        self.filter_box = ttk.Combobox(frame,textvariable=self.check_filter,state="readonly")
        _field(frame,1,"Группа вопросов",self.filter_box)
        self.filter_box.bind("<<ComboboxSelected>>",lambda _:self._filter_checks())
        self.check_box = ttk.Combobox(frame,textvariable=self.check_title,state="readonly")
        _field(frame,2,"Вопрос модели",self.check_box)
        self.check_description = tk.StringVar()
        ttk.Label(frame,textvariable=self.check_description,wraplength=745,justify="left").grid(
            row=3,column=0,columnspan=2,sticky="w",pady=4)
        self.check_box.bind("<<ComboboxSelected>>",lambda e:self._question())
        photos = ttk.Frame(frame)
        photos.grid(row=4,column=0,columnspan=2,sticky="w",pady=(3,0))
        self.preview_button = ttk.Button(photos,text="Посмотреть снимки",command=self._preview,state="disabled")
        self.preview_button.pack(side="left")
        self._prefer_guide = True
        self.include_guide = tk.BooleanVar(value=False)
        self.guide_box = ttk.Checkbutton(photos,text="Приложить пример из памятки (до двух запросов)",variable=self.include_guide,
                                         command=self._guide_toggled,state="disabled")
        self.guide_box.pack(side="left",padx=(12,0))
        actions = ttk.Frame(frame)
        actions.grid(row=5,column=0,columnspan=2,sticky="w",pady=9)
        self.review_button = ttk.Button(actions,text="Проверить моделью",command=self._review,state="disabled")
        self.review_button.pack(side="left")
        self.fact_button = ttk.Button(actions,text="Искать внешние подтверждения",command=self._factcheck)
        self.fact_button.pack(side="left",padx=8)
        ttk.Button(actions,text="Проверить очередь обхода",command=self._batch).pack(side="left")
        ttk.Button(actions,text="История проверок",command=self._history).pack(side="left",padx=(8,0))
        self.source_urls = tk.StringVar()
        _field(frame,6,"Дополнительные URL через пробел",ttk.Entry(frame,textvariable=self.source_urls))
        self.result = _text_output(frame)
        self.result.grid(row=7,column=0,columnspan=2,sticky="nsew")
        self.checks, self.visible_checks, self.loaded = [], [], None
        self.card_filters = {}
        latest = self.db.execute("SELECT max(page_id) FROM page_passport").fetchone()[0]
        if latest:
            self.pid.set(str(latest))
            self._load_checks()

    def _load_checks(self):
        pid = int(self.pid.get())
        self.checks = page_checks(self.db,pid)
        row = self.db.execute("""SELECT b.page_kind,b.title,o.id FROM page_passport b JOIN page p ON p.id=b.page_id
            JOIN organization o ON o.id=p.id_organization WHERE b.page_id=?""",(pid,)).fetchone()
        self.page_info.set(f"Учреждение {row[2]}  ·  {row[1][:76]}")
        if pid != self.loaded:
            self.source_urls.set("")
            self.check_filter.set(ALL_CHECKS)
        self.loaded = pid
        visuals = [c["visual"] for c in self.checks if c.get("visual")]
        names = dict(self.db.execute("SELECT id,full_name FROM staff_observation WHERE page_id=?",(pid,)))
        self.card_filters = {f"Карточка №{oid} · {names.get(oid) or 'ФИО не указано'}":oid
                             for oid in sorted({oid for v in visuals if v["target"]=="staff"
                                                for oid in v.get("observation_ids",[])})}
        groups = [ALL_CHECKS]
        present_groups = {review_group(check) for check in self.checks}
        groups.extend(label for key,label in GROUPS.items() if key in present_groups and key!="photos")
        if visuals:
            groups.append(VISUAL_CHECKS)
        if any(v["target"]=="staff_series" for v in visuals):
            groups.append(SERIES_CHECKS)
        if any(v.get("rule_scope")=="advisory" for v in visuals):
            groups.append(ADVISORY_CHECKS)
        groups.extend(self.card_filters)
        self.filter_box.configure(values=groups)
        if self.check_filter.get() not in groups:
            self.check_filter.set(ALL_CHECKS)
        self._filter_checks()
        self.fact_button.configure(state="normal" if row[0] in ("news_detail","event_detail") else "disabled")
        self._history()

    def _filter_checks(self):
        group = self.check_filter.get()
        selected = self._current_check()
        self.visible_checks = []
        for check in self.checks:
            v = check.get("visual") or {}
            if (group==ALL_CHECKS or group==GROUPS.get(review_group(check)) or group==VISUAL_CHECKS and v
                    or group==SERIES_CHECKS and v.get("target")=="staff_series"
                    or group==ADVISORY_CHECKS and v.get("rule_scope")=="advisory"
                    or group in self.card_filters and v.get("target")=="staff"
                    and self.card_filters[group] in v.get("observation_ids",())):
                self.visible_checks.append(check)
        labels = [_check_label(c) for c in self.visible_checks]
        counts = Counter(labels)
        self.check_box.configure(values=[f"{label} · {c['id']}" if counts[label]>1 else label
                                         for label,c in zip(labels,self.visible_checks)])
        if self.visible_checks:
            index = next((n for n,c in enumerate(self.visible_checks) if selected and c["id"]==selected["id"]),0)
            self.check_box.current(index)
        else:
            self.check_title.set("")
        self._question()

    def _current_check(self):
        index = self.check_box.current()
        return self.visible_checks[index] if 0<=index<len(self.visible_checks) else None

    def _guide_toggled(self):
        self._prefer_guide = self.include_guide.get()

    def _question(self):
        check = self._current_check()
        visual = check.get("visual") if check else None
        has_guide = bool(visual and visual.get("reference_pages") and guide_illustration(check) is not None)
        self.check_description.set(check["instruction"] if check else "")
        self.review_button.configure(state="normal" if check else "disabled")
        self.preview_button.configure(state="normal" if visual else "disabled")
        self.guide_box.configure(state="normal" if has_guide else "disabled")
        self.include_guide.set(has_guide and self._prefer_guide)

    def _history(self):
        pid = self._selected()
        lines = []
        retired = retired_for_page(self.db,pid)
        if retired:
            replacements = sorted({item["replacement_page_id"] for item in retired})
            lines.append(f"По сроку хранения освобождено материалов · {len(retired)}. "
                         "Сохранились более свежие снимки № "+", ".join(map(str,replacements))+".")
        for row in self.db.execute("""SELECT a.id,a.check_id,a.provider,a.model,a.outcome,a.error,
            json_extract(a.response,'$.results[0].evidence'),json_extract(a.request,'$.checks[0].visual'),v.status
            FROM model_assessment a LEFT JOIN model_visual_verification v ON v.assessment_id=a.id
            WHERE a.page_id=? ORDER BY a.id DESC""",(pid,)):
            aid,check,provider,model,outcome,error,evidence,raw_visual,verification = row
            visual = json.loads(raw_visual) if raw_visual else None
            lines.extend([f"ИИ №{aid} · {PROVIDERS.get(provider,provider)} · {model}",
                          f"{_check_label({'visual':visual,'id':check}) if visual else check} · {_decision(outcome,visual)}",
                          error or evidence or ""])
            if verification:
                lines.append(VISION_RECHECK.get(verification,verification))
            passes = self.db.execute("""SELECT ordinal,material_ids,outcome,error FROM model_evidence_pass
                WHERE assessment_id=? ORDER BY ordinal""",(aid,)).fetchall()
            if passes:
                lines.append(f"Отправлено фрагментов или сравнений: {len(passes)}.")
                for number,mids,decision,problem in passes[:5]:
                    lines.append(f"  {number}. {', '.join(json.loads(mids))} · "
                                 + (problem or OUTCOMES.get(decision,decision) or "нет ответа"))
                if len(passes)>5:
                    lines.append("Остальные фрагменты сохранены в базе и в выгрузке страницы.")
            lines.append("")
        for fid,status,count,claim in self.db.execute("""SELECT id,status,source_count,claim FROM factcheck_run
            WHERE page_id=? ORDER BY id DESC""",(pid,)):
            event = json.loads(claim)
            lines.append(f"Внешняя проверка №{fid}")
            old_policy = event.get("allowlist_fingerprint")
            if old_policy is None:
                lines.append("Архивная проверка до введения списка допустимых источников.")
            elif old_policy != allowlist_fingerprint(self.db):
                lines.append("Архивная проверка по прежнему списку допустимых источников.")
            if "event_status" in event:
                lines.append(f"Проведение события · {FACT_OUTCOMES.get(event['event_status'],event['event_status'])}"
                             f" · источников {event['event_source_count']}")
            lines.append(f"Участие учреждения · {FACT_OUTCOMES.get(status,status)} · источников {count}")
            for url,raw in self.db.execute("SELECT url,evidence FROM factcheck_source WHERE check_id=? ORDER BY ordinal",(fid,)):
                source = json.loads(raw)
                origin = "указан вручную" if source.get("discovery")=="provided" else "найден поиском"
                state = source.get("status","")
                if state=="source_not_allowed":
                    state = "вне списка допустимых источников"
                lines.append(f"{source.get('title') or url} · {origin} · {state}\n{url}")
            lines.append("")
        _display(self.result,"\n".join(lines) if lines else "По этому снимку пока нет сохранённых ответов ИИ и внешних проверок.")

    def _browse(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Страницы в архиве")
        dialog.geometry("880x470")
        dialog.transient(self.root)
        box = ttk.Frame(dialog,padding=14)
        box.pack(fill="both",expand=True)
        ttk.Label(box,text="Название учреждения, страницы, адрес или ID").pack(anchor="w")
        term = tk.StringVar()
        field = ttk.Entry(box,textvariable=term)
        field.pack(fill="x",pady=(3,8))
        tree = ttk.Treeview(box,columns=("id","org","title","run"),show="headings",selectmode="browse")
        for column,label,width in (("id","Снимок",65),("org","Учреждение",320),("title","Страница",350),("run","Обход",60)):
            tree.heading(column,text=label)
            tree.column(column,width=width,stretch=column in ("org","title"))
        tree.pack(fill="both",expand=True)
        rows = self.db.execute("""SELECT b.page_id,o.name,b.title,f.run_id,p.url
            FROM page_passport b JOIN page p ON p.id=b.page_id JOIN organization o ON o.id=p.id_organization
            JOIN crawl_fetch f ON f.page_id=b.page_id ORDER BY b.page_id DESC""").fetchall()
        def filter_rows(*_):
            terms = term.get().casefold().split()
            tree.delete(*tree.get_children())
            count = 0
            for row in rows:
                text = " ".join(str(value) for value in row).casefold()
                if all(word in text for word in terms):
                    tree.insert("", "end",iid=str(row[0]),values=tuple(row)[:4])
                    count += 1
                    if count==200:
                        break
        def pick(*_):
            selected = tree.selection()
            if selected:
                self.pid.set(selected[0])
                self._load_checks()
                dialog.destroy()
        ttk.Button(box,text="Выбрать страницу",command=pick).pack(anchor="e",pady=(8,0))
        term.trace_add("write",filter_rows)
        tree.bind("<Double-1>",pick)
        tree.bind("<Return>",pick)
        filter_rows()
        field.focus()

    def _selected(self):
        if self.loaded is None or str(self.loaded)!=self.pid.get().strip():
            raise ValueError("Сначала откройте снимок по выбранному ID")
        return self.loaded

    def _preview(self):
        pid = self._selected()
        check = self._current_check()
        if not check or not check.get("visual"):
            return
        guide = self.include_guide.get()
        self._job("Подготавливаю изображения…",
                  lambda c:visual_bundle(c,pid,check,include_guide=guide),
                  lambda bundle:self._preview_dialog(pid,check,bundle))

    def _preview_dialog(self,pid,check,bundle):
        from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Снимок {pid} · {_check_label(check)}")
        dialog.transient(self.root)
        panel = ttk.Frame(dialog,padding=12)
        panel.pack(fill="both",expand=True)
        if not bundle:
            ttk.Label(panel,text="В архиве нет подходящего изображения для этого вопроса.",wraplength=440).pack(
                padx=12,pady=(15,10))
            ttk.Button(panel,text="Закрыть",command=dialog.destroy).pack(anchor="e")
            return dialog
        dialog.geometry("830x650")
        panel.columnconfigure(0,weight=1)
        panel.rowconfigure(2,weight=1)
        captions = [f"{i}. {caption}" for i,(_,_,caption) in enumerate(bundle,1)]
        picker = ttk.Combobox(panel,state="readonly",values=captions)
        picker.grid(row=0,column=0,columnspan=2,sticky="ew")
        picker.current(0)
        source = tk.StringVar(master=dialog)
        ttk.Label(panel,textvariable=source,wraplength=775,justify="left").grid(
            row=1,column=0,columnspan=2,sticky="w",pady=6)
        canvas = tk.Canvas(panel,background="#eeeeee",highlightthickness=0)
        canvas.grid(row=2,column=0,sticky="nsew")
        horizontal = ttk.Scrollbar(panel,orient="horizontal",command=canvas.xview)
        vertical = ttk.Scrollbar(panel,orient="vertical",command=canvas.yview)
        horizontal.grid(row=3,column=0,sticky="ew")
        vertical.grid(row=2,column=1,sticky="ns")
        canvas.configure(xscrollcommand=horizontal.set,yscrollcommand=vertical.set)
        original = tk.BooleanVar(master=dialog,value=False)
        selected_image = None

        def draw(*_):
            canvas.delete("all")
            if selected_image is None:
                canvas.configure(scrollregion=(0,0,0,0))
                canvas.preview_image = None
                return
            width,height = max(1,canvas.winfo_width()),max(1,canvas.winfo_height())
            frame = selected_image
            if not original.get() and (frame.width>width or frame.height>height):
                frame = selected_image.copy()
                frame.thumbnail((width,height),Image.Resampling.LANCZOS)
            canvas.preview_image = ImageTk.PhotoImage(frame,master=dialog)
            canvas.create_image(max(width,frame.width)//2,max(height,frame.height)//2,image=canvas.preview_image)
            canvas.configure(scrollregion=(0,0,max(width,frame.width),max(height,frame.height)))

        def choose(*_):
            nonlocal selected_image
            meta,data,caption = bundle[picker.current()]
            source.set(caption+"\n"+_preview_source(meta))
            selected_image = None
            try:
                with Image.open(BytesIO(data)) as img:
                    selected_image = ImageOps.exif_transpose(img).convert("RGB")
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
                source.set(source.get()+" · Не удалось открыть изображение.")
            canvas.xview_moveto(0)
            canvas.yview_moveto(0)
            draw()
        bar = ttk.Frame(panel)
        bar.grid(row=4,column=0,columnspan=2,sticky="ew",pady=(6,0))
        ttk.Checkbutton(bar,text="Исходный размер (100%)",variable=original,command=draw).pack(side="left")
        ttk.Button(bar,text="Закрыть",command=dialog.destroy).pack(side="right")
        picker.bind("<<ComboboxSelected>>",choose)
        canvas.bind("<Configure>",draw)
        choose()
        return dialog

    def _review(self):
        pid = self._selected()
        check = self._current_check()
        if not check:
            raise ValueError("Выберите вопрос для проверки")
        check_id = check["id"]
        guide = self.include_guide.get()
        profile = get_profile(self.db)
        def done(r):
            if self.loaded!=pid:
                self.status.set(f"Ответ сохранён для снимка {pid}, запись {r['assessment_id']}.")
                return
            row = self.db.execute("SELECT json_extract(request,'$.checks[0].visual') FROM model_assessment WHERE id=?",
                                  (r["assessment_id"],)).fetchone()
            visual = (json.loads(row[0]) if row[0] else None) if row else check.get("visual")
            value = r["error"] or r["response"]["results"][0]["evidence"]
            _display(self.result,f"Снимок {pid}   ·   {profile.model}\n{_check_label(check)}\n"
                     f"{_decision(r['outcome'],visual)}\n\n{value}\n\n"
                     f"Запись {r['assessment_id']}   ·   {r['seconds']} с")
        self._job(f"Модель {profile.model} проверяет снимок {pid}…",
                  lambda c:assess(c,pid,check_id,profile=profile,include_guide=guide),done)

    def _factcheck(self):
        pid = self._selected()
        urls = tuple(dict.fromkeys(self.source_urls.get().split()))
        for url in urls:
            address = urlsplit(url)
            if address.scheme not in ("https","http") or not address.hostname or address.username or address.password:
                raise ValueError("Каждый дополнительный источник должен быть веб-адресом без логина и пароля.")
        def done(r):
            lines = [f"Снимок {pid}   ·   проверка {r['check_id']}",
                     f"Проведение события · {FACT_OUTCOMES[r['event_status']]} · источников {r['event_source_count']}",
                     f"Участие учреждения · {FACT_OUTCOMES[r['status']]} · источников {r['source_count']}",""]
            for s in r["sources"]:
                origin = "указан вручную" if s.get("discovery")=="provided" else "найден поиском"
                state = "вне списка допустимых источников" if s["status"]=="source_not_allowed" else s["status"]
                lines.extend([s.get("title") or s["url"],s.get("excerpt",""),f"{origin} · {state}   {s['url']}",""])
            if r["claim"].get("allowlist_status")=="empty":
                lines.append("Список допустимых источников пуст. Добавьте базовый URL во вкладке «Источники».")
            for search in r["searches"]:
                if search.get("error"):
                    lines.append(str(search["error"]))
            if r["claim"].get("allowlist_fingerprint") != allowlist_fingerprint(self.db):
                lines.insert(1,"За время запроса список допустимых источников изменился. Сохранён результат по прежнему списку.")
            _display(self.result,"\n".join(lines))
        self._job("Ищу внешние источники и сверяю материалы…",lambda c:run_factcheck(c,pid,candidate_urls=urls),done)

    def _batch(self):
        pid = self._selected()
        run = self.db.execute("SELECT run_id FROM crawl_fetch WHERE page_id=?",(pid,)).fetchone()[0]
        def done(r):
            _display(self.result,f"Фактчекинг обхода {run}\n\nОбработано: {r['processed']}\n"
                     f"Осталось в очереди: {r['remaining']}\nОшибок в партии: {r['errors']}\n\n"
                     "Проверенные снимки: "+", ".join(map(str,r["processed_ids"])))
        self._job(f"Проверяю партию публикаций из обхода {run}…",lambda c:batch_check(c,[run]),done)

    def _model_batch_tab(self):
        frame = self.tab["Очередь ИИ"]
        frame.columnconfigure(1,weight=1)
        frame.rowconfigure(5,weight=1)
        scopes = ("Снимок страницы","Учреждение","Обход","Весь архив")
        self.batch_scope = tk.StringVar(value=scopes[0])
        self.batch_id = tk.StringVar(value=self.pid.get())
        self.batch_group = tk.StringVar(value="Все группы")
        self.batch_limit = tk.StringVar(value="5")
        scope = ttk.Combobox(frame,textvariable=self.batch_scope,values=scopes,state="readonly")
        _field(frame,0,"Какие материалы взять",scope)
        entry = ttk.Entry(frame,textvariable=self.batch_id)
        _field(frame,1,"ID снимка, учреждения или обхода",entry)
        scope.bind("<<ComboboxSelected>>",lambda _:entry.configure(
            state="disabled" if self.batch_scope.get()=="Весь архив" else "normal"))
        _field(frame,2,"Группа вопросов",ttk.Combobox(frame,textvariable=self.batch_group,
               values=("Все группы","Оригиналы расписаний",*GROUPS.values()),state="readonly"))
        _field(frame,3,"Вопросов за запуск",ttk.Spinbox(frame,textvariable=self.batch_limit,from_=1,to=10000))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4,column=0,columnspan=2,sticky="w",pady=10)
        ttk.Button(buttons,text="Посчитать доступные",command=lambda:self._process_batch(True)).pack(side="left")
        ttk.Button(buttons,text="Передать модели",command=lambda:self._process_batch(False)).pack(side="left",padx=9)
        ttk.Button(buttons,text="Ход догрузки архива",command=self._archive_progress).pack(side="left")
        self.batch_result = _text_output(frame)
        self.batch_result.grid(row=5,column=0,columnspan=2,sticky="nsew")

    def _archive_progress(self):
        def counts(conn):
            states = dict(conn.execute("SELECT state,count(*) FROM material_backfill GROUP BY state"))
            bindings = dict(conn.execute("SELECT state,count(*) FROM page_attachment GROUP BY state"))
            files,size = conn.execute("""SELECT count(*),coalesce(sum(bytes),0) FROM
                (SELECT sha256,max(byte_size) bytes FROM available_page_attachment
                 WHERE state='saved' GROUP BY sha256)""").fetchone()
            runs = conn.execute("""SELECT count(*),count(DISTINCT p.id_organization),coalesce(sum(r.pdf IS NOT NULL),0)
                FROM page p JOIN crawl_fetch f ON f.page_id=p.id
                JOIN material_backfill b ON b.fetch_id=f.id LEFT JOIN page_render r ON r.page_id=p.id
                WHERE b.state='saved'""").fetchone()
            return (f"Догрузка страниц · {runs[0]} сохранено · учреждений {runs[1]} · с PDF {runs[2]}\n"
                    f"В очереди {states.get('pending',0)} · в работе {states.get('working',0)} · "
                    f"не удалось открыть {states.get('failed',0)}\n\n"
                    f"Оригиналы вложений · {files} различных файлов · {size/2**30:.2f} ГиБ\n"
                    f"Привязки к снимкам · {bindings.get('saved',0)} получено · "
                    f"{bindings.get('pending',0)} ожидают · {bindings.get('error',0)} с ошибкой")
        self._job("Читаю состояние очередей из базы…",counts,lambda note:_display(self.batch_result,note))

    def _process_batch(self, plan):
        args = {}
        scope = {"Снимок страницы":"page_ids","Учреждение":"org_ids","Обход":"run_ids"}.get(self.batch_scope.get())
        if scope:
            args[scope] = (int(self.batch_id.get()),)
        group = next((key for key,label in GROUPS.items() if label==self.batch_group.get()),None)
        if self.batch_group.get()=="Оригиналы расписаний":
            args["attachment_role"] = "schedule"
        limit = int(self.batch_limit.get())
        def done(r):
            lines = [f"Снимков в выборке · {r['pages']}"]
            if plan:
                lines.extend([f"Можно передать модели · {r['ready']}",f"Уже завершено в партиях · {r['completed']}",
                              f"Требуется материал · {r['missing']}"])
                lines.extend(f"{GROUPS[k]} · {v}" for k,v in r["by_group"].items())
            else:
                lines.append(f"Обработано вопросов в этой партии · {r['processed']}")
                lines.extend(f"{OUTCOMES.get(k,k)} · {v}" for k,v in r["outcomes"].items())
                if r["last"]:
                    lines.append(f"Последний снимок · {r['last']['page_id']} · запись ИИ № {r['last']['assessment_id']}")
                if r.get("stop_reason"):
                    lines.append(r["stop_reason"])
                lines.append("Ответы и использованные материалы доступны в истории снимка.")
            _display(self.batch_result,"\n".join(lines))
        self._job("Подсчитываю материалы…" if plan else "Передаю выбранные материалы модели…",
                  lambda conn:batch_assess(conn,**args,plan=plan,limit=limit,group=group,include_photos=True),done)

    def _worker_tab(self):
        if __package__:
            from .audit_worker import get_settings
            from .retention import get_policy
        else:
            from audit_worker import get_settings
            from retention import get_policy
        frame = self.tab["Автоматизация"]
        frame.columnconfigure(1,weight=1)
        frame.columnconfigure(3,weight=1)
        defaults = get_settings(self.db)
        fields = (("interval_minutes","Интервал запуска, минут"),("revisit_days","Обновлять сайт через, дней"),
                  ("organizations_per_tick","Учреждений за запуск"),("pages_per_org","Страниц основного обхода на сайт"),
                  ("max_depth","Глубина основного обхода"),("frontier_pages","Страниц на догрузку"),
                  ("attachment_files","Оригиналов на догрузку"),("model_checks","Вопросов модели за запуск"),
                  ("factcheck_items","Публикаций для фактчекинга"),("news_months","Глубина ленты новостей, месяцев"))
        self.worker_values = {}
        for idx,(key,label) in enumerate(fields):
            r,c = idx//2,(idx%2)*2
            ttk.Label(frame,text=label).grid(row=r,column=c,sticky="w",padx=(0,8),pady=6)
            var = self.worker_values[key] = tk.StringVar(value=str(defaults[key]))
            ttk.Entry(frame,textvariable=var,width=9).grid(row=r,column=c+1,sticky="w",padx=(0,18))
        controls = ttk.Frame(frame)
        controls.grid(row=5,column=0,columnspan=4,sticky="w",pady=10)
        for text,action in (("Сохранить","save"),("Выполнить сейчас","once"),("Включить расписание","enable"),
                            ("Отключить расписание","disable"),("Состояние","status")):
            ttk.Button(controls,text=text,command=lambda action=action:self._worker_action(action)).pack(side="left",padx=(0,6))
        policy = get_policy(self.db)
        self.keep_days = tk.StringVar(value=str(policy["days"]))
        self.clean_replaced = tk.BooleanVar(value=policy["enabled"])
        storage = ttk.LabelFrame(frame,text="Хранение архива",padding=(8,6))
        storage.grid(row=6,column=0,columnspan=4,sticky="ew",pady=(2,8))
        ttk.Checkbutton(storage,text="Очищать заменённые материалы старше",variable=self.clean_replaced).pack(side="left")
        ttk.Entry(storage,textvariable=self.keep_days,width=5).pack(side="left")
        ttk.Label(storage,text="дней").pack(side="left",padx=(4,12))
        ttk.Button(storage,text="Сохранить правило",command=lambda:self._storage(True)).pack(side="left")
        ttk.Button(storage,text="Предпросмотр очистки",command=lambda:self._storage(False)).pack(side="left",padx=6)
        self.worker_info = _text_output(frame,height=7)
        self.worker_info.grid(row=7,column=0,columnspan=4,sticky="nsew")
        frame.rowconfigure(7,weight=1)
        _display(self.worker_info,"Система сама выбирает учреждения, открывает недостающие страницы, сохраняет файлы "
                 "и передаёт вопросы активной модели. Расписание Windows действует и при закрытом окне администратора.")

    def _worker_action(self, action):
        if __package__:
            from .audit_worker import last_status,run_once,save_settings
            from .worker_schedule import disable_schedule,enable_schedule,schedule_info
        else:
            from audit_worker import last_status,run_once,save_settings
            from worker_schedule import disable_schedule,enable_schedule,schedule_info
        values = {key:int(var.get()) for key,var in self.worker_values.items()} if action in ("save","enable","once") else {}
        def work(conn):
            if values:
                with conn:
                    save_settings(conn,values)
            if action=="enable":
                enable_schedule(conn,values["interval_minutes"])
            elif action=="disable":
                disable_schedule(conn)
            elif action=="once":
                return run_once(conn)
            elif action=="save":
                current = schedule_info(conn)
                if current.get("enabled"):
                    enable_schedule(conn,values["interval_minutes"])
            return {"schedule":schedule_info(conn),"last":last_status(conn)}
        def done(result):
            lines = []
            if "schedule" in result:
                info = result["schedule"]
                lines.append("Расписание Windows включено." if info.get("enabled") else "Расписание Windows выключено.")
                if info.get("error"):
                    lines.append(str(info["error"]))
                result = result.get("last")
            if result:
                lines.append("Последний автоматический запуск")
                lines.append(_cycle_summary(result))
            else:
                lines.append("В этой базе пока нет записи о завершённом автоматическом запуске.")
            _display(self.worker_info,"\n".join(lines))
        self._job("Выполняю автоматический цикл…" if action=="once" else "Проверяю настройки запуска…",work,done)

    def _storage(self, save):
        if __package__:
            from .retention import maintain,save_policy
        else:
            from retention import maintain,save_policy
        if save:
            policy = save_policy(self.db,{"days":int(self.keep_days.get()),"enabled":self.clean_replaced.get()})
            _display(self.worker_info,f"Сохранено правило хранения · {policy['days']} дней. "
                     + ("Очистка включена в автоматический цикл." if policy["enabled"] else "Автоматическая очистка выключена."))
            return
        def done(stats):
            _display(self.worker_info,f"Предпросмотр по сохранённому правилу · {stats['days']} дней\n"
                     f"Подходящих снимков в партии · {stats['snapshots']}\n"
                     f"Объём заменённых материалов · {stats['bytes']/2**20:.1f} МиБ")
        self._job("Проверяю возраст материалов и ссылки на доказательства…",lambda conn:maintain(conn,apply=False),done)

    def _sources_tab(self):
        frame = self.tab["Источники"]
        frame.columnconfigure(0,weight=1)
        frame.rowconfigure(2,weight=1)
        ttk.Label(frame,text="Допустимые источники для фактчекинга",font=("Segoe UI",13,"bold")).grid(
            row=0,column=0,columnspan=2,sticky="w")
        ttk.Label(frame,text="Укажите базовый URL сайта или раздела. Например, https://example.ru/news/ "
                  "разрешает публикации в этом разделе. Для другого поддомена или схемы нужен свой URL.",
                  wraplength=720,justify="left").grid(row=1,column=0,columnspan=2,sticky="w",pady=(5,12))
        self.allowed_list = tk.Listbox(frame,height=8,exportselection=False)
        self.allowed_list.grid(row=2,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(frame,command=self.allowed_list.yview)
        scroll.grid(row=2,column=1,sticky="ns")
        self.allowed_list.configure(yscrollcommand=scroll.set)
        controls = ttk.Frame(frame)
        controls.grid(row=3,column=0,columnspan=2,sticky="ew",pady=12)
        controls.columnconfigure(1,weight=1)
        ttk.Label(controls,text="Базовый URL").grid(row=0,column=0,padx=(0,8))
        self.publisher_url = tk.StringVar()
        url = ttk.Entry(controls,textvariable=self.publisher_url)
        url.grid(row=0,column=1,sticky="ew")
        url.bind("<Return>",lambda _:self._edit_source(True))
        ttk.Button(controls,text="Добавить",command=lambda:self._edit_source(True)).grid(row=0,column=2,padx=8)
        ttk.Button(controls,text="Удалить выбранный",command=lambda:self._edit_source(False)).grid(row=0,column=3)
        self.source_count = tk.StringVar()
        ttk.Label(frame,textvariable=self.source_count,wraplength=720).grid(row=4,column=0,columnspan=2,sticky="w")
        self._show_sources()

    def _show_sources(self):
        items = get_allowed_sources(self.db)
        self.allowed_list.delete(0,"end")
        for base in items:
            self.allowed_list.insert("end",base)
        self.source_count.set(f"Допустимых базовых URL · {len(items)}" if items else
                              "Список пуст. Для внешних подтверждений добавьте источники.")

    def _edit_source(self, add):
        with self.db:
            if add:
                changed = add_allowed_source(self.db,self.publisher_url.get().strip())
            else:
                selected = self.allowed_list.curselection()
                if not selected:
                    raise ValueError("Сначала выберите базовый URL в списке.")
                changed = remove_allowed_source(self.db,self.allowed_list.get(selected[0]))
        self.publisher_url.set("")
        self._show_sources()
        if changed:
            if self.loaded is not None and str(self.loaded)==self.pid.get().strip():
                self._history()
            self.status.set("Список сохранён. Новые проверки будут использовать обновлённые источники.")
        else:
            self.status.set("Этот базовый URL уже есть в списке." if add else "Этот базовый URL уже удалён.")

    def _rules_tab(self):
        frame = self.tab["Правила и эталоны"]
        frame.columnconfigure(1,weight=1)
        frame.rowconfigure(7,weight=1)
        opts = setting(self.db,"factcheck",{})
        self.minimum = tk.StringVar(value=str(opts.get("minimum_sources",2)))
        self.limit = tk.StringVar(value=str(opts.get("limit",6)))
        self.share = tk.StringVar(value=f"{100*setting(self.db,'reference_minimum',0.5):g}")
        self.auto_fact = tk.BooleanVar(value=setting(self.db,"factcheck_auto",False))
        self.batch_limit = tk.StringVar(value=str(setting(self.db,"factcheck_batch_limit",3)))
        _field(frame,0,"Независимых источников",ttk.Spinbox(frame,textvariable=self.minimum,from_=2,to=30))
        _field(frame,1,"Кандидатов на проверку",ttk.Spinbox(frame,textvariable=self.limit,from_=2,to=30))
        self.sites = tk.Text(frame,height=3,wrap="none",font="TkTextFont")
        self.sites.insert("1.0","\n".join(opts.get("search_sites",DEFAULT_SEARCH_SITES)))
        _field(frame,2,"Поиск через WordPress, по URL в строке",self.sites)
        _field(frame,3,"Порог типичного поля шапки, %",ttk.Entry(frame,textvariable=self.share))
        batch = ttk.Frame(frame)
        ttk.Spinbox(batch,textvariable=self.batch_limit,from_=1,to=100,width=5).pack(side="left")
        ttk.Checkbutton(batch,variable=self.auto_fact,text="Запускать партию после обхода").pack(side="left",padx=10)
        _field(frame,4,"Публикаций в одной партии",batch)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5,column=0,columnspan=2,sticky="w",pady=8)
        ttk.Button(buttons,text="Сохранить правила",command=self._save_rules).pack(side="left")
        for kind,label in (("school","Эталон школ"),("kindergarten","Эталон детсадов")):
            ttk.Button(buttons,text=label,command=lambda k=kind:self._reference(k)).pack(side="left",padx=(8,0))
        ttk.Button(buttons,text="Сравнить документы…",command=self._documents).pack(side="left",padx=(8,0))
        self.reference = _text_output(frame,7)
        self.reference.grid(row=7,column=0,columnspan=2,sticky="nsew")

    def _save_rules(self):
        minimum,limit = int(self.minimum.get()),int(self.limit.get())
        batch_limit = int(self.batch_limit.get())
        if not 1<=batch_limit<=100:
            raise ValueError("Размер партии должен быть от 1 до 100 публикаций")
        share = float(self.share.get().replace(",","."))
        sites = list(dict.fromkeys(s.strip().rstrip("/") for s in self.sites.get("1.0","end").splitlines() if s.strip()))
        if not 2<=minimum<=limit<=30 or not 0<share<=100:
            raise ValueError("Нужно 2–30 источников, лимит не меньше минимума и процент в диапазоне (0, 100].")
        for url in sites:
            u = urlsplit(url)
            if u.scheme!="https" or not u.hostname or u.query or u.fragment or u.username or u.password:
                raise ValueError("Укажите публичные HTTPS-адреса поисковых площадок без параметров")
        if len(sites)>16:
            raise ValueError("Максимум 16 поисковых площадок")
        with self.db:
            set_setting(self.db,"factcheck",{"minimum_sources":minimum,"limit":limit,"search_sites":sites})
            set_setting(self.db,"reference_minimum",share/100)
            set_setting(self.db,"factcheck_auto",self.auto_fact.get())
            set_setting(self.db,"factcheck_batch_limit",batch_limit)
        self.status.set("Правила сохранены для следующих проверок.")

    def _reference(self,kind):
        r = reference_report(self.db,kind,minimum_prevalence=setting(self.db,"reference_minimum",0.5))
        lines = [f"Обследовано учреждений: {r['assessed']} из {r['registered']}. Порог: {100*r['threshold']:g}%.",""]
        for label, group in (("В шапке и под шапкой",r["fields"]),("На странице контактов",r["contact_section_fields"])):
            lines.append(label)
            for field,v in group.items():
                lines.append(f"{FIELDS.get(field,field)}  ·  {v['count']} из {v['assessed']}")
            lines.append("")
        best = r["reference"]
        if best:
            lines.extend(["",f"Эталон по проверенным признакам: ID {best['organization_id']} · {best['name']}",
                          "С тем же результатом: "+", ".join(map(str,r["equal_candidates"])),r["scope"]])
        _display(self.reference,"\n".join(lines))

    def _documents(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Документы учреждения и эталона")
        dialog.geometry("880x600")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog,padding=14)
        frame.pack(fill="both",expand=True)
        rows = self.db.execute("SELECT id,name,kind FROM organization ORDER BY id").fetchall()
        choices = {f"{oid} · {name}":(oid,kind) for oid,name,kind in rows}
        selection = tk.StringVar()
        selector = ttk.Combobox(frame,values=list(choices),textvariable=selection,state="readonly")
        selector.pack(fill="x")
        output = _text_output(frame)
        cache = {}
        def compare(*_):
            label = selection.get()
            if not label:
                return
            org,kind = choices[label]
            known = cache.get(kind)
            def read(conn):
                report = known or reference_report(conn,kind)
                candidate = next((x for x in report["candidates"] if x["organization_id"]==org),None)
                if not candidate:
                    return report, ["У учреждения ещё нет обследованной главной страницы."]
                docs = candidate["documents"]
                inventory = document_inventory(conn,candidate["run_id"])
                ref = report["reference"]
                coverage = docs["coverage"]
                lines = [f"Эталон {ref['organization_id']} · {ref['name']}",
                         f"Обход {candidate['run_id']} · страниц каталога {coverage['observed_catalog_pages']} · "
                         f"ссылок на файлы в каталоге {docs['catalog_files']}, в других разделах {docs['other_files']}.",
                         f"Непосещённых ссылок на продолжения каталога {len(coverage['unvisited_pagination'])}, "
                         f"на карточки документов {len(coverage['unvisited_detail_urls'])}.",""]
                lines.append("Совпали темы документов")
                for group in docs["matched_groups"]:
                    lines.extend([group["label"],f"У учреждения · {group['institution']['title']} · {group['institution']['url']}",
                                  f"У эталона · {group['reference']['title']} · {group['reference']['url']}",""])
                if not docs["matched_groups"]:
                    lines.extend(["—",""])
                for key,heading in (("reference_groups_unseen","Темы эталона, не обнаруженные в этом каталоге"),
                                    ("additional_groups","Дополнительные темы в каталоге учреждения")):
                    labels = ", ".join(g["label"] for g in docs[key]) or "—"
                    lines.extend([heading,labels,""])
                lines.append("Совпадений названий без учёта регистра и пунктуации · "+str(len(docs["matching_titles"])))
                lines.extend(m["institution"]["title"] for m in docs["matching_titles"])
                lines.extend(["",report["documents"]["scope"],"","Ссылки, обнаруженные у учреждения",""])
                for item in inventory["items"]:
                    where = "каталог" if item["catalog"] else "другой раздел"
                    lines.extend([f"{item['title']} · {item['format']} · {where}",item["url"],""])
                return report,lines
            def show(value):
                report,lines = value
                cache[kind] = report
                if dialog.winfo_exists() and label==selection.get():
                    _display(output,"\n".join(lines))
            self._job("Сопоставляю перечни документов…",read,show)
        ttk.Button(frame,text="Показать сравнение",command=compare).pack(anchor="w",pady=8)
        output.pack(fill="both",expand=True)
        if choices:
            selector.current(0)
            compare()

    def _words_tab(self):
        frame = self.tab["Поисковые слова"]
        frame.columnconfigure(1,weight=1)
        frame.rowconfigure(1,weight=1)
        self.category,self.new_word = tk.StringVar(),tk.StringVar()
        cats = ttk.Combobox(frame,values=list(CATEGORY_LABELS.values()),state="readonly",textvariable=self.category)
        _field(frame,0,"Искать в категории",cats)
        self.word_list = tk.Listbox(frame,height=9,exportselection=False)
        self.word_list.grid(row=1,column=0,columnspan=2,sticky="nsew")
        actions = ttk.Frame(frame)
        actions.grid(row=2,column=0,columnspan=2,sticky="ew",pady=10)
        actions.columnconfigure(0,weight=1)
        ttk.Entry(actions,textvariable=self.new_word).grid(row=0,column=0,sticky="ew")
        ttk.Button(actions,text="Добавить",command=lambda:self._word(True)).grid(row=0,column=1,padx=8)
        ttk.Button(actions,text="Удалить выбранное",command=lambda:self._word(False)).grid(row=0,column=2)
        cats.current(0)
        cats.bind("<<ComboboxSelected>>",lambda e:self._words())
        self._words()

    def _category(self):
        return next(k for k,v in CATEGORY_LABELS.items() if v==self.category.get())

    def _words(self):
        self.word_list.delete(0,"end")
        for word in get_terms(self.db)[self._category()]:
            self.word_list.insert("end",word)

    def _word(self,add):
        if not add and not self.word_list.curselection():
            raise ValueError("Выберите слово в списке")
        text = self.new_word.get() if add else self.word_list.get(self.word_list.curselection()[0])
        with self.db:
            (add_term if add else remove_term)(self.db,self._category(),text)
        self.new_word.set("")
        self._words()
        self.status.set("Словарь сохранён.")


def main():
    with closing(connect_db()) as conn:
        initialize_db(conn)
        root = tk.Tk()
        Admin(root,conn)
        root.mainloop()


if __name__=="__main__":
    main()
