#!/usr/bin/env python3
"""Reproducible, sequential benchmark against local Ollama only."""
import argparse
from collections import Counter
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import requests
from audit.llm import LLMSettings, OpenAICompatChecker
from audit.factcheck import extract_event_date

POLICY=(
 '\nПравило выбора даты: нужна дата основного конкретного события новости, включая объявленное будущее событие. '
 'Дата праздника в поздравлении или исторической справке не является датой мероприятия учреждения. '
 'Дата документа, срок регистрации, дата заголовка и риторическое «сегодня» сами по себе не датируют событие. '
 'Если основное событие длится несколько дней либо описаны несколько равноправных событий — верни null, '
 'поскольку схема допускает только одну дату. Если сведений недостаточно, также верни null. '
 'Относительные даты «сегодня» и «вчера» считай относительно даты публикации, если они действительно датируют событие.'
)
original=requests.sessions.Session.request
def local_only(self,method,url,*args,**kwargs):
 p=urlsplit(url)
 if p.scheme!='http' or p.hostname!='127.0.0.1' or p.port!=11434:
  raise RuntimeError('Non-local HTTP request blocked')
 self.trust_env=False
 kwargs['allow_redirects']=False
 return original(self,method,url,*args,**kwargs)
requests.sessions.Session.request=local_only

def summarize(rows):
 n=len(rows)
 dated=[r for r in rows if r['expected'] is not None]
 abstain=[r for r in rows if r['expected'] is None]
 correct=sum(r['correct'] for r in rows)
 return {'total':n,'correct':correct,'accuracy':round(correct/n,4) if n else None,
 'dated_correct':sum(r['correct'] for r in dated),'dated_total':len(dated),
 'null_correct':sum(r['correct'] for r in abstain),'null_total':len(abstain),
 'wrong_dates':sum(r['predicted'] is not None and not r['correct'] for r in rows),
 'missed_dates':sum(r['predicted'] is None and r['expected'] is not None for r in rows),
 'api_errors':sum(bool(r.get('error')) for r in rows),
 'median_seconds':round(statistics.median(r['seconds'] for r in rows),3) if rows else None,
 'total_seconds':round(sum(r['seconds'] for r in rows),3),
 'by_origin':{origin:{'correct':sum(r['correct'] for r in group),'total':len(group)} for origin in {r['origin'] for r in rows} for group in [[r for r in rows if r['origin']==origin]]}}

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument('--model',required=True)
 parser.add_argument('--thinking',action='store_true')
 parser.add_argument('--dataset',default=str(ROOT/'benchmarks/event_dates.json'))
 args=parser.parse_args()
 if subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip()!='AI-only-solution':
  raise SystemExit('Benchmark is restricted to AI-only-solution')
 dataset=Path(args.dataset);cases=json.loads(dataset.read_text())['cases']
 run=ROOT/'.cache/benchmarks'/datetime.now().strftime('%Y%m%d-%H%M%S')
 run.mkdir(parents=True)
 os.environ['AUDIT_CACHE_DIR']=str(run/'cache')
 cfg=LLMSettings(provider='openai',base_url='http://127.0.0.1:11434/v1/',api_key='ollama',model=args.model,
                 web_search='off',json_mode='json_schema',timeout=180,max_tokens=2048,
                 reasoning_effort='' if args.thinking else 'none')
 checker=OpenAICompatChecker(cfg)
 checker.system+=POLICY
 info=requests.post('http://127.0.0.1:11434/api/show',json={'model':args.model},timeout=30)
 info.raise_for_status()
 out={'model':args.model,'thinking':args.thinking,'started_at':datetime.now().astimezone().isoformat(),
      'dataset_sha256':hashlib.sha256(dataset.read_bytes()).hexdigest(),
      'system_prompt':checker.system,'system_sha256':hashlib.sha256(checker.system.encode()).hexdigest(),
      'model_info':info.json(),'configuration':{'context':8192,'parallel':1,'max_tokens':2048},'rows':[],'gpu_samples':[]}
 stop=threading.Event()
 def monitor():
  while not stop.is_set():
   try:
    v=subprocess.check_output(['nvidia-smi','--query-gpu=memory.used,utilization.gpu','--format=csv,noheader,nounits'],text=True,timeout=5).strip()
    out['gpu_samples'].append({'time':time.time(),'value':v})
   except Exception:pass
   stop.wait(0.5)
 def save():
  out['summary']=summarize(out['rows'])
  (run/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str)+'\n')
 thread=threading.Thread(target=monitor,daemon=True);thread.start()
 try:
  for case in cases:
   start=time.monotonic();data=None;error=None
   try:
    data=checker.check_news(case['title'],case['text'],datetime.fromisoformat(case['published']),case['site_name'])
    if data is None:error='No parsed response'
   except Exception as exc:error=str(exc)
   predicted=data.get('event_date') if data else None
   predicted=predicted.isoformat() if isinstance(predicted,date) else predicted
   heuristic,basis=extract_event_date(case['title']+'\n'+case['text'],datetime.fromisoformat(case['published']))
   heuristic=heuristic.isoformat() if heuristic else None
   row={'id':case['id'],'origin':case['origin'],'kind':case['kind'],'category':case['category'],
        'expected':case['expected_event_date'],'predicted':predicted,
        'correct':error is None and predicted==case['expected_event_date'],
        'heuristic':heuristic,'heuristic_basis':basis,'seconds':round(time.monotonic()-start,3),'response':data,'error':error}
   out['rows'].append(row);save()
   print(f"{len(out['rows'])}/{len(cases)} {case['id']} expected={row['expected']} got={predicted} correct={row['correct']} {row['seconds']}s",flush=True)
  out['usage']=checker.usage
  out['ollama_ps']=subprocess.check_output(['ollama','ps'],text=True)
  out['heuristic_summary']=summarize([{**r,'predicted':r['heuristic'],'correct':r['heuristic']==r['expected'],'seconds':0,'error':None} for r in out['rows']])
 finally:
  stop.set();thread.join(timeout=6);save()
 print('RESULTS',run,flush=True)
 print(json.dumps(out['summary'],ensure_ascii=False),flush=True)
 return 0
if __name__=='__main__':sys.exit(main())
