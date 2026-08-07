"""Login sekali jalan — bikin session file yang dipakai main.py."""
import asyncio

from telethon import TelegramClient

import config


async def main():
    if not config.API_ID or not config.API_HASH:
        print("❌ Isi dulu TG_API_ID dan TG_API_HASH di file .env")
        print("   Ambil dari https://my.telegram.org → API development tools")
        return

    client = TelegramClient(config.SESSION_FILE, config.API_ID, config.API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Login OK: {me.first_name} (@{me.username})")
    print(f"   Session tersimpan: {config.SESSION_FILE}.session")
    print("   Sekarang tinggal jalankan: python3 main.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
