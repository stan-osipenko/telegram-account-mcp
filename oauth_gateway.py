"""Private onboarding (web login to Telegram) and OAuth gateway for ChatGPT-style MCP clients.

One owner per instance; settings come from private/instance.json (see instance.py).
Never logs secrets or Telegram data."""
from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import html
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.parse import parse_qs

import httpx
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
import qrcode
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route
from telethon import TelegramClient, errors
import uvicorn

from oauth import Provider, digest

os.umask(0o077)
logging.basicConfig(level=logging.CRITICAL)
from instance import CONFIG, PRIVATE
PUBLIC=(CONFIG['public_url'] or 'https://tg.example.com').rstrip('/')
EXPECTED=CONFIG['expected_account_id']
NAME=CONFIG['name']
OWNER=CONFIG['owner_label']
provider=Provider(PRIVATE,PUBLIC,str(EXPECTED))
STATE={'stage':'complete' if (PRIVATE/'ready.json').exists() else 'phone' if (PRIVATE/'credentials.json').exists() else 'credentials'}
CLIENT=None
TASK=None
LOGIN_PHONE=None
PHONE_CODE_HASH=None
LOCK=asyncio.Lock()
ATTEMPTS={}

def sent_code_record(sent):
    kind=type(sent.type).__name__
    messages={
        'SentCodeTypeApp':'Telegram направил код в служебный чат на уже подключённых устройствах '+OWNER+'.',
        'SentCodeTypeSms':'Telegram направил код по SMS на номер '+OWNER+'.',
        'SentCodeTypeCall':'Telegram выбрал голосовой звонок с кодом на номер '+OWNER+'.',
        'SentCodeTypeFlashCall':'Telegram выбрал проверочный звонок. Этот способ пока не поддерживается страницей.',
        'SentCodeTypeMissedCall':'Telegram выбрал пропущенный звонок; код — последние цифры номера звонящего.',
        'SentCodeTypeEmailCode':'Telegram направил код на почту: '+str(getattr(sent.type,'email_pattern','указанную в аккаунте')),
        'SentCodeTypeSetUpEmailRequired':'Telegram требует сначала настроить почту для входа. Код в служебный чат не отправлен.',
        'SentCodeTypeFragmentSms':'Telegram направил код через Fragment. Сообщение в служебном чате не ожидается.',
        'SentCodeTypeSmsWord':'Telegram направил код-слово по SMS.',
        'SentCodeTypeSmsPhrase':'Telegram направил код-фразу по SMS.',
    }
    return {'stage':'code','delivery':messages.get(kind,'Telegram выбрал способ '+kind+'. Нужна проверка поддержки.'),
        'delivery_type':kind,'next_type':type(sent.next_type).__name__ if getattr(sent,'next_type',None) else None,
        'resend_after':time.time()+max(30,getattr(sent,'timeout',None) or 60)}

def save_pending(sent):
    record=sent_code_record(sent)
    (PRIVATE/'pending-code.json').write_text(json.dumps({'phone':LOGIN_PHONE,'hash':PHONE_CODE_HASH,**record}))
    (PRIVATE/'login-diagnostic.json').write_text(json.dumps({k:record[k] for k in ['delivery_type','next_type','resend_after']}))
    STATE.clear();STATE.update(record)

if STATE['stage']=='phone' and (PRIVATE/'pending-code.json').exists():
    try:
        pending=json.loads((PRIVATE/'pending-code.json').read_text())
        LOGIN_PHONE=pending['phone'];PHONE_CODE_HASH=pending['hash']
        STATE={k:v for k,v in pending.items() if k not in ['phone','hash']}
    except Exception:pass
if STATE.get('stage')!='complete' and (PRIVATE/'login-progress.json').exists():
    try:
        if json.loads((PRIVATE/'login-progress.json').read_text()).get('stage')=='password':STATE={'stage':'password'}
    except Exception:pass


