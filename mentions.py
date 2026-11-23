"""Typed Telegram mentions. No username/phone lookup and no mutations here."""
from difflib import SequenceMatcher
import hashlib
import json
import sqlite3

from pydantic import BaseModel, ConfigDict, Field
from telethon import errors, functions, types, utils

MAX_MENTIONS = 100


class MentionSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    user_id: int = Field(gt=0, description='Numeric user ID from get_chat_members; never a phone number')
    expected_user: str = Field(min_length=1, max_length=256,
                              description='Exact display name or @username returned for this user')
    offset: int = Field(ge=0, description='Start offset in UTF-16 code units, not Python characters')
    length: int = Field(gt=0, description='Mention length in UTF-16 code units')


def boundaries(text):
    """Map legal UTF-16 boundaries to Python character indexes; reject lone surrogates."""
    result, units = {0: 0}, 0
    for index, char in enumerate(text):
        if 0xD800 <= ord(char) <= 0xDFFF:
            raise ValueError('Text contains an isolated UTF-16 surrogate')
        units += 2 if ord(char) > 0xFFFF else 1
        result[units] = index + 1
    return result


def utf16_length(text):
    return max(boundaries(text))


def validate_spans(text, specs):
    if len(specs) > MAX_MENTIONS:
        raise ValueError(f'At most {MAX_MENTIONS} mentions per message')
    positions = boundaries(text)
    ordered = sorted(specs, key=lambda m: (m['offset'], m['length'], m['user_id']))
    previous_end = 0
    for spec in ordered:
        start, end = spec['offset'], spec['offset'] + spec['length']
        if start not in positions or end not in positions or end <= start:
            raise ValueError('Mention range is outside text or splits a UTF-16 surrogate pair')
        if start < previous_end:
            raise ValueError('Overlapping or duplicate mention ranges are not allowed')
        if not text[positions[start]:positions[end]].strip():
            raise ValueError('A mention must cover a visible name, not whitespace')
        previous_end = end
    return ordered


def normalize_mentions(text, mentions):
    specs = [MentionSpec.model_validate(m).model_dump() for m in (mentions or [])]
    return validate_spans(text, specs)


def entity_records(message):
    """Expose entity metadata only, never InputUser access_hash or unrelated private data."""
    result = []
    for entity in (getattr(message, 'entities', None) or []):
        item = {'type': type(entity).__name__, 'offset': entity.offset, 'length': entity.length}
        if isinstance(entity, types.MessageEntityMentionName):
            item['user_id'] = entity.user_id
        result.append(item)
    return result


def mention_records(message):
    return [{'offset': e.offset, 'length': e.length, 'user_id': e.user_id}
            for e in (getattr(message, 'entities', None) or [])
            if isinstance(e, types.MessageEntityMentionName)]


def mention_signature(specs):
    return sorted((m['offset'], m['length'], m['user_id']) for m in specs)


def edit_fingerprint(text, expected_text):
    return hashlib.sha256(json.dumps([text, expected_text], ensure_ascii=True).encode()).hexdigest()


def remember_edit(private, chat_id, message_id, text, expected_text, specs):
    """Keep desired mention semantics across an uncertain edit or a process restart."""
    with sqlite3.connect(private / 'sent-receipts.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS mention_edits '
                   '(chat_id INTEGER, message_id INTEGER, fingerprint TEXT, mentions TEXT, '
                   'PRIMARY KEY(chat_id,message_id))')
        db.execute('INSERT OR REPLACE INTO mention_edits VALUES (?,?,?,?)',
                   (chat_id, message_id, edit_fingerprint(text, expected_text),
                    json.dumps(mention_signature(specs))))


def verify_edit_retry(private, chat_id, message_id, text, expected_text, actual):
    database = private / 'sent-receipts.sqlite3'
    if not database.exists():
        return
    with sqlite3.connect(database) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mention_edits'").fetchone():
            return
        row = db.execute('SELECT fingerprint,mentions FROM mention_edits WHERE chat_id=? AND message_id=?',
                         (chat_id, message_id)).fetchone()
    if row and row[0] == edit_fingerprint(text, expected_text):
        desired = [tuple(m) for m in json.loads(row[1])]
        if desired != mention_signature(actual):
            raise RuntimeError('Previous edit mention verification is uncertain; fetch the message and '
                               'supply an explicit mentions list before changing it again')


