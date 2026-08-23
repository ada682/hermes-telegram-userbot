"""
moderation.py — rule engine moderasi userbot (GRATIS — TANPA LLM).

Dipanggil dari main.py on_incoming SEBELUM _run_agent:
  kalau pesan match rule → eksekusi action (mute/delete_all/kick) langsung
  via client → return True (skip agent, 0 credit).
Kalau gak match → return False (lanjut ke agent normal).

Rule dibuat oleh AGENT (lewat skill group-moderation) atas instruksi
admin/owner grup — disimpen di mod_rules.json per sandbox grup.
"""

import glob
import json
import logging
import os
import re
import time

log = logging.getLogger("moderation")

OWNER_ID = 1118770958
MOD_RULES_GLOB = "/srv/ubox/groups/*/mod/mod_rules.json"

# cache rules: (path, mtime) → rules — biar gak baca file tiap pesan
_cache = {}


def _load_rules(path: str) -> list:
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return []
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules", []) or []
        _cache[path] = (mtime, rules)
        return rules
    except Exception:
        return []


def _rules_for_chat(chat_id: int) -> list:
    """Semua rule yang scope-nya chat ini (dari semua sandbox grup)."""
    out = []
    try:
        for p in glob.glob(MOD_RULES_GLOB):
            for r in _load_rules(p):
                if r.get("enabled", True) and int(r.get("scope", 0)) == chat_id:
                    out.append(r)
    except Exception:
        pass
    return out


# stopword umum obrolan — jangan dihitung sebagai keyword (biar pattern
# "kasih modal dong" gak match gara-gara "dong" doang, tapi match karna "modal")
_STOPWORDS = {
    "yang", "dengan", "untuk", "tidak", "gak", "ga", "ngga", "nggak", "bang",
    "kak", "mas", "bro", "koh", "om", "gan", "boss", "jir", "anj", "woy", "woi",
    "dong", "nih", "nihh", "ya", "yah", "deh", "lah", "loh", "kan", "sih", "tuh",
    "aja", "doang", "kalo", "kalau", "karna", "karena", "bisa", "dari", "sama",
    "saya", "aku", "gw", "gua", "lu", "lo", "kamu", "dia", "mereka", "kita",
    "sudah", "udah", "belum", "belom", "mau", "pengen", "pingin", "saja",
}

def _content_words(pattern: str) -> list:
    return [w for w in re.split(r"\W+", pattern) if len(w) >= 3 and w not in _STOPWORDS]


def _match_rule(text: str, rule: dict) -> bool:
    """Cocokin pesan sama pattern rule.

    1) substring langsung (case-insensitive)
    2) keyword overlap: pecah pattern jadi content words (>=3 huruf, bukan
       stopword) — match kalau >=60% keyword muncul di pesan, ATAU minimal
       satu keyword UNIK (len >= 5) muncul.
    """
    pattern = (rule.get("pattern") or "").strip().lower()
    if not pattern:
        return False
    text_l = text.lower()
    # 1) substring langsung
    if pattern in text_l:
        return True
    # 2) keyword overlap
    words = _content_words(pattern)
    if not words:
        return False
    hits = sum(1 for w in words if w in text_l)
    if hits / len(words) >= 0.6:
        return True
    # 3) keyword unik (panjang >= 5) — satu doang cukup buat match
    #    ("modal" di "kasih modal dong" → "modal dikit bang" MATCH)
    if any(len(w) >= 5 and w in text_l for w in words):
        return True
    return False


