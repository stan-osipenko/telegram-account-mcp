# How to write in Telegram on the owner's behalf

Any assistant that sends messages through this connector reads this guide (tool `get_messaging_guide`).
It says which Telegram features to use by default, so new features show up in everyday messages
without the owner asking for each one. The weekly adoption agent adds new features to the log at the end.

These are defaults for messages the owner already asked for. A format never authorizes a message:
whom to write, what to say and when still come only from the owner.

## Defaults

- **Match the chat.** Short, informal chats get short plain messages. Do not turn a one-line reply into a report.
- **Tables for comparisons and numbers.** Two or more items compared on two or more attributes, money by line, schedules, statuses: send a rich message with a table (`send_rich_message`, or `send_message` with `parse_mode='rich_markdown'`).
- **Headings and lists for long answers.** A summary, a report or instructions with several steps: rich message with a heading, short paragraphs and a list or numbered list.
- **Corrections in place.** Fix a mistake in an own message with `edit_message` / `edit_rich_message` instead of deleting and resending. Rich messages are edited in place too.
- **Reactions instead of empty replies.** When the owner only wants to acknowledge ("ok", "seen", "thanks"), use `set_reaction` instead of a text message.
- **Links as text with a label** in rich messages (`[site name](https://...)`), not bare long URLs, unless the chat is plain.
- **Voice messages in the owner's voice** only when the owner asks for a voice message.
- **Never** use a new feature that changes who can see something, costs money (Stars, gifts, paid media) or affects other people's accounts without the owner's explicit request.

## Feature log

New Telegram features the connector supports, newest first. Each entry: date, feature, how to use it, when it is a good default.

- 2026-11-28 · **Rich message editing** · `edit_rich_message` / `edit_message` rich modes · fix tables and reports in place.
- 2026-11-27 · **Rich messages (Telegram editor format)** · `send_rich_message` / `parse_mode='rich_markdown'` · tables, headings, lists, quotes, code; default for structured content.
