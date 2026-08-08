"""Telegram userbot — auto-reply via OpenAI-compatible LLM backend.

Cara pakai:
  1. python3 login.py        (sekali aja, isi kode OTP)
  2. python3 main.py         (jalanin botnya)

Perintah dari akun sendiri (diketik manual):
  .ub on        → aktifkan auto-reply
  .ub off       → nonaktifkan
  .ub status    → cek status
  .ub reset     → bersihkan konteks chat
"""
import asyncio
import html
import logging
import os
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timedelta

import httpx
from telethon import TelegramClient, events

import config
import hermes_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("userbot")

if not config.API_ID or not config.API_HASH:
    raise SystemExit("❌ Isi TG_API_ID dan TG_API_HASH di .env dulu (my.telegram.org)")
if not config.API_KEY:
    raise SystemExit("❌ API key LLM nggak ketemu. Set UB_API_KEY di .env atau export HERMES_CUSTOM_INFERENCE_API_NOUSRESEARCH_COM_API_KEY")

client = TelegramClient(config.SESSION_FILE, config.API_ID, config.API_HASH)

enabled = True
cooldowns = {}   # (chat_id, user_id) -> datetime terakhir balas
history = {}     # chat_id -> deque[(role, text)]

# Pola perintah destruktif — kalau ketemu, bot nolak tanpa nerusin ke LLM.
# Level HARD: selalu blokir (nggak ada konteks aman).
DANGER_PATTERNS_HARD = [
    r"\brm\s+-?[a-z]*r[a-z]*f",          # rm -rf / rm -fr / rm rf (dengan atau tanpa dash)
    r"\bdd\s+if=/dev/(zero|urandom)",     # dd nulis ke disk
    r":\(\)\s*\{\s*:\|:&",                # fork bomb
    r"\b(base64|cat|curl|wget).*\|.*\b(ba)?sh\b",  # pipe ke shell
    r"\bbash\s+-c\b", r"\bsh\s+-c\b",
    r"chmod\s+-R\s+777\s+/",
    r"\bkill\s+-9\b", r"\bpkill\b", r"\bkillall\b",
    r"\bdocker\s+(rm|rmi|system\s+prune)", r"\bdocker\s+rm\s+-f",
    r"rm\s+-rf\s+/",                      # rm -rf /
]

# Level SOFT: kata perintah yang bisa jadi pertanyaan penjelasan —
# blokir cuma kalau bukan "apa itu X / jelasin X".
DANGER_PATTERNS_SOFT = [
    r"\bshutdown\b", r"\bpoweroff\b", r"\bhalt\b", r"\breboot\b",
    r"\bmkfs\b", r"\bfdisk\b", r"\bparted\b", r"\bformat\s+[cd]:",
    r"\bwipe\b", r"\bshred\b",
]

QUESTION_HINTS = (
    "apa itu", "what is", "what's", "jelasin", "jelaskan",
    "arti", "maksud", "definisi", "tutorial", "cara",
    "apakah", "katanya", "benarkah", "gimana", "bagaimana",
    "kenapa", "mengapa", "is it", "kabar", "tau nggak", "tahu nggak",
    "bener", "betul", "ada yang", "info", "berita",
)

# ── Filter exfil: minta baca/kirim data sensitif (defense-in-depth).
EXFIL_VERBS = (
    "baca", "kirim", "kirimin", "lihat", "tampilkan", "tunjukkan",
    "dump", "leak", "exfil", "grab", "ambil", "read", "send", "show",
)
EXFIL_NOUNS = (
    ".env", "api key", "api_key", "apikey", "private key", "private_key",
    "id_rsa", "seed phrase", "seedphrase", "mnemonic", "wallet",
    "password", "token", "cookie", ".session", "secret", "credential",
    ".ssh", "private", "key",
)

_danger_hard = [re.compile(p, re.IGNORECASE) for p in DANGER_PATTERNS_HARD]
_danger_soft = [re.compile(p, re.IGNORECASE) for p in DANGER_PATTERNS_SOFT]