def headers():
    return {'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
        'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
        'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}

def _fill(text):
    return text.replace('{{OWNER}}',OWNER).replace('{{NAME}}',NAME)


def page(body, *, oauth_form=False):
    response_headers=headers()
    # Native form POSTs use Origin: null under no-referrer. Keep same-origin
    # validation effective without sending the flow URL to external origins.
    response_headers['Referrer-Policy']='same-origin'
    if oauth_form:
        # Chrome checks the final destination of a form's 303 redirect against
        # the original document's form-action policy. The OAuth callback must
        # therefore be allowed here, as well as in Provider.register_client.
        response_headers['Content-Security-Policy']=response_headers['Content-Security-Policy'].replace(
            "form-action 'self';",
            "form-action 'self' https://chatgpt.com/connector/oauth/ https://chatgpt.com/connector_platform_oauth_redirect https://chatgpt.com/connector_platform/oauth/callback;")
    return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+NAME+'</title><style>body{font:17px system-ui;background:#eef3f7;color:#142337;margin:0}main{max-width:680px;margin:40px auto;padding:28px;background:white;border-radius:20px}input,button{font:inherit;box-sizing:border-box;padding:12px;margin:8px 0;width:100%;border:1px solid #aab9c8;border-radius:8px}button{background:#175bc9;color:white;cursor:pointer}code{overflow-wrap:anywhere}img{max-width:260px;width:100%}.muted{color:#5d6b7c}pre{white-space:pre-wrap}</style><main>'+_fill(body)+'</main></html>',headers=response_headers)

def limited(key, maximum=10, window=60):
    now=time.monotonic()
    values=[t for t in ATTEMPTS.get(key,[]) if now-t<window]
    if len(values)>=maximum:return True
    ATTEMPTS[key]=values+[now]
    return False

def same_origin(request): return request.headers.get('origin')==PUBLIC

def owner(request):
    key=request.cookies.get(CONFIG['setup_cookie'],'')
    return bool(key and provider.get('setup-session',digest(key)))

async def body_json(request):
    body=await request.body()
    if len(body)>8192:raise ValueError('Request too large')
    return json.loads(body)

