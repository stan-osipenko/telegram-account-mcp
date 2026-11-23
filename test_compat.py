"""Compatibility features: chat references as strings, parse_mode, aliases. Fake client, no network."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from telethon import types

import server

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)
USER = types.User(id=77, first_name='Анна', access_hash=5, username='anna_test')
ME = types.User(id=1, first_name='Owner', access_hash=1, is_self=True)


class Fake:
    def __init__(self):
        self.sent = []
    def is_connected(self): return True
    async def get_me(self): return ME
    async def get_entity(self, ref):
        if ref in (77, '@anna_test'):
            return USER
        raise ValueError('not found')
    async def get_input_entity(self, ref):
        return types.InputPeerUser(77, 5)
    async def send_message(self, entity, text, **kwargs):
        self.sent.append((text, kwargs))
        return NS(id=10, date=NOW)
    async def __call__(self, request):
        return True


class CompatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'PRIVATE', Path(self.tmp.name))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_chat_references_are_resolved_to_numeric_ids(self):
        seen = []

        async def tool(chat_id: int, limit: int = 1) -> dict:
            seen.append(chat_id)
            return {}
        wrapped = server.accept_chat_refs(tool)
        with patch.object(server, 'CLIENT', Fake()):
            for value in (77, '77', '@anna_test', 't.me/anna_test', 'me', 0):
                await wrapped(value)
            with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                await wrapped('Анна')
        self.assertEqual(seen, [77, 77, 77, 77, 1, 0])

    async def test_tool_schemas_accept_strings_for_chat_ids(self):
        tools = {t.name: t for t in await server.mcp.list_tools()}
        for name, field in (('read_messages', 'chat_id'), ('send_message', 'chat_id'),
                            ('forward_messages', 'source_chat_id'), ('delete_messages', 'chat_id'),
                            ('list_topics', 'chat_id'), ('transcribe_voice', 'chat_id')):
            schema = tools[name].inputSchema['properties'][field]
            kinds = {s.get('type') for s in schema.get('anyOf', [schema])}
            self.assertTrue({'integer', 'string'} <= kinds, (name, schema))
        self.assertIn('parse_mode', tools['send_message'].inputSchema['properties'])
        self.assertIn('send_as', tools['send_file'].inputSchema['properties'])
        self.assertNotIn('request_id', tools['create_invite_link'].inputSchema.get('required', []))

    def test_parse_modes(self):
        self.assertIsNone(server.parse_mode_of('plain'))
        self.assertEqual(server.parse_mode_of('markdown'), 'md')
        self.assertEqual(server.parse_mode_of('HTML'), 'html')
        with self.assertRaises(ValueError):
            server.parse_mode_of('rtf')
        with self.assertRaisesRegex(ValueError, 'mentions'):
            server.parse_mode_of('markdown', mentions_used=True)
        self.assertEqual(server.plain_text_of('**жирный** текст', 'md'), 'жирный текст')
        self.assertEqual(server.plain_text_of('<b>a</b> b', 'html'), 'a b')

    async def test_send_message_with_markdown_and_string_chat(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            result = await server.send_message(77, '@anna_test', '**Привет**', 'compat-md-0001',
                                               parse_mode='markdown')
        self.assertTrue(result['sent'])
        self.assertEqual(fake.sent[0][1]['parse_mode'], 'md')
        self.assertFalse(fake.sent[0][1]['link_preview'])


if __name__ == '__main__':
    unittest.main()
