"""Authenticated entry point for a separate demonstration copy."""
import hmac
import os
import secrets
from contextlib import closing

import uvicorn
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response, RedirectResponse
from starlette.websockets import WebSocket
from . import config, demo_settings
from .portal import create_app

LOGIN = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Контур</title>
<style>body{background:#f0f5f4;color:#172f39;font:18px/1.55 system-ui}main{max-width:32rem;margin:12vh auto;padding:2rem;background:white;border-radius:16px}input,button{font:inherit;padding:.55rem;max-width:90%}button{background:#175c53;color:white;border:0}</style>
<main><h1>Контур</h1><p>Демонстрация для жюри. Откройте ссылку из архива или введите код показа.</p>
<form action="/demo-login" method="post"><label>Код показа <input name="invite" type="password" required></label> <button>Войти</button></form></main>
<script>const key=new URLSearchParams(location.hash.slice(1)).get('invite');if(key){history.replaceState(null,'','/');document.querySelector('input').value=key;document.forms[0].submit();}</script></html>"""


class JuryEntry:
    def __init__(self, app, invite, host):
        self.app, self.invite, self.host = app, invite, host
        self.session = secrets.token_urlsafe(32)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http","websocket"):
            return await self.app(scope,receive,send)
        headers = Headers(scope=scope)
        host = headers.get("host","")
        origin = headers.get("origin")
        valid = host == self.host and (not origin or origin=="http://"+host)
        # A browser mutation or socket must originate in this demo.
        if scope["type"]=="websocket" or scope.get("method") not in ("GET","HEAD"):
            valid = valid and origin=="http://"+host
        socket = scope["type"]=="websocket"
        request = WebSocket(scope,receive,send) if socket else Request(scope,receive)
        def equal(a,b):
            return hmac.compare_digest(a.encode(), b.encode())
        if valid and not socket and scope["path"]=="/demo-login" and scope["method"]=="POST":
            from urllib.parse import parse_qs
            if 0<int(headers.get("content-length","0") or 0)<=512:
                value = parse_qs((await request.body()).decode("ascii","ignore")).get("invite",[""])[0]
                if equal(value,self.invite):
                    response = RedirectResponse("/",303)
                    response.set_cookie("kontur_jury",self.session,httponly=True,samesite="strict",max_age=86400)
                    return await response(scope,receive,send)
        if not valid or not equal(request.cookies.get("kontur_jury",""),self.session):
            if socket:
                return await request.close(code=1008)
            if valid and scope["path"]=="/" and scope["method"]=="GET":
                return await HTMLResponse(LOGIN,headers={"Cache-Control":"no-store","Referrer-Policy":"same-origin"})(scope,receive,send)
            return await Response("Доступ по ссылке из архива",403,media_type="text/plain")(scope,receive,send)
        # The inner portal enforces its original loopback host/origin checks.
        local = ("127.0.0.1:"+str(scope["server"][1])).encode()
        clean = [(k,v) for k,v in scope["headers"] if k not in (b"host",b"origin") and not k.startswith(b"x-forwarded")]
        clean.append((b"host", local))
        if origin: clean.append((b"origin",b"http://"+local))
        await self.app({**scope,"headers":clean},receive,send)


def main():
    root=demo_settings.directory()
    if root is None or not (root/"search.sqlite3").is_file():
        raise SystemExit("Нужна подготовленная копия для демонстрации.")
    app=create_app()
    with closing(config.connect_db()) as conn:
        demo_settings.baseline(conn)
    invite=(root.parent/"invite.txt").read_text(encoding="ascii").strip()
    if len(invite)<24:
        raise ValueError("Слишком короткое приглашение.")
    uvicorn.run(JuryEntry(app,invite,os.environ["KONTUR_DEMO_HOST"]),host="127.0.0.1",port=8766,
                proxy_headers=False,access_log=False)


if __name__=="__main__":
    main()