async def setup_page(request):
    return page('''<h1>Настройка {{NAME}}</h1><p class="muted">Личный Telegram {{OWNER}} → личный ChatGPT. Ключи и пароль вводятся на сервере по HTTPS.</p><div id="view">Проверяем персональную ссылку…</div>
<script>
const view=document.getElementById('view');let busy=false;
async function api(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined});let v=await r.json();if(!r.ok)throw Error(v.error||'Доступ не подтверждён');return v}
function msg(t){view.textContent=t}
async function refresh(){if(busy)return;try{let s=await api('/setup/state');
if(s.stage==='credentials'){if(document.getElementById('creds'))return;view.innerHTML='<form id="creds"><h2>1. Ключи Telegram API</h2><p>Получите их в my.telegram.org → API development tools. Это не код входа и не токен бота.</p><label>api_id<input id="apiid" inputmode="numeric" autocomplete="off" required></label><label>api_hash<input id="apihash" type="password" autocomplete="off" required></label><button>Продолжить — вход по коду</button></form>';document.getElementById('creds').onsubmit=async e=>{e.preventDefault();busy=true;try{await api('/setup/credentials',{api_id:document.getElementById('apiid').value,api_hash:document.getElementById('apihash').value});view.innerHTML='Подключаемся…'}catch(e){alert(e.message)}finally{busy=false;refresh()}}}
else if(s.stage==='phone'){if(document.getElementById('phoneform'))return;view.innerHTML='<form id="phoneform"><h2>2. Вход по номеру и коду</h2><p>API-ключи уже сохранены. Укажите номер телефона {{OWNER}} в международном формате. Код вводится только на этой странице.</p><label>Номер {{OWNER}}<input id="phone" type="tel" placeholder="+7 …" autocomplete="tel" required></label><button>Получить код Telegram</button></form>';document.getElementById('phoneform').onsubmit=async e=>{e.preventDefault();busy=true;try{await api('/setup/phone',{phone:document.getElementById('phone').value});view.innerHTML='Ожидаем код…'}catch(e){alert(e.message)}finally{busy=false;refresh()}}}
else if(s.stage==='code'){if(!document.getElementById('codeform')){view.innerHTML='<form id="codeform"><h2>3. Введите код</h2><p id="delivery"></p><label>Код Telegram<input id="code" autocomplete="one-time-code" required></label><button>Подтвердить вход</button></form><button id="resend" type="button">Код не пришёл — доступный повтор</button><p id="resendinfo" class="muted"></p>';document.getElementById('codeform').onsubmit=async e=>{e.preventDefault();busy=true;try{await api('/setup/code',{code:document.getElementById('code').value});document.getElementById('code').value='';view.innerHTML='Проверяем…'}catch(e){alert(e.message)}finally{busy=false;refresh()}};document.getElementById('resend').onclick=async()=>{busy=true;try{await api('/setup/resend',{});view.innerHTML='Проверяем способ доставки…'}catch(e){alert(e.message)}finally{busy=false;refresh()}}}document.getElementById('delivery').textContent=s.delivery;let wait=Math.max(0,Math.ceil((s.resend_after||0)-Date.now()/1000));document.getElementById('resend').disabled=!!wait||!s.next_type;document.getElementById('resendinfo').textContent=!s.next_type?'Telegram не предложил другой способ доставки.':wait?'Повтор доступен через '+wait+' с. Следующий способ: '+s.next_type:'Telegram разрешил повтор. Следующий способ: '+s.next_type}
else if(s.stage==='qr'){view.innerHTML='<h2>2. Подтвердите вход</h2><p>На телефоне {{OWNER}}: Telegram → Настройки → Устройства → Подключить устройство. Сканируйте этот QR. Он обновляется автоматически.</p><img id="qr"><p>Подключается только аккаунт {{OWNER}}.</p>';document.getElementById('qr').src=s.qr}
else if(s.stage==='password'){if(document.getElementById('twofa'))return;view.innerHTML='<form id="twofa"><h2>Пароль двухэтапной защиты</h2><p>Введите облачный пароль Telegram {{OWNER}}. Он используется только для входа и не сохраняется.</p><input id="password" type="password" autocomplete="current-password" required><button>Подтвердить</button></form>';document.getElementById('twofa').onsubmit=async e=>{e.preventDefault();busy=true;try{await api('/setup/password',{password:document.getElementById('password').value});view.innerHTML='Завершаем вход…'}catch(e){alert(e.message)}finally{busy=false;refresh()}}}
else if(s.stage==='complete'){if(document.getElementById('done'))return;view.innerHTML='<div id="done"><h2>Telegram подключён</h2><p>Аккаунт {{OWNER}} подтверждён. Следующий шаг — добавить клиент в его личный ChatGPT.</p><p>Адрес MCP:</p><code>'+s.mcp_url+'</code><p>Название: <b>{{NAME}}</b>. Авторизация: <b>OAuth</b>. Регистрация клиента: <b>Dynamic client registration / DCR</b>, если предлагается выбор.</p><p>Откройте chatgpt.com/plugins → «+» → Add custom MCP server. Укажите адрес выше. При подключении появится наша страница подтверждения доступа.</p><p>Сохраните личный код подключения. Он нужен на странице OAuth и отличается от пароля Telegram:</p><code id="connection"></code><p class="muted">Код не нужно пересылать кому-либо или в переписку.</p></div>';document.getElementById('connection').textContent=s.connection_password||'Код уже выдавался. Если он утрачен — обратитесь к администратору для сброса.'}
else if(s.stage==='error'){view.innerHTML='<h2>Вход пока не завершён</h2><p id="error"></p><button id="retry">Повторить ввод ключей</button>';document.getElementById('error').textContent=s.error||'Попробуйте ещё раз';document.getElementById('retry').onclick=async()=>{busy=true;try{await api('/setup/retry',{});view.innerHTML='Повторяем…'}catch(e){alert(e.message)}finally{busy=false;refresh()}}}
else{msg('Завершаем настройку…')}
}catch(e){msg(e.message)}}
(async()=>{try{let key=new URLSearchParams(location.hash.slice(1)).get('key');history.replaceState(null,'',location.pathname);if(key)await api('/setup/access',{key});await refresh();setInterval(refresh,2000)}catch(e){msg(e.message)}})();
</script>''')

