"use strict";
const byId = id => document.getElementById(id);
const fieldLabels = {
  interval_minutes:["Как часто запускать обновление, минут","1440 означает один запуск в сутки при включённом расписании."],
  revisit_days:["Через сколько дней сайт снова попадёт в очередь","Сначала обходятся новые и давно не проверявшиеся сайты. Это минимальный интервал между обычными обходами."],
  organizations_per_tick:["Сколько сайтов открыть за запуск","Например, при одном сайте в день первый обход 80 учреждений займёт минимум 80 запусков. Ноль отключает этот этап."],
  pages_per_org:["Сколько основных страниц загрузить с одного сайта","Главная и страницы из её меню входят в этот лимит. Дополнительные ссылки учитываются отдельно."],
  max_depth:["На сколько переходов уходить от главной","0 — только главная, 1 — также страницы по ссылкам с главной, 2 — ещё один переход."],
  frontier_pages:["Сколько дополнительных страниц взять из очереди","Общий лимит для всех учреждений за запуск. Например, страницы документов и профили сотрудников."],
  attachment_files:["Сколько файлов скачать за запуск","Общий лимит для вложений, например PDF с расписанием. Ноль пропускает загрузку файлов."],
  model_checks:["Сколько вопросов передать ИИ за запуск","Действует при включённом ИИ. Один вопрос по большому документу может потребовать нескольких запросов."],
  factcheck_items:["Сколько публикаций сверить с независимыми источниками","Общий лимит за запуск. Сверка работает при включённой проверке публикаций в разделе источников."],
  news_months:["За сколько последних месяцев собирать новости","Например, 12 ограничивает глубину сбора новостей последним годом."]
};
const profiles = {lmstudio:"LM Studio",ollama:"Ollama",compatible:"OpenAI-совместимый API"};
let state;
async function call(action, data) {
  const response = await fetch("/api/admin/"+action,data===undefined?{}:{
    method:"POST",headers:{"Content-Type":"application/json","X-Audit-Token":state.token},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || "Запрос не выполнен");
  return result;
}
function bind(id,event,work) {
  byId(id).addEventListener(event,async e=>{
    e.preventDefault();
    try {byId("message").textContent="";await work();}
    catch(err){byId("message").textContent=err.message;}
  });
}
function option(parent,value,title){parent.add(new Option(title,value));}
function displayList(id,items,remove) {
  const list=byId(id);list.replaceChildren();
  items.forEach(value=>{
    const item=document.createElement("li");const phrase=document.createElement("span");phrase.textContent=value;item.append(phrase);
    const button=document.createElement("button");button.type="button";button.textContent="Удалить";
    button.addEventListener("click",()=>remove(value).catch(e=>byId(id==="words"?"word-result":"message").textContent=e.message));
    item.append(button);list.append(item);
  });
}
function sources(){displayList("sources",state.sources,async url=>{
  state.sources=(await call("sources",{operation:"remove",url})).sources;sources();
});}
function words(){const category=byId("word-cat").value;displayList("words",state.words[category]||[],async word=>{
  state.words=(await call("words",{operation:"remove",category,word})).words;words();byId("word-result").textContent="Фраза удалена.";
});}
function profile(){
  const p=state.profiles[byId("provider").value];byId("endpoint").value=p.endpoint;
  byId("model-id").value=p.model;byId("timeout").value=p.timeout;byId("response").value=p.response_mode;
  byId("thinking").checked=p.disable_thinking;byId("secret").value="";
}
function showAiStatus(status) {
  state.ai_status=status;
  const label=status==="ИИ настроен"?"ИИ включён":status;
  byId("ai-status").textContent=label;
  byId("access").textContent=label;
  const budget=document.querySelector('[name="model_checks"]');
  budget.disabled=status!=="ИИ настроен";
  budget.parentElement.title=budget.disabled?status+". Настройте подключение, чтобы менять лимит вопросов ИИ.":"";
}
function wireRegistry() {
  const examples={
    json:['Массив объектов. Обязательные поля: name, kind, site_url.', '[{"name":"Школа № 1","kind":"school","site_url":"https://school.example"}]'],
    csv:['Первая строка — заголовки. Разделитель — запятая или точка с запятой. Поля с разделителем заключайте в кавычки.', 'name,kind,site_url\nШкола № 1,school,https://school.example'],
    txt:['Одна запись в строке: название & тип & ссылка.', 'Детский сад № 2 & детский сад & https://sad.example']
  };
  function schema(){
    const fmt=byId("registry-format").value;
    byId("registry-file").accept="."+fmt;
    byId("registry-help").textContent=examples[fmt][0]+" Типы: school или школа, kindergarten или детский сад.";
    byId("registry-schema").textContent=examples[fmt][1];
  }
  function draw(){
    const q=byId("registry-search").value.trim().toLowerCase(),list=byId("registry-list");
    list.replaceChildren();
    for(const org of state.organizations.filter(o=>(o.name+" "+o.site_url).toLowerCase().includes(q))){
      const li=document.createElement("li"),btn=document.createElement("button");btn.type="button";btn.textContent="Убрать";
      li.append(document.createTextNode(org.name+" · "+(org.kind==="school"?"Школа":"Детский сад")+" · "+org.site_url+" "),btn);list.append(li);
      btn.onclick=()=>change({operation:"remove",id:org.id});
    }
    byId("registry-count").textContent="В списке: "+state.organizations.length+". Показано: "+list.children.length+".";
  }
  async function change(payload){
    const out=byId("registry-message");
    try{
      const saved=await call("roster",payload);
      state.organizations=saved.organizations;
      const ids=new Set(state.organizations.map(o=>o.id));for(const id of chosen) if(!ids.has(id)) chosen.delete(id);
      renderOrgs();invalidatePlan();draw();
      out.textContent=["add","import"].includes(payload.operation)?"Добавлено: "+saved.inserted+". Совпало по адресу или восстановлено: "+saved.matched+".":"Список обновлён. История проверок сохранена.";
      return true;
    }catch(e){out.textContent=e.message;return false;}
  }
  byId("registry-format").onchange=()=>{byId("registry-file").value="";schema();};
  byId("registry-search").oninput=draw;
  bind("registry-add","submit",async()=>{const form=byId("registry-add");if(await change({operation:"add",record:Object.fromEntries(new FormData(form))})) form.reset();});
  bind("registry-import","submit",async()=>{const file=byId("registry-file").files[0];
    if(file.size>2000000){byId("registry-message").textContent="Выберите файл до 2 МБ.";return;}
    let content;
    try{content=new TextDecoder("utf-8",{fatal:true}).decode(await file.arrayBuffer());}
    catch(error){byId("registry-message").textContent="Не удалось прочитать файл в кодировке UTF-8.";return;}
    await change({operation:"import",format:byId("registry-format").value,content});
  });
  byId("registry-clear").onclick=()=>{if(confirm("Очистить список в обоих режимах? История сохранится. Повторный импорт сайта вернёт его в список.")) change({operation:"clear",confirmed:true});};
  schema();draw();
}
async function init() {
  state=await call("state");
  if(state.demo){
    byId("demo-tools").hidden=false;
    bind("restore-demo","click",async()=>{await call("restore-demo",{});location.reload();});
    for(const id of ["provider","endpoint","model-id","secret","sched-enable","sched-disable","sched-status","registry-clear","source-url"])byId(id).disabled=true;
    for(const form of ["registry-add","registry-import","source-form"])
      for(const input of byId(form).elements)input.disabled=true;
  }
  if (!state.windows_schedule) {
    for (const id of ["sched-status","sched-enable","sched-disable"]) byId(id).hidden=true;
    byId("schedule-status").textContent="В Linux запустите sh start.sh --once для одного цикла или sh start.sh --loop для повторения с заданным интервалом.";
  }
  for (const [key,[name,help]] of Object.entries(fieldLabels)){
    const label=document.createElement("label"),input=document.createElement("input");
    input.type="number";input.required=true;input.min=["interval_minutes","revisit_days","pages_per_org","news_months"].includes(key)?1:0;
    input.name=key;input.value=state.settings[key];const hint=document.createElement("small");hint.className="help";hint.textContent=help;label.append(document.createTextNode(name),input,hint);byId("cycle-fields").append(label);
  }
  byId("clean").checked=state.retention.enabled;byId("days").value=state.retention.days;
  byId("fact-auto").checked=state.factcheck_auto;
  for(const [key,title] of Object.entries(profiles))option(byId("provider"),key,title);
  byId("ai-enabled").checked=state.ai_enabled;showAiStatus(state.ai_status);
  byId("provider").value=state.provider;profile();sources();
  for(const [key,title] of Object.entries(state.word_labels))option(byId("word-cat"),key,title);
  words();
  byId("open-words").onclick=()=>byId("word-dialog").showModal();
  byId("close-words").onclick=()=>byId("word-dialog").close();
  byId("model").oninput=()=>{dirtyProfile=true;byId("profile-state").textContent="Есть несохранённые изменения.";byId("benchmark-choice").hidden=true;byId("connection-result").textContent="";updateActions();};
  byId("profile-state").textContent="Сохранённый профиль · "+profiles[state.provider]+".";
  if(state.last_probe) byId("connection-result").textContent=state.last_probe;
  bind("provider","change",profile);bind("word-cat","change",()=>{byId("word-result").textContent="";words();});
  bind("settings","submit",async()=>{const settings=Object.fromEntries([...new FormData(byId("settings"))].map(([k,v])=>[k,Number(v)]));
    byId("settings-result").textContent=(await call("settings",{settings})).message;});
  bind("once","click",async()=>{await call("cycle",{});byId("cycle-status").textContent="Один цикл запущен.";await pollProgress();});
  for(const operation of ["enable","disable","status"])bind("sched-"+operation,"click",async()=>{
    const r=await call("schedule",{operation});byId("schedule-status").textContent=r.detail||"Настройка сохранена.";
  });
  bind("retention","submit",async()=>{await call("retention",{days:Number(byId("days").value),enabled:byId("clean").checked});byId("message").textContent="Правило хранения сохранено";});
  bind("preview","click",async()=>{const r=await call("retention",{preview:true});byId("retention-result").textContent="Подходящих снимков: "+r.snapshots+". Освобождаемый объём: "+(r.bytes/1048576).toFixed(1)+" МиБ.";});
  bind("fact","submit",async()=>{await call("factcheck",{enabled:byId("fact-auto").checked});byId("message").textContent="Настройка фактчекинга сохранена";});
  bind("source-form","submit",async()=>{state.sources=(await call("sources",{operation:"add",url:byId("source-url").value})).sources;byId("source-url").value="";sources();});
  bind("word-form","submit",async()=>{
    try{state.words=(await call("words",{operation:"add",category:byId("word-cat").value,word:byId("word").value})).words;byId("word").value="";words();byId("word-result").textContent="Словарь сохранён.";}
    catch(e){byId("word-result").textContent=e.message;}
  });
  bind("model","submit",async()=>{try{await saveProfile();}catch(e){byId("profile-state").textContent="Не удалось сохранить. "+e.message;}});
  wireSelection();wireRegistry();
}
let dirtyProfile=false;
let probing=false;
async function saveProfile() {
  const enabled=byId("ai-enabled").checked, form=byId("model");
  if (!form.checkValidity() && enabled){form.reportValidity();throw Error("Проверьте поля подключения.");}
  const provider=byId("provider").value;
  const p={provider,endpoint:byId("endpoint").value,model:byId("model-id").value,timeout:Number(byId("timeout").value),response_mode:byId("response").value,disable_thinking:byId("thinking").checked};
  const saved=form.checkValidity()?await call("profile",{profile:p,enabled,secret:byId("secret").value}):await call("disable-ai",{});
  if(saved.profile){state.provider=provider;state.profiles[provider]=saved.profile;}
  byId("provider").value=state.provider;profile();dirtyProfile=false;
  byId("profile-state").textContent=(saved.profile?"Настройки сохранены. ":"Использование ИИ выключено. ")+profiles[state.provider]+".";
  byId("connection-result").textContent="";byId("benchmark-choice").hidden=true;
  showAiStatus(saved.ai_status);invalidatePlan();
}
let lastJob=null;
const chosen = new Set();
let currentPlan = null;
let revision = 0;
let scope = "one";
let starting = false;
let running = false;
let planPending = false;
let refreshTimer;
function invalidatePlan() {
  revision++;
  planPending=false;
  currentPlan=null;
  byId("estimate").textContent="";
  byId("material-gaps").hidden=true;
  updateActions();
}
function disableWithReason(id,wrapper,reason) {
  byId(id).disabled=Boolean(reason);
  byId(wrapper).title=reason;
  byId(id).title=reason;
}
function updateActions(){
  const configured=state.ai_status==="ИИ настроен";
  const reason=!configured ? state.ai_status+". Откройте настройки подключения." : "";
  const canSaveAndVerify=dirtyProfile && byId("ai-enabled").checked;
  byId("verify-model").textContent=canSaveAndVerify?"Сохранить и проверить ответ модели":"Проверить ответ модели";
  disableWithReason("verify-model","verify-reason",probing?"Идёт проверка ответа модели.":dirtyProfile?(canSaveAndVerify?"":"Включите использование модели для проверки."):reason);
  const mode=byId("operation").value;
  byId("once").disabled=running || starting;
  byId("run-selection").textContent=mode.startsWith("download_")?"Загрузить и проверить":mode==="download"?"Загрузить материалы":scope==="one"?"Начать анализ учреждения":"Начать анализ выбранных учреждений";
  let why="";
  if (starting || running) why="Дождитесь завершения текущей операции.";
  else if (!chosen.size) why="Выберите учреждение.";
  else if (["ai","download_ai"].includes(mode) && !configured) why=reason;
  else if (!currentPlan) why="Сначала проверьте материалы и оцените время.";
  else if (mode==="ai" && !currentPlan.ready) why="Нет готовых вопросов. Проверьте материалы и уже обработанные вопросы.";
  else if (mode==="algorithm" && !currentPlan.pages) why="Нужны сохранённые страницы. Сначала загрузите материалы.";
  disableWithReason("run-selection","start-reason",why);
  byId("plan").disabled=!chosen.size || starting || planPending;
}
function renderOrgs(){
  const search=byId("org-query").value.trim().toLocaleLowerCase();
  const many=scope==="many";
  const list=byId("org-list");list.replaceChildren();
  for(const org of state.organizations.filter(o=>(o.name+" "+o.site_url).toLocaleLowerCase().includes(search))){
    const label=document.createElement("label"),input=document.createElement("input"),text=document.createElement("span"),url=document.createElement("small");
    input.type=many?"checkbox":"radio";input.name="selected-org";input.checked=chosen.has(org.id);
    input.addEventListener("change",()=>{
      if (!many) chosen.clear();
      if(input.checked) chosen.add(org.id);else chosen.delete(org.id);
      invalidatePlan();showSelectedCount();
    });
    text.textContent=org.name;url.textContent=org.site_url;text.append(url);label.append(input,text);list.append(label);
  }
  if (!list.childElementCount) list.textContent="Совпадений нет.";
  showSelectedCount();
}
function showSelectedCount(){
  byId("org-count").textContent="Выбрано учреждений: "+chosen.size+" из "+state.organizations.length;
}
function duration(seconds) {
  if (seconds===null || seconds===undefined) return "недостаточно замеров";
  if(seconds<60) return Math.ceil(seconds)+" с";
  return Math.ceil(seconds/60)+" мин";
}
function selection(){return {ids:[...chosen],mode:byId("operation").value};}
function showPlan(p){
  const lines=["Учреждений: "+p.organizations+". В архиве последних страниц: "+p.pages+"."];
  const gaps=byId("material-gaps");
  gaps.hidden=!p.gaps.length;
  gaps.querySelector("ul").replaceChildren(...p.gaps.map(g=>{
    const li=document.createElement("li");li.textContent=g.name+" — "+g.sections.join(", ");return li;
  }));
  if (p.empty.length) lines.push("Нужна первичная загрузка: "+p.empty.join(", ")+".");
  if((p.gaps.length||p.empty.length) && ["ai","algorithm"].includes(byId("operation").value))
    lines.push("Проверка охватит доступные материалы. Чтобы сначала пополнить архив, выберите «Загрузить и сразу проверить алгоритмами» или вариант с ИИ.");
  if(["ai","download_ai","download"].includes(byId("operation").value) && p.counts_known){
    lines.push("Вопросы ИИ в текущем архиве: готово "+p.ready+", уже обработано "+p.completed+", не хватает материалов "+p.missing+".");
    lines.push("Обработка ИИ: "+(p.model_seconds===null ? p.benchmark_reason || "Станет известна после загрузки материалов." : "около "+duration(p.model_seconds)+" по текстовой пробе. Фото и PDF могут занять больше времени."));
  }
  if(byId("operation").value==="ai" && p.ready)
    lines.push("Запуск обработает готовые вопросы выбранных учреждений. Для многостраничного документа один вопрос может потребовать нескольких обращений к API.");
  if(byId("operation").value.startsWith("download")) lines.push("Загрузка: "+duration(p.download_seconds)+" по предыдущим полным загрузкам при тех же лимитах. До "+p.page_limit+" страниц в основном обходе на учреждение, затем дополнительные ссылки и вложения. В совмещённом режиме система пересчитает объём анализа после загрузки.");
  if(!p.counts_known && ["ai","download_ai"].includes(byId("operation").value)) lines.push(p.benchmark_reason);
  lines.push("Большая выборка может занять длительное время. Оставьте приложение запущенным.");
  byId("estimate").textContent=lines.join("\n");
}
async function pollProgress(){
  clearTimeout(refreshTimer);
  try {
    const s=await call("progress");
    const j=s.job;
    const cycle=s.cycle;
    lastJob=j;
    running=j.state==="running" || s.manual_cycle.state==="running" || cycle?.state==="running";
    byId("resume-job").hidden=!["error","interrupted"].includes(j.state);
    byId("resume-job").disabled=running || starting;
    if(j.state!=="idle"){
      const now=Date.now()/1000,active=j.state==="running";
      const elapsed=j.elapsed+(active?Math.max(0,now-j.updated):0);
      const modes={download:"Загрузка",ai:"Проверка ИИ",algorithm:"Алгоритмы",download_algorithm:"Загрузка и алгоритмы",download_ai:"Загрузка, алгоритмы и ИИ"};
      const states={running:"Выполняется",completed:"Все запланированные этапы завершены",interrupted:"Прервано при остановке сервера",error:"Остановлено из-за ошибки"};
      const lines=[modes[j.mode]+" · "+states[j.state], "Этапов: "+j.done+" из "+j.total+" · время работы: "+Math.floor(elapsed/60)+" мин "+Math.floor(elapsed%60)+" с"];
      if(j.detail && j.state!=="completed") lines.push(j.detail);
      const units={algorithm:"Страниц проверено",ai:"Ответов ИИ получено",download:"Обходов выполнено"};
      for(const [stage,n] of Object.entries(j.processed||{}))lines.push(units[stage]+": "+n);
      if(active && j.unit_total){
        const t=Math.max(0,now-j.unit_started);
        const left=j.unit_done>0?t*(j.unit_total-j.unit_done)/j.unit_done:j.unit_estimate==null?null:Math.max(0,j.unit_estimate-t);
        lines.push("На текущем этапе: "+j.unit_done+" из "+j.unit_total+(left>0?" · осталось примерно "+duration(left):""));
      }
      const details=byId("job-notes");
      details.hidden=!j.notes?.length;
      if(!details.hidden){
        details.querySelector("summary").textContent="Что осталось без проверки или загрузки ("+j.notes.length+")";
        details.querySelector("ul").replaceChildren(...j.notes.map(note=>{const li=document.createElement("li");li.textContent=note;return li;}));
      }
      byId("job-progress").textContent=lines.join("\n");
    }
    if (s.manual_cycle.error) byId("cycle-status").textContent=s.manual_cycle.error;
    else if (cycle) {
      const names={crawl:"сайты",frontier:"дополнительные страницы",attachments:"вложения",factcheck:"публикации",models:"ИИ",retention:"очистка"};
      byId("cycle-status").textContent="Цикл № "+cycle.id+" · "+({running:"Выполняется",partial:"Цикл закончился с замечаниями",completed:"Завершён",interrupted:"Прерван"}[cycle.state]||cycle.state)+(cycle.stage?" · "+(names[cycle.stage]||cycle.stage):"");
      const timing=cycle.state==="interrupted"?Object.values(cycle.stages).reduce((n,s)=>n+(s.seconds||0),0):(cycle.finished_at||Date.now()/1000)-cycle.started_at;
      byId("cycle-status").textContent+=" · "+(cycle.state==="interrupted"?"время сохранённых этапов: ":"время: ")+duration(Math.max(0,timing));
      for(const [stage,v] of Object.entries(cycle.stages)){if(v.error||v.errors||v.stop_reason)byId("cycle-status").textContent+="\n"+(names[stage]||stage)+" · "+(v.error||v.stop_reason||("Ошибок: "+v.errors));}
      if(cycle.state==="interrupted")byId("cycle-status").textContent+=". Следующий запуск возьмёт оставшиеся материалы из очереди.";
    }
    updateActions();
  } catch(error){byId("job-progress").textContent="Не удалось обновить состояние. "+error.message;}
  refreshTimer=setTimeout(pollProgress,running?1500:8000);
}
async function verifyModel() {
  if(probing)return;
  probing=true;updateActions();
  byId("benchmark-choice").hidden=true;
  byId("connection-result").textContent="Ждём ответ выбранной модели…";
  try {
    if(dirtyProfile) await saveProfile();
    const r=await call("verify",{});
    byId("connection-result").textContent=r.model+" · "+r.message+" · "+r.seconds+" с";
    byId("benchmark-choice").hidden=dirtyProfile;
  } catch(e){byId("connection-result").textContent="Проверка подключения не прошла. "+e.message+(byId("provider").value==="compatible"?"":" Проверьте, что сервер запущен и модель доступна по указанному адресу.");}
  finally{probing=false;updateActions();}
}
function wireSelection(){
  renderOrgs();updateActions();
  byId("org-query").addEventListener("input",renderOrgs);
  for (const button of byId("scope").querySelectorAll("button")) button.addEventListener("click",()=>{
    scope=button.dataset.scope;
    if(scope==="one" && chosen.size>1) chosen.clear();
    byId("scope").querySelectorAll("button").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.scope===scope)));
    invalidatePlan();renderOrgs();
  });
  byId("operation").addEventListener("change",invalidatePlan);
  bind("plan","click",async()=>{
    const version=revision;
    planPending=true;updateActions();byId("estimate").textContent="Проверяем архив…";
    try {const p=await call("plan",selection());if(version===revision){currentPlan=p;showPlan(p);}}
    catch(e){if(version===revision){currentPlan=null;byId("estimate").textContent="Не удалось оценить материалы. "+e.message;}}
    finally{if(version===revision){planPending=false;updateActions();}}
  });
  bind("run-selection","click",async()=>{
    starting=true;updateActions();byId("job-progress").textContent="Проверяем возможность запуска…";
    try{await call("analyze",{...selection(),ticket:currentPlan.ticket});invalidatePlan();await pollProgress();}
    catch(e){byId("message").textContent=byId("job-progress").textContent="Запуск отклонён. "+e.message;}
    finally{starting=false;updateActions();}
  });
  bind("resume-job","click",async()=>{
    starting=true;updateActions();byId("resume-job").disabled=true;
    try{await call("resume",{id:lastJob.id});}catch(e){byId("message").textContent=e.message;}
    finally{starting=false;await pollProgress();}
  });
  bind("verify-model","click",verifyModel);
  for(const [button,decline] of [["benchmark-run",false],["benchmark-decline",true]]) bind(button,"click",async()=>{
    byId("benchmark-choice").hidden=true;
    try{const v=await call("benchmark",{decline});byId("connection-result").textContent=decline?v.message:"Пробный текстовый запрос: "+duration(v.seconds)+". Замер сохранён.";invalidatePlan();}
    catch(e){byId("benchmark-choice").hidden=false;throw e;}
  });
  pollProgress();
}

init().catch(e=>byId("message").textContent=e.message);

if (location.hash === "#registry") document.querySelector("#registry details").open = true;
