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

    def test_blocks_to_markdown(self):
        md = rich.blocks_to_markdown([
            {'type': 'heading', 'text': 'Банки', 'level': 1},
            {'type': 'table', 'header': ['Банк', 'Лимит'], 'rows': [['Т|Банк', '15 000 ₽'], ['Сбер']],
             'align': ['left', 'right']},
            {'type': 'paragraph', 'text': 'Итог **жирно**'},
            {'type': 'ordered_list', 'items': ['a', 'b']}, {'type': 'divider'}])
        self.assertEqual(md, '# Банки\n\n| Банк | Лимит |\n|:---|---:|\n| Т\\|Банк | 15 000 ₽ |\n| Сбер |  |'
                             '\n\nИтог **жирно**\n\n1. a\n2. b\n\n---')
        for bad in ([], [{'type': 'video'}], [{'type': 'table', 'header': ['a'], 'rows': [['1', '2']]}],
                    [{'type': 'heading', 'text': 'x', 'level': 9}],
                    [{'type': 'table', 'header': ['a', 'b'], 'rows': [], 'align': ['up', 'left']}]):
            with self.assertRaises(ValueError):
                rich.blocks_to_markdown(bad)

    def test_markdown_rendering_of_read_blocks(self):
        record = rich.rich_record(NS(rich_message=STORED))
        self.assertEqual(record['markdown'], '# Отчёт\n\n| Объект | Сумма |\n|---|---|\n| Брестская | 1 000 |\n\n- аб')

    async def test_send_rich_message_tool(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            result = await server.send_rich_message(77, '@anna_test', 'rich-0004', [
                {'type': 'table', 'header': ['A', 'B'], 'rows': [['1', '2']]},
                {'type': 'paragraph', 'text': 'под таблицей'}])
        self.assertTrue(result['sent'])
        self.assertIn('| A | B |', result['markdown_source'])
        request = next(r for r in fake.requests if isinstance(r, functions.messages.SendMessageRequest))
        self.assertEqual(request.rich_message.markdown, result['markdown_source'])

    async def test_fallback_to_plain_only_when_asked(self):
        from telethon.errors import RPCError

        class Refusing(Fake):
            def __init__(self):
                super().__init__()
                self.plain = []
            async def __call__(self, request):
                if isinstance(request, functions.messages.SendMessageRequest):
                    raise RPCError(request, 'RICH_MESSAGE_FORBIDDEN', 400)
                return True
            async def send_message(self, entity, text, **kwargs):
                self.plain.append((text, kwargs))
                return NS(id=12, date=NOW)
        source = '| a | b |\n|---|---|\n| 1 | 2 |'
        fake = Refusing()
        with patch.object(server, 'CLIENT', fake):
            with self.assertRaisesRegex(ValueError, 'nothing was sent'):
                await server.send_message(77, '@anna_test', source, 'rich-0005', parse_mode='rich_markdown')
            result = await server.send_message(77, '@anna_test', source, 'rich-0006', parse_mode='rich_markdown',
                                               rich_fallback=True)
        self.assertEqual(result['rich_fallback']['sent_as'], 'plain')
        self.assertEqual(fake.plain, [(source, {'parse_mode': None, 'link_preview': False, 'reply_to': None})])

    async def test_partial_rich_is_completed_on_read(self):
        partial = types.RichMessage(blocks=[types.PageBlockParagraph(types.TextPlain('часть'))], photos=[], documents=[], part=True)

        class Reader(Fake):
            async def __call__(self, request):
                self.requests.append(request)
                return STORED
        fake, message = Reader(), NS(id=5, rich_message=partial)
        await rich.full_rich(fake, 77, message)
        self.assertIs(message.rich_message, STORED)
        self.assertIsInstance(fake.requests[0], functions.messages.GetRichMessageRequest)

    def test_parse_mode_of_points_to_rich_tools(self):
        with self.assertRaisesRegex(ValueError, 'edit_message only'):
            server.parse_mode_of('rich_markdown')

    async def test_edit_rich_in_place(self):
        class Editor(Fake):
            def __init__(self):
                super().__init__()
                self.current = NS(id=11, chat_id=77, out=True, action=None, media=None, message='',
                                  web_preview=None, rich_message=STORED)
            async def get_messages(self, entity, ids=None):
                return self.current
            async def __call__(self, request):
                self.requests.append(request)
                return True
        fake = Editor()
        current = rich.rich_record(fake.current)['markdown']
        blocks = [{'type': 'table', 'header': ['Объект', 'Сумма'], 'rows': [['Брестская', '2 000']]}]
        with patch.object(server, 'CLIENT', fake), patch.object(server, 'message_record', lambda m: {'id': m.id}):
            with self.assertRaisesRegex(ValueError, 'changed since'):
                await server.edit_rich_message(77, '@anna_test', 11, 'старый текст', blocks)
            result = await server.edit_rich_message(77, '@anna_test', 11, current, blocks)
            self.assertTrue(result['edited'])
            self.assertEqual(result['rich']['block_types'], ['Heading1', 'Table', 'List'])
            request = next(r for r in fake.requests if isinstance(r, functions.messages.EditMessageRequest))
            self.assertEqual(request.id, 11)
            self.assertEqual(request.message, '')
            self.assertEqual(request.rich_message.markdown, result['markdown_source'])
            self.assertTrue(request.no_webpage)
            again = await server.edit_rich_message(77, '@anna_test', 11, current, blocks)
            self.assertTrue(again['already_matches'])
            with self.assertRaisesRegex(ValueError, 'mentions'):
                await server.edit_message(77, '@anna_test', 11, 'x', current, parse_mode='rich_markdown',
                                          mentions=[{'user_id': 77, 'expected_user': 'Анна', 'offset': 0, 'length': 1}])
            fake.current = NS(id=11, chat_id=77, out=True, action=None, media=types.MessageMediaPhoto(),
                              message='подпись', web_preview=None, rich_message=None)
            with self.assertRaisesRegex(ValueError, 'attachments'):
                await server.edit_message(77, '@anna_test', 11, '| a |\n|---|\n| 1 |', 'подпись',
                                          parse_mode='rich_markdown')

    async def test_plain_message_becomes_rich(self):
        class Editor(Fake):
            def __init__(self):
                super().__init__()
                self.current = NS(id=12, chat_id=77, out=True, action=None, media=None, message='было',
                                  web_preview=None, rich_message=None)
            async def get_messages(self, entity, ids=None):
                return self.current
            async def __call__(self, request):
                self.requests.append(request)
                if isinstance(request, functions.messages.EditMessageRequest):
                    self.current = NS(**{**vars(self.current), 'message': '', 'rich_message': STORED})
                return True
        fake = Editor()
        with patch.object(server, 'CLIENT', fake), patch.object(server, 'message_record', lambda m: {'id': m.id}):
            result = await server.edit_message(77, '@anna_test', 12, '# Отчёт', 'было', parse_mode='rich_markdown')
        self.assertTrue(result['edited'])


if __name__ == '__main__':
    unittest.main()
