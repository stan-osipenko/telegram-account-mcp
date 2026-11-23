"""Isolated invoke_mtproto tests: fake Telegram client, temporary state, no network or sends."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from telethon import errors, functions, types

import raw_mtproto as raw
import server

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
USER = types.User(id=77, first_name='Анна', access_hash=5, username='anna_test')


class Fake:
    def __init__(self):
        self.calls = []
    def is_connected(self): return True
    async def get_entity(self, ref):
        if ref in (77, '@anna_test'):
            return USER
        raise ValueError('not found')
    async def get_input_entity(self, ref):
        return types.InputPeerUser(user_id=(await self.get_entity(ref)).id, access_hash=5)
    async def __call__(self, request):
        self.calls.append(request)
        if isinstance(request, functions.messages.GetMessageReactionsListRequest):
            return types.messages.MessageReactionsList(
                count=1, reactions=[types.MessagePeerReaction(peer_id=types.PeerUser(77), date=NOW,
                                                              reaction=types.ReactionEmoji('👍'))],
                chats=[], users=[USER])
        if isinstance(request, functions.messages.ToggleDialogPinRequest):
            return True
        if isinstance(request, functions.help.GetConfigRequest):
            raise errors.RPCError(request, 'TEST_ERROR', 400)
        return True


class RawTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = Path(self.tmp.name)
        self.patch = patch.object(server, 'PRIVATE', self.private)
        self.patch.start()
        self.describe, self.invoke, self.stats = raw.register_raw_tools(
            FastMCP('t'), self.private, server.telegram, server.checked_entity, server.mutate_once,
            server.READ, server.WRITE)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_method_names_and_classification(self):
        for name in ('messages.getMessageReactionsList', 'messages.GetMessageReactionsList',
                     'messages.GetMessageReactionsListRequest', ' messages.getmessagereactionslist '):
            key, cls = raw.resolve_method(name)
            self.assertIs(cls, functions.messages.GetMessageReactionsListRequest)
            self.assertEqual(raw.classify(key), ('read', None))
        for bad in ('InvokeWithLayer', 'messages.noSuchMethod', 'nope.getX', ''):
            with self.assertRaises(ValueError):
                raw.resolve_method(bad)
        kind = lambda m, p=None: raw.classify(raw.resolve_method(m)[0], p)[0]
        self.assertEqual(kind('auth.logOut'), 'blocked')
        self.assertEqual(kind('auth.exportAuthorization'), 'blocked')
        self.assertEqual(kind('account.deleteAccount'), 'blocked')
        self.assertEqual(kind('account.getTmpPassword'), 'blocked')
        self.assertEqual(kind('account.getAuthorizations'), 'read')
        self.assertEqual(kind('payments.sendStarsForm'), 'blocked')
        self.assertEqual(kind('payments.getStarsStatus'), 'read')
        self.assertEqual(kind('phone.requestCall'), 'write')
        self.assertEqual(kind('phone.getCallConfig'), 'read')
        self.assertEqual(kind('channels.deleteChannel'), 'blocked')
        self.assertEqual(kind('messages.deleteHistory'), 'blocked')
        self.assertEqual(kind('messages.sendMessage'), 'dedicated')
        self.assertEqual(kind('messages.sendReaction'), 'dedicated')
        self.assertEqual(kind('messages.readHistory'), 'dedicated')
        self.assertEqual(kind('messages.getBotCallbackAnswer'), 'write')
        self.assertEqual(kind('messages.getMessagesViews', {'increment': True}), 'write')
        self.assertEqual(kind('messages.getMessagesViews', {'increment': False}), 'read')
        self.assertEqual(kind('messages.toggleDialogPin'), 'write')
        self.assertEqual(kind('stories.readStories'), 'write')

    async def test_describe(self):
        exact = await self.describe('messages.getMessageReactionsList')
        self.assertEqual(exact['method'], 'messages.GetMessageReactionsList')
        self.assertEqual(exact['kind'], 'read')
        self.assertIn('peer', [p['name'] for p in exact['parameters']])
        self.assertEqual(exact['docs'], 'https://core.telegram.org/method/messages.getMessageReactionsList')
        found = await self.describe('reactionslist')
        self.assertIn('messages.GetMessageReactionsList', [m['method'] for m in found['matches']])

    async def test_read_runs_at_once_and_is_logged(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            result = await self.invoke('messages.getMessageReactionsList',
                                       {'peer': '@anna_test', 'id': 5, 'limit': 10,
                                        'reaction': {'_': 'ReactionEmoji', 'emoticon': '👍'}})
        request = fake.calls[0]
        self.assertEqual(request.peer, '@anna_test')  # Telethon resolves top-level peers itself
        self.assertEqual(request.reaction, types.ReactionEmoji('👍'))
        self.assertEqual(result['kind'], 'read')
        self.assertEqual(result['result']['reactions'][0]['reaction']['emoticon'], '👍')
        self.assertEqual(result['result']['reactions'][0]['date'], NOW.isoformat())
        stats = await self.stats()
        self.assertEqual(stats['methods'][0]['method'], 'messages.GetMessageReactionsList')
        self.assertEqual(stats['methods'][0]['ok'], 1)
        with sqlite3.connect(self.private / 'raw-usage.sqlite3') as db:
            stored = db.execute('SELECT * FROM calls').fetchall()
        self.assertNotIn('anna', repr(stored))  # values are not logged, only parameter names
        self.assertIn('peer', stored[0][5])

    async def test_bare_word_peer_rejected(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                await self.invoke('messages.getMessageReactionsList', {'peer': 'Анна', 'id': 5, 'limit': 1})
            with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                await self.invoke('messages.getMessageReactionsList',
                                  {'peer': {'_peer': 'Анна'}, 'id': 5, 'limit': 1})
        self.assertEqual(fake.calls, [])

    async def test_write_needs_confirmation_target_and_runs_once(self):
        fake = Fake()
        params = {'peer': {'_': 'InputDialogPeer', 'peer': {'_peer': 77}}, 'pinned': True}
        with patch.object(server, 'CLIENT', fake):
            with self.assertRaisesRegex(ValueError, 'explicit permission'):
                await self.invoke('messages.toggleDialogPin', params)
            with self.assertRaisesRegex(ValueError, 'request_id'):
                await self.invoke('messages.toggleDialogPin', params, confirm_write=True)
            self.assertEqual(fake.calls, [])
            flat = {'peer': 77, 'pinned': True}
            with self.assertRaisesRegex(ValueError, 'expected_chat is required'):
                await self.invoke('messages.toggleDialogPin', flat, True, 'pin-test-0001')
            with self.assertRaisesRegex(ValueError, 'Identity does not match'):
                await self.invoke('messages.toggleDialogPin', flat, True, 'pin-test-0001', 'Борис')
            self.assertEqual(fake.calls, [])
            # Failed checks did not leave the request_id uncertain:
            first = await self.invoke('messages.toggleDialogPin', flat, True, 'pin-test-0001', '@anna_test')
            again = await self.invoke('messages.toggleDialogPin', flat, True, 'pin-test-0001', '@anna_test')
            self.assertEqual(first['result'], True)
            self.assertEqual(first['chat']['chat_id'], 77)
            self.assertTrue(again['duplicate_prevented'])
            self.assertEqual(len(fake.calls), 1)
            nested = await self.invoke('messages.toggleDialogPin', params, True, 'pin-test-0002')
            self.assertEqual(fake.calls[-1].peer, types.InputDialogPeer(types.InputPeerUser(77, 5)))
            self.assertEqual(nested['result'], True)

    async def test_write_quota(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake), patch.dict(raw.admin_tools.LIMITS,
                                                              {'invoke_mtproto_write': (1, 86400)}):
            await self.invoke('messages.toggleDialogPin', {'peer': 77, 'pinned': True}, True,
                              'quota-test-01', 'Анна')
            with self.assertRaisesRegex(ValueError, 'safety limit'):
                await self.invoke('messages.toggleDialogPin', {'peer': 77, 'pinned': False}, True,
                                  'quota-test-02', 'Анна')
        self.assertEqual(len(fake.calls), 1)

    async def test_refusals_and_errors(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            with self.assertRaisesRegex(ValueError, 'dedicated tool send_message'):
                await self.invoke('messages.sendMessage', {'peer': 77, 'message': 'x'}, True, 'x-test-0001', 'Анна')
            with self.assertRaisesRegex(ValueError, 'not available'):
                await self.invoke('auth.logOut', {}, True, 'x-test-0002')
            with self.assertRaisesRegex(ValueError, 'not available'):
                await self.invoke('payments.sendStarsForm', {}, True, 'x-test-0003')
            with self.assertRaisesRegex(ValueError, 'Unknown parameters'):
                await self.invoke('messages.getMessageReactionsList', {'peer': 77, 'bogus': 1})
            with self.assertRaisesRegex(ValueError, 'TEST_ERROR'):
                await self.invoke('help.getConfig')
        self.assertEqual(len(fake.calls), 1)  # only help.getConfig reached Telegram
        stats = await self.stats(min_calls=1)
        outcomes = {m['method']: m for m in stats['methods']}
        self.assertEqual(outcomes['messages.SendMessage']['refused'], 1)
        self.assertEqual(outcomes['help.GetConfig']['errors'], 1)
        self.assertIn('TEST_ERROR', outcomes['help.GetConfig']['last_error'])

    async def test_candidates(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            for _ in range(5):
                await self.invoke('messages.getMessageReactionsList', {'peer': 77, 'id': 1, 'limit': 1})
            for n in range(2):
                await self.invoke('messages.toggleDialogPin', {'peer': 77, 'pinned': True}, True,
                                  f'cand-test-{n:04}', 'Анна')
        stats = await self.stats()
        self.assertEqual(stats['total_calls'], 7)
        self.assertEqual({c['method'] for c in stats['candidates']},
                         {'messages.GetMessageReactionsList', 'messages.ToggleDialogPin'})

    def test_json_conversion(self):
        self.assertEqual(raw.jsonable(b'ab'), {'bytes_b64': 'YWI='})
        self.assertEqual(raw.jsonable(b'x' * 5000), {'bytes_len': 5000, 'omitted': True})
        self.assertEqual(raw.jsonable(types.PeerUser(5)), {'_': 'PeerUser', 'user_id': 5})

    async def test_registered_in_server(self):
        listing = {tool.name for tool in await server.mcp.list_tools()}
        self.assertTrue({'invoke_mtproto', 'describe_mtproto', 'get_raw_usage_stats'} <= listing)
        with patch.object(server, 'CLIENT', type('C', (), {'is_connected': lambda s: True,
                                                          'get_me': lambda s: _me()})()):
            status = await server.get_status()
        self.assertEqual(status['tool_count'], len(listing))
        self.assertEqual(status['service_version'], '1.10.1')


async def _me():
    return types.User(id=1, first_name='Test')


if __name__ == '__main__':
    unittest.main()
