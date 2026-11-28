"""Feature watch: schema parsing, diffs, report and the read-only tools. No network."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import feature_watch as fw
import server

SCHEMA_V1 = """
boolTrue#997275b5 = Bool;
inputPeerSelf#7da07ec9 = InputPeer;
---functions---
messages.sendMessage#fe05dc9a flags:# peer:InputPeer message:string = Updates;
// LAYER 233
"""
SCHEMA_V2 = """
boolTrue#997275b5 = Bool;
inputPeerSelf#7da07ec9 = InputPeer;
---functions---
messages.sendMessage#fe05dc9a flags:# peer:InputPeer message:string effect:flags.18?long = Updates;
messages.sendHologram#12345678 peer:InputPeer depth:int = Updates;
// LAYER 234
"""
BLOG = '<a href="/blog/holograms-and-more">x</a><a href="/blog/old-post">y</a><a href="/blog/old-post">y</a>'


def fake_fetch(schema, blog=BLOG, latest='1.46.0'):
    def fetch(url, timeout=30):
        if url == fw.SCHEMA_URL:
            return schema
        if url == fw.PYPI_URL:
            return json.dumps({'info': {'version': latest}})
        return blog
    return fetch


class FeatureWatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_schema(self):
        parsed = fw.parse_schema(SCHEMA_V2)
        self.assertEqual(parsed['layer'], 234)
        self.assertEqual(parsed['items']['messages.sendHologram']['kind'], 'method')
        self.assertEqual(parsed['items']['messages.sendHologram']['params'], ['peer', 'depth'])
        self.assertEqual(parsed['items']['boolTrue']['kind'], 'type')
        self.assertEqual(fw.blog_posts(BLOG), ['holograms-and-more', 'old-post'])

    def test_first_run_is_a_baseline_then_diffs(self):
        with patch.object(fw, 'fetch', fake_fetch(SCHEMA_V1, blog='<a href="/blog/old-post">')):
            first = fw.check(self.private)
        self.assertFalse(first['new_since_last_check']['added'])
        self.assertEqual(first['telegram_layer'], 233)
        with patch.object(fw, 'fetch', fake_fetch(SCHEMA_V2)):
            second = fw.check(self.private, first)
        self.assertTrue(second['news'])
        self.assertEqual(len(second['new_since_last_check']['added']), 1)
        self.assertIn('messages.sendHologram', second['new_since_last_check']['added'][0])
        self.assertIn('effect', second['new_since_last_check']['changed'][0])
        self.assertEqual(second['blog']['new'], [fw.BLOG_URL + '/holograms-and-more'])
        self.assertIn('messages.sendHologram', second['missing_in_telethon']['names'])
        text = fw.summary(second)
        self.assertIn('слой 234', text)
        self.assertIn('holograms-and-more', text)
        with patch.object(fw, 'fetch', fake_fetch(SCHEMA_V2)):
            third = fw.check(self.private, second)
        self.assertFalse(third['news'])
        self.assertEqual(len(third['history']), 1)

    def test_upgrade_noticed_once(self):
        with patch.object(fw, 'fetch', fake_fetch(SCHEMA_V1, latest='9.0.0')):
            first = fw.check(self.private, {'schema_items': fw.parse_schema(SCHEMA_V1)['items'],
                                            'blog': {'posts': fw.blog_posts(BLOG)}})
            second = fw.check(self.private, first)
        self.assertTrue(first['telethon']['upgrade_available'])
        self.assertTrue(first['news'])
        self.assertFalse(second['news'])

    async def test_tools(self):
        with patch.object(server, 'PRIVATE', self.private):
            tool = fw.register_feature_watch_tool(server.mcp.__class__('t'), self.private, server.READ)
            self.assertFalse((await tool())['available'])
            with patch.object(fw, 'fetch', fake_fetch(SCHEMA_V2)):
                report = fw.check(self.private)
            (self.private / fw.REPORT).write_text(json.dumps(report))
            served = await tool()
            self.assertTrue(served['available'])
            self.assertNotIn('schema_items', served)
            self.assertNotIn('names', served['missing_in_telethon'])
            self.assertIn('names', (await tool(include_missing=True))['missing_in_telethon'])
        guide = await server.get_messaging_guide()
        self.assertIn('Feature log', guide['guide'])
        names = {t.name for t in await server.mcp.list_tools()}
        self.assertTrue({'get_telegram_updates', 'get_messaging_guide'} <= names)


if __name__ == '__main__':
    unittest.main()
