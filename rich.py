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
    blocks = [block_record(b) for b in rich.blocks]
    return {'blocks': blocks, 'markdown': record_to_markdown(blocks),
            **({'partial': True} if getattr(rich, 'part', False) else {})}


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


# ---- Structured blocks (send_rich_message) → Markdown that Telegram parses ----

BLOCK_TYPES = ('paragraph', 'heading', 'table', 'list', 'ordered_list', 'quote', 'code', 'divider', 'markdown')
ALIGN = {'left': ':---', 'center': ':---:', 'right': '---:', None: '---', '': '---'}


def _cell(value):
    text = value.get('text', '') if isinstance(value, dict) else value
    return str('' if text is None else text).replace('\n', ' ').replace('|', '\\|')


def blocks_to_markdown(blocks):
    """[{type, ...}] → Markdown. Text fields accept inline Markdown (**bold**, _italic_, `code`, [link](url))."""
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('blocks must be a non-empty list')
    out = []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get('type') not in BLOCK_TYPES:
            raise ValueError(f'blocks[{i}].type must be one of {", ".join(BLOCK_TYPES)}')
        kind = block['type']
        if kind == 'paragraph':
            out.append(str(block.get('text', '')).strip())
        elif kind == 'heading':
            level = int(block.get('level', 2))
            if not 1 <= level <= 6:
                raise ValueError(f'blocks[{i}].level must be 1–6')
            out.append('#' * level + ' ' + str(block.get('text', '')).strip())
        elif kind == 'table':
            header, rows = block.get('header') or [], block.get('rows') or []
            if not header or not all(isinstance(r, list) for r in rows):
                raise ValueError(f'blocks[{i}]: a table needs header (list) and rows (list of lists)')
            width = len(header)
            if any(len(r) > width for r in rows):
                raise ValueError(f'blocks[{i}]: a row has more cells than the header')
            align = block.get('align') or [None] * width
            if len(align) != width or any(a not in ALIGN for a in align):
                raise ValueError(f'blocks[{i}].align must list left/center/right for each column')
            lines = ['| ' + ' | '.join(_cell(c) for c in header) + ' |',
                     '|' + '|'.join(ALIGN[a] for a in align) + '|']
            lines += ['| ' + ' | '.join(_cell(c) for c in r + [''] * (width - len(r))) + ' |' for r in rows]
            if block.get('title'):
                lines.insert(0, f"**{str(block['title']).strip()}**\n")
            out.append('\n'.join(lines))
        elif kind in ('list', 'ordered_list'):
            items = block.get('items') or []
            if not items:
                raise ValueError(f'blocks[{i}]: a list needs items')
            out.append('\n'.join((f'{n}. ' if kind == 'ordered_list' else '- ') + str(t).replace('\n', ' ')
                                 for n, t in enumerate(items, 1)))
        elif kind == 'quote':
            out.append('\n'.join('> ' + line for line in str(block.get('text', '')).splitlines() or ['']))
        elif kind == 'code':
            out.append(f"```{block.get('language', '')}\n{block.get('text', '')}\n```")
        elif kind == 'divider':
            out.append('---')
        else:
            out.append(str(block.get('text', '')).strip())
    return '\n\n'.join(out)


# ---- Reading: full content for long messages and a Markdown rendering ----

async def full_rich(client, entity, message):
    """Telegram may store only part of a long rich message in Message.rich_message (part=True)."""
    from telethon.tl import functions as fn
    rich = getattr(message, 'rich_message', None)
    if rich and getattr(rich, 'part', False):
        try:
            message.rich_message = await client(fn.messages.GetRichMessageRequest(entity, message.id))
        except Exception:
            pass  # Keep the partial content; the record marks it as partial.
    return message


def record_to_markdown(blocks):
    out = []
    for b in blocks:
        kind = b['type']
        if kind == 'Table':
            rows = b['rows'] or [[]]
            width = max(len(r) for r in rows) or 1
            fix = lambda r: '| ' + ' | '.join(_cell(c) for c in r + [''] * (width - len(r))) + ' |'
            out.append('\n'.join([fix(rows[0]), '|' + '|'.join(['---'] * width) + '|'] + [fix(r) for r in rows[1:]]))
        elif kind.startswith('Heading'):
            out.append('#' * int(kind[-1]) + ' ' + b['text'])
        elif kind in ('List', 'OrderedList'):
            out.append('\n'.join((f'{n}. ' if kind == 'OrderedList' else '- ') + t for n, t in enumerate(b['items'], 1)))
        elif kind == 'Blockquote':
            out.append('> ' + b['text'])
        elif kind == 'Preformatted':
            out.append(f"```\n{b['text']}\n```")
        elif kind == 'Divider':
            out.append('---')
        elif 'blocks' in b:
            out.append(record_to_markdown(b['blocks']))
        else:
            out.append(b.get('text', ''))
    return '\n\n'.join(x for x in out if x)
