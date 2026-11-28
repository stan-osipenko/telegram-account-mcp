"""Gateway tools: reach tools added after a client cached its tool list. Fake client, no network."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telethon.tl import functions

import server
import tool_gateway
from test_rich import Fake


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(server, 'PRIVATE', Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    async def test_registered_with_stable_signatures(self):
        tools = {t.name: t for t in await server.mcp.list_tools()}
        for name in tool_gateway.GATEWAY_TOOLS:
            self.assertIn(name, tools)
        self.assertEqual(set(tools['call_tool'].inputSchema['properties']), {'name', 'arguments'})
        self.assertEqual(set(tools['call_read_tool'].inputSchema['properties']), {'name', 'arguments'})
        self.assertTrue(tools['call_read_tool'].annotations.readOnlyHint)
        self.assertFalse(tools['call_tool'].annotations.readOnlyHint)
        self.assertIn(server.SERVICE_VERSION, tools['list_tools_live'].description)

    async def test_live_catalog(self):
        listing = await server.list_tools_live()
        names = {t['name'] for t in listing['tools']}
        self.assertIn('send_rich_message', names)
        self.assertFalse(names & set(tool_gateway.GATEWAY_TOOLS))
        self.assertEqual(listing['tool_count'], len(names))
        rich = next(t for t in listing['tools'] if t['name'] == 'send_rich_message')
        self.assertFalse(rich['read_only'])
        self.assertIn('blocks', rich['parameters'])
        self.assertIn('fallback_plain?', rich['parameters'])
        one = await server.list_tools_live(name='describe_mtproto')
        self.assertTrue(one['tool']['read_only'])
        self.assertIn('query', one['tool']['input_schema']['properties'])
        filtered = await server.list_tools_live(query='mtproto')
        self.assertTrue(all('mtproto' in (t['name'] + t['summary']).lower() or t['parameters'] for t in filtered['tools']))
        with self.assertRaisesRegex(ValueError, 'Unknown tool'):
            await server.list_tools_live(name='no_such_tool')

    async def test_call_read_tool(self):
        result = await server.call_read_tool('describe_mtproto', {'query': 'messages.getRichMessage'})
        self.assertEqual(result['tool'], 'describe_mtproto')
        self.assertIn('getRichMessage', str(result['result']))
        with self.assertRaisesRegex(ValueError, 'changes data'):
            await server.call_read_tool('send_message', {})
        with self.assertRaisesRegex(ValueError, 'gateway tool'):
            await server.call_read_tool('call_tool', {'name': 'send_message'})

    async def test_call_tool_runs_target_with_its_checks(self):
        fake = Fake()
        with patch.object(server, 'CLIENT', fake):
            result = await server.call_tool('send_rich_message', {
                'chat_id': '@anna_test', 'expected_chat': '@anna_test', 'request_id': 'gateway-0001',
                'blocks': [{'type': 'table', 'header': ['A'], 'rows': [['1']]}]})
            self.assertTrue(result['result']['sent'])
            self.assertTrue(any(isinstance(r, functions.messages.SendMessageRequest) for r in fake.requests))
            with self.assertRaises(Exception):  # Validation of the target tool still applies.
                await server.call_tool('send_rich_message', {'chat_id': 77})
        with self.assertRaisesRegex(ValueError, 'gateway tool'):
            await server.call_tool('call_tool', {})


if __name__ == '__main__':
    unittest.main()
