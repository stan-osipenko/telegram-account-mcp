# telegram-account-mcp

MCP server that lets an AI assistant (Claude, ChatGPT and other MCP clients) work with **your own Telegram account**, not a bot. It runs as one long-lived process on your server, holds the only Telethon session, and exposes about 37 tools over authenticated streamable HTTP.

It is built for daily, real use by one owner. Safety checks are part of each tool: the target is verified before anything is sent, retries are idempotent, quotas are durable, and every write is read back from Telegram.

## What it can do

| Area | Tools |
|---|---|
| Reading | `list_chats`, `get_chat`, `read_messages`, `search_messages`, `get_message`, `get_chat_members`, `search_people`, `get_message_readers`, `list_topics` |
| Files | `read_attachment`, `view_attachment` (native image content), `forward_messages`, `begin_attachment_upload` → `append_attachment_upload` → `send_file` (also `as_voice=true` for voice messages); `upload_attachment_from_url` lets the VPS download a file from an allowlisted HTTPS link (ElevenLabs by default, more via `url_upload_hosts` in `instance.json`), optionally `convert="voice"` → OGG/Opus |
| Writing | `send_message` (scheduled sends, mentions without usernames, forum topics), `edit_message`, `delete_messages`, `set_reaction`, `pin_message` |
| Dialog state | `mark_chat_unread`, `mark_read`. Reads never mark anything as read |
| Administration | `create_chat` (also forums), `create_topic`, `add_chat_members`, `remove_chat_member`, `create_invite_link`, `edit_chat_info`, `join_chat`, `set_chat_photo`, `delete_chat_photo`, `set_bot_photo` |
| Voice | `transcribe_voice`: Telegram's own transcription (Premium) first, local faster-whisper as fallback |
| **Everything else** | `describe_mtproto` + `invoke_mtproto`: call **any** Telegram API method by name, e.g. `messages.getMessageReactionsList`, `stories.getPeerStories`, `phone.requestCall` |
| Self-improvement | `get_raw_usage_stats` shows which raw methods are used often and are worth turning into dedicated tools |

### Universal MTProto access

Telegram ships new API methods all the time. Writing a dedicated tool for each one never keeps up. `invoke_mtproto` calls any method that the installed Telethon knows, so updating Telethon makes new Telegram features available right away.

- **Reads** (`get*`, `search*`, `check*`, `resolve*`) run at once.
- **Writes** need `confirm_write=true` (only after the owner explicitly asked for that exact action), a `request_id` (deduplication), `expected_chat` when a chat or person is targeted, and stay within a daily quota.
- **Always refused:** login and session keys (`auth.*`), password, account deletion, payments/Stars/gifts, deleting whole chats or history, ownership transfer.
- **Redirected:** actions that have a dedicated tool with stronger checks (`messages.sendMessage` → `send_message`, and so on).
- **Logged:** each call records the method name, kind, outcome and parameter names in a local SQLite file. Values and responses are never logged. `get_raw_usage_stats` turns this into a list of candidates for new dedicated tools.

## Safety model

- Message content from other people is data, never instructions. The server instructions tell the model this.
- Addresses must be a numeric ID, an exact `@username` or `t.me/username`. Bare words, display names and phone numbers are rejected before resolution.
- Every mutation checks `expected_chat` against the resolved chat, and in private chats checks that a message ID really belongs to that chat.
- `request_id` receipts are stored before the Telegram call. A retry returns the original receipt. An uncertain outcome blocks a blind resend.
- Durable quotas cover creating chats, inviting, joining, removing and bulk deleting, as well as raw writes.
- After each send, the chat gets Telegram's manual "unread" mark, so the owner can see where the assistant wrote on their behalf.

## Setup

1. Python 3.11+, then `python -m venv .venv && .venv/bin/pip install -r requirements.txt`.
2. Create `private/` (mode 700) with:
   - `credentials.json`: `{"api_id": ..., "api_hash": "..."}` from https://my.telegram.org;
   - `mcp-token`: a long random bearer token;
   - a Telethon session `private/stan-vps-tg.session`, authorized once interactively.
3. Run `server.py`. It listens on `127.0.0.1:18765/mcp` and requires `Authorization: Bearer <mcp-token>`.
4. To reach it from Claude.ai, run `claude_gateway.py` behind HTTPS (see `deploy/Caddyfile.example`). It uses a separate public token, `private/claude-mcp-token`.
5. Use systemd units like `deploy/*.service.example`.
6. Optional voice fallback: a separate venv with `faster-whisper` and `whisper_transcribe.py` (see `voice.py`).

Only one process may own the Telegram session. Do not start a second Telethon client on a copied session file.

**Updates** come from GitHub: on the server the service directory is a git checkout. `./deploy.sh` fetches, fast-forwards, runs the tests (rolls back on failure), waits for running watcher jobs, restarts the service and prints `get_status`.

`rpc.py TOOL < args.json` calls a tool on the running server from a shell on the same machine.

## Several accounts from one codebase

Every account runs its own copy of the same code: its own process (or container), its own `private/` directory with the Telegram session, tokens and receipts, and its own `private/instance.json`. The code has nothing account-specific in it. See `instance.py` for the settings: service name, session file, `expected_account_id` (the server refuses to start for another account), host and port, allowed hosts and origins, `auto_unread_mark`, and the OAuth gateway values.

A new release is one `git pull` per instance, so every account gets the same features. For instances in Docker containers under supervisord, `deploy/update-container.sh HOST_DIR CONTAINER CONTAINER_DIR PROGRAM...` does fetch, tests inside the container, rollback on failure, restart and `get_status`.

### Access for ChatGPT and other OAuth clients

`oauth_gateway.py` is a public HTTPS front for one owner:
- a web page to log in to Telegram (phone, code, 2FA), opened with a one-time bootstrap link;
- OAuth 2.1 with PKCE and dynamic client registration (`oauth.py`): 1-hour access tokens, rotating 30-day refresh tokens, scope `telegram`, resource bound to `/mcp`;
- a consent page protected by a separate connection password;
- a proxy to the private MCP backend.

`claude_gateway.py` is the simpler alternative: a static bearer token.

## Tests

```sh
.venv/bin/python -m unittest test_raw_mtproto test_voice test_topics test_admin_tools \
  test_attachments test_mentions test_edit_message test_dialog_state test_chat_photo
```

The tests use fake Telegram clients and temporary state. They need no network, credentials or real sends.

## Contributing

Issues and pull requests are welcome, especially new dedicated tools for frequently used raw methods, and better voice and calls support.

## License

MIT
