import unittest
from types import SimpleNamespace
from unittest.mock import patch
from telethon import types, functions
import server


class FakeClient:
    def __init__(self):
        self.mark = False
        self.mutations = 0
        self.apply_changes = True

    def is_connected(self): return True
    async def get_entity(self, peer): return types.User(id=123,first_name='Test')
    async def get_input_entity(self, entity): return types.InputPeerUser(123,0)
    async def __call__(self, request):
        if isinstance(request,functions.messages.GetPeerDialogsRequest):
            return SimpleNamespace(dialogs=[SimpleNamespace(unread_mark=self.mark,
                unread_count=3,read_inbox_max_id=100)])
        if isinstance(request,functions.messages.MarkDialogUnreadRequest):
            self.mutations += 1
            if self.apply_changes: self.mark = bool(request.unread)
            return True
        raise AssertionError('Unexpected API call: must not send messages or read acknowledgements')


class DialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminder_toggle_preserves_unread_messages_and_read_receipts(self):
        client = FakeClient()
        with patch.object(server,'CLIENT',client):
            for value in [True,True,False]:
                result = await server.mark_chat_unread(123,'Test',value)
                self.assertEqual(result['unread_mark'],value)
                self.assertEqual(result['unread_message_count'],3)
                self.assertTrue(result['read_receipts_unchanged'])

    async def test_wrong_chat_and_unconfirmed_change_are_not_reported_as_success(self):
        client = FakeClient()
        with patch.object(server,'CLIENT',client):
            with self.assertRaises(ValueError): await server.mark_chat_unread(123,'Wrong')
            self.assertEqual(client.mutations,0)
            client.apply_changes = False
            with self.assertRaises(RuntimeError): await server.mark_chat_unread(123,'Test')


if __name__ == '__main__': unittest.main()
