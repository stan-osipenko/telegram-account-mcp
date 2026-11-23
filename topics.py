"""Forum topic support (service 1.6.0).

A forum topic is identified by the ID of its MessageActionTopicCreate service message.
Topic 1 is the built-in General topic. Messages inside other topics carry
reply_to.forum_topic=True with reply_to_top_id (replies) or reply_to_msg_id (plain posts)
pointing at the topic ID. Messages without forum_topic in a forum belong to General.
"""
from datetime import datetime

from telethon import functions, types, utils

GENERAL_TOPIC = 1
GENERAL_SCAN_FACTOR = 20
GENERAL_SCAN_MAX = 2000


def message_topic_id(message, forum=None):
    """Topic of a message: topic ID, 1 for General, None outside forums/unknown."""
    if not message:
        return None
    reply = getattr(message, 'reply_to', None)
    if isinstance(reply, types.MessageReplyHeader) and reply.forum_topic:
        return reply.reply_to_top_id or reply.reply_to_msg_id
    if isinstance(getattr(message, 'action', None), types.MessageActionTopicCreate):
        return message.id
    if forum is None:
        forum = bool(getattr(getattr(message, 'chat', None), 'forum', False))
    return GENERAL_TOPIC if forum else None


def require_forum(entity):
    if not getattr(entity, 'forum', False):
        raise ValueError('Chat is not a forum with topics; omit topic_id or use list_chats/get_chat')


def short_message(message):
    if not isinstance(message, (types.Message, types.MessageService)):
        return None
    record = {'id': message.id, 'date': message.date.isoformat() if message.date else None,
              'sender_id': utils.get_peer_id(message.from_id) if message.from_id else None}
    if isinstance(message, types.MessageService):
        return {**record, 'service_action': type(message.action).__name__}
    return {**record, 'text': (message.message or '')[:300], 'has_attachment': bool(message.media)}


def topic_record(topic, last_message=None):
    return {'topic_id': topic.id, 'title': topic.title, 'general': topic.id == GENERAL_TOPIC,
            'closed': bool(topic.closed), 'pinned': bool(topic.pinned), 'hidden': bool(topic.hidden),
            'created_at': topic.date.isoformat() if topic.date else None,
            'unread_count': topic.unread_count, 'last_message_id': topic.top_message,
            'last_message': short_message(last_message)}


async def get_topic(client, entity, topic_id):
    """Verify the chat is a forum and topic_id is an existing (not deleted) topic."""
    if topic_id < 1:
        raise ValueError('topic_id must be a positive topic ID from list_topics (1 = General)')
    require_forum(entity)
    result = await client(functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=[topic_id]))
    topic = next((t for t in result.topics if t.id == topic_id), None)
    if not isinstance(topic, types.ForumTopic):
        raise ValueError('Topic does not exist in this chat; use list_topics')
    return topic


def finish(client, entity, result):
    """Attach Telethon entities to raw request results so .file/.chat work as usual."""
    if isinstance(result, types.messages.MessagesNotModified):
        return []
    entities = {utils.get_peer_id(x): x for x in [*result.users, *result.chats]}
    messages = [m for m in result.messages if isinstance(m, (types.Message, types.MessageService))]
    for message in messages:
        message._finish_init(client, entities, entity)
    return messages


async def read_topic(client, entity, topic_id, limit, before_message_id):
    """Newest-first page of one topic. Returns (messages, next_before_message_id, scanned)."""
    await get_topic(client, entity, topic_id)
    if topic_id != GENERAL_TOPIC:
        messages = await client.get_messages(entity, limit=limit, offset_id=before_message_id,
                                             reply_to=topic_id)
        return list(messages), (messages[-1].id if len(messages) == limit else None), None
    # General has no root message for messages.getReplies: scan history and keep General posts.
    found, scanned, last_id = [], 0, None
    async for message in client.iter_messages(entity, offset_id=before_message_id,
                                              limit=min(limit * GENERAL_SCAN_FACTOR, GENERAL_SCAN_MAX)):
        scanned += 1
        last_id = message.id
        if message_topic_id(message, forum=True) == GENERAL_TOPIC:
            found.append(message)
            if len(found) == limit:
                break
    exhausted = scanned < min(limit * GENERAL_SCAN_FACTOR, GENERAL_SCAN_MAX) and len(found) < limit
    return found, (None if exhausted or last_id is None else last_id), scanned


async def search_topic(client, entity, topic_id, query, limit):
    await get_topic(client, entity, topic_id)
    if topic_id == GENERAL_TOPIC:
        messages = await client.get_messages(entity, search=query, limit=100)
        return [m for m in messages if message_topic_id(m, forum=True) == GENERAL_TOPIC][:limit]
    result = await client(functions.messages.SearchRequest(
        peer=entity, q=query, filter=types.InputMessagesFilterEmpty(), min_date=None, max_date=None,
        offset_id=0, add_offset=0, limit=limit, max_id=0, min_id=0, hash=0, top_msg_id=topic_id))
    return finish(client, entity, result)


