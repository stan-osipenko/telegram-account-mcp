"""Isolated chat-photo tests: fake Telegram client, temporary uploads/receipts, no network."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from telethon import functions, types

import chat_photo
from attachments import UploadStore

NOW = datetime.now(timezone.utc)


def photo(pid):
    return types.Photo(id=pid, access_hash=1, file_reference=b'', date=NOW, sizes=[], dc_id=2)


class Fake:
    def __init__(self, kind='channel', creator=True, change_info=False, has_photo=False):
        rights = types.ChatAdminRights(change_info=change_info) if change_info else None
        if kind == 'channel':
            self.entity = types.Channel(id=700, title='Заборы Посад', photo=types.ChatPhotoEmpty(), date=NOW,
                                        megagroup=True, creator=creator, admin_rights=rights, access_hash=1)
        else:
            self.entity = types.Chat(id=701, title='Заборы Шины', photo=types.ChatPhotoEmpty(),
                                     participants_count=3, date=NOW, version=1, creator=creator, admin_rights=rights)
        self.photo = photo(5) if has_photo else types.PhotoEmpty(id=0)
        self.calls, self.uploaded = [], []
        self.bot = types.User(id=900, bot=True, first_name='Example Bot', username='example_shiny_bot', access_hash=1)
        self.bot_photo = photo(5)

    async def upload_file(self, path, file_name=None):
        assert file_name and file_name.endswith(('.png', '.jpg')), file_name
        self.uploaded.append(Path(path))
        return types.InputFile(id=1, parts=1, name=file_name, md5_checksum='')

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, (functions.channels.GetFullChannelRequest, functions.messages.GetFullChatRequest)):
            return NS(full_chat=NS(chat_photo=self.photo))
        if isinstance(request, (functions.channels.EditPhotoRequest, functions.messages.EditChatPhotoRequest)):
            self.photo = photo(99) if isinstance(request.photo, types.InputChatUploadedPhoto) else types.PhotoEmpty(id=0)
            return NS()
        if isinstance(request, functions.bots.GetAdminedBotsRequest):
            return [self.bot]
        if isinstance(request, functions.photos.GetUserPhotosRequest):
            return NS(photos=[self.bot_photo] if self.bot_photo else [])
        if isinstance(request, functions.photos.UploadProfilePhotoRequest):
            assert request.bot is self.bot
            self.bot_photo = photo(77)
            return NS()
        raise AssertionError(request)


class Tools:
    def __init__(self, fake, private):
        self.fake, self.private, self.receipts, self.tools = fake, private, {}, {}
        mcp = NS(tool=lambda **kw: (lambda f: self.tools.setdefault(f.__name__, f)))

        @asynccontextmanager
        async def telegram():
            yield fake

        async def checked_entity(client, chat_id, expected):
            if expected != fake.entity.title:
                raise ValueError('Identity does not match expected_chat')
            return fake.entity, {'id': chat_id, 'name': fake.entity.title}

        async def mutate_once(request_id, payload, operation):
            if request_id in self.receipts:
                assert self.receipts[request_id][0] == payload
                return {**self.receipts[request_id][1], 'duplicate_prevented': True}
            receipt = await operation()
            self.receipts[request_id] = (payload, receipt)
            return receipt

        chat_photo.register_chat_photo_tools(mcp, private, telegram, checked_entity, mutate_once, None)


def stage(private, size=(512, 512), fmt='PNG', name='ava.png'):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / name
        Image.new('RGB', size, (200, 30, 30)).save(src, fmt)
        data = src.read_bytes()
    store = UploadStore(private / 'uploads')
    up = store.begin(name, len(data), hashlib.sha256(data).hexdigest(), 'image/png')
    import base64
    for offset in range(0, len(data), 196608):
        store.append(up['upload_id'], offset, base64.b64encode(data[offset:offset + 196608]).decode())
    return up['upload_id']


class ChatPhotoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, tools, name, **kw):
        return asyncio.run(tools.tools[name](**kw))

    def test_channel_set_verified_and_idempotent(self):
        t = Tools(Fake('channel'), self.private)
        uid = stage(self.private)
        r = self.run_tool(t, 'set_chat_photo', chat_id=-1000000000700, expected_chat='Заборы Посад',
                          upload_id=uid, request_id='photo-test-1')
        self.assertTrue(r['verified']); self.assertEqual(r['photo_id'], 99); self.assertFalse(r['resized'])
        self.assertIn('EditPhotoRequest', t.fake.calls)
        again = self.run_tool(t, 'set_chat_photo', chat_id=-1000000000700, expected_chat='Заборы Посад',
                              upload_id=uid, request_id='photo-test-1')
        self.assertTrue(again['duplicate_prevented']); self.assertEqual(t.fake.calls.count('EditPhotoRequest'), 1)

    def test_basic_group_uses_edit_chat_photo(self):
        t = Tools(Fake('chat', creator=False, change_info=True), self.private)
        r = self.run_tool(t, 'set_chat_photo', chat_id=-701, expected_chat='Заборы Шины',
                          upload_id=stage(self.private), request_id='photo-test-2')
        self.assertTrue(r['verified']); self.assertIn('EditChatPhotoRequest', t.fake.calls)

    def test_large_image_is_shrunk(self):
        t = Tools(Fake('channel'), self.private)
        r = self.run_tool(t, 'set_chat_photo', chat_id=-1000000000700, expected_chat='Заборы Посад',
                          upload_id=stage(self.private, (3000, 1500), 'JPEG', 'big.jpg'), request_id='photo-test-3')
        self.assertTrue(r['resized']); self.assertEqual(r['size'], [2048, 1024]); self.assertEqual(r['original_size'], [3000, 1500])
        with Image.open(t.fake.uploaded[0]) as im:
            self.assertEqual(im.size, (2048, 1024))

    def test_rejects_without_rights_wrong_name_and_non_image(self):
        t = Tools(Fake('channel', creator=False), self.private)
        with self.assertRaisesRegex(ValueError, 'change_info'):
            self.run_tool(t, 'set_chat_photo', chat_id=-1000000000700, expected_chat='Заборы Посад',
                          upload_id=stage(self.private), request_id='photo-test-4')
        t = Tools(Fake('channel'), self.private)
        with self.assertRaisesRegex(ValueError, 'expected_chat'):
            self.run_tool(t, 'set_chat_photo', chat_id=-1000000000700, expected_chat='Заборы',
                          upload_id=stage(self.private), request_id='photo-test-5')
        store = UploadStore(self.private / 'uploads')
        data = b'GIF89a' + b'\0' * 50
        up = store.begin('x.gif', len(data), hashlib.sha256(data).hexdigest(), 'image/gif')
        import base64
        store.append(up['upload_id'], 0, base64.b64encode(data).decode())
        with self.assertRaisesRegex(ValueError, 'JPEG/PNG|JPEG or PNG'):
            self.run_tool(t, 'set_chat_photo', chat_id=-1000000000700, expected_chat='Заборы Посад',
                          upload_id=up['upload_id'], request_id='photo-test-6')
        self.assertNotIn('EditPhotoRequest', t.fake.calls)

    def test_delete_photo(self):
        t = Tools(Fake('channel', has_photo=True), self.private)
        r = self.run_tool(t, 'delete_chat_photo', chat_id=-1000000000700, expected_chat='Заборы Посад',
                          request_id='photo-test-7')
        self.assertTrue(r['verified']); self.assertTrue(r['had_photo'])
        self.assertIsInstance(t.fake.photo, types.PhotoEmpty)


    def test_bot_photo_owned_only(self):
        t = Tools(Fake('channel'), self.private)
        r = self.run_tool(t, 'set_bot_photo', bot_username='@example_shiny_bot',
                          upload_id=stage(self.private), request_id='photo-test-8')
        self.assertTrue(r['verified']); self.assertEqual((r['photo_id'], r['previous_photo_id']), (77, 5))
        with self.assertRaisesRegex(ValueError, 'does not own'):
            self.run_tool(t, 'set_bot_photo', bot_username='@other_bot',
                          upload_id=stage(self.private), request_id='photo-test-9')


if __name__ == '__main__':
    unittest.main()
