"""get_message_readers (1.7.2): кто и когда прочитал исходящее сообщение. Только чтение.

Группы/супергруппы: messages.GetMessageReadParticipants (Telegram отдаёт его, если в чате <=100
участников и сообщению не больше 7 дней; время прочтения — с ReadParticipantDate).
Личка: read_outbox_max_id диалога — прочитано или нет, без времени.
Ничего не помечает прочитанным.
"""
from __future__ import annotations

from telethon import errors, functions, types, utils


async def _name(client, user_id):
    try:
        return utils.get_display_name(await client.get_entity(user_id))
    except Exception:
        return None


async def message_readers(client, chat_id: int, message_id: int) -> dict:
    entity = await client.get_entity(chat_id)
    message = await client.get_messages(entity, ids=message_id)
    if not message:
        raise ValueError('Message not found in this chat')
    base = {'chat_id': utils.get_peer_id(entity), 'message_id': message_id, 'outgoing': bool(message.out)}
    if isinstance(entity, types.User):
        peer = types.InputDialogPeer(await client.get_input_entity(entity))
        result = await client(functions.messages.GetPeerDialogsRequest(peers=[peer]))
        if not result.dialogs:
            raise ValueError('Chat is not an existing Telegram dialog')
        outbox = result.dialogs[0].read_outbox_max_id
        read = bool(message.out) and message_id <= outbox
        readers = [{'user_id': entity.id, 'name': utils.get_display_name(entity), 'read_at': None}] if read else []
        return {**base, 'kind': 'private', 'read': read, 'read_outbox_max_id': outbox, 'readers': readers,
                'note': None if message.out else 'Входящее сообщение: отметку о прочтении собеседником Telegram не даёт'}
    if not message.out:
        return {**base, 'kind': 'group', 'read': None, 'readers': [], 'error': 'not_outgoing',
                'note': 'Telegram отдаёт прочтения только для своих (исходящих) сообщений'}
    try:
        raw = await client(functions.messages.GetMessageReadParticipantsRequest(
            peer=await client.get_input_entity(entity), msg_id=message_id))
    except errors.MsgIdInvalidError:
        return {**base, 'kind': 'group', 'read': None, 'readers': [], 'error': 'msg_id_invalid',
                'note': 'Telegram не дал прочтения для этого сообщения'}
    except errors.MsgTooOldError:
        return {**base, 'kind': 'group', 'read': None, 'readers': [], 'error': 'too_old',
                'note': 'Telegram отдаёт прочтения только для сообщений не старше 7 дней'}
    except errors.ChatTooBigError:
        return {**base, 'kind': 'group', 'read': None, 'readers': [], 'error': 'chat_too_big',
                'note': 'Telegram отдаёт прочтения только в чатах до 100 участников'}
    readers = []
    for item in raw or []:
        uid = getattr(item, 'user_id', item)
        date = getattr(item, 'date', None)
        readers.append({'user_id': uid, 'name': await _name(client, uid),
                        'read_at': date.isoformat() if date else None})
    return {**base, 'kind': 'group', 'read': bool(readers), 'readers': readers}


def register_read_receipt_tools(mcp, telegram, read_annotations):
    @mcp.tool(annotations=read_annotations)
    async def get_message_readers(chat_id: int, message_id: int) -> dict:
        """Who has read a message and when. Groups/supergroups: list of readers with read time
        (Telegram allows it only in chats with <=100 members and for messages <=7 days old).
        Private chats: whether the other side has read our outgoing message (no time).
        Read-only: marks nothing as read."""
        async with telegram() as client:
            return await message_readers(client, chat_id, message_id)
    return get_message_readers
