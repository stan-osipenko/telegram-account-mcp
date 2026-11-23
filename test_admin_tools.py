import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest
from unittest.mock import patch

from telethon import types, functions
import admin_tools as admin
import server


class Registry:
    def __init__(self):
        self.tools, self.annotations = {}, {}
    def tool(self, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = kwargs['annotations']
            return fn
        return register


class Fake:
    def __init__(self):
        self.channel = types.Channel(id=100, title='Test', photo=types.ChatPhotoEmpty(),
            date=datetime.now(timezone.utc), creator=True, broadcast=True, access_hash=1, left=False)
        self.user = types.User(id=123, first_name='Person', username='person')
        self.own = types.User(id=1, first_name='Owner')
        self.messages = {10: NS(id=10, chat_id=-1000000000100, out=True, action=None,
                               pinned=False, reactions=None)}
        self.calls, self.sends = [], []
        self.about = ''
        self.partial = False
        self.unread, self.count = True, 3
        self.preview = None
    def is_connected(self): return True
    async def get_entity(self, reference):
        if isinstance(reference, (types.Channel, types.User)): return reference
        if reference in (-1000000000100, '@testchannel'): return self.channel
        if reference in (123, '@person'): return self.user
        if reference == 1: return self.own
        raise ValueError('unknown fake entity')
    async def get_me(self): return self.own
    async def get_messages(self, entity, ids):
        return [self.messages.get(i) for i in ids] if isinstance(ids, list) else self.messages.get(ids)
    async def delete_messages(self, entity, ids, revoke):
        self.calls.append(('delete', ids))
        for i in ids:
            self.messages[i] = NS(id=i,chat_id=-1000000000100,out=True,action=object())
    async def send_message(self, entity, text, **kwargs):
        self.sends.append(kwargs)
        return NS(id=50, date=datetime.now(timezone.utc))
    async def pin_message(self, entity, mid, notify): self.messages[mid].pinned = True
    async def unpin_message(self, entity, mid): self.messages[mid].pinned = False
    async def get_input_entity(self, entity): return types.InputPeerChannel(entity.id,1)
    async def send_read_acknowledge(self, entity, **kwargs): self.count = 0
    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, functions.channels.CreateChannelRequest): return NS(chats=[self.channel])
        if isinstance(request, functions.channels.InviteToChannelRequest):
            return NS(missing_invitees=[NS(user_id=123)] if self.partial else [])
        if isinstance(request, functions.messages.ExportChatInviteRequest): return NS(link='https://t.me/+test-only-private')
        if isinstance(request, functions.channels.GetFullChannelRequest): return NS(full_chat=NS(about=self.about,participants_count=1))
        if isinstance(request, functions.messages.EditChatAboutRequest): self.about = request.about; return True
        if isinstance(request, functions.channels.EditTitleRequest): self.channel.title = request.title; return True
        if isinstance(request, functions.messages.CheckChatInviteRequest): return self.preview
        if isinstance(request, functions.messages.SendReactionRequest):
            self.messages[request.msg_id].reactions = NS(results=[NS(reaction=r,chosen_order=0) for r in request.reaction]); return True
        if isinstance(request, functions.messages.GetPeerDialogsRequest):
            return NS(dialogs=[NS(unread_mark=self.unread,unread_count=self.count,read_inbox_max_id=10)])
        if isinstance(request, functions.messages.MarkDialogUnreadRequest): self.unread=bool(request.unread); return True
        if isinstance(request, functions.channels.JoinChannelRequest): self.channel.left=False; return NS(chats=[self.channel])
        raise AssertionError(type(request).__name__)


