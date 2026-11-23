"""Тесты voice.py (transcribe_voice) без Telegram и без Whisper."""
import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from telethon import types

import voice


class FakeMCP:
    def tool(self, **_):
        return lambda fn: fn


def voice_message():
    doc = SimpleNamespace(attributes=[types.DocumentAttributeAudio(duration=12, voice=True)], mime_type='audio/ogg')
    return SimpleNamespace(document=doc, file=SimpleNamespace(size=1000))


class Client:
    def __init__(self, message, premium_text=None):
        self.message, self.premium_text = message, premium_text

    async def get_entity(self, chat_id):
        return chat_id

    async def get_input_entity(self, entity):
        return entity

    async def get_messages(self, entity, ids):
        return self.message

    async def __call__(self, request):
        if self.premium_text is None:
            from telethon import errors
            raise errors.RPCError(request, 'PREMIUM_ACCOUNT_REQUIRED', 400)
        return SimpleNamespace(pending=False, text=self.premium_text)

    async def download_media(self, message, file):
        Path(file).write_bytes(b'ogg')


def make(client, private):
    @asynccontextmanager
    async def telegram():
        yield client
    return voice.register_voice_tools(FakeMCP(), private, telegram, None)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.private = Path(self.dir.name)
        voice.JOBS.clear()

    def tearDown(self):
        self.dir.cleanup()

    def test_media_kind(self):
        self.assertEqual(voice.media_kind(voice_message()), ('voice', 12))
        self.assertIsNone(voice.media_kind(SimpleNamespace(document=None)))

    def test_telegram_engine_and_cache(self):
        tool = make(Client(voice_message(), premium_text='привет'), self.private)
        res = asyncio.run(tool(-100, 5))
        self.assertEqual((res['status'], res['engine'], res['text'], res['cached']), ('done', 'telegram', 'привет', False))
        res = asyncio.run(tool(-100, 5))
        self.assertTrue(res['cached'])

    def test_whisper_fallback_deletes_file(self):
        async def fake_whisper(path):
            self.assertTrue(path.exists())
            return {'text': 'текст', 'duration': 12.0}
        with mock.patch.object(voice, 'whisper', fake_whisper):
            res = asyncio.run(make(Client(voice_message()), self.private)(-100, 6))
        self.assertEqual((res['engine'], res['text']), ('whisper-small', 'текст'))
        self.assertEqual(list((self.private / 'voice-tmp').iterdir()), [])

    def test_pending_then_done(self):
        async def slow_whisper(path):
            await asyncio.sleep(0.3)
            return {'text': 'долго', 'duration': 1.0}

        async def run():
            tool = make(Client(voice_message()), self.private)
            with mock.patch.object(voice, 'whisper', slow_whisper), mock.patch.object(voice, 'WAIT', 0.05):
                first = await tool(-100, 7)
                await asyncio.sleep(0.5)
                second = await tool(-100, 7)
            return first, second
        first, second = asyncio.run(run())
        self.assertEqual(first['status'], 'pending')
        self.assertEqual((second['status'], second['text']), ('done', 'долго'))

    def test_not_voice(self):
        msg = SimpleNamespace(document=None, file=None)
        with self.assertRaises(ValueError):
            asyncio.run(make(Client(msg), self.private)(-100, 8))


if __name__ == '__main__':
    unittest.main()
