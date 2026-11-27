"""Rich messages (tables, headings, lists) via messages.sendMessage rich_message. Fake client, no network."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from telethon.tl import functions, types

import rich
import server

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)
USER = types.User(id=77, first_name='Анна', access_hash=5, username='anna_test')
ME = types.User(id=1, first_name='Owner', access_hash=1, is_self=True)


def cell(text, header=False):
    return types.PageTableCell(header=header or None, text=types.TextPlain(text))


STORED = types.RichMessage(blocks=[
    types.PageBlockHeading1(types.TextBold(types.TextPlain('Отчёт'))),
    types.PageBlockTable(title=types.TextEmpty(), rows=[
        types.PageTableRow([cell('Объект', True), cell('Сумма', True)]),
        types.PageTableRow([cell('Брестская'), cell('1 000')]),
    ]),
    types.PageBlockList([types.PageListItemText(types.TextConcat([types.TextPlain('а'), types.TextItalic(types.TextPlain('б'))]))]),
], photos=[], documents=[])


class Fake:
    def __init__(self, stored=STORED):
        self.requests, self.stored = [], stored
    def is_connected(self): return True
    async def get_me(self): return ME
    async def get_entity(self, ref):
        if ref in (77, '@anna_test'):
            return USER
        raise ValueError('not found')
    async def get_input_entity(self, ref):
        return types.InputPeerUser(77, 5)
    async def __call__(self, request):
        self.requests.append(request)
        return True
    def _get_response_message(self, request, result, entity):
        return NS(id=11, date=NOW)
    async def get_messages(self, entity, ids=None):
        return NS(id=ids, chat_id=77, rich_message=self.stored)
    async def send_message(self, *a, **k):
        raise AssertionError('rich mode must not use classic send_message')


class RichTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(server, 'PRIVATE', Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_modes_and_input(self):
        self.assertEqual(rich.rich_kind('rich_markdown'), 'markdown')
        self.assertEqual(rich.rich_kind('RICH_HTML'), 'html')
        self.assertIsNone(rich.rich_kind('markdown'))
        md = rich.input_rich('| a | b |\n|---|---|\n| 1 | 2 |', 'markdown', link_preview=False)
        self.assertIsInstance(md, types.InputRichMessageMarkdown)
        self.assertTrue(md.noautolink)
        self.assertIsInstance(rich.input_rich('<table></table>', 'html'), types.InputRichMessageHTML)
        with self.assertRaises(ValueError):
            rich.input_rich('  ', 'markdown')
        with self.assertRaises(ValueError):
            rich.input_rich('x' * (rich.MAX_RICH_SOURCE + 1), 'markdown')

    def test_rich_record_flattens_blocks(self):
        record = rich.rich_record(NS(rich_message=STORED))
        self.assertEqual([b['type'] for b in record['blocks']], ['Heading1', 'Table', 'List'])
        self.assertEqual(record['blocks'][0]['text'], 'Отчёт')
        self.assertEqual(record['blocks'][1]['rows'], [['Объект', 'Сумма'], ['Брестская', '1 000']])
        self.assertEqual(record['blocks'][2]['items'], ['аб'])
        self.assertIsNone(rich.rich_record(NS(rich_message=None)))

    async def test_send_rich_markdown_table(self):
        fake = Fake()
        source = '# Отчёт\n\n| Объект | Сумма |\n|---|---|\n| Брестская | 1 000 |'
        with patch.object(server, 'CLIENT', fake):
            result = await server.send_message(77, '@anna_test', source, 'rich-0001',
                                               parse_mode='rich_markdown')
        self.assertTrue(result['sent'])
        self.assertEqual(result['rich']['block_types'], ['Heading1', 'Table', 'List'])
        request = next(r for r in fake.requests if isinstance(r, functions.messages.SendMessageRequest))
        self.assertEqual(request.message, '')
        self.assertIsInstance(request.rich_message, types.InputRichMessageMarkdown)
        self.assertEqual(request.rich_message.markdown, source)
        self.assertTrue(request.no_webpage)

    async def test_rich_rejects_mentions_and_unconfirmed_storage(self):
        with patch.object(server, 'CLIENT', Fake()):
            with self.assertRaisesRegex(ValueError, 'mentions'):
                await server.send_message(77, '@anna_test', 'x', 'rich-0002', parse_mode='rich_html',
                                          mentions=[{'user_id': 77, 'expected_user': 'Анна', 'offset': 0, 'length': 1}])
        with patch.object(server, 'CLIENT', Fake(stored=None)):
            with self.assertRaisesRegex(RuntimeError, 'rich content'):
                await server.send_message(77, '@anna_test', '| a |\n|---|\n| 1 |', 'rich-0003',
                                          parse_mode='rich_markdown')

    def test_edit_message_rejects_rich(self):
        with self.assertRaisesRegex(ValueError, 'send_message only'):
            server.parse_mode_of('rich_markdown')


if __name__ == '__main__':
    unittest.main()
