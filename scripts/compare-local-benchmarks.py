#!/usr/bin/env python3
"""Build a reviewable comparison from two completed local runs; no network calls."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from audit.event_dates import reconcile_event_dates

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument('runs',nargs='+',type=Path)
 parser.add_argument('--output',type=Path,default=ROOT/'benchmarks/comparison.json')
 args=parser.parse_args()
 if subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip()!='AI-only-solution':
  raise SystemExit('Restricted to AI-only-solution')
 fixture=ROOT/'benchmarks/event_dates.json'
 dataset=json.loads(fixture.read_text());cases={c['id']:c for c in dataset['cases']}
 digest=hashlib.sha256(fixture.read_bytes()).hexdigest()
 runs=[json.loads(p.read_text()) for p in args.runs]
 assert len(runs)>=2
 assert all(r['dataset_sha256']==digest and len(r['rows'])==len(cases) for r in runs)
 assert len({r['system_sha256'] for r in runs})==1,'Different prompts cannot be compared as the same test'
 report={'dataset_sha256':digest,'system_sha256':runs[0]['system_sha256'],
 'annotation_method':dataset['annotation_method'],
 'limitations':['Small, selected sample; no independent human annotation.',
               '24 real saved texts and 6 synthetic examples; no full site audit.',
               'Text extraction only; this does not compare image recognition.',
               'Heuristic intentionally skips some future announcements.',
               'One run per model, no repeated-run stability measurement.',
               'Unspecified inference penalties retain model defaults; this does not isolate parameter count or quantization.'],
 'models':[],'heuristic':runs[0]['heuristic_summary']}
 for source,run in zip(args.runs,runs):
  rows=run['rows'];reviews=0;accepted=[];errors=[]
  for row in rows:
   decision=reconcile_event_dates(date.fromisoformat(row['heuristic']) if row['heuristic'] else None,
                                  date.fromisoformat(row['predicted']) if row['predicted'] else None,
                                  model_checked=not bool(row.get('error')))
   if decision.status=='review':reviews+=1
   if decision.value is not None:accepted.append((decision.value.isoformat(),row['expected']))
   if not row['correct']:
    errors.append({'id':row['id'],'title':cases[row['id']]['title'],'url':cases[row['id']].get('url'),
                   'expected':row['expected'],'observed':row['predicted'],'category':row['category'],
                   'basis':(row.get('response') or {}).get('event_date_basis'),'error':row.get('error')})
  memories=[]
  for sample in run.get('gpu_samples', []):
   try:memories.append(int(sample['value'].split(',')[0]))
   except ValueError:pass
  report['models'].append({'model':run['model'],'summary':run['summary'],'first_request_seconds':rows[0]['seconds'],
      'gpu_total_peak_mib':max(memories) if memories else run.get('gpu_total_peak_mib'),'ollama_ps':run.get('ollama_ps'),
      'guard':{'requires_review':reviews,'accepted_dates':len(accepted),
               'accepted_dates_correct':sum(value==gold for value,gold in accepted),
               'accepted_dates_wrong':sum(value!=gold for value,gold in accepted)},
      'errors':errors,'raw_results':str(source.resolve())})
 args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
