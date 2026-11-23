"""Isolated forum-topic tests: fake Telegram client, temporary receipts, no network or sends."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from telethon import functions, types

import server
import topics

CHAT = -1000000000600
NOW = datetime.now(timezone.utc)


def header(msg_id, top=None, forum=True):
    return types.MessageReplyHeader(forum_topic=forum, reply_to_msg_id=msg_id, reply_to_top_id=top)


def msg(mid, reply_to=None, action=None, chat=None):
    return NS(id=mid, chat_id=CHAT, date=NOW, sender_id=1, out=False, message=f'm{mid}', entities=None,
              reply_to=reply_to, reply_to_msg_id=getattr(reply_to, 'reply_to_msg_id', None),
              action=action, chat=chat, edit_date=None, file=None, document=None, photo=None,
              web_preview=None, media=None)


class Fake:
    def __init__(self, forum=True):
        self.group = types.Channel(id=600, title='Forum', photo=types.ChatPhotoEmpty(), date=NOW,
                                   megagroup=True, forum=forum, access_hash=1)
        self.topics = {1: 'General', 50: 'Материалы', 70: 'Другое'}
        self.stored = {
            50: msg(50, action=types.MessageActionTopicCreate(title='Материалы', icon_color=0)),
            51: msg(51, header(50)), 52: msg(52, header(51, 50)),
            60: msg(60, chat=self.group), 71: msg(71, header(70))}
        self.sent, self.calls, self.place_in = [], [], None
    def is_connected(self): return True
    async def get_entity(self, ref):
        assert ref == CHAT
        return self.group
    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, functions.messages.GetForumTopicsByIDRequest):
            return NS(topics=[types.ForumTopic(id=t, date=NOW, peer=types.PeerChannel(600), title=self.topics[t],
                      icon_color=0, top_message=51 if t == 50 else t, read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                      unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
                      from_id=types.PeerUser(1), notify_settings=types.PeerNotifySettings())
                      for t in request.topics if t in self.topics])
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            listed = [(await self(functions.messages.GetForumTopicsByIDRequest(peer=None, topics=[t]))).topics[0]
                      for t in self.topics]
            last = types.Message(id=51, peer_id=types.PeerChannel(600), date=NOW, message='последнее',
                                 from_id=types.PeerUser(9))
            return NS(topics=listed, messages=[last], count=len(listed))
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            self.topics[90] = request.title
            service = types.MessageService(id=90, peer_id=types.PeerChannel(600), date=NOW,
                                           action=types.MessageActionTopicCreate(title=request.title, icon_color=0))
            return NS(updates=[types.UpdateNewChannelMessage(message=service, pts=1, pts_count=1)])
        raise AssertionError(f'Unexpected API call {request!r}')
    async def get_messages(self, entity, ids=None, limit=None, offset_id=0, reply_to=None, search=None):
        if ids is not None:
            return self.stored.get(ids)
        if reply_to is not None:
            self.calls.append(('replies', reply_to))
            return [m for m in sorted(self.stored.values(), key=lambda m: -m.id)
                    if topics.message_topic_id(m, forum=True) == reply_to][:limit]
        raise AssertionError('unexpected history call')
    async def iter_messages(self, entity, offset_id=0, limit=None):
        for m in sorted(self.stored.values(), key=lambda m: -m.id)[:limit]:
            yield m
    async def send_message(self, entity, text, reply_to=None, **kwargs):
        self.sent.append(reply_to)
        mid = 100 + len(self.sent)
        target = self.place_in if self.place_in is not None else None
        if target is None:
            target_header = None if reply_to is None else (
                header(reply_to) if reply_to in self.topics else header(reply_to, topics.message_topic_id(self.stored[reply_to], True)))
        else:
            target_header = None if target == 1 else header(target)
        self.stored[mid] = msg(mid, target_header)
        return NS(id=mid, date=NOW)


class TopicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, 'PRIVATE', Path(self.tmp.name))]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def test_topic_of_message(self):
        f = Fake()
        self.assertEqual(topics.message_topic_id(f.stored[50]), 50)       # topic root service message
        self.assertEqual(topics.message_topic_id(f.stored[51]), 50)       # plain post into topic
        self.assertEqual(topics.message_topic_id(f.stored[52]), 50)       # reply inside topic
        self.assertEqual(topics.message_topic_id(f.stored[60]), 1)        # forum chat, no header: General
        self.assertIsNone(topics.message_topic_id(msg(5)))                # not a forum
        self.assertIsNone(topics.message_topic_id(msg(6, header(5, forum=False))))  # ordinary reply
        self.assertEqual(server.message_record(f.stored[52])['topic_id'], 50)

    async def test_read_and_list_topics(self):
        f = Fake()
        with patch.object(server, 'CLIENT', f):
            page = await server.read_messages(CHAT, limit=10, topic_id=50)
            self.assertEqual([m['id'] for m in page['messages']], [52, 51, 50])
            self.assertIn(('replies', 50), f.calls)
            general = await server.read_messages(CHAT, limit=10, topic_id=1)
            self.assertEqual([m['id'] for m in general['messages']], [60])
            self.assertIn('scanned_for_general', general)
            listed = await server.list_topics(CHAT)
            names = {t['topic_id']: t for t in listed['topics']}
            self.assertEqual(names[50]['title'], 'Материалы')
            self.assertEqual(names[50]['last_message']['text'], 'последнее')
            self.assertTrue(names[1]['general'])
            with self.assertRaises(ValueError):
                await server.read_messages(CHAT, topic_id=999)
            with self.assertRaises(ValueError):
                await server.search_messages('x', topic_id=50)    # topic requires chat_id
        self.assertEqual(f.sent, [])

    async def test_send_into_topic_and_reply_inside_topic(self):
        f = Fake()
        with patch.object(server, 'CLIENT', f):
            r = await server.send_message(CHAT, 'Forum', 'в топик', 'topic-send-0001', topic_id=50)
            self.assertEqual(f.sent[-1], 50)
            self.assertEqual(r['topic'], {'topic_id': 50, 'title': 'Материалы', 'verified': True})
            again = await server.send_message(CHAT, 'Forum', 'в топик', 'topic-send-0001', topic_id=50)
            self.assertTrue(again['duplicate_prevented'])
            self.assertEqual(len(f.sent), 1)
            r = await server.send_message(CHAT, 'Forum', 'ответ', 'topic-reply-0001', topic_id=50,
                                          reply_to_message_id=51)
            self.assertEqual(f.sent[-1], 51)
            self.assertTrue(r['topic']['verified'])
            r = await server.send_message(CHAT, 'Forum', 'в General', 'topic-general-01', topic_id=1)
            self.assertIsNone(f.sent[-1])
            self.assertEqual(r['topic']['topic_id'], 1)

    async def test_rejections_happen_before_sending(self):
        f = Fake()
        with patch.object(server, 'CLIENT', f):
            with self.assertRaises(ValueError):   # reply target in another topic
                await server.send_message(CHAT, 'Forum', 'x', 'topic-wrong-0001', topic_id=50, reply_to_message_id=71)
            with self.assertRaises(ValueError):   # missing topic
                await server.send_message(CHAT, 'Forum', 'x', 'topic-wrong-0002', topic_id=999)
            with self.assertRaises(ValueError):   # wrong expected chat
                await server.send_message(CHAT, 'Wrong', 'x', 'topic-wrong-0003', topic_id=50)
        self.assertEqual(f.sent, [])
        plain = Fake(forum=False)
        with patch.object(server, 'CLIENT', plain):
            with self.assertRaises(ValueError):
                await server.send_message(CHAT, 'Forum', 'x', 'topic-wrong-0004', topic_id=50)
        self.assertEqual(plain.sent, [])

    async def test_unconfirmed_topic_leaves_receipt_uncertain(self):
        f = Fake()
        f.place_in = 1   # Telegram puts it into General instead of the topic
        with patch.object(server, 'CLIENT', f):
            with self.assertRaises(RuntimeError):
                await server.send_message(CHAT, 'Forum', 'x', 'topic-uncertain-1', topic_id=50)
            with self.assertRaises(ValueError) as error:
                await server.send_message(CHAT, 'Forum', 'x', 'topic-uncertain-1', topic_id=50)
            self.assertIn('uncertain', str(error.exception))
        self.assertEqual(len(f.sent), 1)

    async def test_create_topic_once(self):
        f = Fake()
        with patch.object(server, 'CLIENT', f):
            r = await server.create_topic(CHAT, 'Forum', 'Тест', 'topic-create-0001')
            self.assertEqual((r['topic_id'], r['title']), (90, 'Тест'))
            again = await server.create_topic(CHAT, 'Forum', 'Тест', 'topic-create-0001')
            self.assertTrue(again['duplicate_prevented'])
        self.assertEqual(f.calls.count('CreateForumTopicRequest'), 1)
        plain = Fake(forum=False)
        with patch.object(server, 'CLIENT', plain):
            with self.assertRaises(ValueError):
                await server.create_topic(CHAT, 'Forum', 'Тест', 'topic-create-0002')

    async def test_legacy_send_without_topic_is_unchanged(self):
        f = Fake(forum=False)
        with patch.object(server, 'CLIENT', f):
            r = await server.send_message(CHAT, 'Forum', 'plain', 'legacy-send-0001')
        self.assertEqual(f.sent, [None])
        self.assertNotIn('topic', r)
        self.assertEqual(f.calls, [])   # no topic API calls for ordinary sends


if __name__ == '__main__': unittest.main()