async def setup_access(request):
    if not same_origin(request) or limited('bootstrap'):return JSONResponse({'error':'Повторите позже'},status_code=403,headers=headers())
    # Reopening the original link in its already authorized browser must not consume it again.
    if owner(request):return JSONResponse({'ok':True},headers=headers())
    try:
        data=await body_json(request)
        record=json.loads((PRIVATE/'bootstrap.json').read_text())
        if record.get('used') or record['expires']<time.time() or not hmac.compare_digest(digest(data.get('key','')),record['digest']):raise ValueError()
        key=secrets.token_urlsafe(48)
        provider.put('setup-session',digest(key),{'owner':EXPECTED},time.time()+7200)
        record['used']=True;(PRIVATE/'bootstrap.json').write_text(json.dumps(record))
        response=JSONResponse({'ok':True},headers=headers())
        response.set_cookie(CONFIG['setup_cookie'],key,max_age=7200,secure=True,httponly=True,samesite='strict',path='/')
        return response
    except Exception:return JSONResponse({'error':'Персональная ссылка неверна, использована или истекла'},status_code=403,headers=headers())

async def setup_state(request):
    if not owner(request):return JSONResponse({'error':'Откройте персональную ссылку настройки'},status_code=403,headers=headers())
    return JSONResponse({**STATE,'mcp_url':PUBLIC+'/mcp'},headers=headers())

async def finish_login():
    global CLIENT
    me=await CLIENT.get_me()
    if me.id!=EXPECTED:
        await CLIENT.log_out();await CLIENT.disconnect();CLIENT=None
        for filename in ['pending-code.json','login-progress.json']:(PRIVATE/filename).unlink(missing_ok=True)
        STATE.clear();STATE.update(stage='error',error='Вход выполнен другим аккаунтом. Нужен Telegram '+OWNER+'; чужая сессия завершена.')
        return
    await CLIENT.disconnect();CLIENT=None
    (PRIVATE/'pending-code.json').unlink(missing_ok=True)
    (PRIVATE/'login-progress.json').unlink(missing_ok=True)
    password=secrets.token_urlsafe(24)
    salt=secrets.token_hex(16)
    password_hash=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),300000).hex()
    (PRIVATE/'connection-password.json').write_text(json.dumps({'salt':salt,'hash':password_hash}))
    (PRIVATE/'ready.json').write_text(json.dumps({'account_id':EXPECTED,'authorized':True}))
    result=await asyncio.to_thread(subprocess.run,['supervisorctl','start',CONFIG['mcp_program']],capture_output=True)
    if result.returncode:STATE.update(stage='error',error='Вход подтверждён. Требуется проверка запуска сервиса администратором.');return
    STATE.clear();STATE.update(stage='complete',connection_password=password)

async def qr_wait():
    global CLIENT
    try:
        while CLIENT and STATE.get('stage')=='qr':
            qr=await CLIENT.qr_login()
            image=qrcode.make(qr.url);out=io.BytesIO();image.save(out,format='PNG')
            STATE['qr']='data:image/png;base64,'+base64.b64encode(out.getvalue()).decode()
            try:
                await qr.wait(timeout=35)
                await finish_login();return
            except asyncio.TimeoutError:continue
            except errors.SessionPasswordNeededError:
                (PRIVATE/'login-progress.json').write_text(json.dumps({'stage':'password'}))
                STATE.clear();STATE['stage']='password';return
    except Exception:
        if CLIENT:await CLIENT.disconnect();CLIENT=None
        STATE.clear();STATE.update(stage='error',error='Telegram не завершил вход. Администратор проверит подключение; ключи в чат не отправляйте.')