def is_exfil(text: str) -> bool:
    low = text.lower()
    has_verb = any(v in low for v in EXFIL_VERBS)
    has_noun = any(n in low for n in EXFIL_NOUNS)
    if not (has_verb and has_noun):
        return False
    # Pertanyaan (bukan perintah) → biarin agent yang nilai.
    # Agent udah dilatih nolak permintaan bahaya ("ngerampok digital 😂").
    if text.rstrip().endswith("?"):
        return False
    if any(h in low for h in QUESTION_HINTS):
        return False
    return True


def is_dangerous(text: str) -> bool:
    if any(p.search(text) for p in _danger_hard):
        return True
    if any(p.search(text) for p in _danger_soft):
        low = text.lower()
        if any(h in low for h in QUESTION_HINTS):
            return False  # cuma nanya arti, bukan nyuruh eksekusi
        return True
    return False


def split_reply(text: str, max_chars: int = None) -> list:
    """Pecah reply panjang jadi beberapa pesan (kayak orang ngetik beberapa kali).

    Prioritas: pecah per paragraf → per kalimat → hard split karakter.
    """
    max_chars = max_chars or config.MAX_MSG_CHARS
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    def hard_split(s: str) -> list:
        return [s[i:i + max_chars] for i in range(0, len(s), max_chars)]

    chunks = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        # paragraf kepanjangan → pecah per kalimat
        sentences = []
        buf = ""
        for part in re.split(r"(?<=[.!?])\s+", para):
            if len(buf) + len(part) + 1 <= max_chars:
                buf = (buf + " " + part).strip() if buf else part
            else:
                if buf:
                    sentences.append(buf)
                buf = part
        if buf:
            sentences.append(buf)
        for s in sentences:
            if len(s) <= max_chars:
                chunks.append(s)
            else:
                chunks.extend(hard_split(s))
    return chunks


def _extract_media(text: str):
    """Ambil baris MEDIA:/path dari reply → (teks bersih, list path file)."""
    import re
    paths = re.findall(r"MEDIA:(\S+)", text)
    clean = re.sub(r"\s*MEDIA:\S+", "", text).strip()
    return clean, paths


async def _send_media(event, paths) -> None:
    """Kirim file (MAX 2) yang di-sebut agent via MEDIA:/path."""
    for p in (paths or [])[:2]:
        try:
            if os.path.exists(p):
                await asyncio.wait_for(event.reply(file=p), timeout=30)
                log.info("media terkirim: %s", p)
            else:
                await asyncio.wait_for(
                    event.reply(f"⚠️ File {p} nggak ketemu di sandbox.", link_preview=False),
                    timeout=20,
                )
        except Exception as e:
            log.warning("send media gagal %s: %s", p, e)


async def send_reply(event, reply_text: str) -> None:
    """Kirim reply — pecah jadi beberapa bubble kalau panjang, HTML + fallback plain,
    plus file MEDIA:/path kalau agent nyebutnya."""
    reply_text, media_paths = _extract_media(reply_text)
    chunks = split_reply(reply_text)
    if not chunks:
        return
    for i, chunk in enumerate(chunks):
        try:
            await asyncio.wait_for(
                event.reply(
                    hermes_bridge._sanitize_html(chunk),
                    link_preview=False, parse_mode="html",
                ),
                timeout=20,
            )
            log.info("replied (html) chat=%s chunk=%d/%d", event.chat_id, i + 1, len(chunks))
        except Exception:
            try:
                # fallback plain — strip tag biar nggak liat <b> mentah
                await asyncio.wait_for(
                    event.reply(
                        hermes_bridge._strip_html_tags(chunk),
                        link_preview=False,
                    ),
                    timeout=20,
                )
                log.info("replied (plain fallback) chat=%s chunk=%d/%d", event.chat_id, i + 1, len(chunks))
            except Exception:
                log.warning("send chunk %d gagal chat=%s", i + 1, event.chat_id)
        if i < len(chunks) - 1:
            await asyncio.sleep(config.MSG_DELAY)
    await _send_media(event, media_paths)