def preserve_mentions(old_text, new_text, message):
    old = validate_spans(old_text, mention_records(message))
    if old_text == new_text:
        return old
    positions = boundaries(old_text)
    new_boundaries = {index: units for units, index in boundaries(new_text).items()}
    matches = SequenceMatcher(None, old_text, new_text, autojunk=False).get_matching_blocks()
    result = []
    for spec in old:
        start = positions[spec['offset']]
        end = positions[spec['offset'] + spec['length']]
        label = old_text[start:end]
        # Repeated names may refer to different people; never guess which occurrence to tag.
        candidates = [block for block in matches if block.a <= start and end <= block.a + block.size]
        if len(candidates) != 1 or new_text.count(label) != 1:
            raise ValueError('Existing mention changed, removed or became ambiguous; supply the full '
                             'mentions list explicitly, or mentions=[] to intentionally remove all')
        block = candidates[0]
        new_start = block.b + start - block.a
        new_end = new_start + end - start
        if ((new_start and new_text[new_start - 1].isalnum() and
             (not start or not old_text[start - 1].isalnum())) or
            (new_end < len(new_text) and new_text[new_end].isalnum() and
             (end == len(old_text) or not old_text[end].isalnum()))):
            raise ValueError('Existing mention label boundary changed; supply mentions explicitly')
        result.append({**spec, 'offset': new_boundaries[new_start],
                       'length': new_boundaries[new_end] - new_boundaries[new_start]})
    return validate_spans(new_text, result)


async def require_member(client, group, user):
    try:
        if isinstance(group, types.Channel):
            result = await client(functions.channels.GetParticipantRequest(group, user))
            participant = result.participant
            absent = isinstance(participant, types.ChannelParticipantLeft)
            if isinstance(participant, types.ChannelParticipantBanned):
                absent = bool(participant.left or participant.banned_rights.view_messages)
            if absent:
                raise ValueError('Mention target is not a current member of this group')
        elif isinstance(group, types.Chat):
            result = await client(functions.messages.GetFullChatRequest(group.id))
            members = getattr(result.full_chat.participants, 'participants', None)
            if members is None:
                raise ValueError('Telegram does not expose this group member list to this account')
            if user.id not in {p.user_id for p in members}:
                raise ValueError('Mention target is not a current member of this group')
        else:
            raise ValueError('Explicit personal mentions require a group; use get_chat_members first')
    except (errors.ChatAdminRequiredError, errors.ChannelPrivateError, errors.UserNotParticipantError) as error:
        raise ValueError('Telegram denied group membership verification: ' + type(error).__name__) from None


async def input_mentions(client, group, specs, *, verify_membership):
    result, resolved = [], {}
    for spec in specs:
        user_id = spec['user_id']
        if user_id not in resolved:
            try:
                # PeerUser forces ID lookup in this authorized session. Never try a name/phone,
                # fabricate access_hash=0, or start another Telegram connection.
                peer = await client.get_input_entity(types.PeerUser(user_id))
                user = await client.get_entity(peer)
                if not isinstance(user, types.User) or user.id != user_id or user.deleted:
                    raise ValueError('Not an available Telegram user')
                input_user = utils.get_input_user(peer)
            except (ValueError, TypeError):
                raise ValueError(f'User {user_id} cannot be resolved in this Telegram session; '
                                 'fetch accessible group members first') from None
            if verify_membership:
                await require_member(client, group, user)
            resolved[user_id] = (user, input_user)
        user, input_user = resolved[user_id]
        if 'expected_user' in spec:
            expected = spec['expected_user'].casefold()
            names = [utils.get_display_name(user).casefold()]
            if user.username:
                names.append('@' + user.username.casefold())
            if expected not in names:
                raise ValueError('Mention user does not match expected_user; resolve the exact member first')
        result.append(types.InputMessageEntityMentionName(spec['offset'], spec['length'], input_user))
    return result


def verify_mentions(message, text, specs):
    if not message or (message.message or '') != text:
        raise RuntimeError('Telegram did not confirm the requested text; inspect before retrying')
    if mention_signature(mention_records(message)) != mention_signature(specs):
        raise RuntimeError('Telegram did not confirm MessageEntityMentionName with the requested '
                           'user IDs and UTF-16 ranges; inspect before retrying')
    return entity_records(message)
