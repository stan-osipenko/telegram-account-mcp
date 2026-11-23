"""Bounded Telegram administration; all actions share the existing session/lock/receipts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import re
import sqlite3
import time

from mcp.types import ToolAnnotations
from telethon import errors, functions, types, utils

LIMITS = {'create_chat': (5, 86400), 'add_chat_members': (30, 86400),
          'join_chat': (10, 86400), 'remove_chat_member': (30, 86400),
          'delete_messages': (300, 3600)}
INVITE = re.compile(r'^(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)([A-Za-z0-9_-]{8,128})/?$')
USERNAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,31}')


def strict_reference(value):
    """Never allow Telethon to interpret a display name/bare word/phone as a username."""
    if isinstance(value, int) and not isinstance(value, bool) and value:
        return value
    if not isinstance(value, str):
        raise ValueError('Use a numeric Telegram ID, exact @username or t.me/username')
    text = value.strip()
    if re.fullmatch(r'-?[1-9][0-9]{0,19}', text):
        return int(text)
    match = re.fullmatch(r'(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?', text)
    username = match.group(1) if match else text[1:] if text.startswith('@') else None
    if username and USERNAME.fullmatch(username):
        return '@' + username
    raise ValueError('Ambiguous recipient rejected: bare words/display names/phone numbers are not addresses; '
                     'use list_chats/search_people, then a verified numeric ID or exact @username')


def parse_schedule(value, now=None):
    try:
        when = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('schedule_at must be ISO 8601 with a timezone offset') from None
    now = now or datetime.now(timezone.utc)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError('schedule_at must include a timezone offset')
    if not now + timedelta(seconds=30) <= when <= now + timedelta(days=365):
        raise ValueError('schedule_at must be between 30 seconds and 365 days in the future')
    return when


def check_expected(record, expected):
    names = [record['name'].casefold()]
    if record.get('username'):
        names.append('@' + record['username'].casefold())
    if not expected or expected.casefold() not in names or record.get('deleted'):
        raise ValueError('Identity does not match expected_chat/expected_user; resolve the exact target first')


def require_message_chat(message, chat_id):
    # In private chats Telegram IDs are account-wide: GetMessages can return another chat's message.
    if message and message.chat_id != chat_id:
        raise ValueError('Message belongs to another chat; no action performed')


async def full_info(client, entity):
    if isinstance(entity, types.Channel):
        info = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
        return {'about': info.about, 'participants_count': getattr(info, 'participants_count', None),
                'admins_count': getattr(info, 'admins_count', None),
                'linked_chat_id': getattr(info, 'linked_chat_id', None),
                'is_creator': bool(entity.creator), 'admin_rights': bool(entity.creator or entity.admin_rights),
                'public': bool(entity.username)}
    if isinstance(entity, types.Chat):
        info = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
        participants = getattr(info.participants, 'participants', None)
        return {'about': info.about, 'participants_count': len(participants) if participants is not None else entity.participants_count,
                'is_creator': bool(entity.creator), 'admin_rights': bool(entity.creator or entity.admin_rights)}
    if isinstance(entity, types.User):
        info = (await client(functions.users.GetFullUserRequest(entity))).full_user
        return {'about': info.about, 'is_contact': bool(entity.contact),
                'mutual_contact': bool(entity.mutual_contact), 'common_chats_count': info.common_chats_count}
    raise ValueError('Unsupported chat type')


def reserve_quota(private, operation, units=1):
    """Count durable attempted operations, including uncertain outcomes, before the API call."""
    maximum, window = LIMITS[operation]
    with sqlite3.connect(private / 'sent-receipts.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS admin_quota (operation TEXT, at REAL, units INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        used = db.execute('SELECT COALESCE(SUM(units),0) FROM admin_quota WHERE operation=? AND at>?',
                          (operation, time.time() - window)).fetchone()[0]
        if used + units > maximum:
            raise ValueError(f'Account safety limit: {operation} allows {maximum} items per {window//3600}h; '
                             'ask the owner before changing it, do not bypass')
        db.execute('INSERT INTO admin_quota VALUES (?,?,?)', (operation, time.time(), units))
        db.execute('DELETE FROM admin_quota WHERE at<?', (time.time()-7*86400,))


def register_admin_tools(mcp, private, telegram, checked_entity, mutate_once, read, write,
                         chat_record, message_record, dialog_state):
    destructive = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    def group(entity):
        if not isinstance(entity, (types.Channel, types.Chat)):
            raise ValueError('This operation requires a group or channel, not a private chat')

    async def users_resolved(client, references):
        if len(references) > 10:
            raise ValueError('At most 10 users per call')
        result = []
        for reference in references:
            entity = await client.get_entity(strict_reference(reference))
            if not isinstance(entity, types.User) or entity.deleted:
                raise ValueError('Each target must be an existing user')
            if entity.id in [u.id for u in result]:
                raise ValueError('Duplicate users are not allowed')
            result.append(entity)
        return result

    async def invite(client, entity, users):
        added, missing = [], []
        if isinstance(entity, types.Channel):
            result = await client(functions.channels.InviteToChannelRequest(entity, users))
            failed = {m.user_id for m in (getattr(result, 'missing_invitees', None) or [])}
            added = [u.id for u in users if u.id not in failed]
            missing = [u.id for u in users if u.id in failed]
        else:
            for user in users:
                try:
                    await client(functions.messages.AddChatUserRequest(entity.id, user, fwd_limit=0))
                    added.append(user.id)
                except (errors.UserPrivacyRestrictedError, errors.UserNotMutualContactError):
                    missing.append(user.id)
        return {'added_user_ids': added, 'missing_invitees': missing,
                'hint': 'Some users could not be added; offer a user-authorized invite link' if missing else None}

    async def exact_message(client, entity, message_id):
        if message_id <= 0:
            raise ValueError('message_id must be positive')
        message = await client.get_messages(entity, ids=message_id)
        require_message_chat(message, utils.get_peer_id(entity))
        if not message or getattr(message, 'action', None):
            raise ValueError('Message not found or is a service message')
        return message

    @mcp.tool(annotations=read)
    async def search_people(query: str, limit: int = 20) -> dict:
        """Discover people/chats only when needed. Separates own contacts/chats from global strangers.
        Never infer that a global match is the intended recipient; obtain owner confirmation first."""
        if not 2 <= len(query.strip()) <= 100 or not 1 <= limit <= 50:
            raise ValueError('query must contain 2–100 characters; limit 1–50')
        async with telegram() as client:
            result = await client(functions.contacts.SearchRequest(q=query.strip(), limit=limit))
            entities = {utils.get_peer_id(e): e for e in [*result.users, *result.chats]}
            def rows(peers):
                return [chat_record(entities[utils.get_peer_id(p)]) for p in peers if utils.get_peer_id(p) in entities]
            return {'in_my_chats_and_contacts': rows(result.my_results), 'global_public': rows(result.results),
                    'note': 'Global matches are strangers; do not send without exact recipient authorization'}

    @mcp.tool(annotations=read)
    async def get_chat_members(chat_id: int, limit: int = 50, query: str = '') -> dict:
        """Read a bounded member page with creator/admin/member roles. Telegram permissions apply;
        channel member lists may require administrator rights. Numeric user_id and exact name
        can be used in send_message/edit_message mentions even without username or visible phone.
        can_mention indicates usable InputUser data, not permission to contact or notify this user.
        Does not invite, notify, contact anyone or mark messages read."""
        if not 1 <= limit <= 200 or len(query) > 100:
            raise ValueError('limit must be 1–200 and query at most 100 characters')
        async with telegram() as client:
            entity = await client.get_entity(chat_id)
            group(entity)
            try:
                participants = await client.get_participants(entity, limit=limit, search=query)
            except errors.ChatAdminRequiredError:
                raise ValueError('Telegram requires administrator rights to list these members') from None
            rows = []
            for user in participants:
                part = getattr(user, 'participant', None)
                role = ('creator' if isinstance(part, (types.ChannelParticipantCreator, types.ChatParticipantCreator))
                        else 'admin' if isinstance(part, (types.ChannelParticipantAdmin, types.ChatParticipantAdmin)) else 'member')
                try:
                    utils.get_input_user(user)
                    can_mention = not bool(user.deleted)
                except (ValueError, TypeError):
                    can_mention = False
                rows.append({'user_id': user.id, 'name': utils.get_display_name(user),
                             'username': user.username, 'bot': bool(user.bot), 'role': role,
                             'can_mention': can_mention})
            return {'chat': chat_record(entity), 'total': getattr(participants, 'total', len(rows)), 'members': rows}

    @mcp.tool(annotations=destructive)
    async def delete_messages(chat_id: int, expected_chat: str, message_ids: list[int], request_id: str,
                              for_everyone: bool = True, allow_others: bool = False) -> dict:
        """Delete ONLY explicitly user-authorized messages. Resolve/read the exact chat/messages first.
        Max 100 IDs per call, 300/hour. allow_others requires explicit authorization to delete others'
        messages. for_everyone=True revokes for everyone when Telegram permits. Irreversible."""
        if not 1 <= len(message_ids) <= 100 or any(i <= 0 for i in message_ids) or len(set(message_ids)) != len(message_ids):
            raise ValueError('Provide 1–100 distinct positive message IDs')
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            if isinstance(entity, types.Channel) and not for_everyone:
                raise ValueError('Telegram channels/supergroups delete for everyone; a self-only deletion cannot be honored')
            async def action():
                messages = await client.get_messages(entity, ids=message_ids)
                for message in messages:
                    require_message_chat(message, chat_id)
                present = [m.id for m in messages if m and not getattr(m, 'action', None)]
                if any(m and not getattr(m, 'action', None) and not m.out for m in messages) and not allow_others:
                    raise ValueError('Others\' messages require explicit owner authorization and allow_others=True')
                if present:
                    reserve_quota(private, 'delete_messages', len(present))
                    await client.delete_messages(entity, present, revoke=for_everyone)
                    after = await client.get_messages(entity, ids=present)
                    if any(m and not getattr(m, 'action', None) for m in after):
                        raise RuntimeError('Deletion not confirmed; inspect before retrying')
                return {'chat': record, 'deleted_message_ids': present,
                        'already_absent': [i for i in message_ids if i not in present],
                        'for_everyone': for_everyone, 'verified': True}
            return await mutate_once(request_id, ['delete_messages', chat_id, sorted(message_ids), for_everyone, allow_others], action)

    @mcp.tool(annotations=write)
    async def create_chat(kind: str, title: str, request_id: str, about: str = '',
                          members: list[str] | None = None, public_username: str | None = None,
                          forum: bool = False) -> dict:
        """Create a user-requested private supergroup (kind=group) or channel. Max 5/day. Remains
        private unless public_username explicitly requested. Inviting up to 10 people needs separate
        explicit recipient authorization; no one is invited by default. Stable request_id prevents duplicates.
        forum=True (groups only) creates a forum with topics; add topics with create_topic."""
        if kind not in ('group', 'channel') or not 1 <= len(title.strip()) <= 128 or len(about) > 255:
            raise ValueError('kind must be group/channel; title 1–128, about at most 255 characters')
        if public_username and not USERNAME.fullmatch(public_username.lstrip('@')):
            raise ValueError('Invalid public_username')
        if forum and kind != 'group':
            raise ValueError('forum=True is only available for kind=group')
        async with telegram() as client:
            users = await users_resolved(client, members or [])
            async def action():
                reserve_quota(private, 'create_chat')
                if users:
                    reserve_quota(private, 'add_chat_members', len(users))
                result = await client(functions.channels.CreateChannelRequest(title=title.strip(), about=about,
                                      broadcast=kind == 'channel', megagroup=kind == 'group',
                                      forum=True if forum else None))
                entity = next(c for c in result.chats if isinstance(c, types.Channel))
                receipt = {'created': True, 'chat': chat_record(entity), 'kind': kind,
                           **({'forum': bool(getattr(entity, 'forum', False))} if forum else {})}
                if public_username:
                    try:
                        await client(functions.channels.UpdateUsernameRequest(entity, public_username.lstrip('@')))
                        receipt['chat'] = chat_record(await client.get_entity(entity))
                    except errors.FloodWaitError:
                        raise
                    except errors.RPCError as error:
                        receipt['username_error'] = type(error).__name__ + ': chat created; public username not applied'
                if users:
                    try:
                        receipt['invite'] = await invite(client, entity, users)
                    except errors.FloodWaitError:
                        raise
                    except errors.RPCError as error:
                        receipt['invite_error'] = type(error).__name__ + ': chat created; inspect membership before retrying'
                return receipt
            payload = ['create_chat', kind, title, about, [u.id for u in users], public_username]
            return await mutate_once(request_id, payload + (['forum'] if forum else []), action)

    @mcp.tool(annotations=write)
    async def add_chat_members(chat_id: int, expected_chat: str, users: list[str], request_id: str) -> dict:
        """Invite explicitly user-authorized people, max 10/call and 30/day. Use verified numeric IDs
        or exact @usernames; bare display names are rejected. Privacy failures return missing_invitees.
        Do not invite global search matches without owner confirmation. Does not send an invite link."""
        if not 1 <= len(users) <= 10:
            raise ValueError('users must contain 1–10 entries')
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            group(entity)
            resolved = await users_resolved(client, users)
            async def action():
                reserve_quota(private, 'add_chat_members', len(resolved))
                return {'chat': record, **await invite(client, entity, resolved)}
            return await mutate_once(request_id, ['add_chat_members', chat_id, [u.id for u in resolved]], action)

    @mcp.tool(annotations=destructive)
    async def remove_chat_member(chat_id: int, expected_chat: str, user: str, expected_user: str,
                                 request_id: str) -> dict:
        """Remove one explicitly authorized person. Verify both chat AND person, max 30/day. Owner
        self-removal is rejected. This is a kick, not a permanent ban. Telegram admin rights apply."""
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            group(entity)
            target = (await users_resolved(client, [user]))[0]
            check_expected(chat_record(target), expected_user)
            if target.id == (await client.get_me()).id:
                raise ValueError('Refusing to remove the account owner')
            async def absent():
                if isinstance(entity, types.Channel):
                    try:
                        participant = (await client(functions.channels.GetParticipantRequest(entity, target))).participant
                        return isinstance(participant, types.ChannelParticipantLeft) or bool(getattr(participant, 'left', False))
                    except errors.UserNotParticipantError:
                        return True
                result = await client(functions.messages.GetFullChatRequest(entity.id))
                parts = getattr(result.full_chat.participants, 'participants', None)
                if parts is None:
                    raise ValueError('Cannot verify group membership with these permissions')
                return target.id not in [p.user_id for p in parts]
            async def action():
                if await absent():
                    return {'chat': record, 'user_id': target.id, 'already_absent': True, 'verified': True}
                reserve_quota(private, 'remove_chat_member')
                await client.kick_participant(entity, target)
                if not await absent():
                    raise RuntimeError('Removal not confirmed; inspect membership before retrying')
                return {'chat': record, 'removed_user_id': target.id, 'verified': True}
            return await mutate_once(request_id, ['remove_chat_member', chat_id, target.id], action)

    @mcp.tool(annotations=write)
    async def create_invite_link(chat_id: int, expected_chat: str, request_id: str = '',
                                 expire_hours: int | None = 24, usage_limit: int | None = 1,
                                 title: str | None = '', join_request: bool = False) -> dict:
        """Create a user-requested invite link; never send it automatically. Defaults: 24 hours,
        one use. Set usage_limit=0 for unlimited uses only if requested; join requests cannot have
        usage limits. Stable request_id returns the same link on retry (without one, every call creates
        a new link). The link is private data."""
        import uuid
        request_id = request_id or 'invite-' + uuid.uuid4().hex
        expire_hours = 24 if expire_hours is None else expire_hours
        usage_limit = 1 if usage_limit is None else usage_limit
        title = title or ''
        if not 1 <= expire_hours <= 8760 or not 0 <= usage_limit <= 100000 or len(title) > 32:
            raise ValueError('expire_hours 1–8760, usage_limit 0–100000, title at most 32 characters')
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            group(entity)
            async def action():
                expiry = datetime.now(timezone.utc) + timedelta(hours=expire_hours)
                result = await client(functions.messages.ExportChatInviteRequest(peer=entity, expire_date=expiry,
                    usage_limit=None if join_request or usage_limit == 0 else usage_limit,
                    title=title or None, request_needed=join_request))
                return {'chat': record, 'link': result.link, 'expires_at': expiry.isoformat(),
                        'usage_limit': None if join_request else usage_limit, 'join_request': join_request}
            return await mutate_once(request_id, ['create_invite_link', chat_id, expire_hours, usage_limit, title, join_request], action)

    @mcp.tool(annotations=write)
    async def edit_chat_info(chat_id: int, expected_chat: str, title: str | None = None,
                             about: str | None = None) -> dict:
        """Change an explicitly user-authorized group/channel title or description; verifies both.
        title max 128, about max 255 characters. Does not change usernames or chat permissions."""
        if (title is None and about is None) or (title is not None and not 1 <= len(title.strip()) <= 128) or (about is not None and len(about) > 255):
            raise ValueError('Provide title (1–128 characters) and/or about (0–255)')
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            group(entity)
            if title is not None:
                try:
                    await client(functions.channels.EditTitleRequest(entity, title) if isinstance(entity, types.Channel)
                                 else functions.messages.EditChatTitleRequest(entity.id, title))
                except errors.ChatNotModifiedError:
                    pass
            if about is not None:
                try:
                    await client(functions.messages.EditChatAboutRequest(entity, about))
                except errors.ChatAboutNotModifiedError:
                    pass
            after = await client.get_entity(entity)
            info = await full_info(client, after)
            if (title is not None and after.title != title) or (about is not None and info['about'] != about):
                raise RuntimeError('Chat information change not confirmed; reread before retrying')
            return {'chat': chat_record(after), **info, 'verified': True}

    @mcp.tool(annotations=write)
    async def pin_message(chat_id: int, expected_chat: str, message_id: int,
                           unpin: bool = False, notify: bool = False) -> dict:
        """Pin or unpin exactly one user-requested message; silent by default. Verify the message
        and chat first. Telegram permissions apply. Never unpins all messages."""
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            await exact_message(client, entity, message_id)
            if unpin:
                await client.unpin_message(entity, message_id)
            else:
                await client.pin_message(entity, message_id, notify=notify)
            for attempt in range(5):
                after = await exact_message(client, entity, message_id)
                if bool(after.pinned) == (not unpin):
                    return {'chat': record, 'message_id': message_id, 'pinned': bool(after.pinned), 'verified': True}
                if attempt < 4:
                    await asyncio.sleep(.8)
            raise RuntimeError('Requested pinned state not confirmed')

    @mcp.tool(annotations=write)
    async def set_reaction(chat_id: int, expected_chat: str, message_id: int, emoji: str | None = None) -> dict:
        """Set the account's one standard emoji reaction, or remove its reactions with emoji=null.
        Requires user authorization and exact message/chat. Allowed emoji depend on Telegram/chat.
        Returns the verified own selected reaction, not other people's reactions."""
        if emoji is not None and (not emoji.strip() or len(emoji) > 16):
            raise ValueError('Provide one standard emoji or null; custom emoji are not supported')
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            await exact_message(client, entity, message_id)
            await client(functions.messages.SendReactionRequest(peer=entity, msg_id=message_id,
                         reaction=[types.ReactionEmoji(emoji)] if emoji else []))
            for attempt in range(5):
                after = await exact_message(client, entity, message_id)
                selected = [getattr(r.reaction, 'emoticon', None) for r in (getattr(after.reactions, 'results', None) or [])
                            if r.chosen_order is not None]
                if selected == ([emoji] if emoji else []):
                    return {'chat': record, 'message_id': message_id, 'reaction': emoji, 'verified': True}
                if attempt < 4:
                    await asyncio.sleep(.8)
            raise RuntimeError('Own reaction not confirmed; inspect the message')

    @mcp.tool(annotations=write)
    async def mark_read(chat_id: int, expected_chat: str) -> dict:
        """Mark this chat read ONLY on an explicit user request: other people may see read receipts.
        Acknowledges messages/mentions and clears the manual unread reminder. Ordinary reads do not
        do this. To leave a reminder after a notification, use mark_chat_unread instead."""
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            peer, _ = await dialog_state(client, entity)
            await client.send_read_acknowledge(entity, clear_mentions=True)
            await client(functions.messages.MarkDialogUnreadRequest(peer=peer, unread=False))
            for attempt in range(5):
                _, state = await dialog_state(client, entity)
                if not state['unread_mark'] and state['unread_message_count'] == 0:
                    return {'chat': record, **state, 'verified': True, 'read_receipts_sent': True}
                if attempt < 4:
                    await asyncio.sleep(.8)
            raise RuntimeError('Read state not confirmed; new messages may have arrived')

    @mcp.tool(annotations=write)
    async def join_chat(target: str, expected_chat: str, request_id: str) -> dict:
        """Join only an explicitly user-requested channel/group, max 10/day. For invite links,
        checks the preview title against expected_chat before joining; reports pending join requests
        honestly. Public targets require @username/numeric ID/t.me/username. No-op if already joined."""
        match = INVITE.fullmatch(target.strip())
        reference = None if match else strict_reference(target)
        async with telegram() as client:
            if match:
                preview = await client(functions.messages.CheckChatInviteRequest(match.group(1)))
                entity = getattr(preview, 'chat', None)
                record = chat_record(entity) if entity else {'chat_id': None, 'name': preview.title, 'username': None}
                check_expected(record, expected_chat)
                if isinstance(preview, types.ChatInviteAlready):
                    return {'chat': record, 'already_member': True}
            else:
                entity = await client.get_entity(reference)
                record = chat_record(entity)
                check_expected(record, expected_chat)
                if not isinstance(entity, types.Channel):
                    raise ValueError('Only channels/supergroups or invite links can be joined')
                if not entity.left:
                    return {'chat': record, 'already_member': True}
            async def action():
                reserve_quota(private, 'join_chat')
                try:
                    result = await client(functions.messages.ImportChatInviteRequest(match.group(1)) if match
                                          else functions.channels.JoinChannelRequest(entity))
                except errors.InviteRequestSentError:
                    return {'chat': record, 'joined': False, 'join_request_sent': True}
                chats = getattr(result, 'chats', [])
                joined = chats[0] if chats else entity
                if not joined:
                    raise RuntimeError('Join outcome uncertain; inspect membership before retrying')
                after = await client.get_entity(joined)
                if getattr(after, 'left', False):
                    raise RuntimeError('Membership not confirmed; inspect before retrying')
                return {'chat': chat_record(after), 'joined': True, 'verified': True}
            return await mutate_once(request_id, ['join_chat', target], action)
