#!/usr/bin/env python3
"""watchdog: pantau pane grup staff, kalau agent minta continue (error/stuck/idle lama),
kirim 'sonnet continue' pake session userbot otomatis. jalan tiap 5 menit via cron.
Gak butuh restart userbot — panggil telethon langsung dari script ini."""
import subprocess, re, sys, os, time

PANES = ["ub_5377245777"]
CONTINUE_MSG = "sonnet continue"
COOLDOWN_FILE = "/tmp/staff_continue_cooldown"
COOLDOWN_SECS = 1800  # min 30 menit antar kirim otomatis

def pane_text(pane):
    try:
        out = subprocess.run(["tmux", "-S", "/tmp/tmux-997/default", "capture-pane", "-t", pane, "-p"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout or ""
    except Exception:
        return ""

def send_via_userbot(msg, chat_id):
    """kirim pesan pake session telethon userbot (kinji_userbot.session)"""
    env = {}
    for line in open("/root/telegram-userbot/.env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    for line in open("/etc/userbot.env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env.setdefault(k, v)
    code = f'''
import asyncio, os
from telethon import TelegramClient
async def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    client = TelegramClient(os.environ["SESSION_FILE"], api_id, api_hash)
    await client.start()
    await client.send_message({chat_id}, "{msg}")
    await client.disconnect()
asyncio.run(main())
'''
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=90,
                       env={**os.environ, **env})
    return r.returncode == 0

def main():
    now = time.time()
    if os.path.exists(COOLDOWN_FILE):
        try:
            if now - float(open(COOLDOWN_FILE).read().strip()) < COOLDOWN_SECS:
                return  # masih cooldown
        except Exception:
            pass

    for pane in PANES:
        t = pane_text(pane)
        # pola minta continue / error / stuck
        patterns = [
            r"sonnet continue", r"kirim.*continue", r"butuh.*instruksi",
            r"proxy.*ab[is]s", r"semua.*block", r"gagal.*beruntun",
            r"traceback", r"exception",
            r"nunggu", r"mentok", r"stuck", r"idle",
        ]
        hit = False
        for p in patterns:
            if re.search(p, t, re.IGNORECASE):
                hit = True
                break
        if not hit:
            continue
        # cek emang lagi gak aktif (ada proses jalan baru = masih kerja)
        if re.search(r"deliberating|analyzing|preparing terminal|deepseek-v4-flash.*⏱", t):
            continue
        ok = send_via_userbot(CONTINUE_MSG, "-1005377245777")
        if ok:
            open(COOLDOWN_FILE, "w").write(str(now))
            print(f"continue dikirim ke {pane}")
            return
        print("kirim gagal")

if __name__ == "__main__":
    main()
