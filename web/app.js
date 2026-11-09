(() => {
  "use strict";

  const byId = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"})[c]);
  const names = {overview:"Сводный аудит", institutions:"Учреждения", findings:"Результаты проверок"};
  const labels = {
    kind:{school:"Школа", kindergarten:"Детский сад"},
    status:{issue:"Замечание",pass:"Подтверждено",review:"Требует сверки",unverified:"Не проверено",info:"Справочно"},
    severity:{high:"Высокая",medium:"Средняя",low:"Низкая"},
    confidence:{high:"Высокая",medium:"Средняя",low:"Низкая",unknown:"Не определена"},
    quality:{excellent:"Отличный",good:"Хороший",fair:"Удовлетворительный",poor:"Низкий",unrated:"Без оценки"},
    category:{home:"Главная",news:"Новости",events:"Мероприятия",documents:"Документы",staff:"Сотрудники",
      contacts:"Контакты",schedule:"Расписание",programs:"Программы",layout:"Оформление",factcheck:"Фактчекинг",structure:"Структура"},
    group:{content:"Содержание",publication:"Публикации",layout:"Оформление",photos:"Фото",attachments:"Вложения",navigation:"Навигация"}
  };
  const facets = {criterion:"Тип замечания",detector:"Кто проверил",organization_id:"Учреждения",status:"Статус",severity:"Критичность",quality:"Уровень",
    category:"Категория",group:"Группа проверок",confidence:"Уверенность"};
  const known = ["kind", ...Object.keys(facets), "date_from","date_to","q"];
  const dateFmt = new Intl.DateTimeFormat("ru-RU",{day:"2-digit",month:"2-digit",year:"numeric",timeZone:"Asia/Magadan"});
  const numberFmt = new Intl.NumberFormat("ru-RU",{maximumFractionDigits:1});
  const sortNames = {rank:"По рейтингу", name:"По названию", issues:"По числу замечаний", coverage:"По охвату", date:"По дате проверки"};
  const pageSize = 25;
  const selected = new Set();
  let data = null;
  let controller = null;
  let options = {};
  let listPage = 0;
  let sort = "rank";
  const chartFilters={status:"",severity:"",category:""};
  let queryTimer;
  let renderedHash = location.hash;
  let view = "simple";
  try { if (localStorage.getItem("audit-view")==="detailed") view="detailed"; } catch {}
  function readFilters() {
    const input = new URLSearchParams(location.search);
    const values = {};
    for (const key of known) {
      const parts = input.getAll(key);
      if (!parts.length) continue;
      if (["q","date_from","date_to"].includes(key)) values[key] = parts.at(-1);
      else values[key] = [...new Set(parts.flatMap(v => v.split(",")).filter(Boolean))];
    }
    return values;
  }
  let filters = readFilters();
  function params(obj) {
    const result = new URLSearchParams();
    for (const key of known) {
      const value = obj[key];
      if (value && value.length) result.set(key, Array.isArray(value) ? value.join(",") : value);
    }
    return result.toString();
  }
  function route() {
    const name = location.hash.slice(1);
    const detail = /^institution\/(\d+)$/.exec(name);
    return detail ? {tab:"institution",id:Number(detail[1])} : {tab:names[name] ? name : "overview"};
  }
  function navigate(tab) {
    location.hash=tab;
  }
  function saveFilters(next) {
    filters=next;
    listPage=0;
    const query=params(filters);
    history.replaceState(null,"",location.pathname+(query?"?"+query:"")+location.hash);
    refresh();
  }
  function label(key,value) {
    const item=(options[key] || []).find(i => String(i.value)===String(value));
    return item?.label || labels[key]?.[value] || (key==="organization_id" ? "Учреждение № "+value : String(value || "—"));
  }
  function optionList(key) {
    const list=options[key];
    return Array.isArray(list) && list.length ? list : Object.entries(labels[key] || {}).map(([value,label])=>({value,label}));
  }
  function num(value) {
    return value == null || value === "" || !Number.isFinite(Number(value)) ? "—" : numberFmt.format(Number(value));
  }
  function percent(value) {return value==null ? "—" : num(value)+"%";}
  function date(value) {
    if (!value) return "—";
    const parsed = new Date(typeof value==="number" ? value*1000 : value);
    return Number.isNaN(parsed.getTime()) ? "—" : dateFmt.format(parsed);
  }
  function url(value) {
    try {
      const u=new URL(value);
      return ["https:","http:"].includes(u.protocol) && !u.username && !u.password ? u.href : "";
    } catch {return "";}
  }
  function external(value,text) {
    const href=url(value);
    return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(text)} ↗</a>` : "";
  }
  function pill(key,value) {
    const token=Object.hasOwn(labels[key]||{},value)?value:"unknown";
    return `<span class="pill ${token}">${esc(label(key,value))}</span>`;
  }
  function pdf(obj) {
    const query=new URLSearchParams(params(obj));
    if(byId("pdf-quality").checked) query.set("include_quality","1");
    const value=query.toString();
    return "/report.pdf"+(value?"?"+value:"");
  }
  function downloadMenu() {
    const r=route();
    const record=(title,obj)=>`<a href="${esc(pdf(obj))}" download="audit-report.pdf">${esc(title)}</a>`;
    const chosen=[...selected].filter(id=>!filters.organization_id || filters.organization_id.includes(String(id)));
    byId("downloads").innerHTML=[
      record("По текущим фильтрам",r.id ? {...filters,organization_id:[r.id]} : filters),
      chosen.length ? record("По отмеченным с текущими фильтрами ("+chosen.length+")",{...filters,organization_id:chosen}) :
        '<span class="disabled">Отмеченных учреждений нет</span>',
      r.id ? record("По этому учреждению",{...filters,organization_id:[r.id]}) : "",
      record("Все учреждения без фильтров",{}),
      '<button type="button" id="pdf-filters">Настроить фильтры PDF…</button>'
    ].join("");
  }
  function updateControls() {
    const activeKeys=Object.keys(filters).filter(key=>filters[key]?.length);
    if (document.activeElement !== byId("query")) byId("query").value=filters.q || "";
    byId("date-from").value=filters.date_from || "";
    byId("date-to").value=filters.date_to || "";
    for (const button of document.querySelectorAll("[data-kind]")) {
      const value=button.dataset.kind;
      const isActive=value ? (filters.kind||[]).includes(value) : !(filters.kind||[]).length;
      button.setAttribute("aria-pressed",String(isActive));
    }
    const count=activeKeys.filter(k=>k!=="kind" && k!=="q").length;
    byId("filter-count").hidden=!count;
    byId("filter-count").textContent=count;
    const chips=[];
    for (const key of activeKeys) {
      for (const value of (Array.isArray(filters[key])?filters[key]:[filters[key]])) {
        const text=key==="q" ? "Поиск · "+value : key==="date_from" ? "С "+value : key==="date_to" ? "По "+value :
          (["severity","confidence"].includes(key)?facets[key]+" · ":"")+label(key,value);
        chips.push(`<button class="chip" data-remove-key="${esc(key)}" data-remove-value="${esc(value)}" aria-label="Снять фильтр ${esc(text)}">${esc(text)} ×</button>`);
      }
    }
    byId("chips").innerHTML=chips.join("");
    byId("chips").hidden=!chips.length;
    downloadMenu();
  }
  function drawFacets() {
    const open=new Set([...byId("facets").querySelectorAll("details[open]")].map(el=>el.dataset.facet));
    const previousField=byId("facets").querySelector(".org-search");
    const find=previousField?.value || "";
    const caret=previousField && previousField===document.activeElement
      ? [previousField.selectionStart,previousField.selectionEnd] : null;
    byId("facets").innerHTML=Object.entries(facets).map(([key,title])=>{
      const selectedValues=filters[key] || [];
      const list=optionList(key).map(item=>{
        const val=String(item.value);
        return `<label data-option-text="${esc((String(item.label)+" "+(item.url||"")).toLocaleLowerCase("ru"))}"><input type="checkbox" data-set="${key}" value="${esc(val)}" ${selectedValues.includes(val)?"checked":""}> ${esc(item.label)}</label>`;
      }).join("");
      return `<details class="facet" data-facet="${key}" ${open.has(key)?"open":""}><summary>${esc(title)}${selectedValues.length?" · "+selectedValues.length:""}<i class="caret"></i></summary><div class="facet-options">${key==="organization_id"?'<input type="search" class="org-search" aria-label="Поиск в списке учреждений" placeholder="Название или адрес сайта">':""}${list || '<p class="subtle">В выборке нет вариантов</p>'}</div></details>`;
    }).join("");
    const field=byId("facets").querySelector(".org-search");
    if (field && find) {field.value=find; filterOrganizations(field);}
    if (field && caret) {field.focus({preventScroll:true});field.setSelectionRange(...caret);}
  }
  function filterOrganizations(field) {
    const term=field.value.toLocaleLowerCase("ru").trim();
    for (const el of field.parentElement.querySelectorAll("label")) el.hidden=!el.dataset.optionText.includes(term);
  }
  function empty(title,text,reset=true) {
    return `<div class="empty"><h3>${esc(title)}</h3><p>${esc(text)}</p>${reset && params(filters)?'<button class="secondary" data-reset>Сбросить фильтры</button>':""}</div>`;
  }
  function kpi(title,value,detail,tone) {
    const status={"Замечания":"issue","Требуют исправления":"issue","Нужна сверка":"review","Требуют сверки":"review"}[title];
    const target=status?{status:[status]}:null;
    if(target && route().id) target.organization_id=[String(route().id)];
    const tag=target?"a":"article";
    return `<${tag} class="kpi" ${target?drillAttrs(target):""} style="--tone:${tone}"><p>${esc(title)}</p><strong>${esc(value)}</strong><small>${esc(detail)}</small></${tag}>`;
  }
  function score(value) {return value==null ? "—" : num(value);}
  function sortFindings(list) {
    const order={issue:0,review:1,unverified:2,pass:3,info:4};
    return [...list].sort((a,b)=>(order[a.status]??4)-(order[b.status]??4) ||
      (a.priority??9)-(b.priority??9) || String(b.checked_at||"").localeCompare(String(a.checked_at||"")));
  }
  function finding(row) {
    const meta=[row.organization_name,label("category",row.category)].filter(Boolean).join(" · ");
    const clue=typeof row.evidence==="string" ? row.evidence : "";
    const link=external(row.url,"Исходная страница");
    const inst=Number.isInteger(Number(row.organization_id)) && Number(row.organization_id)>0 ?
      `<a href="#institution/${Number(row.organization_id)}">Карточка учреждения</a>` : "";
    const detail=(title,value)=>value?`<div><dt>${esc(title)}</dt><dd>${esc(value)}</dd></div>`:"";
    const refs=(row.materials || []).map(m=>{
      const name=[m.publisher || m.id,m.page ? "стр. "+m.page : ""].filter(Boolean).join(" · ");
      return name ? `<li>${external(m.url,name) || esc(name)}</li>` : "";
    }).join("");
    return `<details class="find-row"><summary><div><h3>${esc(row.title || "Результат проверки")}</h3><p class="caption">${esc(meta)}</p></div>
      <span class="severity-label">${row.status==="issue" && row.severity ? pill("severity",row.severity) : ""}</span>${pill("status",row.status)}<i class="caret"></i></summary>
      <div class="find-body"><dl>${detail("Наблюдение",row.description)}${detail("Основание",clue)}${detail("Что сделать",row.recommendation)}</dl>
      <div class="find-meta">${row.status==="issue" && row.severity?`<span class="mobile-severity">Критичность · ${esc(label("severity",row.severity))}</span>`:""}${row.confidence?`<span>Уверенность · ${esc(label("confidence",row.confidence))}</span>`:""}
      ${row.priority?`<span>Приоритет ${esc(row.priority)}</span>`:""}${row.checked_at?`<span>Проверено ${date(row.checked_at)}</span>`:""}
      ${row.page_id?`<span>Снимок № ${esc(row.page_id)}</span>`:""}${row.source?`<span>Источник · ${esc(row.source)}</span>`:""}</div>
      ${refs?`<div class="material-refs"><strong>Материалы проверки</strong><ul>${refs}</ul></div>`:""}
      <div class="wide-actions">${link}${inst}</div></div></details>`;
  }
  function drill(extra, tab="findings", chart=false) {
    const next={...filters};
    for (const [key,value] of Object.entries(chart ? chartFilters : {})) if (value) next[key]=[value];
    for (const [key,values] of Object.entries(extra)) {
      const allowed=next[key];
      next[key]=allowed ? values.filter(v=>allowed.includes(v)) : values;
      if (!next[key].length) return "";
    }
    return "/archive?"+params(next)+"#"+tab;
  }
  function drillAttrs(extra, tab, chart=false) {
    const href=drill(extra,tab,chart);
    return href ? `href="${esc(href)}" data-drill title="Открыть соответствующие результаты"` : 'aria-disabled="true"';
  }
  let hoverTimer;
  document.addEventListener("pointerover",e=>{
    const link=e.target.closest("a[data-drill][href]");
    if (!link || e.pointerType!=="mouse" || link.contains(e.relatedTarget)) return;
    clearTimeout(hoverTimer);
    hoverTimer=setTimeout(()=>{if(link.matches(":hover")) location.assign(link.href.baseVal || link.getAttribute("href"));},800);
  });
  document.addEventListener("pointerout",e=>{
    const link=e.target.closest("a[data-drill]");
    if(link && !link.contains(e.relatedTarget)) clearTimeout(hoverTimer);
  });
  function resultsPanel(counts) {
    const sequence=["pass","issue","review","unverified","info"];
    const total=sequence.reduce((s,k)=>s+Number(counts[k]||0),0);
    const stacks=sequence.filter(k=>counts[k]>0).map(k=>`<a ${drillAttrs({status:[k]})} aria-label="${esc(label("status",k))}" class="${k}" style="flex:${Number(counts[k])}"></a>`).join("");
    const legend=sequence.map(k=>`<a class="legend-row" ${drillAttrs({status:[k]})}><i class="dot ${k}"></i><span>${esc(label("status",k))}</span><strong>${num(counts[k]||0)}</strong></a>`).join("");
    return `<section class="panel"><div class="panel-head"><h2>Результаты проверок</h2><span class="subtle">${num(total)} всего</span></div>
      <div class="stack">${stacks}</div><div class="legend">${legend}</div></section>`;
  }
  function qualityPanel(counts) {
    const list=["excellent","good","fair","poor","unrated"];
    const max=Math.max(1,...list.map(k=>Number(counts[k]||0)));
    const rows=list.map(k=>`<a class="quality-row" ${drillAttrs({quality:[k]},"institutions")}><span>${esc(label("quality",k))}</span><span class="bar" aria-hidden="true"><i class="${k}" style="width:${100*(counts[k]||0)/max}%"></i></span><strong>${num(counts[k]||0)}</strong></a>`).join("");
    const grading=data.grading || {};
    const guidance=[grading.method,grading.coverage,grading.classification,grading.scope].filter(Boolean).map(t=>`<p>${esc(t)}</p>`).join("");
    return `<section class="panel"><div class="panel-head"><h2>Уровень учреждений</h2></div><div class="quality-list">${rows}</div>
      ${guidance?`<details class="grading"><summary>Как рассчитывается оценка <i class="caret"></i></summary>${guidance}</details>`:""}</section>`;
  }
  function insightCharts() {
    const source=data.findings||[];
    const rows=source.filter(f=>(!chartFilters.status||f.status===chartFilters.status) &&
      (!chartFilters.severity||f.severity===chartFilters.severity) &&
      (!chartFilters.category||f.category===chartFilters.category));
    const statuses=["issue","review","pass","unverified","info"];
    const palette={issue:"#c96957",review:"#d5a24f",pass:"#27876b",unverified:"#a4b4b5",info:"#6895ab"};
    const count=k=>rows.filter(f=>f.status===k).length;
    const total=rows.length;
    let start=0;
    const slices=statuses.map(k=>{
      const n=count(k);
      if (!n) return "";
      const angle=n/total*2*Math.PI;
      const arc=(r,t)=>[64+r*Math.sin(t),64-r*Math.cos(t)].join(" ");
      const end=start+Math.min(angle,2*Math.PI-.000001);
      const large=angle>Math.PI?1:0;
      const d=`M ${arc(62,start)} A 62 62 0 ${large} 1 ${arc(62,end)} L ${arc(41,end)} A 41 41 0 ${large} 0 ${arc(41,start)} Z`;
      start+=angle;
      return `<a ${drillAttrs({status:[k]},"findings",true)} aria-label="${esc(label("status",k))}"><title>${esc(label("status",k))} · ${n}</title><path d="${d}" fill="${palette[k]}"/></a>`;
    }).join("");
    const categories=optionList("category").map(item=>String(item.value));
    const categoryRows=categories.filter(k=>!chartFilters.category||k===chartFilters.category).map(k=>({key:k,value:rows.filter(f=>f.category===k && (f.status==="issue"||f.status==="review")).length})).sort((a,b)=>b.value-a.value).slice(0,6);
    const categoryMax=Math.max(1,...categoryRows.map(x=>x.value));
    const severityRows=["high","medium","low"].map(k=>({key:k,value:rows.filter(f=>f.status==="issue"&&f.severity===k).length}));
    const severityMax=Math.max(1,...severityRows.map(x=>x.value));
    const select=(key,title,choices)=>`<label>${esc(title)}<select data-chart-filter="${key}"><option value="">Все</option>${choices.map(([value,label])=>`<option value="${esc(value)}" ${chartFilters[key]===value?"selected":""}>${esc(label)}</option>`).join("")}</select></label>`;
    return `<section class="analytics"><div class="analytics-head"><div><p class="eyebrow">Аналитика среза</p><h2>Что видно в результатах</h2><p>Графики пересчитываются по выбранным учреждениям и условиям.</p></div><div class="chart-filters">
      ${select("status","Статус",statuses.map(k=>[k,label("status",k)]))}
      ${select("severity","Критичность",["high","medium","low"].map(k=>[k,label("severity",k)]))}
      ${select("category","Категория",categories.map(k=>[k,label("category",k)]))}</div></div>
      <div class="chart-grid"><article class="panel chart-card"><div class="panel-head"><h3>Состояние проверок</h3><span class="subtle">${num(total)} результатов</span></div><div class="donut-layout"><div class="donut"><svg viewBox="0 0 128 128" aria-label="Результаты по статусам">${slices}</svg><div><strong>${num(total)}</strong><small>в выборке</small></div></div><div class="chart-legend">${statuses.map(k=>`<a ${drillAttrs({status:[k]},"findings",true)}><i style="background:${palette[k]}"></i><span>${esc(label("status",k))}</span><b>${num(count(k))}</b></a>`).join("")}</div></div></article>
      <article class="panel chart-card"><div class="panel-head"><h3>Где нужна работа</h3><span class="subtle">Замечания + сверка</span></div><div class="chart-bars">${categoryRows.map(x=>`<a class="chart-bar-row" ${drillAttrs({category:[x.key],status:["issue","review"]},"findings",true)}><span>${esc(label("category",x.key))}</span><div class="chart-track"><i style="width:${100*x.value/categoryMax}%"></i></div><b>${num(x.value)}</b></a>`).join("")}</div></article>
      <article class="panel chart-card"><div class="panel-head"><h3>Критичность замечаний</h3><span class="subtle">Подтверждённые</span></div><div class="chart-bars">${severityRows.map(x=>`<a class="chart-bar-row" ${drillAttrs({severity:[x.key],status:["issue"]},"findings",true)}><span>${esc(label("severity",x.key))}</span><div class="chart-track severity-${x.key}"><i style="width:${100*x.value/severityMax}%"></i></div><b>${num(x.value)}</b></a>`).join("")}</div><p class="chart-note">Сверка и непроверенные критерии не включены.</p></article></div></section>`;
  }
  let diagram="archive";
  function relationships() {
    const archive=[["Учреждение","organization","1"],["Обход","crawl_run","N"],["Загрузка","crawl_fetch","N"],["Снимок страницы","page","1"],["Вложение","page_attachment","N"]];
    const evidence=[["Снимок страницы","page","1"],["Проверка модели","model_assessment","N"],["Подтверждение","model_evidence_pass","N"]];
    const items=diagram==="archive"?archive:evidence;
    return `<section class="panel relationship-panel"><div class="panel-head"><div><p class="eyebrow">Связи данных</p><h2>От страницы до вывода</h2></div><div class="diagram-switch" role="group" aria-label="Тип ER-диаграммы"><button data-diagram="archive" aria-pressed="${diagram==="archive"}">Архив</button><button data-diagram="evidence" aria-pressed="${diagram==="evidence"}">Доказательства</button></div></div>
      <p class="diagram-description">${diagram==="archive"?"Один сайт может иметь много обходов; каждый обход сохраняет загрузки страниц. Снимок может иметь вложения.":"Один снимок может участвовать в нескольких проверках модели. У проверки могут быть отдельные проходы проверки доказательств."}</p>
      <div class="er-flow" aria-label="ER-диаграмма: ${diagram==="archive"?"архив":"доказательства"}">${items.map((item,i)=>`${i?'<span class="er-link" aria-hidden="true">→</span>':""}<a class="er-node" ${drillAttrs(item[1]==="organization"?{}:item[1]==="page_attachment"?{group:["attachments"],status:["issue","review"]}:diagram==="evidence"?{detector:["ai"],status:["issue","review"]}:{detector:["algorithm"],status:["issue","review"]},item[1]==="organization"?"institutions":"findings")}><small>${esc(item[2])}</small><strong>${esc(item[0])}</strong><code>${esc(item[1])}</code></a>`).join("")}</div></section>`;
  }
  function overview() {
    if (view==="simple") return simpleOverview();
    const s=data.summary || {}, counts=s.counts || {};
    const priority=sortFindings(data.findings||[]).filter(x=>["issue","review"].includes(x.status)).slice(0,5);
    return `<div class="kpis">${kpi("Учреждения",num(s.total),"Школы "+num(s.schools)+" · Сады "+num(s.kindergartens),"#257b68")}
      ${kpi("Охват проверок",percent(s.coverage),"По выбранным учреждениям","#608b9d")}
      ${kpi("Замечания",num(counts.issue??0),"Требуют исправления","#c36552")}
      ${kpi("Требуют сверки",num(counts.review??0),"Требуется оценка специалиста","#c19a4d")}</div>
      ${s.total===0?empty("Учреждения не найдены","Измените параметры выборки."):""}
      <div class="panels">${resultsPanel(counts)}${qualityPanel(s.quality_counts || {})}</div>
      <section class="list-panel"><div class="list-head"><h2>На что обратить внимание</h2><a class="small-link" href="#findings">Все результаты →</a></div>
      ${priority.length?priority.map(finding).join(""):empty("Замечаний в выборке нет","Для этой выборки нет подтверждённых замечаний и результатов на сверку.",false)}</section>`;
  }
  function simpleOverview() {
    const s=data.summary||{}, c=s.counts||{};
    const urgent=sortFindings(data.findings||[]).filter(f=>f.status==="issue" && f.severity==="high").slice(0,4);
    const attention=[...(data.institutions||[])].sort((a,b)=>(b.counts?.issue||0)-(a.counts?.issue||0)).slice(0,5);
    return `<section class="brief-hero"><div><h2>Аудит сайтов учреждений</h2>
      <p>Выберите учреждения для анализа архива или откройте результаты по страницам, документам и фотографиям.</p></div>
      <div class="hero-action"><a href="/admin#selected-audit" class="primary">Проверить учреждение</a><a href="#findings" class="small-link">Сводные результаты →</a></div></section>
      <div class="kpis">${kpi("Учреждений",num(s.total),"Школы "+num(s.schools)+" · сады "+num(s.kindergartens),"#257b68")}
      ${kpi("Требуют исправления",num(c.issue||0),"Подтверждённые замечания","#c36552")}
      ${kpi("Нужна сверка",num(c.review||0),"Решение принимает специалист","#c19a4d")}
      ${kpi("Охват",percent(s.coverage),"Доля оценённых критериев","#608b9d")}</div>
      ${insightCharts()}${relationships()}
      <div class="brief-grid"><section class="list-panel"><div class="list-head"><div><p class="eyebrow">Приоритет</p><h2>Критичные замечания</h2></div><a class="small-link" href="#findings">Все результаты →</a></div>
      ${urgent.length?urgent.map(finding).join(""):empty("Критичных замечаний нет","Для этой выборки таких результатов не найдено.",false)}</section>
      <section class="list-panel"><div class="list-head"><div><p class="eyebrow">Где начать работу</p><h2>Учреждения с замечаниями</h2></div><a class="small-link" href="#institutions">Весь реестр →</a></div>
      <div class="attention-list">${attention.map(row=>`<a href="#institution/${Number(row.id)}"><span><strong>${esc(row.name)}</strong><small>${esc(label("kind",row.kind))} · охват ${percent(row.coverage)}</small></span><b>${num(row.counts?.issue||0)}</b></a>`).join("")}</div></section></div>
      <p class="scope-note">Это углублённый анализ сохранённого массива материалов, поэтому новый полный обход требует времени. Недостаток данных и результаты на сверку не считаются подтверждёнными ошибками. <button type="button" data-view="detailed">Показать методику и все показатели →</button></p>`;
  }
  function pager(total) {
    const pages=Math.max(1,Math.ceil(total/pageSize));
    if (pages<=1) return "";
    return `<div class="pagination"><span>${num(1+listPage*pageSize)}–${num(Math.min(total,(listPage+1)*pageSize))} из ${num(total)}</span>
      <div class="pages"><button data-page="-1" ${listPage===0?"disabled":""}>Назад</button><button data-page="1" ${listPage+1>=pages?"disabled":""}>Далее</button></div></div>`;
  }
  function orderedInstitutions() {
    const list=[...(data.institutions||[])];
    return list.sort((a,b)=>{
      if (sort==="issues") return (b.counts?.issue||0)-(a.counts?.issue||0);
      if (sort==="coverage") return (b.coverage??-1)-(a.coverage??-1);
      if (sort==="date") return String(b.last_checked||"").localeCompare(String(a.last_checked||""));
      if (sort==="name") return a.name.localeCompare(b.name,"ru");
      return (a.rank??1e8)-(b.rank??1e8) || a.name.localeCompare(b.name,"ru");
    });
  }
  function institutions() {
    const list=orderedInstitutions();
    listPage=Math.min(listPage,Math.max(0,Math.ceil(list.length/pageSize)-1));
    const part=list.slice(listPage*pageSize,(listPage+1)*pageSize);
    const rows=part.map(row=>`<tr><td><input type="checkbox" data-select="${Number(row.id)}" ${selected.has(String(row.id))?"checked":""} aria-label="Отметить ${esc(row.name)}"></td>
      <td class="inst-name"><a class="institution-link" href="#institution/${Number(row.id)}">${esc(row.name)}</a><small class="cell-muted">${esc(label("kind",row.kind))}</small></td>
      <td class="score-cell num"><strong>${score(row.score)}</strong></td><td>${pill("quality",row.quality)}</td><td class="num">${percent(row.coverage)}</td>
      <td class="num">${num(row.counts?.issue??0)}</td><td class="num matrix-extra">${date(row.last_checked)}</td>
      <td><a href="${esc(pdf({...filters,organization_id:[row.id]}))}" download="audit-report.pdf" aria-label="PDF · ${esc(row.name)}">PDF</a></td></tr>`).join("");
    return `<section class="list-panel"><div class="list-head"><h2>Реестр учреждений <span class="subtle">${num(list.length)}</span></h2>
      <div class="selection-tools"><span id="selected-count"></span><label>Сортировка <select class="sort" id="sort">${Object.entries(sortNames).map(([k,v])=>`<option value="${k}" ${sort===k?"selected":""}>${v}</option>`).join("")}</select></label></div></div>
      ${list.length?`<div class="table-wrap"><table class="inst-table"><thead><tr><th><input id="select-page" type="checkbox" aria-label="Отметить все учреждения на этой странице"></th>
        <th>Учреждение</th><th>Балл</th><th>Уровень</th><th>Охват</th><th>Замечания</th><th class="matrix-extra">Проверено</th><th>Отчёт</th></tr></thead>
        <tbody>${rows}</tbody></table></div>${pager(list.length)}`:empty("Учреждения не найдены","Уточните название или снимите часть фильтров.")}</section>`;
  }
  function findings() {
    const list=sortFindings(data.findings||[]);
    listPage=Math.min(listPage,Math.max(0,Math.ceil(list.length/pageSize)-1));
    return `<section class="list-panel"><div class="list-head"><h2>Результаты по критериям <span class="subtle">${num(list.length)}</span></h2><span class="subtle">По приоритету</span></div>
      ${list.length?list.slice(listPage*pageSize,(listPage+1)*pageSize).map(finding).join("")+pager(list.length):empty("Результаты не найдены","Для выбранных условий подходящих проверок нет.")}</section>`;
  }
  function institution() {
    const row=data.institution;
    if (!row) return empty("Учреждение не найдено","Проверьте фильтры или вернитесь к полному реестру.");
    const categories=data.categories||[];
    const history=data.history||[];
    const categoriesRows=categories.map(c=>`<tr><td>${esc(c.label||label("category",c.id))}</td><td>${num(c.counts?.issue||0)}</td><td>${num(c.counts?.review||0)}</td><td>${percent(c.coverage)}</td><td>${score(c.score)}</td></tr>`).join("");
    const states={running:"Идёт",completed:"Завершён",partial:"Частичный",failed:"С ошибкой",interrupted:"Прерван"};
    const historyRows=history.map(h=>`<tr><td>${date(h.date)}</td><td>${h.purpose==="sample"?"Выборочный":"Основной"}</td><td>${num(h.pages)}</td><td>${esc(states[h.state] || h.state)}</td></tr>`).join("");
    const missing=(data.findings || []).filter(f=>f.status==="unverified");
    const needed={home:"главная страница и исходные изображения",news:"список новостей, карточки и даты публикаций",
      events:"список мероприятий и карточки с датами",documents:"каталог и оригиналы документов",staff:"каталог и карточки сотрудников",
      contacts:"шапка главной страницы и раздел контактов",schedule:"раздел расписания и файлы с занятиями",
      programs:"раздел образования и тексты программ",layout:"экранные снимки и PDF проверяемых страниц",
      factcheck:"публикации из разрешённых независимых источников",structure:"страницы со сведениями об учреждении"};
    const gaps=Object.entries(needed).filter(([cat])=>missing.some(f=>f.category===cat && !f.page_id))
      .map(([cat,hint])=>`<li><strong>${esc(label("category",cat))}</strong> · ${esc(hint)}</li>`).join("");
    const pendingModel=missing.some(f=>f.page_id);
    const nextSteps=row.quality==="unrated" ? `<section class="panel gaps"><h2>Недостаточно данных для оценки</h2>
      <p>Оценено ${num(row.verified_criteria)} из ${num(row.total_criteria)} применимых критериев. Для присвоения уровня нужны минимум три оценённых критерия и охват от 50%.</p>
      ${gaps?`<details><summary>Какие материалы проверить или догрузить <i class="caret"></i></summary><ul>${gaps}</ul></details>`:""}
      ${pendingModel?"<p>Для части критериев снимки уже сохранены. Откройте непроверенные результаты и повторите проверку по их основаниям.</p>":""}
      ${!gaps&&!pendingModel?"<p>Проверьте результаты на сверку или расширьте период и фильтры.</p>":""}</section>` : "";
    const quickLink=data.quick_id ? `<p><a class="small-link" href="/site/${Number(data.quick_id)}">Открыть алгоритмическую проверку этого учреждения →</a></p>` : "";
    return `${quickLink}<div class="kpis">${kpi("Оценка",score(row.score),"Из 100 баллов","#257b68")}${kpi("Охват проверок",percent(row.coverage),label("quality",row.quality),"#608b9d")}
      ${kpi("Замечания",num(row.counts?.issue||0),"В выбранном периоде","#c36552")}${kpi("Требуют сверки",num(row.counts?.review||0),"В выбранном периоде","#c19a4d")}</div>
      ${nextSteps}<div class="panels"><section class="list-panel"><div class="list-head"><h2>По категориям</h2></div>${categoriesRows?`<div class="table-wrap"><table><thead><tr><th>Категория</th><th>Замечания</th><th>Сверка</th><th>Охват</th><th>Балл</th></tr></thead><tbody>${categoriesRows}</tbody></table></div>`:empty("Категории пока не оценены","Проверки появятся после обработки материалов.",false)}</section>
      <section class="list-panel"><div class="list-head"><h2>История обходов</h2>${external(row.site_url,"Сайт учреждения")}</div>${historyRows?`<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Обход</th><th>Страниц</th><th>Состояние</th></tr></thead><tbody>${historyRows}</tbody></table></div>`:empty("Обходов в периоде нет","Выберите другой интервал дат.",false)}</section></div>${findings()}`;
  }
  function selectionState() {
    const el=byId("selected-count");
    if (el) el.textContent=selected.size ? "Отмечено · "+num(selected.size) : "";
    const all=byId("select-page");
    if (all) {
      const boxes=[...document.querySelectorAll("[data-select]")];
      all.checked=!!boxes.length && boxes.every(x=>x.checked);
      all.indeterminate=!all.checked && boxes.some(x=>x.checked);
    }
    downloadMenu();
  }
  function show() {
    const r=route();
    byId("content").innerHTML=r.tab==="overview"?overview():r.tab==="institutions"?institutions():r.tab==="findings"?findings():institution();
    byId("content").setAttribute("aria-busy","false");
    selectionState();
  }
  async function fetchJson(path,signal) {
    const res=await fetch(path,{signal,headers:{Accept:"application/json"},credentials:"same-origin"});
    if (!res.ok) throw new Error(res.status===404?"Такой страницы или учреждения нет.":"Сервер вернул ошибку "+res.status+".");
    return res.json();
  }
  async function refresh() {
    const r=route();
    renderedHash=location.hash;
    document.body.classList.toggle("detail-page",Boolean(r.id));
    controller?.abort();
    controller=new AbortController();
    const signal=controller.signal;
    document.querySelectorAll("[data-tab]").forEach(a=>{
      if (a.dataset.tab===(r.tab==="institution"?"institutions":r.tab)) a.setAttribute("aria-current","page");
      else a.removeAttribute("aria-current");
    });
    byId("title").textContent=names[r.tab] || "Карточка учреждения";
    document.querySelectorAll("[data-view]").forEach(b=>b.setAttribute("aria-pressed",String(b.dataset.view===view)));
    document.title=byId("title").textContent+" · Аудит сайтов образования";
    byId("breadcrumb").innerHTML=r.id?'<a class="back" href="#institutions">← К учреждениям</a>':"";
    byId("asof").textContent="";
    updateControls();
    byId("content").setAttribute("aria-busy","true");
    const query=params(filters);
    const path=r.id?"/api/institution/"+r.id:"/api/dashboard";
    try {
      const body=await fetchJson(path+(query?"?"+query:""),signal);
      if (signal.aborted) return;
      data=body;
      options={...options,...body.options};
      if (!options.organization_id?.length) {
        const base=r.id ? await fetchJson("/api/dashboard",signal) : body;
        options={...options,...base.options};
        if (!options.organization_id?.length)
          options.organization_id=(base.institutions||[]).map(x=>({value:String(x.id),label:x.name}));
      }
      if (signal.aborted) return;
      if (r.id && body.institution) {
        const row=body.institution;
        byId("title").textContent=row.name;
        byId("asof").textContent=label("kind",row.kind)+" · Последняя проверка "+date(row.last_checked);
      } else if (body.as_of) byId("asof").textContent="Данные на "+date(body.as_of);
      document.title=byId("title").textContent+" · Аудит сайтов образования";
      drawFacets();
      updateControls();
      show();
      byId("announcement").textContent="Результаты обновлены.";
    } catch(error) {
      if (signal.aborted) return;
      data=null;
      byId("content").setAttribute("aria-busy","false");
      byId("content").innerHTML=`<div class="panel">${empty("Не удалось загрузить данные",error.message,false)}<button class="secondary" id="retry">Повторить загрузку</button></div>`;
    }
  }
  document.addEventListener("click",event=>{
    const el=event.target.closest("a,button,input,summary");
    if (!el) return;
    if (!el.closest("#download")) byId("download").open=false;
    if (!el.closest(".facet")) document.querySelectorAll(".facet[open]").forEach(x=>x.open=false);
    if (el.closest("#downloads")) byId("download").open=false;
    if (el.hasAttribute("data-kind")) {
      const next={...filters};
      if (el.dataset.kind) next.kind=[el.dataset.kind]; else delete next.kind;
      saveFilters(next);
    } else if (el.hasAttribute("data-view")) {
      view=el.dataset.view;try { localStorage.setItem("audit-view",view); } catch {} refresh();
    } else if (el.hasAttribute("data-diagram")) {
      diagram=el.dataset.diagram;show();
    } else if (el.id==="pdf-filters") {
      byId("advanced").hidden=false;byId("toggle-filters").setAttribute("aria-expanded","true");
      byId("advanced").scrollIntoView({block:"center"});byId("facets").querySelector("summary")?.focus();
    } else if (el.id==="toggle-filters") {
      const advanced=byId("advanced");
      advanced.hidden=!advanced.hidden;
      el.setAttribute("aria-expanded",String(!advanced.hidden));
    } else if (el.hasAttribute("data-remove-key")) {
      const key=el.dataset.removeKey,next={...filters};
      if (Array.isArray(next[key])) {
        next[key]=next[key].filter(v=>String(v)!==el.dataset.removeValue);
        if (!next[key].length) delete next[key];
      } else delete next[key];
      saveFilters(next);
    } else if (el.id==="clear" || el.hasAttribute("data-reset")) {
      byId("date-error").hidden=true;
      saveFilters({});
    } else if (el.hasAttribute("data-page") && data) {
      listPage=Math.max(0,listPage+Number(el.dataset.page));
      show();
      document.querySelector("#content .list-panel:last-child")?.scrollIntoView({block:"start"});
    } else if (el.id==="retry") refresh();
    else if (el.id==="skip") {event.preventDefault();byId("main").focus();}
  });
  document.addEventListener("toggle",event=>{
    if (event.target.matches("details.facet[open]"))
      document.querySelectorAll("details.facet[open]").forEach(d=>{if(d!==event.target)d.open=false;});
  },true);
  document.addEventListener("change",event=>{
    const el=event.target;
    if(el.id==="pdf-quality"){downloadMenu();show();return;}
    if (el.dataset.chartFilter) {
      chartFilters[el.dataset.chartFilter]=el.value;show();
    } else if (el.dataset.set) {
      const key=el.dataset.set,next={...filters};
      const values=new Set(next[key]||[]);
      if (el.checked) values.add(el.value); else values.delete(el.value);
      if (values.size) next[key]=[...values]; else delete next[key];
      saveFilters(next);
    } else if (el.id==="date-from" || el.id==="date-to") {
      const from=byId("date-from").value,to=byId("date-to").value;
      const error=byId("date-error");
      error.hidden=!(from && to && from>to);
      error.textContent=error.hidden?"":"Дата окончания должна быть не раньше даты начала.";
      if (!error.hidden) return;
      saveFilters({...filters,date_from:from,date_to:to});
    } else if (el.dataset.select) {
      if (el.checked) selected.add(el.dataset.select); else selected.delete(el.dataset.select);
      selectionState();
    } else if (el.id==="select-page") {
      document.querySelectorAll("[data-select]").forEach(box=>{
        box.checked=el.checked;
        if(el.checked)selected.add(box.dataset.select);else selected.delete(box.dataset.select);
      });
      selectionState();
    } else if (el.id==="sort") {sort=el.value;listPage=0;show();}
  });
  document.addEventListener("input",event=>{
    if (event.target.matches(".org-search")) filterOrganizations(event.target);
    if (event.target.id==="query") {
      clearTimeout(queryTimer);
      queryTimer=setTimeout(()=>saveFilters({...filters,q:byId("query").value}),320);
    }
  });
  document.addEventListener("keydown",event=>{
    if (event.key==="Escape") {
      for (const menu of document.querySelectorAll(".facet[open],#download[open]")) {
        menu.open=false;
        menu.querySelector("summary")?.focus();
      }
    }
  });
  let printOpen=[];
  addEventListener("beforeprint",()=>{
    printOpen=[...document.querySelectorAll("details.find-row[open]")];
    document.querySelectorAll("details.find-row").forEach(el=>{el.open=true;});
  });
  addEventListener("afterprint",()=>{
    document.querySelectorAll("details.find-row").forEach(el=>{el.open=printOpen.includes(el);});
  });
  addEventListener("hashchange",()=>{filters=readFilters();listPage=0;refresh();window.scrollTo(0,0);});
  addEventListener("popstate",()=>{
    if (renderedHash===location.hash) {filters=readFilters();listPage=0;refresh();}
  });
  refresh();
})();