async def setup_credentials(request):
    if not owner(request) or not same_origin(request):return JSONResponse({'error':'Нет доступа'},status_code=403,headers=headers())
    async with LOCK:
        if STATE.get('stage')!='credentials':return JSONResponse({'error':'Вход уже начат'},status_code=409,headers=headers())
        try:
            data=await body_json(request)
            clean=lambda x:str(x).replace('\x1b[200~','').replace('\x1b[201~','').strip()
            api_id=int(clean(data['api_id']));api_hash=clean(data['api_hash'])
            if api_id<=0 or not re.fullmatch('[a-fA-F0-9]{32}',api_hash):raise ValueError()
        except Exception:return JSONResponse({'error':'Проверьте api_id и api_hash: hash должен содержать 32 шестнадцатеричных символа'},status_code=400,headers=headers())
        (PRIVATE/'credentials.json').write_text(json.dumps({'api_id':api_id,'api_hash':api_hash}))
        STATE.clear();STATE['stage']='phone'
        return JSONResponse({'ok':True},headers=headers())

async def ensure_login_client():
    global CLIENT
    if CLIENT is None:
        credentials=json.loads((PRIVATE/'credentials.json').read_text())
        CLIENT=TelegramClient(str(PRIVATE/CONFIG['session']),credentials['api_id'],credentials['api_hash'],
            device_model=''+NAME+'',system_version='Linux VPS',app_version='oauth-gateway',
            receive_updates=True,flood_sleep_threshold=0)
        await CLIENT.connect()
    if await CLIENT.is_user_authorized():
        await finish_login();return False
    return True

async def setup_phone(request):
    global LOGIN_PHONE,PHONE_CODE_HASH,TASK
    if not owner(request) or not same_origin(request):return JSONResponse({'error':'Нет доступа'},status_code=403,headers=headers())
    async with LOCK:
        if STATE.get('stage') not in ['phone','qr'] or (PRIVATE/'ready.json').exists():return JSONResponse({'error':'Сейчас запрос кода не требуется'},status_code=409,headers=headers())
        try:
            data=await body_json(request)
            phone=re.sub(r'[\s()\-]','',str(data.get('phone','')))
            if re.fullmatch(r'8\d{10}',phone):phone='+7'+phone[1:]
            elif re.fullmatch(r'\d{8,15}',phone):phone='+'+phone
            if not re.fullmatch(r'\+[1-9]\d{7,14}',phone) or phone!=(PRIVATE/'owner-phone').read_text().strip():
                return JSONResponse({'error':'Укажите номер Telegram '+OWNER+'. Вход другим аккаунтом не разрешён.'},status_code=400,headers=headers())
            if limited('request-code',3,600):return JSONResponse({'error':'Код уже запрашивали. Подождите перед повторным запросом.'},status_code=429,headers=headers())
            if TASK and not TASK.done():
                TASK.cancel();await asyncio.gather(TASK,return_exceptions=True);TASK=None
            if not await ensure_login_client():return JSONResponse({'ok':True},headers=headers())
            sent=await CLIENT.send_code_request(phone)
            LOGIN_PHONE=phone;PHONE_CODE_HASH=sent.phone_code_hash
            save_pending(sent)
            return JSONResponse({'ok':True},headers=headers())
        except errors.FloodWaitError as error:
            return JSONResponse({'error':f'Telegram просит подождать {error.seconds} секунд перед повтором.'},status_code=429,headers=headers())
        except Exception:return JSONResponse({'error':'Telegram не отправил код. Не повторяйте запрос многократно; сообщите администратору.'},status_code=400,headers=headers())

