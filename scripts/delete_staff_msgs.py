#!/usr/bin/env python3
"""hapus SEMUA pesan di grup staff (-5377245777). butuh admin + permission delete messages."""
import asyncio
import os

from telethon import TelegramClient
from telethon.tl.functions.messages import DeleteHistoryRequest

API_ID = 20044546
API_HASH = "2b742a458bb50efa080ff95cb25c1a09"
SESSION_FILE = os.environ.get("SESSION_FILE", "kinji_userbot")
CHAT_ID = -5377245777  # grup staff


async def main():
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"login sebagai: {me.first_name} ({me.id})")

    chat = await client.get_entity(CHAT_ID)
    print(f"target: {chat.title} ({chat.id})")

    # cek admin
    try:
        part = await client.get_permissions(chat, me.id)
        print(f"is_admin: {part.is_admin}, delete_messages: {part.delete_messages}")
    except Exception as e:
        print(f"cek permission error: {e}")

    # hapus SEMUA history grup (semua orang)
    try:
        result = await client(DeleteHistoryRequest(
            peer=chat,
            max_id=0,
            just_clear=False,
            revoke=True,
        ))
        print(f"SELESAI: history grup dihapus total. result: {result}")
    except Exception as e:
        print(f"DeleteHistoryRequest gagal: {e}")
        print("fallback: hapus pesan userbot sendiri aja...")
        my_ids = []
        async for msg in client.iter_messages(chat, limit=None):
            if msg.sender_id == me.id:
                my_ids.append(msg.id)
        print(f"pesan userbot: {len(my_ids)}")
        for i in range(0, len(my_ids), 100):
            await client.delete_messages(chat, my_ids[i:i + 100])
            await asyncio.sleep(0.3)
        print(f"SELESAI fallback: {len(my_ids)} pesan userbot dihapus")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