async def prepare_topic_send(client, entity, chat_id, topic_id, reply_to_message_id):
    """Validate topic/reply before any mutation. Returns (topic_info or None, reply_to or None)."""
    from admin_tools import require_message_chat
    topic = await get_topic(client, entity, topic_id) if topic_id else None
    if reply_to_message_id:
        reply = await client.get_messages(entity, ids=reply_to_message_id)
        require_message_chat(reply, chat_id)
        if not reply:
            raise ValueError('Reply target message does not exist')
        if topic_id and message_topic_id(reply, forum=True) != topic_id:
            raise ValueError('Reply target belongs to another topic; no message was sent')
        return ({'topic_id': topic.id, 'title': topic.title} if topic else None), reply_to_message_id
    if not topic_id:
        return None, None
    # Posting into a non-General topic = reply to its root service message. General needs no reply.
    return ({'topic_id': topic.id, 'title': topic.title},
            None if topic_id == GENERAL_TOPIC else topic_id)


async def verify_topic(client, entity, chat_id, message_id, topic_id, scheduled=False):
    """Read the sent message back and confirm it is in the requested topic."""
    from admin_tools import require_message_chat
    if scheduled:
        result = await client(functions.messages.GetScheduledMessagesRequest(entity, id=[message_id]))
        confirmed = next((m for m in result.messages if m.id == message_id), None)
    else:
        confirmed = await client.get_messages(entity, ids=message_id)
    require_message_chat(confirmed, chat_id)
    if not confirmed or message_topic_id(confirmed, forum=True) != topic_id:
        raise RuntimeError('Telegram did not confirm the message in the requested topic; '
                           'inspect the chat before any resend')
    return confirmed


def register_topic_tools(mcp, telegram, chat_record, limit_check, READ,
                         checked_entity=None, mutate_once=None, WRITE=None):
    from admin_tools import strict_reference

    @mcp.tool(annotations=READ)
    async def list_topics(chat_id: int, query: str = '', limit: int = 50, offset_date: str = '',
                          offset_id: int = 0, offset_topic: int = 0) -> dict:
        """List topics (threads) of a forum supergroup: topic_id, title, closed/pinned state,
        unread count and last message. topic_id 1 is General. Pass topic_id to read_messages,
        search_messages, send_message or send_file to work inside one topic. Optional query filters
        by title. Use next_page arguments to continue. total is Telegram's count (may exclude General).
        Read-only; does not mark anything read."""
        limit_check(limit)
        async with telegram() as client:
            entity = await client.get_entity(strict_reference(chat_id))
            record = chat_record(entity)
            require_forum(entity)
            result = await client(functions.messages.GetForumTopicsRequest(
                peer=entity, offset_date=datetime.fromisoformat(offset_date) if offset_date else None,
                offset_id=offset_id, offset_topic=offset_topic, limit=limit, q=query or None))
            last_messages = {m.id: m for m in result.messages}
            topics = [topic_record(t, last_messages.get(t.top_message)) for t in result.topics
                      if isinstance(t, types.ForumTopic)]
            last = result.topics[-1] if result.topics else None
            last_message = last_messages.get(getattr(last, 'top_message', None))
            next_page = None
            if isinstance(last, types.ForumTopic) and len(result.topics) == limit and last_message:
                next_page = {'offset_date': last_message.date.isoformat(), 'offset_id': last.top_message,
                             'offset_topic': last.id}
            return {'chat': record, 'topics': topics, 'total': result.count, 'next_page': next_page}

    @mcp.tool(annotations=WRITE)
    async def create_topic(chat_id: int, expected_chat: str, title: str, request_id: str) -> dict:
        """Create a new topic in a forum supergroup, only when the user explicitly requests that
        topic in that exact chat. Verify the chat first (get_chat shows forum=true). Requires the
        right to manage topics. Stable request_id prevents duplicate topics on retry. Does not post
        any other message; the topic is read back and verified."""
        title = title.strip()
        if not 1 <= len(title) <= 128:
            raise ValueError('Topic title must contain 1–128 characters')
        async with telegram() as client:
            entity, record = await checked_entity(client, chat_id, expected_chat)
            require_forum(entity)
            async def create():
                updates = await client(functions.messages.CreateForumTopicRequest(peer=entity, title=title))
                created = next((u.message for u in getattr(updates, 'updates', [])
                                if isinstance(getattr(u, 'message', None), types.MessageService)
                                and isinstance(u.message.action, types.MessageActionTopicCreate)), None)
                if not created:
                    raise RuntimeError('Telegram did not return the new topic; inspect list_topics before retrying')
                topic = await get_topic(client, entity, created.id)
                if topic.title != title:
                    raise RuntimeError('Created topic title was not confirmed; inspect list_topics')
                return {'created': True, 'chat': record, 'topic_id': topic.id, 'title': topic.title}
            return await mutate_once(request_id, ['create_topic', chat_id, title], create)

    return list_topics, create_topic
