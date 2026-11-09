"use strict";
const byId = id => document.getElementById(id);
const fieldLabels = {interval_minutes:"Интервал запуска, минут",revisit_days:"Повторный обход каждого сайта через, дней",
  organizations_per_tick:"Учреждений за цикл",pages_per_org:"Страниц основного обхода на сайт",
  max_depth:"Глубина основного обхода",frontier_pages:"Дополнительных страниц за цикл",
  attachment_files:"Вложений за цикл",model_checks:"Вопросов модели за цикл",
  factcheck_items:"Публикаций для фактчекинга",news_months:"Глубина новостей, месяцев"};
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
    const item=document.createElement("li");item.append(document.createTextNode(value+" "));
    const button=document.createElement("button");button.type="button";button.textContent="Удалить";
    button.addEventListener("click",()=>remove(value).catch(e=>byId("message").textContent=e.message));
    item.append(button);list.append(item);
  });
}
function sources(){displayList("sources",state.sources,async url=>{
  state.sources=(await call("sources",{operation:"remove",url})).sources;sources();
});}
function words(){const category=byId("word-cat").value;displayList("words",state.words[category]||[],async word=>{
  state.words=(await call("words",{operation:"remove",category,word})).words;words();
});}
function profile(){
  const p=state.profiles[byId("provider").value];byId("endpoint").value=p.endpoint;
  byId("model-id").value=p.model;byId("timeout").value=p.timeout;byId("response").value=p.response_mode;
  byId("thinking").checked=p.disable_thinking;byId("secret").value="";
}
function showAiStatus(status) {
  state.ai_status=status;
  byId("ai-status").textContent=status;
  byId("access").textContent=status;
  const budget=document.querySelector('[name="model_checks"]');
  budget.disabled=status!=="ИИ настроен";
  budget.parentElement.title=budget.disabled?status+". Настройте подключение, чтобы менять лимит вопросов ИИ.":"";
}
async function init() {
  state=await call("state");
  for (const [key,name] of Object.entries(fieldLabels)){
    const label=document.createElement("label"),input=document.createElement("input");
    input.type="number";input.required=true;input.min=["interval_minutes","revisit_days","pages_per_org","news_months"].includes(key)?1:0;
    input.name=key;input.value=state.settings[key];label.append(document.createTextNode(name),input);byId("cycle-fields").append(label);
  }
  byId("clean").checked=state.retention.enabled;byId("days").value=state.retention.days;
  byId("fact-auto").checked=state.factcheck_auto;
  for(const [key,title] of Object.entries(profiles))option(byId("provider"),key,title);
  byId("ai-enabled").checked=state.ai_enabled;showAiStatus(state.ai_status);
  byId("provider").value=state.provider;profile();sources();
  for(const [key,title] of Object.entries(state.word_labels))option(byId("word-cat"),key,title);
  words();
  bind("provider","change",profile);bind("word-cat","change",words);
  bind("settings","submit",async()=>{const settings=Object.fromEntries([...new FormData(byId("settings"))].map(([k,v])=>[k,Number(v)]));
    byId("message").textContent=(await call("settings",{settings})).message;});
  bind("once","click",async()=>{await call("cycle",{});byId("cycle-status").textContent="Один цикл запущен.";await pollProgress();});
  for(const operation of ["enable","disable","status"])bind("sched-"+operation,"click",async()=>{
    const r=await call("schedule",{operation});byId("schedule-status").textContent=r.detail||"Настройка сохранена.";
  });
  bind("retention","submit",async()=>{await call("retention",{days:Number(byId("days").value),enabled:byId("clean").checked});byId("message").textContent="Правило хранения сохранено";});
  bind("preview","click",async()=>{const r=await call("retention",{preview:true});byId("retention-result").textContent="Подходящих снимков: "+r.snapshots+". Освобождаемый объём: "+(r.bytes/1048576).toFixed(1)+" МиБ.";});
  bind("fact","submit",async()=>{await call("factcheck",{enabled:byId("fact-auto").checked});byId("message").textContent="Настройка фактчекинга сохранена";});
  bind("source-form","submit",async()=>{state.sources=(await call("sources",{operation:"add",url:byId("source-url").value})).sources;byId("source-url").value="";sources();});
  bind("word-form","submit",async()=>{state.words=(await call("words",{operation:"add",category:byId("word-cat").value,word:byId("word").value})).words;byId("word").value="";words();});
  bind("model","submit",async()=>{
    if (!byId("model").checkValidity()) {
      if (byId("ai-enabled").checked) {byId("model").reportValidity();return;}
      const saved=await call("disable-ai",{});showAiStatus(saved.ai_status);invalidatePlan();
      byId("benchmark-choice").hidden=true;byId("connection-result").textContent="";byId("message").textContent="ИИ выключен";return;
    }
    const provider=byId("provider").value,p={provider,endpoint:byId("endpoint").value,model:byId("model-id").value,timeout:Number(byId("timeout").value),response_mode:byId("response").value,disable_thinking:byId("thinking").checked};
    const saved=await call("profile",{profile:p,enabled:byId("ai-enabled").checked,secret:byId("secret").value});showAiStatus(saved.ai_status);state.profiles[provider]=p;byId("secret").value="";byId("message").textContent="Активный профиль сохранён";invalidatePlan();byId("benchmark-choice").hidden=true;byId("connection-result").textContent="";if(saved.ai_status==="ИИ настроен") await verifyModel();});
  wireSelection();
}
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
  disableWithReason("verify-model","verify-reason",reason);
  const mode=byId("operation").value;
  byId("once").disabled=running || starting;
  byId("run-selection").textContent=mode==="download"?"Загрузить материалы":scope==="one"?"Начать анализ учреждения":"Начать анализ выбранных учреждений";
  let why="";
  if (starting || running) why="Дождитесь завершения текущей операции.";
  else if (!chosen.size) why="Выберите учреждение.";
  else if (mode==="ai" && !configured) why=reason;
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
      invalidatePlan();renderOrgs();
    });
    text.textContent=org.name;url.textContent=org.site_url;text.append(url);label.append(input,text);list.append(label);
  }
  if (!list.childElementCount) list.textContent="Совпадений нет.";
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
  if(byId("operation").value!=="algorithm"){
    lines.push("Вопросы ИИ в текущем архиве: готово "+p.ready+", уже обработано "+p.completed+", не хватает материалов "+p.missing+".");
    lines.push("Обработка ИИ: "+(p.model_seconds===null ? p.benchmark_reason || "Станет известна после загрузки материалов." : "около "+duration(p.model_seconds)+" по текстовой пробе. Фото и PDF могут занять больше времени."));
  }
  if(byId("operation").value==="ai" && p.ready)
    lines.push("Запуск обработает готовые вопросы выбранных учреждений. Для многостраничного документа один вопрос может потребовать нескольких обращений к API.");
  if(byId("operation").value==="download") lines.push("Загрузка: "+duration(p.download_seconds)+" по предыдущим полным загрузкам при тех же лимитах. До "+p.page_limit+" страниц в основном обходе на учреждение, затем дополнительные ссылки и вложения. После загрузки оцените новый объём обработки.");
  lines.push("Большая выборка может занять длительное время. Оставьте приложение запущенным.");
  byId("estimate").textContent=lines.join("\n");
}
async function pollProgress(){
  clearTimeout(refreshTimer);
  try {
    const s=await call("progress");
    const j=s.job;
    const cycle=s.cycle;
    running=j.state==="running" || s.manual_cycle.state==="running";
    if(j.state!=="idle"){
      const elapsed=Math.max(0,Date.now()/1000-j.started);
      const left=j.done>0 ? elapsed*(j.total-j.done)/j.done : j.estimate==null?null:Math.max(0,j.estimate-elapsed);
      const stages={download:"Загрузка",ai:"Проверка ИИ",algorithm:"Проверка страниц"};
      const states={running:"Выполняется",completed:"Завершено",partial:"Завершено частично",error:"Ошибка"};
      byId("job-progress").textContent=[stages[j.mode],states[j.state],j.done+" из "+j.total,j.detail,j.state==="running" && left!==null?(left>0?"Осталось примерно "+duration(left):"Шаг ещё выполняется"):""].filter(Boolean).join(" · ");
    }
    if (s.manual_cycle.error) byId("cycle-status").textContent=s.manual_cycle.error;
    else if (cycle) {
      const names={crawl:"сайты",frontier:"дополнительные страницы",attachments:"вложения",factcheck:"публикации",models:"ИИ",retention:"очистка"};
      byId("cycle-status").textContent="Цикл № "+cycle.id+" · "+({running:"Выполняется",partial:"Завершён частично",completed:"Завершён",interrupted:"Прерван"}[cycle.state]||cycle.state)+(cycle.stage?" · "+(names[cycle.stage]||cycle.stage):"");
    }
    updateActions();
  } catch(error){byId("job-progress").textContent="Не удалось обновить состояние. "+error.message;}
  refreshTimer=setTimeout(pollProgress,running?1500:8000);
}
async function verifyModel() {
  byId("benchmark-choice").hidden=true;
  byId("connection-result").textContent="Ждём ответ выбранной модели…";
  try {
    const r=await call("verify",{});
    byId("connection-result").textContent=r.model+" · "+r.message;
    byId("benchmark-choice").hidden=false;
  } catch(e){byId("connection-result").textContent="Проверка подключения не прошла. "+e.message;}
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
  bind("verify-model","click",verifyModel);
  for(const [button,decline] of [["benchmark-run",false],["benchmark-decline",true]]) bind(button,"click",async()=>{
    byId("benchmark-choice").hidden=true;
    try{const v=await call("benchmark",{decline});byId("connection-result").textContent=decline?v.message:"Пробный текстовый запрос: "+duration(v.seconds)+". Замер сохранён.";invalidatePlan();}
    catch(e){byId("benchmark-choice").hidden=false;throw e;}
  });
  pollProgress();
}

init().catch(e=>byId("message").textContent=e.message);