async def check_and_execute(client, event) -> bool:
    """Cek rules buat event ini. Kalau match → eksekusi → return True.

    client: Telethon client (dari main.py)
    event:  NewMessage event
    """
    chat_id = getattr(event, "chat_id", 0)
    if not chat_id or chat_id < 0:  # cuma grup/chanel negatif
        rules = _rules_for_chat(chat_id)
        if not rules:
            return False
    else:
        return False

    text = (event.raw_text or "").strip()
    if not text:
        return False
    sender_id = 0
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", 0)
    except Exception:
        pass
    if sender_id == OWNER_ID:
        return False  # jangan pernah moderasi owner

    for rule in rules:
        # ── FIX (23 Agu): rule khusus FORWARD dari channel — cuma jalan
        # kalau pesan ini emang di-forward dari channel, bukan ketik langsung.
        if rule.get("forward_from_channel"):
            fwd = getattr(event, "forward", None)
            is_fwd_channel = False
            try:
                if fwd:
                    fid = getattr(fwd, "from_id", None)
                    # forward dari channel → PeerChannel (punya channel_id)
                    if fid is not None and hasattr(fid, "channel_id"):
                        is_fwd_channel = True
                    # fallback: chat_id di header forward = channel asal
                    elif getattr(fwd, "chat_id", None) is not None:
                        is_fwd_channel = True
            except Exception:
                pass
            if not is_fwd_channel:
                continue
        if not _match_rule(text, rule):
            continue
        # target = sender pesan ini
        if sender_id == 0:
            return False
        # ── FIX (19 Agu): proteksi admin cuma buat action BERAT (kick/mute) —
        # delete pesan TETEP JALAN buat admin juga. Owner minta "hapus chat
        # yang bilang X" → pesan admin yang bilang X harus tetep ke-hapus.
        # Dulu: semua action di-skip kalau sender admin → pesan admin lolos.
        actions = rule.get("action") or []
        _need_admin_skip = any(a in ("kick", "mute") for a in actions)
        if _need_admin_skip:
            try:
                from telethon.tl.functions.channels import GetParticipantRequest
                from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
                p = await client(GetParticipantRequest(chat_id, sender_id))
                if isinstance(p.participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
                    log.info("mod: skip admin user=%s chat=%s (rule match tapi admin, action berat)", sender_id, chat_id)
                    return False
            except Exception:
                pass
        # ── EKSEKUSI ──
        log.info("mod: RULE MATCH chat=%s user=%s text=%.50s actions=%s",
                 chat_id, sender_id, text, actions)
        for act in actions:
            try:
                if act == "delete_all":
                    await _delete_all(client, chat_id, sender_id)
                elif act == "mute":
                    await _mute(client, chat_id, sender_id)
                elif act == "kick":
                    await client.kick_participant(chat_id, sender_id)
                    log.info("mod: kicked user=%s chat=%s", sender_id, chat_id)
                elif act == "unmute":
                    await _unmute(client, chat_id, sender_id)
                elif act == "unban":
                    await _unban(client, chat_id, sender_id)
            except Exception as e:
                log.warning("mod: action %s gagal user=%s: %s", act, sender_id, e)
        # hapus pesan pemicu (selalu — biar chat bersih)
        try:
            await client.delete_messages(chat_id, event.id)
        except Exception:
            pass
        return True
    return False


async def _delete_all(client, chat_id, user_id, limit=10000):
    deleted = 0
    batch = []
    async for msg in client.iter_messages(chat_id, from_user=user_id, limit=limit):
        batch.append(msg.id)
        if len(batch) >= 100:
            await client.delete_messages(chat_id, batch)
            deleted += len(batch)
            batch = []
            import asyncio
            await asyncio.sleep(0.3)
    if batch:
        await client.delete_messages(chat_id, batch)
        deleted += len(batch)
    log.info("mod: deleted %s messages user=%s chat=%s", deleted, user_id, chat_id)


async def _mute(client, chat_id, user_id):
    from telethon.tl.functions.messages import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    rights = ChatBannedRights(until_date=None, send_messages=True, send_media=True,
                              send_stickers=True, send_gifs=True, send_games=True,
                              send_inline=True, send_polls=True, invite_users=True,
                              pin_messages=True, change_info=True)
    await client(EditBannedRequest(chat_id, user_id, rights))
    log.info("mod: muted user=%s chat=%s", user_id, chat_id)


async def _unmute(client, chat_id, user_id):
    from telethon.tl.functions.messages import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    rights = ChatBannedRights(until_date=None, send_messages=False)
    await client(EditBannedRequest(chat_id, user_id, rights))


async def _unban(client, chat_id, user_id):
    from telethon.tl.functions.messages import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    rights = ChatBannedRights(until_date=None)
    await client(EditBannedRequest(chat_id, user_id, rights))
