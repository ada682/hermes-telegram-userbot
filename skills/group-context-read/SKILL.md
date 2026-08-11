---
name: group-context-read
description: Use when replying in a GROUP chat and you need to understand what members are currently discussing (who's talking about what, ongoing topic, member dynamics). Read the chat's history.md to grasp group context before replying. NOT for private chats — private context comes from the wake-up message and your own session.
---

# Group Context Read

The chat's rolling history lives at `history.md` inside this sandbox's Hermes home (auto-recorded by the userbot — raw messages, FIFO, ~100 lines, with the 👑/🛡️ owner/admin/title header pinned at the top). This skill tells you WHEN to read it and HOW.

## When to read (triggers)

Read `history.md` when replying in a GROUP and:

- Members are discussing a topic you don't have full context on (who said what, what's the ongoing thread)
- Someone references an earlier part of the conversation without repeating it ("itu tadi gimana", "kayak yang barusan", "yang tadi")
- You need to know who's who (member titles, roles, in-jokes) to reply appropriately
- Multiple users are talking at once and you need the full picture before responding

Do NOT read for:

- **Private chats** — context comes from the wake-up message + your own session
- Clear, self-contained wake-up requests — no history needed
- Casual chatter that doesn't need context

## How to read

1. Locate the history file: it lives in the sandbox home for this chat — `~/.hermes/history.md` relative to this session's Hermes home (groups: `{sandbox}/groups/<id>/.hermes/history.md`, private: `{sandbox}/users/<id>/.hermes/history.md`).
2. Read it with the `read_file` tool (or `tail` in terminal).
3. If you need something specific, search past chat with the history-search service on port 9101.
4. Reply using the recovered context — never invent facts about members or past events.
5. Don't dump the whole history into your reply — use it as context only.

## Format notes

- The header (👑/🛡️ — owner, admins, custom member titles like "playa [kocokers]") is pinned at the TOP and never rotates — member titles live there.
- The file is FIFO ~100 lines — recent messages at the bottom.
- Lines look like: `[dd/mm hh:mm](reply ke Nama) Nama: pesan`

## Pitfalls

- Only read when the group context matters — reading every single time wastes time and tokens.
- Never rewrite/truncate/rotate history.md — the userbot owns the file.
- Never append raw messages here — the userbot already records those. Use the chat-history skill for durable insights instead.
