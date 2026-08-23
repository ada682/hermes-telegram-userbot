#!/usr/bin/env python3
"""
mod_actions.py — Moderation actions executor untuk userbot (KINJI).

Dijalankan oleh agent (hermes) via terminal untuk eksekusi aksi moderasi:
  - mute <chat_id> <user_id>            → mute PERMANEN user di grup
  - delete_all <chat_id> <user_id>      → hapus SEMUA pesan user di grup
  - kick <chat_id> <user_id>            → kick user dari grup
  - unmute <chat_id> <user_id>          → balikin izin user (unmute)
  - unban <chat_id> <user_id>           → unban (balikin yang ke-kick/ban)
  - is_admin <chat_id> <user_id>        → cek user admin/owner? (YES/NO)
  - whoami                              → info session

PAKAI SALINAN SESSION (MOD_SESSION) — bukan kinji_userbot.session asli, biar
gak bentrok sama userbot yang lagi jalan.

Rule:
  - Cuma OWNER (1118770958) + ADMIN grup yang boleh bikin rule (cek is_admin).
  - Aksi jalan otomatis pas pattern match (agent yang deteksi & panggil script ini).
"""

import asyncio
import os
import sys
import shutil

# ── konfigurasi ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TG_API_ID = int(os.environ.get("TG_API_ID", "20044546"))
TG_API_HASH = os.environ.get("TG_API_HASH", "2b742a458bb50efa080ff95cb25c1a09")
SESSION_PATH = os.environ.get("MOD_SESSION", os.path.join(BASE_DIR, "kinji_userbot_mod.session"))
OWNER_ID = 1118770958

# kalau salinan session belum ada, salin dari yang asli
_MAIN_SESSION = "/root/telegram-userbot/kinji_userbot.session"
if not os.path.exists(SESSION_PATH) and os.path.exists(_MAIN_SESSION):
    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    shutil.copy2(_MAIN_SESSION, SESSION_PATH)
    print(f"[mod] session disalin dari {_MAIN_SESSION} → {SESSION_PATH}", flush=True)


async def _client():
    from telethon import TelegramClient
    client = TelegramClient(SESSION_PATH, TG_API_ID, TG_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("session tidak authorized — butuh login ulang")
    return client


async def _is_admin(client, chat_id: int, user_id: int) -> bool:
    """Cek user admin/owner/creator di grup."""
    if user_id == OWNER_ID:
        return True
    try:
        from telethon.tl.functions.channels import GetParticipantRequest
        from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
        from telethon.errors import UserNotParticipantError
        p = await client(GetParticipantRequest(chat_id, user_id))
        if isinstance(p.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
            return True
        return False
    except UserNotParticipantError:
        return False
    except Exception as e:
        print(f"[mod] is_admin error: {type(e).__name__}: {e}", flush=True)
        return False


async def _is_owner(client, user_id: int) -> bool:
    return user_id == OWNER_ID


async def cmd_mute(client, chat_id, user_id):
    from telethon.tl.functions.messages import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    # mute permanen per-user = EditBannedRequest (bukan EditChatDefaultBannedRights
    # — itu ngubah default SELURUH GRUP, bahaya!)
    rights = ChatBannedRights(
        until_date=None,
        send_messages=True,
        send_media=True,
        send_stickers=True,
        send_gifs=True,
        send_games=True,
        send_inline=True,
        send_polls=True,
        invite_users=True,
        pin_messages=True,
        change_info=True,
    )
    await client(EditBannedRequest(chat_id, user_id, rights))
    print(f"[mod] MUTE permanen: user={user_id} di chat={chat_id}", flush=True)


async def cmd_delete_all(client, chat_id, user_id, limit=10000):
    """Hapus SEMUA pesan user di grup (dari awal sampe sekarang, max limit)."""
    deleted = 0
    batch = []
    async for msg in client.iter_messages(chat_id, from_user=user_id, limit=limit):
        batch.append(msg.id)
        if len(batch) >= 100:
            await client.delete_messages(chat_id, batch)
            deleted += len(batch)
            batch = []
            await asyncio.sleep(0.3)  # anti rate-limit
    if batch:
        await client.delete_messages(chat_id, batch)
        deleted += len(batch)
    print(f"[mod] DELETE_ALL: user={user_id} di chat={chat_id} → {deleted} pesan dihapus", flush=True)


async def cmd_kick(client, chat_id, user_id):
    await client.kick_participant(chat_id, user_id)
    print(f"[mod] KICK: user={user_id} dari chat={chat_id}", flush=True)


async def cmd_unmute(client, chat_id, user_id):
    from telethon.tl.functions.messages import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    rights = ChatBannedRights(until_date=None, send_messages=False)
    await client(EditBannedRequest(chat_id, user_id, rights))
    print(f"[mod] UNMUTE: user={user_id} di chat={chat_id}", flush=True)


async def cmd_unban(client, chat_id, user_id):
    from telethon.tl.functions.messages import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    rights = ChatBannedRights(until_date=None)
    await client(EditBannedRequest(chat_id, user_id, rights))
    print(f"[mod] UNBAN: user={user_id} di chat={chat_id}", flush=True)


async def main():
    if len(sys.argv) < 2:
        print("usage: mod_actions.py <cmd> [args...]", flush=True)
        print("  mute <chat_id> <user_id>", flush=True)
        print("  delete_all <chat_id> <user_id>", flush=True)
        print("  kick <chat_id> <user_id>", flush=True)
        print("  unmute <chat_id> <user_id>", flush=True)
        print("  unban <chat_id> <user_id>", flush=True)
        print("  is_admin <chat_id> <user_id>", flush=True)
        print("  whoami", flush=True)
        sys.exit(1)

    cmd = sys.argv[1]
    client = await _client()
    try:
        if cmd == "whoami":
            me = await client.get_me()
            print(f"[mod] session as: {me.first_name} (@{me.username or '?'}) id={me.id}", flush=True)
        elif cmd == "is_admin":
            chat_id = int(sys.argv[2]); user_id = int(sys.argv[3])
            ok = await _is_admin(client, chat_id, user_id)
            print(f"YES" if ok else "NO", flush=True)
        elif cmd == "mute":
            await cmd_mute(client, int(sys.argv[2]), int(sys.argv[3]))
        elif cmd == "delete_all":
            await cmd_delete_all(client, int(sys.argv[2]), int(sys.argv[3]))
        elif cmd == "kick":
            await cmd_kick(client, int(sys.argv[2]), int(sys.argv[3]))
        elif cmd == "unmute":
            await cmd_unmute(client, int(sys.argv[2]), int(sys.argv[3]))
        elif cmd == "unban":
            await cmd_unban(client, int(sys.argv[2]), int(sys.argv[3]))
        else:
            print(f"unknown cmd: {cmd}", flush=True)
            sys.exit(1)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