def get_history(chat_id):
    if chat_id not in history:
        history[chat_id] = deque(maxlen=config.HISTORY_LEN)
    return history[chat_id]


def on_cooldown(chat_id, user_id=0, cooldown=None) -> bool:
    cooldown = cooldown or config.COOLDOWN
    last = cooldowns.get((chat_id, user_id))
    if last and datetime.utcnow() - last < timedelta(seconds=cooldown):
        return True
    return False


def set_cooldown(chat_id, user_id=0):
    cooldowns[(chat_id, user_id)] = datetime.utcnow()


async def ask_llm(messages) -> str:
    payload = {
        "model": config.MODEL,
        "messages": messages,
        "max_tokens": config.MAX_TOKENS,
        "temperature": config.TEMPERATURE,
    }
    headers = {
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=config.API_TIMEOUT) as http:
        resp = await http.post(f"{config.BASE_URL}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            raise RuntimeError("model returned empty content (reasoning-only?)")
        return content


async def generate_reply(chat_id, user_text) -> str:
    h = get_history(chat_id)
    h.append(("user", user_text))
    messages = [{"role": "system", "content": config.PERSONA}]
    messages += [{"role": role, "content": text} for role, text in h]
    reply = await ask_llm(messages)
    h.append(("assistant", reply))
    return reply


async def _keep_typing(chat_id: int):
    """Renew Telegram typing indicator tiap 4.5 detik (Telegram expire ~5s).
    Pake SetTypingRequest langsung (bukan client.action context manager)
    biar nggak bentrok sama context manager luar & error ke-log."""
    from telethon.tl.functions.messages import SetTypingRequest
    from telethon.tl.types import SendMessageTypingAction
    logged = False
    try:
        while True:
            try:
                await client(
                    SetTypingRequest(peer=chat_id, action=SendMessageTypingAction())
                )
            except Exception as e:
                if not logged:
                    log.warning("keep_typing gagal chat=%s: %s", chat_id, e)
                    logged = True
            await asyncio.sleep(4.5)
    except asyncio.CancelledError:
        pass


def _elevenlabs_tts(text: str, out_ogg: str) -> bool:
    """ElevenLabs TTS → mp3 → ogg (voice note Telegram). Sinkron, jalan di
    thread executor biar nggak nge-block event loop."""
    import requests
    import subprocess

    key = config.ELEVENLABS_API_KEY
    if not key:
        log.warning("ELEVENLABS_API_KEY nggak diset")
        return False
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": config.ELEVENLABS_MODEL,
                "voice_settings": {
                    "stability": config.ELEVENLABS_STABILITY,
                    "similarity_boost": 0.7,
                    "style": config.ELEVENLABS_STYLE,
                    "speaker_boost": config.ELEVENLABS_SPEAKER_BOOST,
                },
                "speed": config.ELEVENLABS_SPEED,
            },
            timeout=30,
        )
        if r.status_code != 200:
            log.warning("ElevenLabs HTTP %s: %.120s", r.status_code, r.text)
            return False
        mp3 = out_ogg + ".mp3"
        with open(mp3, "wb") as f:
            f.write(r.content)
        # API ElevenLabs IGNORE parameter speed di model flash — jadi speed
        # di-handle via ffmpeg atempo (pitch tetep normal). atempo min 0.5,
        # kalau mau lebih pelan, chain beberapa atempo.
        af_filters = [f"volume={config.ELEVENLABS_VOLUME}"]
        spd = config.ELEVENLABS_SPEED
        while spd < 0.5:
            af_filters.append("atempo=0.5")
            spd /= 0.5
        af_filters.append(f"atempo={spd:.3f}")
        subprocess.run(
            ["ffmpeg", "-y", "-i", mp3,
             "-af", ",".join(af_filters),
             "-c:a", "libopus", out_ogg],
            capture_output=True, timeout=30,
        )
        if os.path.exists(out_ogg) and os.path.getsize(out_ogg) > 500:
            return True
        log.warning("ffmpeg gagal ngubah ke ogg")
        return False
    except Exception as e:
        log.warning("ElevenLabs TTS error: %s", e)
        return False


