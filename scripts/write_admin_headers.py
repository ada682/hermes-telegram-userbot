#!/usr/bin/env python3
"""
write_admin_headers.py — tulis header 👑/🛡️ owner+admin ke history.md
semua grup aktif. Fix: header gak pernah ke-tulis (bug main.py) → agent
gak tau siapa admin. Run: python3 write_admin_headers.py
"""
import asyncio, os, glob
from telethon import TelegramClient
from telethon.tl.types import ChannelParticipantsAdmins

API_ID = 20044546
API_HASH = "2b742a458bb50efa080ff95cb25c1a09"
SESSION = "/root/telegram-userbot/kinji_userbot_mod.session"
GROUPS_DIR = "/srv/ubox/groups"

async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("session gak authorized")
        return

    # semua folder grup yang punya .hermes/history.md
    targets = []
    for hp in glob.glob(f"{GROUPS_DIR}/*/.hermes/history.md"):
        gid = os.path.basename(os.path.dirname(os.path.dirname(hp)))
        targets.append((gid, hp))

    done = 0
    for gid, hp in sorted(targets):
        chat_id = int(gid)
        try:
            ent = await client.get_entity(chat_id)
            is_group = getattr(ent, "participants_count", None) is not None or getattr(ent, "megagroup", False)
            if not is_group:
                continue
            try:
                admins = await client.get_participants(chat_id, filter=ChannelParticipantsAdmins())
            except Exception:
                _all = await client.get_participants(chat_id)
                admins = [u for u in _all if getattr(u, "participant", None)
                          and type(u.participant).__name__ in ("ChannelParticipantAdmin", "ChannelParticipantCreator")]
            owner_name, admin_list = "", []
            for a in admins:
                aname = getattr(a, "first_name", "") or str(getattr(a, "id", ""))[:8]
                aid = getattr(a, "id", 0)
                auname = getattr(a, "username", None)
                aid_s = f" (id:{aid}{', @'+auname if auname else ''})" if aid else ""
                aname_full = f"{aname}{aid_s}"
                rank = ""
                try:
                    rank = getattr(a.participant, "rank", "") or ""
                except Exception:
                    pass
                try:
                    is_creator = type(a.participant).__name__.endswith("Creator")
                except Exception:
                    is_creator = False
                if is_creator:
                    owner_name = aname_full
                else:
                    admin_list.append(f"{aname_full}{(' [' + rank + ']') if rank else ''}")
            parts = []
            if owner_name:
                parts.append(f"👑 Owner grup: {owner_name}")
            if admin_list:
                parts.append("🛡️ Admin: " + ", ".join(admin_list))
            res = "\n".join(parts)
            if not res:
                continue
            with open(hp, encoding="utf-8") as f:
                lines = f.read().splitlines()
            hb = [x for x in lines if x.startswith(("👑", "🛡️"))]
            new_header = res.splitlines()
            # merge: keep yang belum ada
            merged = hb + [x for x in new_header if x not in hb]
            body = [x for x in lines if not x.startswith(("👑", "🛡️"))]
            with open(hp, "w", encoding="utf-8") as f:
                f.write("\n".join(merged + body) + "\n")
            print(f"OK {gid}: {len(merged)} header lines ({len(admins)} admin)")
            done += 1
        except Exception as e:
            print(f"SKIP {gid}: {type(e).__name__}: {e}")
    print(f"\nDONE — {done} grup di-update")
    await client.disconnect()

asyncio.run(main())
