#!/usr/bin/env python3
"""Small local-only integration check. Never runs an audit or calls a cloud model."""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL = 'qwen3.5:9b-q4_K_M'
BASE = 'http://127.0.0.1:11434'
OUT = ROOT / '.cache' / ('ollama-smoke-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
OUT.mkdir(parents=True)
for line in (ROOT / 'profiles/ollama.env').read_text().splitlines():
    if line and not line.startswith('#'):
        key, value = line.split('=', 1)
        os.environ[key] = value
os.environ['AUDIT_CACHE_DIR'] = str(OUT / 'crawler-cache')
os.environ['NO_PROXY'] = '127.0.0.1,localhost'
os.environ.pop('ANTHROPIC_API_KEY', None)
import requests
# Fail closed even if an imported component tries to use a different endpoint.
original_request = requests.sessions.Session.request
def local_only(self, method, url, *args, **kwargs):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != 11434:
        raise RuntimeError('Non-local request blocked')
    self.trust_env = False
    kwargs['allow_redirects'] = False
    return original_request(self, method, url, *args, **kwargs)
requests.sessions.Session.request = local_only

results = {'model': MODEL, 'endpoint': BASE, 'started_at': datetime.datetime.now().astimezone().isoformat(), 'tests': {}}
samples = []
stop = threading.Event()
def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
def monitor():
    while not stop.is_set():
        try:
            samples.append({'time': time.time(), 'gpu': command(['nvidia-smi', '--query-gpu=memory.used,memory.total,utilization.gpu', '--format=csv,noheader,nounits']), 'compute': command(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory', '--format=csv,noheader,nounits'])})
        except Exception as exc:
            samples.append({'error': str(exc)})
        stop.wait(0.5)
def save():
    (OUT / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str) + '\n')
    (OUT / 'gpu-samples.json').write_text(json.dumps(samples, ensure_ascii=False, indent=2) + '\n')
def call(path, body):
    response = requests.post(BASE + path, json=body, timeout=300)
    response.raise_for_status()
    return response.json()
def test(name, fn):
    start = time.monotonic()
    try:
        result = fn()
        results['tests'][name] = {'ok': True, 'seconds': round(time.monotonic()-start, 3), 'result': result}
    except Exception as exc:
        results['tests'][name] = {'ok': False, 'seconds': round(time.monotonic()-start, 3), 'error': str(exc)}
    results['tests'][name]['ollama_ps'] = command(['ollama', 'ps'])
    save()
    print(name, json.dumps(results['tests'][name], ensure_ascii=False, default=str), flush=True)

def native_text():
    d = call('/api/chat', {'model': MODEL, 'stream': False, 'think': False, 'messages': [{'role':'user','content':'Ответь одним коротким предложением: для чего нужен краулер сайтов?'}], 'options': {'num_ctx':8192,'num_predict':160,'temperature':0}})
    assert d['message']['content'].strip()
    return d

def structured():
    schema={'type':'object','properties':{'event_date':{'type':'string'},'has_person':{'type':'boolean'}},'required':['event_date','has_person'],'additionalProperties':False}
    d=call('/v1/chat/completions',{'model':MODEL,'reasoning_effort':'none','max_tokens':256,'temperature':0,'messages':[{'role':'user','content':'Извлеки данные: 5 ноября 2026 года директор школы выступил на собрании. Верни JSON: event_date в формате YYYY-MM-DD и has_person.'}],'response_format':{'type':'json_schema','json_schema':{'name':'test','schema':schema,'strict':True}}})
    parsed=json.loads(d['choices'][0]['message']['content'])
    assert parsed == {'event_date':'2026-11-05','has_person':True}, parsed
    return {'response':d,'parsed':parsed}

def vision():
    image=Path(sys.argv[1] if len(sys.argv)>1 else '/tmp/ollama-smoke-inputs/person.jpg')
    encoded=base64.b64encode(image.read_bytes()).decode('ascii')
    prompt='Опиши только видимое: есть ли человек, какие предметы одежды видны и какого они цвета, различим ли галстук, что находится на фоне. Не угадывай скрытые детали. Ответь кратко по-русски.'
    d=call('/v1/chat/completions',{'model':MODEL,'reasoning_effort':'none','max_tokens':450,'temperature':0,'messages':[{'role':'user','content':[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+encoded}}]}]})
    assert d['choices'][0]['message']['content'].strip()
    return {'image_path':str(image),'image_sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'response':d}

def cli_test():
    from audit.cli import main
    code=main(['llm-test'])
    assert code == 0, code
    return {'exit_code':code}

def saved_news():
    from audit.llm import make_checker
    d=json.loads(Path('/tmp/ollama-smoke-inputs/news.json').read_text())
    checker=make_checker(True)
    assert checker.cfg.base_url == BASE+'/v1/' and not checker.grounding
    data=checker.check_news(d['title'],d['text'],datetime.datetime.fromisoformat(d['published']),d['site_name'])
    assert data is not None
    assert data['event_date'] == datetime.date(2026, 9, 29), data
    assert checker.usage['requests']==1 and not checker.usage.get('cached'),checker.usage
    assert all(c['status']=='unverified' for c in data.get('claims',[]))
    return {'news_url':d['url'],'input':d,'output':data,'usage':checker.usage}

thread=threading.Thread(target=monitor,daemon=True)
thread.start()
try:
    results['version']=requests.get(BASE+'/api/version',timeout=10).json()
    results['model_info']=call('/api/show',{'model':MODEL})
    if '--crawler-only' not in sys.argv:
        test('text',native_text)
        test('structured_json',structured)
        test('image',vision)
    test('crawler_llm_test',cli_test)
    test('crawler_saved_news',saved_news)
    results['ollama_ps']=command(['ollama','ps'])
    results['nvidia_smi']=command(['nvidia-smi'])
finally:
    stop.set();thread.join(timeout=12)
    results['finished_at']=datetime.datetime.now().astimezone().isoformat()
    save()
print('Results:',OUT,flush=True)
expected = 2 if '--crawler-only' in sys.argv else 5
sys.exit(0 if len(results['tests'])==expected and all(t['ok'] for t in results['tests'].values()) else 1)
