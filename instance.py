"""Per-instance settings, so one codebase serves several Telegram accounts.

Each running copy (one per account) keeps its own private directory with the Telegram session,
tokens, receipts, and `instance.json`. Nothing account-specific lives in the code.

private directory: env TG_MCP_PRIVATE, otherwise ./private next to the code.

instance.json (every key is optional):
{
  "name": "My_Telegram",            # service name shown to clients and in get_status
  "session": "telegram",            # Telethon session file name inside private/
  "expected_account_id": 123,       # refuse to serve if the session belongs to another account
  "host": "127.0.0.1", "port": 18765,
  "allowed_hosts": [...], "allowed_origins": [...],   # MCP DNS-rebinding protection (public servers)
  "auto_unread_mark": false,        # mark a chat unread after each send, so the owner reviews it
  "owner_label": "владельца",       # genitive form for the OAuth setup and consent pages
  "public_url": "https://...",      # OAuth gateway: public base URL
  "gateway_port": 18786,            # OAuth gateway port
  "mcp_program": "mcp",             # supervisor program the OAuth gateway starts after login
  "setup_cookie": "__Host-tgmcp-setup",
  "url_upload_hosts": ["cdn.example.com"]  # extra hosts for upload_attachment_from_url
}
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
PRIVATE = Path(os.environ.get('TG_MCP_PRIVATE', BASE / 'private'))

DEFAULTS = {
    'name': 'telegram-account-mcp',
    'session': 'telegram',
    'expected_account_id': None,
    'host': '127.0.0.1',
    'port': 18765,
    'allowed_hosts': None,
    'allowed_origins': None,
    'auto_unread_mark': False,
    'owner_label': 'владельца',
    'public_url': None,
    'gateway_port': 18786,
    'mcp_program': 'mcp',
    'setup_cookie': '__Host-tgmcp-setup',
    'url_upload_hosts': None,
}


def load(private: Path = PRIVATE) -> dict:
    path = private / 'instance.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ValueError(f'Unknown keys in {path}: {sorted(unknown)}')
    return {**DEFAULTS, **data}


CONFIG = load()
