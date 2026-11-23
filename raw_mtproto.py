"""Универсальный вызов MTProto (1.9.0, 23.11.2026, по просьбе Стэна).

invoke_mtproto вызывает любой метод Telegram API по имени (например messages.getMessageReactionsList),
так что новые возможности Telegram доступны сразу после обновления Telethon, без отдельного инструмента.
describe_mtproto ищет методы и показывает их параметры. get_raw_usage_stats показывает, какие методы
чаще всего шли через прослойку: это кандидаты на отдельные инструменты с проверками.

Правила:
- Чтение (get*/search*/check*/resolve*) выполняется сразу.
- Всё остальное — запись: только confirm_write=True (после явного разрешения владельца на это
  конкретное действие), обязательный request_id (защита от дублей), expected_chat для адресата,
  суточная квота.
- Для действий, у которых уже есть свой инструмент с проверками (send_message, delete_messages, …),
  прослойка отказывает и называет нужный инструмент.
- Вход в аккаунт, пароль, сессии, удаление аккаунта, платежи/Stars/подарки, удаление чатов и
  истории, передача владения заблокированы полностью. Звонки (phone.*) — обычная запись.
- В журнал (private/raw-usage.sqlite3) пишутся только имя метода, тип, исход, ошибка и имена
  параметров — без значений и без содержимого ответов.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import inspect
import json
import re
import sqlite3
import time

from telethon import errors, functions, types, utils
from telethon.tl.tlobject import TLObject, TLRequest

import admin_tools
from admin_tools import strict_reference, reserve_quota

admin_tools.LIMITS.setdefault('invoke_mtproto_write', (30, 86400))

MAX_JSON = 200_000
MAX_BYTES = 4096
READ_VERBS = ('get', 'search', 'check', 'resolve')
PEER_FIELDS = ('peer', 'channel', 'user_id', 'bot', 'from_peer', 'to_peer')

# get*-методы, которые на самом деле что-то делают от имени аккаунта.
ACTION_DESPITE_NAME = {
    'messages.getbotcallbackanswer': 'нажимает inline-кнопку бота',
    'messages.getinlinebotresults': 'отправляет запрос inline-боту',
    'contacts.getlocated': 'может публиковать геопозицию аккаунта',
}
BLOCKED_NAMESPACES = {'auth': 'вход/выход и ключи сессии'}
BLOCKED_WRITE_NAMESPACES = {'account': 'настройки аккаунта, пароль, сессии',
                            'payments': 'платежи, Stars, подарки'}
# phone.* (звонки) разблокированы 23.11.2026 по просьбе Стэна — как срочное уведомление; это запись,
# то есть только с confirm_write. Голос в звонке требует отдельного VoIP-стека, одного MTProto мало.
BLOCKED_METHODS = {
    'account.gettmppassword': 'временный пароль',
    'account.getpassword': 'параметры пароля',
    'messages.deletehistory': 'удаление всей истории',
    'messages.deletechat': 'удаление чата',
    'channels.deletechannel': 'удаление канала/группы',
    'channels.deletehistory': 'удаление всей истории',
    'channels.editcreator': 'передача владения',
    'contacts.deletecontacts': 'удаление контактов',
    'contacts.resetsaved': 'удаление контактов',
}
DEDICATED = {
    'messages.sendmessage': 'send_message', 'messages.sendmedia': 'send_file',
    'messages.sendmultimedia': 'send_file', 'messages.forwardmessages': 'forward_messages',
    'messages.editmessage': 'edit_message', 'messages.deletemessages': 'delete_messages',
    'channels.deletemessages': 'delete_messages', 'messages.sendreaction': 'set_reaction',
    'messages.readhistory': 'mark_read', 'channels.readhistory': 'mark_read',
    'messages.markdialogunread': 'mark_chat_unread', 'messages.updatepinnedmessage': 'pin_message',
    'channels.invitetochannel': 'add_chat_members', 'messages.addchatuser': 'add_chat_members',
    'channels.editbanned': 'remove_chat_member', 'messages.deletechatuser': 'remove_chat_member',
    'channels.joinchannel': 'join_chat', 'messages.importchatinvite': 'join_chat',
    'messages.createchat': 'create_chat', 'channels.createchannel': 'create_chat',
    'messages.exportchatinvite': 'create_invite_link', 'channels.createforumtopic': 'create_topic',
    'messages.createforumtopic': 'create_topic',
    'channels.editphoto': 'set_chat_photo', 'messages.editchatphoto': 'set_chat_photo',
}


def resolve_method(name: str):
    """'messages.getMessageReactionsList' / 'messages.GetMessageReactionsListRequest' → (key, class)."""
    match = re.fullmatch(r'\s*([A-Za-z]+)\.([A-Za-z][A-Za-z0-9]*)\s*', name or '')
    if not match:
        raise ValueError('method must look like namespace.method, e.g. messages.getMessageReactionsList')
    namespace, method = match.group(1).lower(), match.group(2)
    if method.endswith('Request'):
        method = method[:-7]
    key = f'{namespace}.{method.lower()}'
    module = getattr(functions, namespace, None)
    cls = getattr(module, method[0].upper() + method[1:] + 'Request', None) if module else None
    if cls is None and module is not None:  # регистр внутри имени не совпал
        cls = next((c for n, c in vars(module).items()
                    if n.lower() == method.lower() + 'request' and isinstance(c, type)), None)
    if not (isinstance(cls, type) and issubclass(cls, TLRequest)):
        raise ValueError(f'Unknown method {name}; use describe_mtproto to search')
    return key, cls


def classify(key: str, params: dict | None = None) -> tuple[str, str | None]:
    """→ ('read'|'write'|'blocked'|'dedicated', reason)."""
    namespace, method = key.split('.', 1)
    if namespace in BLOCKED_NAMESPACES:
        return 'blocked', BLOCKED_NAMESPACES[namespace]
    if key in BLOCKED_METHODS:
        return 'blocked', BLOCKED_METHODS[key]
    if key in DEDICATED:
        return 'dedicated', DEDICATED[key]
    if key in ACTION_DESPITE_NAME:
        return 'write', ACTION_DESPITE_NAME[key]
    if key == 'messages.getmessagesviews' and (params or {}).get('increment'):
        return 'write', 'increment=true увеличивает счётчик просмотров'
    if method.startswith(READ_VERBS):
        return 'read', None
    if namespace in BLOCKED_WRITE_NAMESPACES:
        return 'blocked', BLOCKED_WRITE_NAMESPACES[namespace]
    return 'write', None


def signature(cls) -> list[dict]:
    rows = []
    for name, p in inspect.signature(cls.__init__).parameters.items():
        if name == 'self':
            continue
        annotation = p.annotation if p.annotation is not inspect.Parameter.empty else ''
        rows.append({'name': name, 'type': str(annotation).replace('typing.', ''),
                     'optional': p.default is not inspect.Parameter.empty})
    return rows


def docs_url(cls) -> str:
    namespace = cls.__module__.rsplit('.', 1)[-1]
    name = cls.__name__[:-7]
    return f'https://core.telegram.org/method/{namespace}.{name[0].lower()}{name[1:]}'


def tl_type(name: str):
    target = types
    for part in name.split('.'):
        target = getattr(target, part, None)
        if target is None:
            break
    if not (isinstance(target, type) and issubclass(target, TLObject)):
        raise ValueError(f'Unknown TL type {name}')
    return target


async def build(client, value):
    """JSON → аргументы Telethon. Спец-формы:
    {"_": "ReactionEmoji", "emoticon": "👍"} — TL-объект; {"_peer": id}, {"_user": id}, {"_channel": id} —
    ссылка на чат/человека/канал внутри вложенных объектов; {"_bytes": base64}; {"_date": ISO 8601}."""
    if isinstance(value, list):
        return [await build(client, v) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1:
        (tag, inner), = value.items()
        if tag in ('_peer', '_user', '_channel'):
            entity = await client.get_input_entity(strict_reference(inner))
            return {'_peer': utils.get_input_peer, '_user': utils.get_input_user,
                    '_channel': utils.get_input_channel}[tag](entity)
        if tag == '_bytes':
            return base64.b64decode(inner, validate=True)
        if tag == '_date':
            when = datetime.fromisoformat(str(inner).replace('Z', '+00:00'))
            if when.tzinfo is None:
                raise ValueError('_date needs a timezone offset')
            return when
    if '_' in value:
        cls = tl_type(value['_'])
        return cls(**{k: await build(client, v) for k, v in value.items() if k != '_'})
    return {k: await build(client, v) for k, v in value.items()}


def jsonable(value):
    if isinstance(value, TLObject):
        value = value.to_dict()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes):
        if len(value) > MAX_BYTES:
            return {'bytes_len': len(value), 'omitted': True}
        return {'bytes_b64': base64.b64encode(value).decode()}
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def log_call(private, method, kind, outcome, error=None, param_keys=()):
    try:
        with sqlite3.connect(private / 'raw-usage.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls (at REAL, method TEXT, kind TEXT, outcome TEXT, '
                       'error TEXT, param_keys TEXT)')
            db.execute('INSERT INTO calls VALUES (?,?,?,?,?,?)',
                       (time.time(), method, kind, outcome, (error or '')[:200] or None,
                        ','.join(sorted(param_keys))))
    except sqlite3.Error:
        pass  # журнал не должен ломать вызов


def usage_stats(private, days=30, min_calls=5):
    path = private / 'raw-usage.sqlite3'
    if not path.exists():
        return {'days': days, 'methods': [], 'candidates': [], 'total_calls': 0}
    since = time.time() - days * 86400
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT method, kind, COUNT(*), SUM(outcome='ok'), SUM(outcome='error'), "
            "SUM(outcome IN ('blocked','rejected')), MIN(at), MAX(at), "
            "(SELECT error FROM calls c2 WHERE c2.method=calls.method AND c2.error IS NOT NULL AND c2.at>? "
            " ORDER BY at DESC LIMIT 1) "
            "FROM calls WHERE at>? GROUP BY method, kind ORDER BY COUNT(*) DESC", (since, since)).fetchall()
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds')
    methods = [{'method': m, 'kind': k, 'calls': n, 'ok': ok or 0, 'errors': er or 0, 'refused': rf or 0,
                'first': iso(a), 'last': iso(b), 'last_error': le}
               for m, k, n, ok, er, rf, a, b, le in rows]
    candidates = []
    for row in methods:
        if row['kind'] == 'read' and row['calls'] >= min_calls:
            candidates.append({**row, 'why': f"чтение вызывалось {row['calls']} раз(а)"})
        elif row['kind'] == 'write' and row['ok'] >= 2:
            candidates.append({**row, 'why': 'повторяющееся действие: нужен инструмент с проверками'})
        elif row['errors'] >= 3:
            candidates.append({**row, 'why': 'часто ошибается: параметры неудобны, нужен инструмент'})
    return {'days': days, 'total_calls': sum(r['calls'] for r in methods),
            'methods': methods, 'candidates': candidates}


def register_raw_tools(mcp, private, telegram, checked_entity, mutate_once, read, write):

    @mcp.tool(annotations=read)
    async def describe_mtproto(query: str, limit: int = 20) -> dict:
        """Find Telegram API (MTProto) methods for invoke_mtproto. An exact name such as
        messages.getMessageReactionsList returns its parameters, types, whether it is read or write
        and the documentation link. Otherwise returns up to `limit` methods whose name contains query.
        Use this before invoke_mtproto. Prefer a dedicated tool when one exists."""
        if not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        try:
            key, cls = resolve_method(query)
            kind, reason = classify(key)
            return {'method': f'{cls.__module__.rsplit(".", 1)[-1]}.{cls.__name__[:-7]}', 'kind': kind,
                    'reason': reason, 'parameters': signature(cls), 'docs': docs_url(cls)}
        except ValueError:
            pass
        needle = query.strip().lower().replace('request', '')
        if len(needle) < 3:
            raise ValueError('query must have at least 3 characters')
        found = []
        for namespace, module in sorted(vars(functions).items()):
            if not hasattr(module, '__dict__') or namespace.startswith('_') or isinstance(module, type):
                continue
            for name, cls in sorted(vars(module).items()):
                if isinstance(cls, type) and issubclass(cls, TLRequest) and name.endswith('Request'):
                    full = f'{namespace}.{name[:-7]}'
                    if needle in full.lower():
                        kind, reason = classify(f'{namespace}.{name[:-7].lower()}')
                        found.append({'method': full, 'kind': kind, 'reason': reason})
        return {'query': query, 'matches': found[:limit], 'total': len(found)}

    @mcp.tool(annotations=write)
    async def invoke_mtproto(method: str, params: dict | None = None, confirm_write: bool = False,
                             request_id: str = '', expected_chat: str = '') -> dict:
        """Call ANY Telegram API method by name for things no dedicated tool covers
        (e.g. messages.getMessageReactionsList, stories.getPeerStories, messages.getPollResults).
        Find the method and its parameters with describe_mtproto first.
        params: JSON object of the method's parameters. Top-level peer/user_id/channel/bot accept a
        numeric ID or exact @username. Nested: {"_": "TypeName", ...} builds a TL object
        (e.g. {"_": "ReactionEmoji", "emoticon": "👍"}); {"_peer": id}/{"_user": id}/{"_channel": id}
        reference a chat/person; {"_bytes": base64}; {"_date": ISO 8601 with offset}.
        READ methods (get*/search*/check*/resolve*) run at once and change nothing.
        WRITE methods (everything else) only after the owner explicitly authorized this exact action:
        confirm_write=true, a unique request_id (reuse on retry; prevents duplicates), and expected_chat
        = exact name/@username when a peer is targeted. Daily limit 30 writes.
        Calls (phone.*) are writes: only on explicit owner request.
        Refused: login/password/sessions/account deletion, payments/Stars/gifts, deleting whole
        chats/history, ownership transfer; and actions with a dedicated tool (send_message, delete_messages,
        set_reaction, ...) — use that tool. Result: the raw Telegram response as JSON (large bytes omitted)."""
        params = dict(params or {})
        try:
            key, cls = resolve_method(method)
        except ValueError as error:
            log_call(private, method.strip()[:100], 'unknown', 'rejected', str(error), params)
            raise
        canonical = f'{cls.__module__.rsplit(".", 1)[-1]}.{cls.__name__[:-7]}'
        kind, reason = classify(key, params)
        if kind == 'blocked':
            log_call(private, canonical, kind, 'blocked', reason, params)
            raise ValueError(f'{canonical} is not available through this connector ({reason})')
        if kind == 'dedicated':
            log_call(private, canonical, kind, 'blocked', f'use {reason}', params)
            raise ValueError(f'Use the dedicated tool {reason} for {canonical}: it has target checks and receipts')
        allowed = {p['name'] for p in signature(cls)}
        unknown = set(params) - allowed
        if unknown:
            log_call(private, canonical, kind, 'rejected', f'unknown params {sorted(unknown)}', params)
            raise ValueError(f'Unknown parameters {sorted(unknown)}; {canonical} accepts {sorted(allowed)}')
        for field in PEER_FIELDS:
            if isinstance(params.get(field), str):
                params[field] = strict_reference(params[field])
        if kind == 'write':
            if not confirm_write:
                log_call(private, canonical, kind, 'rejected', 'confirm_write missing', params)
                raise ValueError(f'{canonical} changes something ({reason or "write method"}). Ask the owner for '
                                 'explicit permission for this exact action, then repeat with confirm_write=true '
                                 'and a request_id')
            if not request_id:
                raise ValueError('Write methods require a request_id (reuse it on retry)')

        async def prepare():
            # Проверки адресата и сборка аргументов — до записи намерения, чтобы ошибка здесь
            # не оставляла request_id в состоянии «исход неизвестен».
            async with telegram() as client:
                targets = [params[f] for f in PEER_FIELDS if isinstance(params.get(f), (int, str))]
                checked = None
                if kind == 'write' and targets:
                    if not expected_chat:
                        raise ValueError('expected_chat is required when a write targets a chat or person')
                    _, checked = await checked_entity(client, targets[0], expected_chat)
                return checked, {k: await build(client, v) for k, v in params.items()}

        async def call(checked, arguments):
            async with telegram() as client:
                if kind == 'write':
                    reserve_quota(private, 'invoke_mtproto_write')
                try:
                    result = await client(cls(**arguments))
                except errors.RPCError as error:
                    raise ValueError(f'Telegram error {type(error).__name__}: {error}') from None
                except TypeError as error:
                    raise ValueError(f'Bad parameters for {canonical}: {error}') from None
                data = jsonable(result)
                text = json.dumps(data, ensure_ascii=False)
                response = {'method': canonical, 'kind': kind, 'docs': docs_url(cls)}
                if checked:
                    response['chat'] = checked
                if len(text) > MAX_JSON:
                    response.update(truncated=True, result_json_preview=text[:MAX_JSON])
                else:
                    response['result'] = data
                return response

        try:
            checked, arguments = await prepare()
            if kind == 'write':
                payload = ['mtproto', canonical, json.dumps(params, sort_keys=True, ensure_ascii=False)]
                response = await mutate_once(request_id, payload, lambda: call(checked, arguments))
            else:
                response = await call(checked, arguments)
        except Exception as error:
            log_call(private, canonical, kind, 'error', f'{type(error).__name__}: {error}', params)
            raise
        log_call(private, canonical, kind, 'ok', None, params)
        return response

    @mcp.tool(annotations=read)
    async def get_raw_usage_stats(days: int = 30, min_calls: int = 5) -> dict:
        """Which Telegram API methods went through invoke_mtproto and how often (method names, counts,
        outcomes; no message content). `candidates` lists methods worth turning into dedicated tools:
        frequent reads, repeated writes, frequent parameter errors."""
        if not 1 <= days <= 365 or not 1 <= min_calls <= 1000:
            raise ValueError('days must be 1–365 and min_calls 1–1000')
        return usage_stats(private, days, min_calls)

    return describe_mtproto, invoke_mtproto, get_raw_usage_stats
