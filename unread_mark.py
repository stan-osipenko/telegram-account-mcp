"""Автопометка «непрочитано» (1.8.0, 23.11.2026, по просьбе Стэна).

После каждого сообщения, отправленного от аккаунта Стэна через коннектор (send_message, send_file,
forward_messages, edit_message), чат помечается вручную непрочитанным (синяя точка в Telegram),
чтобы Стэн видел, где писал Claude, и проверял. Прочтения не трогает (ни своих, ни чужих).
Ошибка пометки не ломает отправку: сообщение уже ушло, в ответе будет unread_mark_error.
"""
from __future__ import annotations

from telethon import functions, types


async def flag_unread(client, entity) -> dict:
    try:
        peer = types.InputDialogPeer(await client.get_input_entity(entity))
        await client(functions.messages.MarkDialogUnreadRequest(peer=peer, unread=True))
        return {'unread_mark': True}
    except Exception as exc:  # отправка уже состоялась — не валим её
        return {'unread_mark': False, 'unread_mark_error': f'{type(exc).__name__}: {str(exc)[:200]}'}