def _vn_speakable(text: str) -> str:
    """Siapin teks buat TTS: strip markdown/HTML + trik pacing natural
    (sisipin jeda '...' biar ada napas, kayak orang beneran)."""
    t = re.sub(r"<[^>]+>", "", text)
    t = re.sub(r"[`*_~>#]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return t
    # teks panjang TANPA tanda baca → sisipin jeda "... " tiap ~70-90 kata
    if len(t) > 100 and not re.search(r"[.,!?…]", t):
        t = re.sub(r"(.{60,90}?)\s", r"\1... ", t)
    if t[-1] not in ".!?…":
        t += "."
    return t[:600]  # cap biar VN nggak kepanjangan


def _is_image_media(event) -> bool:
    """True kalau pesan berisi foto / dokumen gambar."""
    if getattr(event, "photo", None):
        return True
    doc = getattr(event, "document", None)
    if doc:
        return (doc.mime_type or "").startswith("image/")
    return False


async def _ocr_event_image(event) -> str:
    """Download media dari event → OCR (tesseract) → teks. Return '' kalau gagal."""
    try:
        path = await asyncio.wait_for(
            event.download_media(file=f"/tmp/ub_media_{int(time.time() * 1000)}"),
            timeout=25,
        )
        if not path or not os.path.exists(path):
            log.warning("download media gagal")
            return ""
        # resize/upscale biar tesseract lebih akurat (dashboard sering teks kecil)
        try:
            from PIL import Image
            im = Image.open(path)
            if im.width < 1200:
                ratio = 1200 / im.width
                im = im.resize((1200, int(im.height * ratio)), Image.LANCZOS)
            elif im.width > 2400:
                ratio = 2400 / im.width
                im = im.resize((2400, int(im.height * ratio)), Image.LANCZOS)
            im.save(path)
        except Exception:
            pass
        # OCR pake eng+ind (dashboard Indonesia), psm 3 auto
        pr = await asyncio.to_thread(
            subprocess.run,
            ["tesseract", path, "stdout", "-l", "eng+ind", "--psm", "3"],
            capture_output=True, text=True, timeout=90,
        )
        text = (pr.stdout or "").strip()
        try:
            os.remove(path)
        except Exception:
            pass
        return text[:4000]  # cap biar nggak banjiri konteks agent
    except Exception as e:
        log.warning("OCR gagal: %s", e)
        return ""


def _wants_voice_note(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in config.VN_KEYWORDS)


def _extract_quoted_text(text: str):
    """Ambil teks yang ke-quote (\"...\", '...', “...”) buat di-TTS langsung.
    Return None kalau nggak ada quote."""
    import re
    m = re.search(r"[\"“]([^\"”]{1,600})[\"”]", text)
    if m:
        return m.group(1).strip()
    # fallback: setelah kata "teksnya"/"textnya"/"baca ini"
    m = re.search(r"(?:teksnya|textnya|baca ini|bacain)\s*[:\-]?\s*[“\"]?([^\"”]{1,600})[\"”]?", text, re.I)
    if m:
        return m.group(1).strip()
    return None


async def _send_vn(event, chat_id: int, text: str) -> bool:
    """TTS + kirim voice note. Return True kalau sukses."""
    from telethon.tl.types import DocumentAttributeAudio

    try:
        vn_ogg = f"/tmp/ub_vn_{chat_id}_{int(asyncio.get_event_loop().time() * 1000)}.ogg"
        speak = _vn_speakable(text)
        if not speak:
            return False
        ok = await asyncio.to_thread(_elevenlabs_tts, speak, vn_ogg)
        if not ok:
            log.warning("TTS gagal chat=%s", chat_id)
            return False
        # durasi dari ffprobe
        dur = 0
        try:
            pr = await asyncio.to_thread(
                subprocess.run,
                ["ffprobe", "-v", "quiet", "-show_entries",
                 "format=duration", "-of", "csv=p=0", vn_ogg],
                capture_output=True, text=True, timeout=10,
            )
            dur = int(float(pr.stdout.strip().split(",")[0]))
        except Exception:
            pass
        await asyncio.wait_for(
            event.reply(
                file=vn_ogg,
                attributes=[DocumentAttributeAudio(voice=True, duration=dur)],
            ),
            timeout=30,
        )
        log.info("VN terkirim chat=%s dur=%ss", chat_id, dur)
        return True
    except Exception as e:
        log.warning("kirim VN gagal chat=%s: %s", chat_id, e)
        return False


@client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    global enabled
    if not enabled:
        return
    if event.out:
        return

    try:
        sender = await event.get_sender()
        if sender is None:
            return  # service message
        if getattr(sender, "bot", False):
            return  # jangan bales bot (hindari loop)
    except Exception:
        return  # channel private / banned / entity gagal — skip aja

    me = await client.get_me()

    text = event.raw_text or ""
    if not text.strip() and not _is_image_media(event):
        return  # sticker/service tanpa teks & bukan gambar — skip
    if text.startswith(config.CMD_PREFIX):
        return  # perintah admin, ditangani handler lain

    # ── Mode wake word: cuma bales kalau pesan DIAWALI prefix (misal "SONNET")
    if config.REQUIRE_PREFIX:
        stripped = text.strip()
        if not stripped.lower().startswith(config.REQUIRE_PREFIX.lower()):
            return  # bukan wake word — diem
        # buang prefix-nya, agent cuma lihat isi permintaannya
        text = stripped[len(config.REQUIRE_PREFIX):].lstrip(": ,\t")
        if not text.strip():
            return
        called = True
    else:
        # ── Aturan lama: "Apakah dipanggil?"
        try:
            if event.is_private:
                called = True
            else:
                called = False
                if event.mentioned:
                    called = True
                if event.is_reply:
                    reply = await event.get_reply_message()
                    if reply is not None and reply.sender_id == me.id:
                        called = True
                raw = (event.raw_text or "").lower()
                if me.username and f"@{me.username}".lower() in raw:
                    called = True
        except Exception:
            return  # entity gagal — skip
        if not called:
            return

    # ── Media gambar → OCR + path buat VISION agent (kayak Hermes asli: coba liat dulu)
    if _is_image_media(event):
        # 1) simpen di sandbox konteks biar agent bisa LIAT gambarnya (vision attachment)
        sandbox_path = None
        try:
            sender_id = getattr(event.sender_id, "id", event.sender_id) or event.chat_id
            ctx = hermes_bridge._resolve_context(event.chat_id, sender_id)
            folder = hermes_bridge._user_home(ctx)
            os.makedirs(folder, exist_ok=True)
            dl = await asyncio.wait_for(
                event.download_media(file=os.path.join(folder, f"img_{event.id}.jpg")),
                timeout=25,
            )
            if dl and os.path.exists(dl):
                sandbox_path = dl
                import pwd
                try:
                    ubox_uid = pwd.getpwnam(config.SANDBOX_USER).pw_uid
                    os.chown(dl, ubox_uid, ubox_uid)
                except Exception:
                    pass
        except Exception as e:
            log.warning("download media ke sandbox gagal: %s", e)
        # 2) OCR fallback (teks di gambar → konteks agent)
        ocr = await _ocr_event_image(event)
        ocr_block = (
            f"\n\n[Gambar dari user — hasil OCR:\n{ocr}]"
            if ocr
            else "\n\n[Gambar dari user — OCR gagal/nggak ada teks]"
        )
        if sandbox_path:
            # path di PALING AWAL biar CLI attach sebagai vision image
            text = f"{sandbox_path}\n{text}{ocr_block}".strip()
        else:
            text = (text + ocr_block).strip()

    chat_id = event.chat_id

    # ── Filter perintah berbahaya: jangan pernah diteruskan ke LLM/agent.
    # is_exfil: di GRUP tetap diblokir (private key/kredensial nggak boleh
    # keluar di grup); di PRIVATE chat dilewatkan ke agent (agent yang nilai —
    # SOUL bilang kirim boleh di private).
    if is_dangerous(text) or (is_exfil(text) and not getattr(event, "is_private", False)):
        log.warning("blocked request in chat=%s: %.80s", chat_id, text)
        h = get_history(chat_id)
        h.append(("user", text))
        h.append(("assistant", config.DANGER_PHRASE))
        try:
            await event.reply(config.DANGER_PHRASE, link_preview=False)
        except Exception as e:
            log.exception("failed to send danger refusal")
        return

    cooldown = config.HERMES_COOLDOWN if config.UB_MODE == "hermes" else config.COOLDOWN
    sender_id = getattr(sender, "id", 0) or 0
    if on_cooldown(chat_id, sender_id, cooldown):
        log.info("cooldown, skip chat=%s user=%s", chat_id, sender_id)
        return
    set_cooldown(chat_id, sender_id)

    # Info pengirim + judul chat buat konteks agent
    sender_name = getattr(sender, "first_name", None) or getattr(sender, "username", None) or str(sender.id)
    try:
        chat_entity = await event.get_chat()
        chat_title = getattr(chat_entity, "title", None) or getattr(chat_entity, "first_name", None)
    except Exception:
        chat_title = None

    log.info("replying in chat=%s from %s: %.80s", chat_id, sender.id, text)
    try:
        if config.UB_MODE == "hermes":
            # ── Streaming Hermes-style: teks ngetik sendiri + bubble terpisah per tool
            msg = None
            last_emitted = ""

            # ── Voice note? kalau user minta VN, reply jadi 1 VN di akhir
            # (single-VN — multi-segmen dipindah ke Hermes asli).
            is_vn = _wants_voice_note(text)

            if is_vn:
                text_for_agent = (
                    text
                    + "\n\n[Catatan: user minta VOICE NOTE — balas HANYA teks singkat "
                    "natural yang bakal dibacain suara TTS. Nggak usah format HTML "
                    "atau markdown, langsung teks polos.]"
                )
                # kalau user kasih teks eksplisit yang mau dibacain (di quote),
                # TTS langsung — JANGAN biarin agent bikin komentar sendiri
                quoted = _extract_quoted_text(text)
                if quoted:
                    log.info("vn override: TTS teks user langsung (chat=%s)", chat_id)
                    await _send_vn(event, chat_id, quoted)
                    return
            else:
                text_for_agent = text

            async def on_tool(emoji: str, tool_name: str, preview: str):
                if not config.SHOW_TOOL_BUBBLES:
                    return
                if tool_name in config.HIDE_TOOL_BUBBLES:
                    return  # tool internal (session_search, memory, todo) — nggak dimunculin
                emoji = emoji.replace("\ufe0f", "")  # buang variation selector
                pv = (preview or "").strip()
                if len(pv) > 45:
                    pv = pv[:42] + "..."
                safe = html.escape(pv)
                try:
                    # Hermes-style: emoji + nama tool di baris 1, command di blok code
                    await asyncio.wait_for(
                        event.reply(
                            f"{emoji} {tool_name}\n<pre>{safe}</pre>",
                            link_preview=False, parse_mode="html",
                        ),
                        timeout=15,
                    )
                except Exception:
                    pass

            async def on_update(partial: str, new_segment: bool = False):
                nonlocal msg, last_emitted
                if not partial:
                    return
                last_emitted = partial
                # format pas streaming: markdown lengkap di-convert, sisa delimiter
                # yang belum ke-close di-strip (biar typing-nya bersih, nggak
                # nampilin ** / # / backtick mentah)
                display = hermes_bridge._strip_stray_markdown(
                    hermes_bridge.format_reply(partial)
                )
                try:
                    if msg is None or new_segment:
                        msg = await asyncio.wait_for(
                            event.reply(
                                hermes_bridge._sanitize_html(display),
                                link_preview=False, parse_mode="html",
                            ),
                            timeout=15,
                        )
                    else:
                        await asyncio.wait_for(
                            msg.edit(
                                hermes_bridge._sanitize_html(display),
                                link_preview=False, parse_mode="html",
                            ),
                            timeout=15,
                        )
                except Exception:
                    try:
                        if msg is None or new_segment:
                            msg = await asyncio.wait_for(
                                event.reply(
                                    hermes_bridge._strip_html_tags(display),
                                    link_preview=False,
                                ),
                                timeout=15,
                            )
                        else:
                            await asyncio.wait_for(
                                msg.edit(
                                    hermes_bridge._strip_html_tags(display),
                                    link_preview=False,
                                ),
                                timeout=15,
                            )
                    except Exception:
                        pass

            stream_update = None  # default: tanpa streaming (VN); non-VN di-set di bawah
            if not is_vn:
                stream_update = on_update  # non-VN: streaming teks biasa

            # ── typing indicator kontinyu: Telegram expire typing ~5 detik,
            # agent bisa jalan 1-2 menit — renew tiap 4 detik biar user tau
            # bot masih kerja
            typing_task = asyncio.create_task(_keep_typing(event.chat_id))

            async with client.action(event.chat_id, "typing"):
                # ── Status queue/redirect (kayak Hermes gateway): tampilkan ke
                # user, auto-delete pas agent mulai proses.
                status_msg = None
                try:
                    owner = hermes_bridge.busy_owner_for(chat_id, getattr(sender, "id", 0))
                    if owner is not None:
                        if owner == getattr(sender, "id", 0):
                            status_msg = await event.respond("↪ Redirected current run. I'll adjust using your correction.")
                        else:
                            status_msg = await event.respond("⏳ Your message is queued — I'll get to it after the current task finishes.")
                except Exception:
                    status_msg = None

                async def _on_start():
                    if status_msg is not None:
                        try:
                            await client.delete_messages(chat_id, status_msg.id)
                        except Exception:
                            pass

                async def _delete_later(secs: float):
                    await asyncio.sleep(secs)
                    if status_msg is not None:
                        try:
                            await client.delete_messages(chat_id, status_msg.id)
                        except Exception:
                            pass

                # watchdog: apa pun yang hang di dalem (Telegram send, tmux,
                # API) → ke-cut di sini, session di-kill, bot tetep responsif
                try:
                    reply_text = await asyncio.wait_for(
                        hermes_bridge.hermes_ask(
                            chat_id, sender_name, chat_title, text_for_agent,
                            on_update=stream_update, on_tool=on_tool,
                            user_id=getattr(sender, "id", 0),
                            msg_date=event.date,
                            on_start=_on_start,
                        ),
                        timeout=config.HERMES_HARD_TIMEOUT + 60,
                    )
                except asyncio.TimeoutError:
                    log.error("HERMES watchdog timeout — session di-kill")
                    await hermes_bridge.reset_sessions()
                if reply_text == hermes_bridge._STEERED:
                    # redirect /steer diterima — reply yang lagi jalan bakal
                    # incorporate koreksi ini; status redirect dihapus setelah 10s.
                    log.info("steer diterima, skip bubble dobel")
                    if status_msg is not None:
                        asyncio.create_task(_delete_later(10))
                    typing_task.cancel()
                    return

            typing_task.cancel()

            # ── Single-VN: kalau user minta voice note, reply jadi 1 VN
            if is_vn and reply_text:
                if await _send_vn(event, chat_id, reply_text):
                    return

            # file yang agent sebut (MEDIA:/path) — pisahin dari teks, kirim belakangan
            reply_text, media_paths = _extract_media(reply_text)

            # edit final biar persis sama hasil agent
            if msg is not None:
                from telethon.errors import MessageNotModifiedError
                edited = False
                try:
                    await asyncio.wait_for(
                        msg.edit(
                            hermes_bridge._sanitize_html(reply_text),
                            link_preview=False, parse_mode="html",
                        ),
                        timeout=20,
                    )
                    edited = True
                except MessageNotModifiedError:
                    # konten udah sama persis (reply teks polos, streaming udah
                    # kirim full) — ini SUKSES, jangan kirim duplikat
                    edited = True
                except Exception as e:
                    log.warning("final edit html gagal chat=%s: %s", chat_id, e)
                    try:
                        await asyncio.wait_for(
                            msg.edit(
                                hermes_bridge._strip_html_tags(reply_text),
                                link_preview=False,
                            ),
                            timeout=20,
                        )
                        edited = True
                    except MessageNotModifiedError:
                        edited = True
                    except Exception as e2:
                        log.warning("final edit plain gagal chat=%s: %s", chat_id, e2)
                if not edited:
                    # edit mentok — kirim pesan BARU biar user tetep dapet reply lengkap
                    # (kalau diem, chat nyangkut di partial streaming yang ke-potong)
                    log.warning("final edit mentok chat=%s — kirim ulang fresh", chat_id)
                    await send_reply(event, reply_text)
                await _send_media(event, media_paths)
            else:
                await send_reply(event, reply_text)
        else:
            async with client.action(event.chat_id, "typing"):
                reply_text = await generate_reply(chat_id, text)
            await send_reply(event, reply_text)
    except hermes_bridge.AgentError as e:
        # provider gagal setelah retry — kasih tau user (jangan diem), session
        # tetap hidup, user bisa ketik "continue" buat nyoba lagi
        log.warning("AgentError: %s", e)
        try:
            typing_task.cancel()
        except Exception:
            pass
        try:
            await event.reply(
                "⚠️ Model provider lagi error sesaat (API 503). "
                "Ketik 'continue' buat nyoba lagi ya.",
                link_preview=False,
            )
        except Exception:
            pass
        return
    except Exception as e:
        log.exception("%s error, staying silent", config.UB_MODE.upper())
        return


@client.on(events.NewMessage(outgoing=True))
async def on_self_command(event):
    global enabled
    text = event.raw_text or ""
    if not text.startswith(config.CMD_PREFIX):
        return
    cmd = text[len(config.CMD_PREFIX):].strip().lower()
    if cmd == "on":
        enabled = True
        await event.edit("✅ Userbot ON")
    elif cmd == "off":
        enabled = False
        await event.edit("⏸ Userbot OFF")
    elif cmd == "status":
        await event.edit(
            f"Userbot {'🟢 ON' if enabled else '🔴 OFF'} · "
            f"mode {config.UB_MODE} · cooldown {config.HERMES_COOLDOWN if config.UB_MODE == 'hermes' else config.COOLDOWN}s · "
            f"model {config.MODEL}"
        )
    elif cmd == "reset":
        history.clear()
        killed = await hermes_bridge.reset_sessions()
        await event.edit(f"🧹 Konteks dibersihkan ({killed} sesi di-reset)")


async def main():
    await client.start()
    me = await client.get_me()
    log.info("✅ Login sebagai %s (@%s)", me.first_name, me.username)
    # bersihin sesi tmux sisa dari run sebelumnya
    try:
        await hermes_bridge.reset_sessions()
    except Exception as e:
        log.warning("cleanup sesi gagal: %s", e)
    log.info("Mendengarkan... ketik '%son' / '%soff' buat toggle", config.CMD_PREFIX, config.CMD_PREFIX)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
