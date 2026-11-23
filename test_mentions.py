"""Isolated tests: no credentials, network, real chats or Telegram sends."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from telethon import errors, functions, types
from telethon.extensions import BinaryReader

import admin_tools
import mentions as mt
import server


TEXT = '😀 Привет, Анна!'
MENTION = {'user_id': 77, 'expected_user': 'Анна', 'offset': 11, 'length': 4}
CHAT = -1000000000500


def message(text=TEXT, specs=None):
    return NS(id=20, chat_id=CHAT, out=True, action=None, message=text,
              date=datetime.now(timezone.utc), sender_id=1, reply_to_msg_id=None,
              edit_date=None, file=None, document=None, photo=None, web_preview=None, media=None,
              entities=[types.MessageEntityMentionName(x['offset'], x['length'], x['user_id']) for x in (specs or [])])


class Fake:
    def __init__(self):
        self.group = types.Channel(id=500, title='Isolated', photo=types.ChatPhotoEmpty(),
                                   date=datetime.now(timezone.utc), megagroup=True, access_hash=17)
        self.user = types.User(id=77, first_name='Анна', username=None, phone=None, access_hash=123456789)
        self.sent = self.edits = self.membership_checks = 0
        self.last_kwargs = None
        self.current = message('old')
        self.reject_member = self.reject_cache = self.drop_entities = self.cross_chat = False
    def is_connected(self): return True
    async def get_entity(self, ref):
        if ref == CHAT: return self.group
        if isinstance(ref, types.InputPeerUser) and ref.user_id == 77: return self.user
        raise ValueError('unavailable')
    async def get_input_entity(self, ref):
        assert isinstance(ref, types.PeerUser), 'must not look up a phone or username'
        if self.reject_cache: raise ValueError('not cached')
        assert ref.user_id == 77
        return types.InputPeerUser(77, self.user.access_hash)
    async def get_messages(self, entity, ids): return self.current
    async def send_message(self, entity, text, **kwargs):
        self.sent += 1
        self.last_kwargs = kwargs
        specs = [{'offset': e.offset, 'length': e.length, 'user_id': e.user_id.user_id}
                 for e in kwargs.get('formatting_entities', [])]
        self.current = message(text, [] if self.drop_entities else specs)
        if self.cross_chat: self.current.chat_id = 999
        return self.current
    async def edit_message(self, entity, mid, text, **kwargs):
        self.edits += 1
        self.last_kwargs = kwargs
        specs = [{'offset': e.offset, 'length': e.length, 'user_id': e.user_id.user_id}
                 for e in kwargs.get('formatting_entities', [])]
        old = self.current
        self.current = message(text, [] if self.drop_entities else specs)
        self.current.media, self.current.file, self.current.document = old.media, old.file, old.document
    async def get_participants(self, entity, limit, search):
        if self.reject_member: raise errors.ChatAdminRequiredError(request=None)
        assert limit == 5 and search == 'Ан'
        return [self.user]
    async def __call__(self, req):
        if isinstance(req, functions.channels.GetParticipantRequest):
            self.membership_checks += 1
            if self.reject_member: raise errors.ChatAdminRequiredError(request=None)
            return NS(participant=types.ChannelParticipant(user_id=77, date=datetime.now(timezone.utc)))
        if isinstance(req, functions.messages.GetScheduledMessagesRequest): return NS(messages=[self.current])
        if isinstance(req, functions.messages.GetFullChatRequest):
            if self.reject_member: return NS(full_chat=NS(participants=types.ChatParticipantsForbidden(chat_id=500)))
            return NS(full_chat=NS(participants=NS(participants=[NS(user_id=77)])))
        raise AssertionError(type(req).__name__)


class SpanTests(unittest.TestCase):
    def test_utf16_cyrillic_emoji_and_tl_round_trip(self):
        specs = mt.normalize_mentions(TEXT, [MENTION])
        self.assertEqual(mt.utf16_length('👩‍💻'), 5)
        self.assertEqual(mt.boundaries(TEXT)[11], 10)
        entity = types.InputMessageEntityMentionName(11, 4, types.InputUser(77, 123456789))
        with BinaryReader(bytes(entity)) as reader:
            restored = reader.tgread_object()
        self.assertIsInstance(restored, types.InputMessageEntityMentionName)
        self.assertEqual(restored.user_id.user_id, 77)
        self.assertEqual(mt.mention_signature(specs), [(11, 4, 77)])

    def test_surrogate_split_out_of_range_overlap_and_bad_shapes_rejected(self):
        for changes in [{'offset': 1}, {'length': 999}, {'offset': -1}, {'length': 0},
                        {'user_id': True}, {'offset': '11'}, {'phone': '+1555'}]:
            with self.subTest(changes=changes), self.assertRaises((ValueError, ValidationError)):
                mt.normalize_mentions(TEXT, [{**MENTION, **changes}])
        with self.assertRaises(ValueError): mt.normalize_mentions(TEXT, [MENTION, MENTION])
        with self.assertRaises(ValueError): mt.normalize_mentions('x\ud800', [])
        with self.assertRaises(ValueError): mt.normalize_mentions(TEXT, [MENTION] * 101)

    def test_preserve_remaps_prefix_emoji_and_refuses_to_guess_changed_names(self):
        old = message(TEXT, [MENTION])
        new = '🚀 ' + TEXT + ' Спасибо.'
        remapped = mt.preserve_mentions(TEXT, new, old)
        self.assertEqual(remapped, [{'offset': 14, 'length': 4, 'user_id': 77}])
        for new in [TEXT.replace('Анна', 'Иван'), TEXT.replace('Анна', 'Аннабель'), 'Текст без имени', TEXT + ' Анна']:
            with self.subTest(text=new), self.assertRaises(ValueError): mt.preserve_mentions(TEXT, new, old)

    def test_readback_exposes_numeric_mention_without_access_hash(self):
        record = server.message_record(message(TEXT, [MENTION]))
        self.assertEqual(record['entities'], [{'type': 'MessageEntityMentionName', 'offset': 11, 'length': 4, 'user_id': 77}])
        self.assertNotIn('access_hash', str(record))


class MentionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fake = Fake()
        # Isolated lock/rate state also keeps old global locks out of new unittest loops.
        @asynccontextmanager
        async def isolated_telegram(): yield self.fake
        self.patches = [patch.object(server, 'PRIVATE', Path(self.directory.name)),
                        patch.object(server, 'telegram', isolated_telegram)]
        for p in self.patches: p.start()
    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.directory.cleanup()

    async def test_hidden_username_phone_resolves_to_input_user_and_checks_member(self):
        entities = await mt.input_mentions(self.fake, self.fake.group, [MENTION], verify_membership=True)
        self.assertIsInstance(entities[0], types.InputMessageEntityMentionName)
        self.assertIsInstance(entities[0].user_id, types.InputUser)
        self.assertEqual(entities[0].user_id.user_id, 77)
        self.assertEqual(self.fake.membership_checks, 1)

    async def test_permission_denied_and_unknown_cache_never_fall_back_to_names_or_phones(self):
        self.fake.reject_member = True
        with self.assertRaisesRegex(ValueError, 'ChatAdminRequiredError'):
            await mt.input_mentions(self.fake, self.fake.group, [MENTION], verify_membership=True)
        self.fake.reject_member = False; self.fake.reject_cache = True
        with self.assertRaisesRegex(ValueError, 'fetch accessible group members'):
            await mt.input_mentions(self.fake, self.fake.group, [MENTION], verify_membership=True)
        self.assertEqual(self.fake.sent, 0)

    async def test_wrong_expected_user_or_private_target_rejected(self):
        with self.assertRaisesRegex(ValueError, 'expected_user'):
            await mt.input_mentions(self.fake, self.fake.group, [{**MENTION, 'expected_user': 'Другой'}], verify_membership=True)
        with self.assertRaisesRegex(ValueError, 'require a group'):
            await mt.input_mentions(self.fake, self.fake.user, [MENTION], verify_membership=True)

    async def test_basic_group_verifies_visible_member_list_and_refuses_forbidden(self):
        basic = types.Chat(id=500, title='Basic', photo=types.ChatPhotoEmpty(), participants_count=1,
                           date=datetime.now(timezone.utc), version=1)
        self.assertEqual(len(await mt.input_mentions(self.fake, basic, [MENTION], verify_membership=True)), 1)
        self.fake.reject_member = True
        with self.assertRaisesRegex(ValueError, 'does not expose'):
            await mt.input_mentions(self.fake, basic, [MENTION], verify_membership=True)

    async def test_send_verifies_entities_and_retry_is_deduplicated(self):
        first = await server.send_message(CHAT, 'Isolated', TEXT, 'mention-send-test', mentions=[MENTION])
        again = await server.send_message(CHAT, 'Isolated', TEXT, 'mention-send-test', mentions=[MENTION])
        self.assertTrue(first['mentions_verified'] and again['duplicate_prevented'])
        self.assertEqual(first['entities'][0]['user_id'], 77)
        self.assertEqual(self.fake.sent, 1)
        self.assertIsNone(self.fake.last_kwargs['parse_mode'])
        with self.assertRaisesRegex(ValueError, 'different message'):
            await server.send_message(CHAT, 'Isolated', TEXT, 'mention-send-test', mentions=[])
        self.assertEqual(self.fake.sent, 1)

    async def test_plain_send_receipt_remains_compatible_with_omitted_or_empty_mentions(self):
        await server.send_message(CHAT, 'Isolated', 'plain', 'plain-send-test')
        result = await server.send_message(CHAT, 'Isolated', 'plain', 'plain-send-test', mentions=[])
        self.assertTrue(result['duplicate_prevented'])
        self.assertNotIn('formatting_entities', self.fake.last_kwargs)
        self.assertEqual(self.fake.sent, 1)

    async def test_lost_entity_marks_outcome_uncertain_and_does_not_resend(self):
        self.fake.drop_entities = True
        with self.assertRaises(RuntimeError):
            await server.send_message(CHAT, 'Isolated', TEXT, 'uncertain-mention', mentions=[MENTION])
        with self.assertRaisesRegex(ValueError, 'uncertain'):
            await server.send_message(CHAT, 'Isolated', TEXT, 'uncertain-mention', mentions=[MENTION])
        self.assertEqual(self.fake.sent, 1)

    async def test_send_readback_from_wrong_chat_is_not_accepted(self):
        self.fake.cross_chat = True
        with self.assertRaisesRegex(ValueError, 'another chat'):
            await server.send_message(CHAT, 'Isolated', TEXT, 'cross-chat-mention', mentions=[MENTION])

    async def test_scheduled_mentions_are_read_from_scheduled_history(self):
        when = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        result = await server.send_message(CHAT, 'Isolated', TEXT, 'scheduled-mention', schedule_at=when, mentions=[MENTION])
        self.assertTrue(result['scheduled'] and result['mentions_verified'])

    async def test_default_edit_preserves_mentions_remaps_offsets_and_retry_is_noop(self):
        self.fake.current = message(TEXT, [MENTION])
        new = '🚀 ' + TEXT
        result = await server.edit_message(CHAT, 'Isolated', 20, new, TEXT)
        again = await server.edit_message(CHAT, 'Isolated', 20, new, TEXT)
        self.assertTrue(result['mentions_verified'] and again['already_matches'])
        self.assertEqual(result['message']['entities'][0]['offset'], 14)
        self.assertEqual(self.fake.edits, 1)
        self.assertEqual(self.fake.membership_checks, 0)  # Already present own-message mention.

    async def test_explicit_same_text_entity_edit_and_removal_are_not_skipped(self):
        self.fake.current = message(TEXT)
        result = await server.edit_message(CHAT, 'Isolated', 20, TEXT, TEXT, mentions=[MENTION])
        self.assertTrue(result['edited'])
        removed = await server.edit_message(CHAT, 'Isolated', 20, TEXT, TEXT, mentions=[])
        self.assertEqual(removed['message']['entities'], [])
        self.assertEqual(self.fake.edits, 2)

    async def test_changed_label_requires_explicit_replacement_and_stale_text_rejected(self):
        self.fake.current = message(TEXT, [MENTION])
        with self.assertRaisesRegex(ValueError, 'explicitly'):
            await server.edit_message(CHAT, 'Isolated', 20, 'Иван!', TEXT)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            await server.edit_message(CHAT, 'Isolated', 20, '🚀 ' + TEXT, 'stale')
        self.assertEqual(self.fake.edits, 0)

    async def test_caption_edit_preserves_attachment_and_detects_stripped_mentions(self):
        self.fake.current = message(TEXT, [MENTION])
        self.fake.current.media = types.MessageMediaDocument(document=types.DocumentEmpty(id=9))
        self.fake.current.document = NS(id=9)
        self.fake.current.file = NS(name='test.txt', size=1, mime_type='text/plain')
        result = await server.edit_message(CHAT, 'Isolated', 20, '🚀 ' + TEXT, TEXT)
        self.assertTrue(result['attachment_unchanged'])
        self.fake.drop_entities = True
        with self.assertRaises(RuntimeError):
            await server.edit_message(CHAT, 'Isolated', 20, '🚀 🚀 ' + TEXT, '🚀 ' + TEXT)

    async def test_uncertain_default_edit_cannot_be_misreported_as_success_on_retry(self):
        self.fake.current = message(TEXT, [MENTION])
        self.fake.drop_entities = True
        new = '🚀 ' + TEXT
        with self.assertRaises(RuntimeError):
            await server.edit_message(CHAT, 'Isolated', 20, new, TEXT)
        with self.assertRaisesRegex(RuntimeError, 'verification is uncertain'):
            await server.edit_message(CHAT, 'Isolated', 20, new, TEXT)
        self.assertEqual(self.fake.edits, 1)

    async def test_confirmed_edit_retry_detects_changed_entities_even_with_identical_text(self):
        self.fake.current = message(TEXT, [MENTION])
        new = '🚀 ' + TEXT
        await server.edit_message(CHAT, 'Isolated', 20, new, TEXT)
        self.fake.current.entities = []  # Simulate an external client removing the entity.
        with self.assertRaisesRegex(RuntimeError, 'verification is uncertain'):
            await server.edit_message(CHAT, 'Isolated', 20, new, TEXT)
        self.assertEqual(self.fake.edits, 1)

    async def test_member_search_exposes_hidden_user_id_and_permission_errors(self):
        class Registry:
            def __init__(self): self.tools = {}
            def tool(self, **kwargs):
                def register(fn): self.tools[fn.__name__] = fn; return fn
                return register
        registry = Registry()
        admin_tools.register_admin_tools(registry, server.PRIVATE, server.telegram, server.checked_entity,
                                        server.mutate_once, server.READ, server.WRITE, server.chat_record,
                                        server.message_record, server.dialog_state)
        read = registry.tools['get_chat_members']
        result = await read(CHAT, limit=5, query='Ан')
        member = result['members'][0]
        self.assertEqual(member['user_id'], 77)
        self.assertIsNone(member['username'])
        self.assertTrue(member['can_mention'])
        self.assertNotIn('phone', member)
        self.assertNotIn('access_hash', str(result))
        self.fake.reject_member = True
        with self.assertRaisesRegex(ValueError, 'administrator rights'):
            await read(CHAT, limit=5, query='Ан')

    async def test_mcp_typed_schema_and_call_accept_structured_mentions(self):
        listing = {tool.name: tool for tool in await server.mcp.list_tools()}
        for name in ['send_message', 'edit_message']:
            schema = listing[name].inputSchema
            self.assertIn('mentions', schema['properties'])
            self.assertNotIn('mentions', schema.get('required', []))
            self.assertEqual(schema['$defs']['MentionSpec']['properties']['offset']['type'], 'integer')
        await server.mcp.call_tool('send_message', {'chat_id': CHAT, 'expected_chat': 'Isolated',
            'text': TEXT, 'request_id': 'mcp-schema-mention', 'mentions': [MENTION]})
        self.assertEqual(self.fake.sent, 1)
        self.assertEqual(mt.mention_records(self.fake.current)[0]['user_id'], 77)


if __name__ == '__main__': unittest.main()
