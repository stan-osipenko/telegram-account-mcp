"""Telegram rich messages (layer 229+): server-parsed Markdown/HTML with tables, headings, lists.

Telegram's rich message editor sends `rich_message=InputRichMessageMarkdown|HTML` in
messages.sendMessage; the server parses it into page blocks (PageBlockTable, PageBlockHeading1…,
PageBlockList, PageBlockBlockquote, PageBlockPreformatted, …) and stores them in Message.rich_message.
"""
from telethon.tl import functions, types

RICH_MODES = {'rich_markdown': 'markdown', 'rich': 'markdown', 'rich_html': 'html'}
MAX_RICH_SOURCE = 32768


def rich_kind(parse_mode):
    return RICH_MODES.get((parse_mode or '').lower())


def input_rich(text, kind, link_preview=True):
    if not text.strip() or len(text) > MAX_RICH_SOURCE:
        raise ValueError(f'Rich message source must contain 1–{MAX_RICH_SOURCE} characters')
    cls = types.InputRichMessageMarkdown if kind == 'markdown' else types.InputRichMessageHTML
    return cls(text, noautolink=None if link_preview else True)


def rich_text(value):
    """Flatten a RichText tree to plain text."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, types.TextConcat):
        return ''.join(rich_text(t) for t in value.texts)
    if isinstance(value, types.TextEmpty):
        return ''
    inner = getattr(value, 'text', None)
    return rich_text(inner) if inner is not None else ''


def block_record(block):
    name = type(block).__name__.removeprefix('PageBlock')
    if isinstance(block, types.PageBlockTable):
        rows = [[rich_text(c.text) for c in row.cells] for row in block.rows]
        return {'type': 'Table', 'title': rich_text(block.title), 'rows': rows}
    if isinstance(block, (types.PageBlockList, types.PageBlockOrderedList)):
        items = []
        for item in block.items:
            if hasattr(item, 'text'):
                items.append(rich_text(item.text))
            else:
                items.append(' '.join(block_text(b) for b in getattr(item, 'blocks', [])))
        return {'type': name, 'items': items}
    nested = getattr(block, 'blocks', None)
    if isinstance(nested, list):
        return {'type': name, 'blocks': [block_record(b) for b in nested]}
    return {'type': name, 'text': block_text(block)}


def block_text(block):
    for attr in ('text', 'title', 'caption'):
        value = getattr(block, attr, None)
        if value is not None and not isinstance(value, (types.PageCaption,)):
            return rich_text(value)
    return ''


def rich_record(message):
    rich = getattr(message, 'rich_message', None)
    if not rich:
        return None
    return {'blocks': [block_record(b) for b in rich.blocks]}


async def send_rich(client, entity, text, kind, reply_to=None, top_msg_id=None, schedule=None,
                    link_preview=True):
    reply = None
    if reply_to or top_msg_id:
        reply = types.InputReplyToMessage(reply_to_msg_id=reply_to or top_msg_id,
                                          top_msg_id=top_msg_id if reply_to and top_msg_id else None)
    request = functions.messages.SendMessageRequest(
        peer=entity, message='', rich_message=input_rich(text, kind, link_preview),
        reply_to=reply, schedule_date=schedule, no_webpage=not link_preview)
    result = await client(request)
    return client._get_response_message(request, result, entity)
