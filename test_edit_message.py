from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from telethon import types
import server


class FakeClient:
    def __init__(self):
        self.edits = 0
        self.apply_changes = True
        self.message = SimpleNamespace(id=10,chat_id=123,out=True,action=None,message='old',
            date=datetime.now(timezone.utc),sender_id=1,reply_to_msg_id=None,edit_date=None,
            file=None,document=None,photo=None,web_preview=None,media=None)
    def is_connected(self): return True
    async def get_entity(self, peer): return types.User(id=123,first_name='Test')
    async def get_messages(self, entity, ids): return self.message
    async def edit_message(self, entity, message_id, text, **kwargs):
        self.edits += 1
        if self.apply_changes:
            self.message.message = text
            self.message.edit_date = datetime.now(timezone.utc)


class EditTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_preserves_message_and_attachment_and_retry_does_not_edit_again(self):
        client = FakeClient()
        client.message.file = SimpleNamespace(name='report.pdf',size=100,mime_type='application/pdf')
        client.message.document = SimpleNamespace(id=999)
        client.message.media = types.MessageMediaDocument(document=types.DocumentEmpty(id=999))
        with patch.object(server,'CLIENT',client):
            result = await server.edit_message(123,'Test',10,'new caption','old')
            repeated = await server.edit_message(123,'Test',10,'new caption','old')
            self.assertTrue(result['verified'] and result['attachment_unchanged'])
            self.assertEqual(result['message']['id'],10)
            self.assertEqual(result['message']['attachment']['name'],'report.pdf')
            self.assertTrue(repeated['already_matches'])
            self.assertEqual(client.edits,1)

    async def test_web_preview_is_not_treated_as_a_file_caption(self):
        client = FakeClient()
        client.message.media = types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))
        with patch.object(server,'CLIENT',client):
            result = await server.edit_message(123,'Test',10,'x'*3000,'old')
            self.assertTrue(result['verified'])

    async def test_wrong_chat_foreign_message_and_stale_text_are_rejected(self):
        client = FakeClient()
        with patch.object(server,'CLIENT',client):
            with self.assertRaises(ValueError): await server.edit_message(123,'Wrong',10,'new','old')
            with self.assertRaises(ValueError): await server.edit_message(123,'Test',10,'new','stale')
            client.message.out=False
            with self.assertRaises(ValueError): await server.edit_message(123,'Test',10,'new','old')
            self.assertEqual(client.edits,0)

    async def test_unconfirmed_edit_is_not_reported_as_success(self):
        client = FakeClient()
        client.apply_changes=False
        with patch.object(server,'CLIENT',client):
            with self.assertRaises(RuntimeError): await server.edit_message(123,'Test',10,'new','old')


if __name__=='__main__': unittest.main()
