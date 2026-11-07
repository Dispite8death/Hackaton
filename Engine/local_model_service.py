"""Prepare a configured loopback inference server for a bounded worker cycle."""

import errno
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

if __package__:
    from .model_gateway import _NoRedirect
else:
    from model_gateway import _NoRedirect


def _state(status,detail):
    return {"ready":status in {"ready","unmanaged"},"status":status,"detail":detail}


def _loopback(host):
    if host=="localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(host or "")
        return str(address) if address.is_loopback else None
    except ValueError:
        return None


def _probe(profile,key):
    headers={"Accept":"application/json"}
    if key:
        headers["Authorization"]="Bearer "+key
    url=profile.endpoint.rstrip("/")+"/models"
    try:
        with build_opener(ProxyHandler({}),_NoRedirect()).open(Request(url,headers=headers),timeout=2.5) as response:
            raw=response.read(1_048_577)
    except HTTPError as error:
        with error:
            reason = ("Проверьте ключ локального профиля." if error.code in (401,403)
                      else "Проверьте адрес API в настройках.")
            return _state("error",f"Локальный сервер ответил HTTP {error.code}. {reason}")
    except (OSError,URLError) as error:
        cause=getattr(error,"reason",error)
        if isinstance(cause,ConnectionRefusedError) or getattr(cause,"errno",None) in {errno.ECONNREFUSED,10061}:
            return _state("offline","На заданном порту нет локальной службы.")
        return _state("waiting","Локальный API не отвечает или соединение прервано. Следующий цикл повторит проверку.")
    try:
        if len(raw)>1_048_576:
            raise ValueError
        data=json.loads(raw)
        models=data["data"]
        if models is None and profile.provider=="ollama" and data.get("object")=="list":
            models=[]
        if not isinstance(models,list):
            raise ValueError
        ids={r["id"] for r in models if isinstance(r,dict) and isinstance(r.get("id"),str)}
    except (ValueError,TypeError,KeyError):
        return _state("error","Локальный адрес не вернул ожидаемый список моделей.")
    if profile.model not in ids:
        return _state("model_missing",f"Сервер доступен. Модель «{profile.model}» не объявлена API. "
                      "Проверьте установленную модель и её загрузку в приложении. Проверка повторится в следующем цикле.")
    return _state("ready",f"Локальный сервер объявил модель «{profile.model}».")


def _executable(provider):
    name = "lms" if provider=="lmstudio" else "ollama"
    # A scheduled process may have an older PATH than an interactive terminal.
    executable = name+".exe" if os.name=="nt" else name
    known = (Path.home()/".lmstudio"/"bin"/executable if provider=="lmstudio" else
             Path(os.environ.get("LOCALAPPDATA",Path.home()))/"Programs"/"Ollama"/executable)
    return str(known) if known.is_file() else shutil.which(executable)


def _start(profile,bind,port):
    cli=_executable(profile.provider)
    if not cli:
        return "CLI локального сервера отсутствует. Проверьте установку LM Studio или Ollama."
    common={"stdin":subprocess.DEVNULL,"stdout":subprocess.DEVNULL,"stderr":subprocess.DEVNULL,
            "creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
    if profile.provider=="lmstudio":
        # Both commands use the installed LM Studio backend. No install, update, or model download commands.
        commands=[[cli,"daemon","up","--json"],[cli,"server","start","--bind",bind,"--port",str(port)]]
        for cmd in commands:
            process=subprocess.run(cmd,timeout=12,**common)
            if process.returncode:
                return (f"LM Studio не запустила службу (код {process.returncode}). "
                        "Проверьте установленный локальный сервер или откройте его в LM Studio.")
    else:
        host=f"[{bind}]" if ":" in bind else bind
        env={**os.environ,"OLLAMA_HOST":f"{host}:{port}","OLLAMA_NO_CLOUD":"1"}
        subprocess.Popen([cli,"serve"],env=env,close_fds=True,**common)
    return None


def prepare_local_service(profile,*,key=""):
    """Start only for a refused loopback connection. External profiles use their usual gateway."""
    if profile.provider not in {"lmstudio","ollama"}:
        return _state("unmanaged","Используется настроенный внешний API.")
    url=urlsplit(profile.endpoint)
    bind=_loopback(url.hostname)
    if not bind:
        return _state("unmanaged","Сетевым сервером управляет владелец хоста.")
    state=_probe(profile,key)
    if state["status"]!="offline":
        return state
    if url.scheme!="http" or url.path.rstrip("/")!="/v1":
        return _state("waiting","Этот локальный адрес требует предварительно настроенного сервера.")
    try:
        issue=_start(profile,bind,url.port or 80)
    except (OSError,subprocess.TimeoutExpired) as exc:
        issue=f"Службу не удалось запустить: {type(exc).__name__}. Следующий цикл повторит проверку."
    if issue:
        # Another process may have won the race to bind the port.
        check=_probe(profile,key)
        return check if check["status"]!="offline" else _state("waiting",issue)
    deadline=time.monotonic()+18
    while True:
        state=_probe(profile,key)
        if state["status"] not in {"offline","waiting"}:
            return state
        if time.monotonic()>=deadline:
            return _state("waiting","Локальный сервер не успел запуститься. Следующий цикл повторит проверку.")
        time.sleep(0.7)
