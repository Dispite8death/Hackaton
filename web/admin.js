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
  byId("provider").value=state.provider;profile();sources();
  for(const [key,title] of Object.entries(state.word_labels))option(byId("word-cat"),key,title);
  words();
  bind("provider","change",profile);bind("word-cat","change",words);
  bind("settings","submit",async()=>{const settings=Object.fromEntries([...new FormData(byId("settings"))].map(([k,v])=>[k,Number(v)]));
    byId("message").textContent=(await call("settings",{settings})).message;});
  bind("once","click",async()=>{await call("cycle",{});byId("cycle-status").textContent="Один цикл запущен.";});
  for(const operation of ["enable","disable","status"])bind("sched-"+operation,"click",async()=>{
    const r=await call("schedule",{operation});byId("cycle-status").textContent=r.detail||"Настройка сохранена.";
  });
  bind("retention","submit",async()=>{await call("retention",{days:Number(byId("days").value),enabled:byId("clean").checked});byId("message").textContent="Правило хранения сохранено";});
  bind("preview","click",async()=>{const r=await call("retention",{preview:true});byId("retention-result").textContent="Подходящих снимков: "+r.snapshots+". Освобождаемый объём: "+(r.bytes/1048576).toFixed(1)+" МиБ.";});
  bind("fact","submit",async()=>{await call("factcheck",{enabled:byId("fact-auto").checked});byId("message").textContent="Настройка фактчекинга сохранена";});
  bind("source-form","submit",async()=>{state.sources=(await call("sources",{operation:"add",url:byId("source-url").value})).sources;byId("source-url").value="";sources();});
  bind("word-form","submit",async()=>{state.words=(await call("words",{operation:"add",category:byId("word-cat").value,word:byId("word").value})).words;byId("word").value="";words();});
  bind("model","submit",async()=>{const provider=byId("provider").value,p={provider,endpoint:byId("endpoint").value,model:byId("model-id").value,timeout:Number(byId("timeout").value),response_mode:byId("response").value,disable_thinking:byId("thinking").checked};
    await call("profile",{profile:p,secret:byId("secret").value});state.profiles[provider]=p;byId("secret").value="";byId("message").textContent="Активный профиль сохранён";});
}
init().catch(e=>byId("message").textContent=e.message);
