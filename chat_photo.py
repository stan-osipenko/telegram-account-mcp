"""Avatars: set_chat_photo / delete_chat_photo (1.7.0), set_bot_photo for owned bots (1.7.1)."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mcp.types import ToolAnnotations
from telethon import errors, functions, types

MAX_PHOTO = 10 * 1024 * 1024
MAX_SIDE = 2048
MAX_PIXELS = 60_000_000
FORMATS = {'JPEG': '.jpg', 'PNG': '.png'}


def prepare_image(path: Path, workdir: Path) -> dict:
    """Validate a staged JPEG/PNG (<=10 MiB) and shrink it so the longest side is <=2048 px.
    Returns the path to upload plus original/final dimensions. Never modifies the original."""
    from PIL import Image
    if path.stat().st_size > MAX_PHOTO:
        raise ValueError('Chat photo must be at most 10 MiB')
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(path) as probe:
            fmt, size = probe.format, probe.size
            probe.verify()
    except (Image.DecompressionBombError, Image.UnidentifiedImageError, OSError, SyntaxError):
        raise ValueError('File is not a readable JPEG/PNG image') from None
    if fmt not in FORMATS:
        raise ValueError(f'Chat photo must be JPEG or PNG, not {fmt}')
    result = {'format': fmt, 'original_size': list(size), 'size': list(size), 'resized': False, 'path': path}
    if max(size) <= MAX_SIDE:
        return result
    with Image.open(path) as image:
        image.load()
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
        target = workdir / ('photo-2048' + FORMATS[fmt])
        if fmt == 'JPEG':
            image.convert('RGB').save(target, 'JPEG', quality=92, optimize=True)
        else:
            image.save(target, 'PNG', optimize=True)
        result.update(size=list(image.size), resized=True, path=target)
    return result


def can_change_info(entity) -> bool:
    if not isinstance(entity, (types.Channel, types.Chat)):
        raise ValueError('Chat photos can be changed only for groups and channels')
    if getattr(entity, 'left', False) or getattr(entity, 'deactivated', False):
        return False
    rights = getattr(entity, 'admin_rights', None)
    return bool(entity.creator or (rights and rights.change_info))


async def current_photo(client, entity):
    """-> (photo id or None). Reads full_chat.chat_photo freshly from Telegram."""
    if isinstance(entity, types.Channel):
        full = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
    else:
        full = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
    photo = getattr(full, 'chat_photo', None)
    return photo.id if isinstance(photo, types.Photo) else None


async def apply_photo(client, entity, photo):
    try:
        if isinstance(entity, types.Channel):
            await client(functions.channels.EditPhotoRequest(entity, photo))
        else:
            await client(functions.messages.EditChatPhotoRequest(entity.id, photo))
    except errors.ChatNotModifiedError:
        pass


async def wait_photo(client, entity, check, attempts=5):
    for attempt in range(attempts):
        photo_id = await current_photo(client, entity)
        if check(photo_id):
            return photo_id
        if attempt < attempts - 1:
            await asyncio.sleep(.8)
    raise RuntimeError('Chat photo change not confirmed; reread the chat before retrying')


def register_chat_photo_tools(mcp, private, telegram, checked_entity, mutate_once, chat_record):
    from attachments import UploadStore
    store = UploadStore(private / 'uploads')
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

    async def checked_group(client, chat_id, expected_chat):
        entity, record = await checked_entity(client, chat_id, expected_chat)
        if not can_change_info(entity):
            raise ValueError('Account must be the creator or an admin with change_info rights; no change made')
        return entity, record

    @mcp.tool(annotations=write)
    async def set_chat_photo(chat_id: int, expected_chat: str, upload_id: str, request_id: str) -> dict:
        """Set a group/supergroup/channel avatar ONLY on the owner's explicit request for this exact
        chat and image. expected_chat is the exact chat title (checked like edit_chat_info).
        Stage the image first with begin_attachment_upload/append_attachment_upload: JPEG or PNG,
        at most 10 MiB; a side longer than 2048 px is shrunk. Requires creator or admin change_info.
        The same request_id prevents a repeated change. full_chat.chat_photo is read back; verified=true."""
        path, meta = store.verified(upload_id)
        image = await asyncio.to_thread(prepare_image, path, path.parent)
        async with telegram() as client:
            entity, record = await checked_group(client, chat_id, expected_chat)
            async def change():
                before = await current_photo(client, entity)
                # Staged uploads are stored as 'data'; Telegram needs a photo extension (PHOTO_EXT_INVALID).
                uploaded = await client.upload_file(str(image['path']), file_name='photo' + FORMATS[image['format']])
                await apply_photo(client, entity, types.InputChatUploadedPhoto(file=uploaded))
                photo_id = await wait_photo(client, entity, lambda p: p is not None and p != before)
                return {'chat': record, 'photo_id': photo_id, 'previous_photo_id': before,
                        'filename': meta['filename'], 'sha256': meta['sha256'], 'format': image['format'],
                        'original_size': image['original_size'], 'size': image['size'],
                        'resized': image['resized'], 'verified': True}
            return await mutate_once(request_id, ['chat_photo', chat_id, upload_id, meta['sha256']], change)

    @mcp.tool(annotations=write)
    async def delete_chat_photo(chat_id: int, expected_chat: str, request_id: str) -> dict:
        """Remove a group/channel avatar ONLY on the owner's explicit request for this exact chat.
        expected_chat is the exact chat title. Requires creator or admin change_info. The same
        request_id prevents a repeat. full_chat.chat_photo is read back as empty; verified=true."""
        async with telegram() as client:
            entity, record = await checked_group(client, chat_id, expected_chat)
            async def change():
                before = await current_photo(client, entity)
                if before is not None:
                    await apply_photo(client, entity, types.InputChatPhotoEmpty())
                    await wait_photo(client, entity, lambda p: p is None)
                return {'chat': record, 'previous_photo_id': before, 'had_photo': before is not None,
                        'verified': True}
            return await mutate_once(request_id, ['delete_chat_photo', chat_id], change)

    async def bot_photo_id(client, bot):
        result = await client(functions.photos.GetUserPhotosRequest(bot, offset=0, max_id=0, limit=1))
        photos = [p for p in result.photos if isinstance(p, types.Photo)]
        return photos[0].id if photos else None

    @mcp.tool(annotations=write)
    async def set_bot_photo(bot_username: str, upload_id: str, request_id: str) -> dict:
        """Set the profile photo of a bot OWNED by this account (BotFather owner), ONLY on the owner's
        explicit request for this exact bot and image. bot_username is the exact @username; the bot
        must appear in bots.getAdminedBots. Same image rules as set_chat_photo (JPEG/PNG <=10 MiB,
        >2048 px shrunk). Same request_id prevents a repeat. The bot's newest photo is read back."""
        if not re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}', bot_username or '') or not bot_username.lower().endswith('bot'):
            raise ValueError('Use the exact bot @username')
        username = bot_username.lstrip('@').casefold()
        path, meta = store.verified(upload_id)
        image = await asyncio.to_thread(prepare_image, path, path.parent)
        async with telegram() as client:
            owned = (await client(functions.bots.GetAdminedBotsRequest())) or []
            bot = next((u for u in owned if (u.username or '').casefold() == username), None)
            if bot is None or not bot.bot:
                raise ValueError('This account does not own that bot; no change made')
            record = {'user_id': bot.id, 'username': bot.username, 'name': bot.first_name}
            async def change():
                before = await bot_photo_id(client, bot)
                uploaded = await client.upload_file(str(image['path']), file_name='photo' + FORMATS[image['format']])
                await client(functions.photos.UploadProfilePhotoRequest(bot=bot, file=uploaded))
                for attempt in range(5):
                    photo_id = await bot_photo_id(client, bot)
                    if photo_id is not None and photo_id != before:
                        return {'bot': record, 'photo_id': photo_id, 'previous_photo_id': before,
                                'filename': meta['filename'], 'sha256': meta['sha256'], 'format': image['format'],
                                'size': image['size'], 'resized': image['resized'], 'verified': True}
                    if attempt < 4:
                        await asyncio.sleep(.8)
                raise RuntimeError('Bot photo change not confirmed; reread the bot profile before retrying')
            return await mutate_once(request_id, ['bot_photo', bot.id, upload_id, meta['sha256']], change)

    return set_chat_photo, delete_chat_photo, set_bot_photo