async def setup_code(request):
    global PHONE_CODE_HASH,LOGIN_PHONE
    if not owner(request) or not same_origin(request):return JSONResponse({'error':'Нет доступа'},status_code=403,headers=headers())
    async with LOCK:
        if STATE.get('stage')!='code' or not PHONE_CODE_HASH:return JSONResponse({'error':'Сначала запросите код на этой странице'},status_code=409,headers=headers())
        if limited('verify-code',5,60):return JSONResponse({'error':'Слишком много попыток. Подождите минуту.'},status_code=429,headers=headers())
        try:
            data=await body_json(request)
            code=str(data.get('code','')).strip()
            if STATE.get('delivery_type') not in ['SentCodeTypeSmsWord','SentCodeTypeSmsPhrase']:
                code=re.sub(r'\s','',code)
                if not re.fullmatch(r'\d{4,8}',code):return JSONResponse({'error':'Введите только цифры кода Telegram'},status_code=400,headers=headers())
            elif not 1<=len(code)<=128:return JSONResponse({'error':'Проверьте код-слово или фразу из SMS'},status_code=400,headers=headers())
            if not await ensure_login_client():return JSONResponse({'ok':True},headers=headers())
            await CLIENT.sign_in(phone=LOGIN_PHONE,code=code,phone_code_hash=PHONE_CODE_HASH)
            PHONE_CODE_HASH=None;LOGIN_PHONE=None
            await finish_login()
            return JSONResponse({'ok':True},headers=headers())
        except errors.SessionPasswordNeededError:
            (PRIVATE/'pending-code.json').unlink(missing_ok=True)
            (PRIVATE/'login-progress.json').write_text(json.dumps({'stage':'password'}))
            PHONE_CODE_HASH=None;LOGIN_PHONE=None;STATE.clear();STATE['stage']='password'
            return JSONResponse({'ok':True},headers=headers())
        except errors.PhoneCodeInvalidError:return JSONResponse({'error':'Код не подошёл. Проверьте последний код Telegram.'},status_code=400,headers=headers())
        except errors.PhoneCodeExpiredError:
            (PRIVATE/'pending-code.json').unlink(missing_ok=True)
            PHONE_CODE_HASH=None;LOGIN_PHONE=None;STATE.clear();STATE['stage']='phone'
            return JSONResponse({'error':'Код истёк. Запросите новый на странице.'},status_code=400,headers=headers())
        except errors.FloodWaitError as error:return JSONResponse({'error':f'Telegram просит подождать {error.seconds} секунд.'},status_code=429,headers=headers())
        except Exception:return JSONResponse({'error':'Вход по коду не завершён. Сообщите администратору, не пересылая код.'},status_code=400,headers=headers())

async def setup_resend(request):
    global PHONE_CODE_HASH
    if not owner(request) or not same_origin(request):return JSONResponse({'error':'Нет доступа'},status_code=403,headers=headers())
    async with LOCK:
        if STATE.get('stage')!='code' or not PHONE_CODE_HASH:return JSONResponse({'error':'Нет активного запроса кода'},status_code=409,headers=headers())
        if not STATE.get('next_type'):return JSONResponse({'error':'Telegram не предложил другой способ доставки; принудительное SMS недоступно'},status_code=409,headers=headers())
        wait=max(0,int(STATE.get('resend_after',0)-time.time()+1))
        if wait:return JSONResponse({'error':f'Повтор разрешён через {wait} секунд'},status_code=429,headers=headers())
        if limited('resend-code',2,600):return JSONResponse({'error':'Лимит повторов. Не запрашивайте код многократно.'},status_code=429,headers=headers())
        try:
            if not await ensure_login_client():return JSONResponse({'ok':True},headers=headers())
            from telethon import functions
            sent=await CLIENT(functions.auth.ResendCodeRequest(phone_number=LOGIN_PHONE,phone_code_hash=PHONE_CODE_HASH))
            PHONE_CODE_HASH=sent.phone_code_hash;save_pending(sent)
            return JSONResponse({'ok':True},headers=headers())
        except errors.FloodWaitError as error:return JSONResponse({'error':f'Telegram просит подождать {error.seconds} секунд'},status_code=429,headers=headers())
        except errors.PhoneCodeExpiredError:
            (PRIVATE/'pending-code.json').unlink(missing_ok=True);PHONE_CODE_HASH=None;STATE.clear();STATE['stage']='phone'
            return JSONResponse({'error':'Предыдущий запрос истёк'},status_code=400,headers=headers())
        except Exception:return JSONResponse({'error':'Telegram не разрешил повтор. Сообщите администратору.'},status_code=400,headers=headers())

