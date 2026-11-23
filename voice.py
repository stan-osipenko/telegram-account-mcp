"""transcribe_voice (1.9.0, 23.11.2026): расшифровка голосовых/кружков/аудио в текст. Только чтение.

1) Встроенная расшифровка Telegram (messages.TranscribeAudio) — если у аккаунта Premium
   (или остались пробные расшифровки). Ничего не помечает прочитанным.
2) Иначе — Whisper (faster-whisper small, int8, ru) локально на VPS в отдельном процессе
   (~/services/whisper). Файл скачивается во private/voice-tmp и удаляется после расшифровки.

Результат кэшируется в private/transcripts.sqlite3. Длинная расшифровка не держит вызов:
если не уложилась в WAIT секунд, ответ {status: 'pending'} — повторный вызов вернёт готовый текст.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from telethon import errors, functions, types

WHISPER = Path(os.environ.get('WHISPER_HOME', Path.home() / 'services/whisper'))
WAIT = 45                 # сек: дольше вызов не держим
WHISPER_TIMEOUT = 900     # сек на одну расшифровку Whisper
MAX_BYTES = 20 * 1024 * 1024
RUN_LOCK = asyncio.Lock()  # один Whisper за раз (1 ядро, 2 ГБ RAM)
JOBS: dict = {}


def _db(private):
    con = sqlite3.connect(private / 'transcripts.sqlite3', timeout=30)
    con.execute('CREATE TABLE IF NOT EXISTS transcripts (chat_id INTEGER, message_id INTEGER, engine TEXT,'
                ' text TEXT, duration REAL, created_at REAL, PRIMARY KEY (chat_id, message_id))')
    return con


def cached(private, chat_id, message_id):
    con = _db(private)
    try:
        row = con.execute('SELECT engine,text,duration FROM transcripts WHERE chat_id=? AND message_id=?',
                          (chat_id, message_id)).fetchone()
    finally:
        con.close()
    return row and {'engine': row[0], 'text': row[1], 'duration': row[2]}


def save(private, chat_id, message_id, res):
    con = _db(private)
    try:
        with con:
            con.execute('INSERT OR REPLACE INTO transcripts VALUES (?,?,?,?,?,?)',
                        (chat_id, message_id, res['engine'], res['text'], res.get('duration'), time.time()))
    finally:
        con.close()


def media_kind(message):
    doc = getattr(message, 'document', None)
    if not doc:
        return None
    for attr in doc.attributes:
        if isinstance(attr, types.DocumentAttributeAudio):
            return ('voice' if attr.voice else 'audio'), attr.duration
        if isinstance(attr, types.DocumentAttributeVideo) and getattr(attr, 'round_message', False):
            return 'video_note', attr.duration
    if (doc.mime_type or '').startswith(('audio/', 'video/')):
        return 'audio', None
    return None


async def telegram_transcribe(client, entity, message_id):
    """-> текст или None (нет Premium/пробных, ошибка)."""
    try:
        peer = await client.get_input_entity(entity)
        for _ in range(12):
            res = await client(functions.messages.TranscribeAudioRequest(peer=peer, msg_id=message_id))
            if not res.pending:
                return (res.text or '').strip() or None
            await asyncio.sleep(2)
    except errors.RPCError:
        return None
    return None


async def whisper(path):
    proc = await asyncio.create_subprocess_exec(
        str(WHISPER / '.venv/bin/python'), str(WHISPER / 'transcribe.py'), str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), WHISPER_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError('Whisper timeout')
    if proc.returncode:
        raise RuntimeError('Whisper failed: ' + err.decode(errors='replace')[-300:])
    return json.loads(out)


def register_voice_tools(mcp, private, telegram, read_annotations):
    tmp = private / 'voice-tmp'

    async def job(chat_id, message_id, engine):
        async with telegram() as client:
            entity = await client.get_entity(chat_id)
            message = await client.get_messages(entity, ids=message_id)
            if not message:
                raise ValueError('Message not found in this chat')
            kind = media_kind(message)
            if not kind:
                raise ValueError('Message has no voice/audio/video note')
            if message.file and message.file.size and message.file.size > MAX_BYTES:
                raise ValueError('Audio is larger than 20 MB')
            text = await telegram_transcribe(client, entity, message_id) if engine in ('auto', 'telegram') else None
            if text:
                res = {'engine': 'telegram', 'text': text, 'duration': kind[1]}
                save(private, chat_id, message_id, res)
                return res
            if engine == 'telegram':
                raise ValueError('Telegram transcription unavailable (no Premium / trial)')
            tmp.mkdir(mode=0o700, exist_ok=True)
            path = tmp / f'{uuid.uuid4().hex}.media'
            await client.download_media(message, file=str(path))
        try:
            os.chmod(path, 0o600)
            async with RUN_LOCK:
                out = await whisper(path)
        finally:
            path.unlink(missing_ok=True)
        res = {'engine': 'whisper-small', 'text': out['text'], 'duration': out.get('duration') or kind[1]}
        save(private, chat_id, message_id, res)
        return res

    @mcp.tool(annotations=read_annotations)
    async def transcribe_voice(chat_id: int, message_id: int, engine: str = 'auto', refresh: bool = False) -> dict:
        """Transcribe a voice message / audio / round video note to text (Russian). Read-only:
        marks nothing as read. engine: 'auto' (Telegram's own transcription if the account has
        Premium/trial, else local Whisper on the VPS), 'telegram' or 'whisper'. Results are cached.
        Long audio does not block: if not ready within ~45 s returns {status:'pending'} — call again
        later with the same arguments to get the text. Whisper text may contain recognition errors."""
        if engine not in ('auto', 'telegram', 'whisper'):
            raise ValueError("engine must be 'auto', 'telegram' or 'whisper'")
        key = (int(chat_id), int(message_id))
        if not refresh and (hit := cached(private, *key)):
            return {'status': 'done', 'chat_id': key[0], 'message_id': key[1], 'cached': True, **hit}
        task = JOBS.get(key)
        if task is None or (task.done() and refresh):
            task = JOBS[key] = asyncio.create_task(job(*key, engine))
            task.add_done_callback(lambda t, k=key: t.exception() is not None and JOBS.pop(k, None))
        try:
            res = await asyncio.wait_for(asyncio.shield(task), WAIT)
        except asyncio.TimeoutError:
            return {'status': 'pending', 'chat_id': key[0], 'message_id': key[1],
                    'note': 'Расшифровка идёт; вызовите ещё раз через минуту-две'}
        JOBS.pop(key, None)
        return {'status': 'done', 'chat_id': key[0], 'message_id': key[1], 'cached': False, **res}
    return transcribe_voice