class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.private = Path(self.directory.name)
        self.fake = Fake()
        self.patches = [patch.object(server,'PRIVATE',self.private),patch.object(server,'CLIENT',self.fake)]
        for p in self.patches: p.start()
        self.registry = Registry()
        admin.register_admin_tools(self.registry,self.private,server.telegram,server.checked_entity,
            server.mutate_once,server.READ,server.WRITE,server.chat_record,server.message_record,server.dialog_state)
        self.tools=self.registry.tools
    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.directory.cleanup()

    async def test_bare_word_phone_and_malformed_link_rejected_without_any_api(self):
        for value in ['Katerina','Катерина','+79991234567','t.me/person/123','https://evil.test/person','@bad name','0']:
            with self.assertRaises(ValueError): await server.get_chat(value)
        self.assertEqual(self.fake.calls,[])
        self.assertEqual(admin.strict_reference('https://t.me/person'),'@person')
        self.assertEqual(admin.strict_reference('-1000000000100'),-1000000000100)

    async def test_creation_retry_does_not_create_or_reserve_quota_twice(self):
        fn=self.tools['create_chat']
        first=await fn('channel','Test','admin-create-test')
        second=await fn('channel','Test','admin-create-test')
        self.assertEqual(first['chat'],second['chat'])
        self.assertTrue(second['duplicate_prevented'])
        self.assertEqual(self.fake.calls.count('CreateChannelRequest'),1)
        for _ in range(4): admin.reserve_quota(self.private,'create_chat')
        with self.assertRaises(ValueError): admin.reserve_quota(self.private,'create_chat')

    async def test_deletion_rejects_cross_chat_ids_before_delete_and_protects_others(self):
        fn=self.tools['delete_messages']
        self.fake.messages[10].chat_id=999
        with self.assertRaises(ValueError): await fn(-1000000000100,'Test',[10],'admin-delete-cross')
        self.fake.messages[10].chat_id=-1000000000100
        self.fake.messages[10].out=False
        with self.assertRaises(ValueError): await fn(-1000000000100,'Test',[10],'admin-delete-foreign')
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='delete' for x in self.fake.calls))

    async def test_history_clear_tombstone_counts_as_deleted_and_retry_is_safe(self):
        fn=self.tools['delete_messages']
        result=await fn(-1000000000100,'Test',[10],'admin-delete-own')
        self.assertTrue(result['verified'])
        again=await fn(-1000000000100,'Test',[10],'admin-delete-own')
        self.assertTrue(again['duplicate_prevented'])
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)

    async def test_channel_self_only_deletion_is_rejected_instead_of_revoking_for_everyone(self):
        with self.assertRaises(ValueError):
            await self.tools['delete_messages'](-1000000000100,'Test',[10],'admin-delete-local',for_everyone=False)
        self.assertFalse(self.fake.calls)

    async def test_invites_report_privacy_failures_and_reject_ambiguous_users(self):
        fn=self.tools['add_chat_members']
        with self.assertRaises(ValueError): await fn(-1000000000100,'Test',['Person'],'admin-invite-bare')
        with self.assertRaises(ValueError): await fn(123,'Person',['@person'],'admin-invite-private')
        self.fake.partial=True
        result=await fn(-1000000000100,'Test',['@person'],'admin-invite-valid')
        self.assertEqual(result['missing_invitees'],[123])
        self.assertEqual(result['added_user_ids'],[])

    async def test_remove_owner_and_wrong_person_rejected_without_kick(self):
        fn=self.tools['remove_chat_member']
        with self.assertRaises(ValueError): await fn(-1000000000100,'Test','1','Owner','admin-remove-self')
        with self.assertRaises(ValueError): await fn(-1000000000100,'Test','123','Wrong','admin-remove-wrong')

    async def test_invite_link_retry_returns_same_link_without_second_export(self):
        fn=self.tools['create_invite_link']
        await fn(-1000000000100,'Test','admin-link-test')
        repeat=await fn(-1000000000100,'Test','admin-link-test')
        self.assertTrue(repeat['duplicate_prevented'])
        self.assertEqual(self.fake.calls.count('ExportChatInviteRequest'),1)

    async def test_invite_already_member_still_checks_expected_name(self):
        self.fake.preview=types.ChatInviteAlready(self.fake.channel)
        fn=self.tools['join_chat']
        with self.assertRaises(ValueError): await fn('https://t.me/+abcdefgh','Wrong','admin-join-wrong')
        result=await fn('https://t.me/+abcdefgh','Test','admin-join-test')
        self.assertTrue(result['already_member'])

    async def test_pin_reaction_and_chat_description_are_verified(self):
        pin=self.tools['pin_message']
        self.assertTrue((await pin(-1000000000100,'Test',10))['pinned'])
        self.assertFalse((await pin(-1000000000100,'Test',10,unpin=True))['pinned'])
        react=self.tools['set_reaction']
        self.assertEqual((await react(-1000000000100,'Test',10,'👍'))['reaction'],'👍')
        self.assertIsNone((await react(-1000000000100,'Test',10,None))['reaction'])
        result=await self.tools['edit_chat_info'](-1000000000100,'Test',title='New',about='Description')
        self.assertTrue(result['verified'])

    async def test_explicit_mark_read_clears_count_and_manual_reminder(self):
        result=await self.tools['mark_read'](-1000000000100,'Test')
        self.assertTrue(result['read_receipts_sent'])
        self.assertEqual(result['unread_message_count'],0)
        self.assertFalse(result['unread_mark'])

    async def test_schedule_timezone_bounds_and_retry_after_date_has_passed(self):
        now=datetime.now(timezone.utc)
        for value in [(now-timedelta(days=1)).isoformat(),(now+timedelta(days=366)).isoformat(),'2027-01-01T10:00:00']:
            with self.assertRaises(ValueError): admin.parse_schedule(value,now=now)
        future=(now+timedelta(minutes=5)).isoformat()
        result=await server.send_message(-1000000000100,'Test','scheduled','admin-schedule-test',schedule_at=future)
        self.assertTrue(result['scheduled'])
        with patch('server.parse_schedule',side_effect=ValueError('date now past')):
            repeat=await server.send_message(-1000000000100,'Test','scheduled','admin-schedule-test',schedule_at=future)
        self.assertTrue(repeat['duplicate_prevented'])
        self.assertEqual(len(self.fake.sends),1)

    async def test_destructive_tools_are_annotated_and_previous_tools_still_present(self):
        self.assertTrue(self.registry.annotations['delete_messages'].destructiveHint)
        self.assertTrue(self.registry.annotations['remove_chat_member'].destructiveHint)
        self.assertTrue(self.registry.annotations['search_people'].readOnlyHint)
        self.assertEqual(len(self.tools),12)


if __name__=='__main__': unittest.main()