async def setup_password(request):
    if not owner(request) or not same_origin(request) or limited('2fa',5,60):return JSONResponse({'error':'Нет доступа или слишком много попыток'},status_code=403,headers=headers())
    async with LOCK:
        if STATE.get('stage')!='password':return JSONResponse({'error':'Сейчас пароль не запрашивается'},status_code=409,headers=headers())
        try:
            data=await body_json(request)
            if not await ensure_login_client():return JSONResponse({'ok':True},headers=headers())
            await CLIENT.sign_in(password=data.get('password',''))
            await finish_login()
            return JSONResponse({'ok':True},headers=headers())
        except errors.PasswordHashInvalidError:return JSONResponse({'error':'Пароль Telegram не подошёл'},status_code=400,headers=headers())
        except Exception:return JSONResponse({'error':'Вход не завершён. Сообщите администратору, не пересылая пароль.'},status_code=400,headers=headers())

async def setup_retry(request):
    if not owner(request) or not same_origin(request):return JSONResponse({'error':'Нет доступа'},status_code=403,headers=headers())
    async with LOCK:
        if (PRIVATE/'ready.json').exists() or STATE.get('stage') not in ['error','credentials']:
            return JSONResponse({'error':'Нельзя сбросить завершённый или выполняющийся вход'},status_code=409,headers=headers())
        STATE.clear();STATE['stage']='credentials'
        return JSONResponse({'ok':True},headers=headers())

async def consent(request):
    flow=request.query_params.get('flow','')
    value=provider.get('flow',flow)
    if not value:return page('<h1>Запрос истёк</h1><p>Начните подключение заново в ChatGPT.</p>')
    return page('<h1>Подключить '+NAME+' к ChatGPT?</h1><p>Разрешается работа с Telegram '+OWNER+': чтение переписки и файлов, отправка и редактирование, группы и другие инструменты. Действия выполняются по вашим поручениям.</p><p>Подключайте только свой аккаунт ChatGPT.</p><form method="post" action="/connect"><input type="hidden" name="flow" value="'+html.escape(flow,quote=True)+'"><input type="hidden" name="csrf" value="'+html.escape(value['csrf'],quote=True)+'"><label>Личный код подключения из настройки<input name="password" type="password" autocomplete="off" required></label><button>Разрешить подключение</button></form>',oauth_form=True)

async def consent_post(request):
    if not same_origin(request) or limited('connect',8,60):return Response('Forbidden',403,headers=headers())
    raw=await request.body()
    if len(raw)>8192:return Response('Too large',413)
    values={k:v[0] for k,v in parse_qs(raw.decode()).items()}
    flow=values.get('flow','');record=provider.get('flow',flow)
    if not record:
        response=page('<h1>Запрос истёк или уже использован</h1><p>Закройте эту вкладку, вернитесь в существующий плагин '+NAME+' в ChatGPT и нажмите «Подключить» ещё раз. Откроется новый запрос. Пересоздавать плагин и входить в Telegram заново не нужно.</p>')
        response.status_code=400
        return response
    if not hmac.compare_digest(record['csrf'],values.get('csrf','')):return Response('Invalid confirmation form: restart Connect from ChatGPT',400,headers=headers())
    try:
        password_record=json.loads((PRIVATE/'connection-password.json').read_text())
        actual=hashlib.pbkdf2_hmac('sha256',values.get('password','').encode(),bytes.fromhex(password_record['salt']),300000).hex()
        valid=hmac.compare_digest(actual,password_record['hash'])
    except Exception:valid=False
    if not valid:return page('<h1>Код подключения не подошёл</h1><p>Это отдельный код, показанный после настройки, а не пароль Telegram. Вернитесь назад и повторите ввод.</p>')
    return RedirectResponse(provider.approve(flow),status_code=303,headers=headers())

async def proxy(request):
    auth=request.headers.get('authorization','')
    token=await provider.load_access_token(auth[7:]) if auth.startswith('Bearer ') else None
    if not token or token.resource!=provider.resource or 'telegram' not in token.scopes:
        return Response('Unauthorized',401,headers={'WWW-Authenticate':'Bearer resource_metadata="'+PUBLIC+'/.well-known/oauth-protected-resource/mcp"',**headers()})
    if not (PRIVATE/'ready.json').exists():return Response('Telegram not configured',503,headers=headers())
    client=httpx.AsyncClient(timeout=httpx.Timeout(180,connect=5))
    forwarded={k:v for k,v in request.headers.items() if k.lower() not in ['host','authorization','connection','cookie','content-length','accept-encoding']}
    forwarded['Authorization']='Bearer '+(PRIVATE/'mcp-token').read_text().strip()
    # Internal backend has an explicit Host allowlist; preserve its public identity.
    forwarded['Host']=PUBLIC.removeprefix('https://')
    try:
        req=client.build_request(request.method,f"http://127.0.0.1:{CONFIG['port']}/mcp",headers=forwarded,content=await request.body())
        result=await client.send(req,stream=True)
    except Exception:
        await client.aclose();return Response('Backend not ready',503,headers=headers())
    async def stream():
        try:
            async for chunk in result.aiter_raw():yield chunk
        finally:await result.aclose();await client.aclose()
    return StreamingResponse(stream(),status_code=result.status_code,headers={k:v for k,v in result.headers.items() if k.lower() not in ['transfer-encoding','connection','content-length']})

async def health(request):return JSONResponse({'ready':(PRIVATE/'ready.json').exists()})

routes=create_auth_routes(provider=provider,issuer_url=AnyHttpUrl(PUBLIC),
    client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=['telegram','offline_access'],default_scopes=['telegram']),
    revocation_options=RevocationOptions(enabled=True))
routes+=create_protected_resource_routes(resource_url=AnyHttpUrl(PUBLIC+'/mcp'),authorization_servers=[AnyHttpUrl(PUBLIC)],scopes_supported=['telegram'])
routes += [Route('/setup',setup_page),Route('/setup/access',setup_access,methods=['POST']),
    Route('/setup/state',setup_state),Route('/setup/credentials',setup_credentials,methods=['POST']),
    Route('/setup/phone',setup_phone,methods=['POST']),Route('/setup/code',setup_code,methods=['POST']),Route('/setup/resend',setup_resend,methods=['POST']),
    Route('/setup/password',setup_password,methods=['POST']),Route('/setup/retry',setup_retry,methods=['POST']),Route('/connect',consent,methods=['GET']),
    Route('/connect',consent_post,methods=['POST']),Route('/mcp',proxy,methods=['GET','POST','DELETE']),Route('/healthz',health)]

@asynccontextmanager
async def lifespan(app):
    yield
    if TASK:TASK.cancel()
    if CLIENT:await CLIENT.disconnect()

app=Starlette(routes=routes,lifespan=lifespan)
if __name__=='__main__':uvicorn.run(app,host='0.0.0.0',port=CONFIG['gateway_port'],access_log=False,log_level='critical')
