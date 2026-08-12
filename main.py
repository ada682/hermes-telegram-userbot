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
_pending_clarify = {}  # chat_id -> True (agent lagi nanya, pesan berikutnya = jawaban)

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
    """Kirim file (MAX 2) yang di-sebut agent via MEDIA:/path.
    .webp (512x512) + .tgs → dikirim sebagai STIKER (file_type='sticker')."""
    for p in (paths or [])[:2]:
        try:
            if os.path.exists(p):
                _ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
                if _ext in ("webp", "tgs"):
                    await asyncio.wait_for(
                        event.reply(file=p, file_type="sticker"), timeout=30)
                    log.info("stiker terkirim: %s", p)
                else:
                    await asyncio.wait_for(event.reply(file=p), timeout=30)
                    log.info("media terkirim: %s", p)
            else:
                await asyncio.wait_for(
                    event.reply(f"⚠️ File {p} nggak ketemu di sandbox.", link_preview=False),
                    timeout=20,
                )
        except Exception as e:
            log.warning("send media gagal %s: %s", p, e)


def _load_packs() -> dict:
    """Load sticker packs dari sticker_packs.json → {pack_name: short_name}.
    Support 2 format:
      - baru: {"scuba": {"set": "...", "keywords": [...]}}
      - lama: {"scuba": "short_name"}
    Return {} kalau file rusak/gak ada — kode tetap jalan (pack gak ke-send)."""
    import json, os
    _packs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sticker_packs.json')
    try:
        with open(_packs_path) as _f:
            _raw = json.load(_f)
    except Exception:
        return {}
    _packs = {}
    for _name, _val in (_raw or {}).items():
        if _name.startswith("_"):
            continue  # skip comment key
        if isinstance(_val, str):
            _packs[_name] = _val  # format lama
        elif isinstance(_val, dict) and _val.get("set"):
            _packs[_name] = _val["set"]  # format baru
    return _packs


def _load_pack_aliases() -> dict:
    """Build keyword → pack_name dari sticker_packs.json.
    Tiap pack otomatis punya keyword = nama pack-nya + semua keywords
    yang didefinisikan user di json. Jadi user baru TINGGAL edit json,
    tanpa nyentuh kode."""
    import json, os
    _packs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sticker_packs.json')
    _aliases = {}
    try:
        with open(_packs_path) as _f:
            _raw = json.load(_f)
    except Exception:
        return _aliases
    for _name, _val in (_raw or {}).items():
        if _name.startswith("_"):
            continue
        _aliases[_name] = _name  # nama pack = keyword default
        if isinstance(_val, dict):
            for _kw in (_val.get("keywords") or []):
                _aliases[str(_kw).lower()] = _name
    return _aliases


async def _send_sticker_pack(event, pack_name: str) -> bool:
    """Kirim stiker dari pack yang tersedia (bukan cuma favorit).
    pack_name: key di sticker_packs.json (scuba, cewe, kucing, dst).
    User baru nambah pack = edit sticker_packs.json aja."""
    _packs = _load_packs()
    short_name = _packs.get(pack_name.lower().strip())
    if not short_name:
        log.warning('sticker pack tidak ditemukan: %s (cek sticker_packs.json)', pack_name)
        return False
    try:
        from telethon.tl.functions.messages import GetStickerSetRequest
        from telethon.tl.types import InputStickerSetShortName
        result = await asyncio.wait_for(
            client(GetStickerSetRequest(
                stickerset=InputStickerSetShortName(short_name=short_name),
                hash=0,
            )),
            timeout=20,
        )
        docs = getattr(result, 'documents', None) or getattr(result, 'packs', None)
        if not docs:
            log.info('sticker pack %s kosong', short_name)
            return False
        import random
        doc = random.choice(docs)
        await asyncio.wait_for(event.reply(file=doc), timeout=20)
        log.info('sticker pack %s terkirim', pack_name)
        return True
    except Exception as e:
        log.warning('sticker pack %s gagal: %s', pack_name, e)
        return False

async def _send_faved_sticker(event, keyword: str = "") -> bool:
    """Kirim stiker FAVORIT akun userbot (dari GetFavedStickers).
    keyword kosong → random; keyword = emoji/teks → cari yang match alt-nya."""
    try:
        from telethon.tl.functions.messages import GetFavedStickersRequest
        r = await asyncio.wait_for(
            client(GetFavedStickersRequest(hash=0)), timeout=15)
        stickers = r.stickers
        if not stickers:
            log.info("stiker favorit kosong")
            return False
        if keyword:
            kw = keyword.strip().lower()
            for s in stickers:
                emo = next((getattr(a, "alt", "") for a in s.attributes
                            if hasattr(a, "alt")), "")
                if kw and kw in emo.lower():
                    await asyncio.wait_for(event.reply(file=s), timeout=20)
                    log.info("stiker favorit terkirim (emoji %s)", emo)
                    return True
        import random
        s = random.choice(stickers)
        await asyncio.wait_for(event.reply(file=s), timeout=20)
        log.info("stiker favorit terkirim (random)")
        return True
    except Exception as e:
        log.warning("stiker favorit gagal: %s", e)
        return False

async def send_reply(event, reply_text: str) -> None:
    """Kirim reply — pecah jadi beberapa bubble kalau panjang, HTML + fallback plain,
    plus file MEDIA:/path kalau agent nyebutnya, plus STICKER: buat stiker favorit."""
    # STICKER marker: "STICKER:" (random fav) / "STICKER: <emoji>" → kirim stiker favorit
    # STICKER_PACK: <pack_name> → kirim stiker dari pack tertentu
    import re as _re
    _st_m = _re.search(r"(?i)STICKER:\s*(\S*)", reply_text)
    _pack_m = _re.search(r"(?i)STICKER_PACK:\s*(\S+)", reply_text)
    if _pack_m:
        reply_text = _re.sub(r"\s*STICKER_PACK:\s*\S+", "", reply_text).strip()
        pack_name = _pack_m.group(1)
        # Explicit pack request → JANGAN fallback ke favorite. Jika pack gagal,
        # user cukup tidak dapat stiker (atau agent bisa jelaskan di teks).
        if not await _send_sticker_pack(event, pack_name):
            log.warning('sticker pack %s gagal, skip (no fav fallback)', pack_name)
    elif _st_m:
        _kw = _st_m.group(1)
        reply_text = _re.sub(r"\s*STICKER:\s*\S*", "", reply_text).strip()
        await _send_faved_sticker(event, _kw)
    reply_text, media_paths = _extract_media(reply_text)
    # Kalau setelah strip marker + media, teksnya HAMPIR KOSONG → jangan kirim
    # teks tambahan. Cuma kirim media/sticker yang udah dikirim di atas.
    if not reply_text or not reply_text.strip():
        return
    # ── FIX (19:31→20:00): filter SEMUA UI CLI yang nyasar ke reply —
    # status bar ("⚕ deepseek..."), boot banner ("Hermes Agent v" + ASCII
    # + "Available Tools/Skills"), welcome box ("30 tools · 127 skills",
    # "writing: ..."), separator ("───"), prompt echo ("[Chat ini:",
    # "/personality", "✦ Tip:") — itu UI CLI, BUKAN jawaban agent!
    _ui_lines = [l for l in reply_text.splitlines()
                 if l.strip().startswith(("⚕ deepseek", "⚕ ❯", "Available Tools",
                                          "Available Skills", "✦ Tip", "│", "╭", "╰",
                                          "[Chat ini:", "──", "╚"))
                 or "Hermes Agent v" in l or "ctx --" in l or "0/1M" in l
                 or "30 tools" in l or "writing:" in l or "/personality" in l
                 or "127 skills" in l or "Session:" in l or "YOLO mode" in l
                 or "Nous Research" in l or l.strip().startswith("██")
                 or (l.strip() and not l.strip().isascii() and len(l.strip()) < 3)]
    if _ui_lines:
        reply_text = "\n".join(
            l for l in reply_text.splitlines() if l not in _ui_lines
        ).strip()
    chunks = split_reply(reply_text)
    if not chunks:
        return
    for i, chunk in enumerate(chunks):
        formatted = hermes_bridge.format_reply(chunk)
        try:
            await asyncio.wait_for(
                event.reply(
                    hermes_bridge._sanitize_html(formatted),
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
                        hermes_bridge._strip_html_tags(formatted),
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
    if cooldown is None:
        cooldown = config.COOLDOWN
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
    last_err = None
    for attempt in range(1, 4):
        try:
            _t0 = time.monotonic()
            async with httpx.AsyncClient(timeout=config.API_TIMEOUT) as http:
                resp = await http.post(f"{config.BASE_URL}/chat/completions", json=payload, headers=headers)
                if resp.status_code == 503:
                    last_err = RuntimeError("upstream capacity limit 503")
                    log.warning("LLM 503 attempt=%d elapsed=%.3fs", attempt, time.monotonic() - _t0)
                    await asyncio.sleep(1)
                    continue
                resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content:
                raise RuntimeError("model returned empty content (reasoning-only?)")
            log.info("LLM ok attempt=%d elapsed=%.3fs", attempt, time.monotonic() - _t0)
            return content
        except Exception as e:
            last_err = e
            log.warning("LLM fail attempt=%d elapsed=%.3fs err=%s", attempt, time.monotonic() - _t0, e)
            await asyncio.sleep(1)
            continue
    raise last_err or RuntimeError("LLM request failed after retries")

async def generate_reply(chat_id, user_text) -> str:
    h = get_history(chat_id)
    h.append(("user", user_text))
    messages = [{"role": "system", "content": config.PERSONA}]
    messages += [{"role": role, "content": text} for role, text in h]
    reply = await ask_llm(messages)
    h.append(("assistant", reply))
    return reply

# ── FIX TYPING NYASAR (17:35): Telegram cuma nampilin SATU typing per akun!
# Chat yang diproses BERSAMAAN (grup + Fajar + ...) → task typing saling
# timpa (last-wins!) → typing kedip-kedip + nyasar ke chat yang salah.

async def _keep_typing(chat_id: int):
    """Renew Telegram typing indicator tiap 2 detik (Telegram expire ~5s —
    2s + latency API = <5s → typing nggak sempet expire, nggak kedip.
    Instant start: kirim typing SEBELUM loop biar user langsung lihat
    indikator di chat pertama, bukan setelah delay."""
    from telethon.tl.functions.messages import SetTypingRequest
    from telethon.tl.types import SendMessageTypingAction
    logged = False
    try:
        # INSTANT typing: kirim sekarang sebelum loop, supaya user langsung lihat
        try:
            await client(
                SetTypingRequest(peer=chat_id, action=SendMessageTypingAction())
            )
        except Exception as e:
            if not logged:
                log.warning("keep_typing instant gagal chat=%s: %s", chat_id, e)
                logged = True
        # loop renewal tiap 2 detik (< Telegram expire 5s)
        while True:
            try:
                await client(
                    SetTypingRequest(peer=chat_id, action=SendMessageTypingAction())
                )
            except Exception as e:
                if not logged:
                    log.warning("keep_typing gagal chat=%s: %s", chat_id, e)
                    logged = True
            await asyncio.sleep(2)
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
    """True kalau pesan punya foto/dokumen gambar."""
    photo = getattr(event, "photo", None)
    if photo:
        return True
    doc = getattr(event, "document", None)
    if doc:
        return (doc.mime_type or "").startswith("image/")
    return False

def _extract_zip_safe(zip_path: str, dest_folder: str) -> list:
    """Extract zip ke folder workspace (anti zip-slip). Return list file yang
    ke-extract. Zip dari user bisa berisi SOUL.md/USER.md/skills/ — nanti
    ke-pickup sama _apply_context_overrides pas spawn berikutnya."""
    import zipfile
    extracted = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                # anti zip-slip: nggak boleh ada .. / absolute path
                clean = member.replace("\\", "/")
                if clean.startswith("/") or ".." in clean.split("/"):
                    log.warning("zip member mencurigakan di-skip: %s", member)
                    continue
                target = os.path.join(dest_folder, clean)
                if member.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as out:
                    out.write(src.read())
                extracted.append(target)
        log.info("zip extracted: %d file dari %s", len(extracted), os.path.basename(zip_path))
    except Exception as e:
        log.warning("extract zip gagal %s: %s", zip_path, e)
    return extracted

def _is_document_media(event) -> bool:
    """True kalau pesan punya DOKUMEN (bukan gambar/voice) — file apa pun:
    zip, .py, .exe, pdf, dll. Buat di-download + di-install agent."""
    doc = getattr(event, "document", None)
    if not doc:
        return False
    if (doc.mime_type or "").startswith("image/"):
        return False
    if getattr(event, "voice", None) is not None:
        return False
    # file audio/video juga masuk dokumen di Telethon — voice note udah di-handle,
    # audio/video lain biarin ke-download juga (bisa dianalisa agent)
    return True

def _is_voice(event) -> bool:
    return getattr(event, "voice", None) is not None

_whisper_model = None

def _get_whisper():
    """Lazy-load faster-whisper model (sama kayak Hermes: local, base, CPU)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model

def _transcribe_audio(path: str) -> str:
    """Transkrip file audio → teks (faster-whisper, auto language)."""
    model = _get_whisper()
    segments, _ = model.transcribe(path)
    return " ".join(s.text.strip() for s in segments).strip()

async def _transcribe_event_voice(event) -> str:
    """Download voice note → transkrip. Return '' kalau gagal/kosong."""
    try:
        path = await asyncio.wait_for(
            event.download_media(file=f"/tmp/ub_voice_{int(time.time() * 1000)}.ogg"),
            timeout=25,
        )
        if not path or not os.path.exists(path):
            log.warning("download voice gagal")
            return ""
        trans = await asyncio.to_thread(_transcribe_audio, path)
        try:
            os.remove(path)
        except Exception:
            pass
        return (trans or "")[:4000]
    except Exception as e:
        log.warning("transkrip voice gagal: %s", e)
        return ""

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

        # ── FALLBACK CHAIN (kayak skill screenshot-ocr): psm 3 kosong →
        # coba psm 6 (block) + psm 11 (sparse) → kalau masih kosong:
        # grayscale + kontras + upscale → ulang psm 3. Jangan nyerah sekali.
        if len(text) < 20:
            for psm in (6, 11):
                try:
                    pr2 = await asyncio.to_thread(
                        subprocess.run,
                        ["tesseract", path, "stdout", "-l", "eng+ind", "--psm", str(psm)],
                        capture_output=True, text=True, timeout=90,
                    )
                    t2 = (pr2.stdout or "").strip()
                    if len(t2) > len(text):
                        text = t2
                except Exception:
                    continue
        if len(text) < 20:
            try:
                from PIL import Image, ImageOps, ImageEnhance
                im = Image.open(path).convert("L")  # grayscale
                im = ImageOps.autocontrast(im)
                im = ImageEnhance.Contrast(im).enhance(2.0)
                im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
                pre = f"{path}.pre.png"
                im.save(pre)
                pr3 = await asyncio.to_thread(
                    subprocess.run,
                    ["tesseract", pre, "stdout", "-l", "eng+ind", "--psm", "3"],
                    capture_output=True, text=True, timeout=90,
                )
                t3 = (pr3.stdout or "").strip()
                if len(t3) > len(text):
                    text = t3
                try:
                    os.remove(pre)
                except Exception:
                    pass
            except Exception:
                pass
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

def _load_enabled() -> bool:
    """Load state enabled (persist — biar /switch_off nggak reset pas restart)."""
    try:
        with open(config.STATE_FILE) as f:
            import json as _json
            return bool(_json.load(f).get("enabled", True))
    except Exception:
        return True

def _load_disabled_users() -> set:
    """User yang /switch_off (per-user): pesan mereka di-ignore, user lain normal."""
    try:
        with open(config.STATE_FILE) as f:
            import json as _json
            return set(_json.load(f).get("disabled_users", []))
    except Exception:
        return set()

def _load_self_trigger() -> bool:
    """Self-trigger: akun sendiri bisa manggil agent pake 'SONNET ...' (outgoing).
    Default OFF (perilaku lama: outgoing di-ignore)."""
    try:
        with open(config.STATE_FILE) as f:
            import json as _json
            return bool(_json.load(f).get("self_trigger", False))
    except Exception:
        return False

def _fmt_ts(dt) -> str:
    """Format tanggal ISO netral (2026-08-19 23:56) — AGENT yang nentuin
    format tampilan sesuai bahasa user pas bikin reply (Indo/English)."""
    try:
        return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return "??"

def _history_path(chat_id: int) -> str:
    """Path history.md per chat (rolling observed context)."""
    try:
        if chat_id < 0:
            home = f"/srv/ubox/groups/{abs(chat_id)}/.hermes"
        else:
            home = f"/srv/ubox/users/{chat_id}/.hermes"
        return os.path.join(home, "history.md")
    except Exception:
        return ""

def _append_history(chat_id: int, name: str, text: str, ts: str = "") -> None:
    """Append pesan grup ke history.md (rolling FIFO — header 👑/🛡️ di-pisah
    biar nggak ke-roll — agent baca file tetep liat owner/admin/title!)."""
    try:
        p = _history_path(chat_id)
        if not p:
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        import datetime as _dt
        if not ts:
            ts = _fmt_ts(_dt.datetime.now())
        line = f"[{ts}] {name}: {(text or '').strip()[:200]}"
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []
        # header (👑/🛡️) di-pisah dari body — nggak ke-roll (FIFO body doang)
        header_lines = [l for l in lines if l.startswith(("👑", "🛡️"))]
        body = [l for l in lines if not l.startswith(("👑", "🛡️"))]
        body.append(line)
        body = body[-config.HISTORY_ROLLING:]  # rolling N baris (FIFO)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(header_lines + body) + "\n")
    except Exception:
        pass

def _record_seen_chat(chat_id: int) -> None:
    """Catat chat yang pernah aktif + timestamp → pre-warm by RECENCY
    (yang paling baru dipake yang di-pre-warm, bukan first-seen FIFO)."""
    try:
        import json as _json
        import time as _time
        try:
            with open(config.STATE_FILE) as f:
                st = _json.load(f)
        except Exception:
            st = {}
        seen = dict(st.get("seen_chats", {}) or {})
        seen[str(chat_id)] = _time.time()
        # cap 20 — drop yang PALING LAMA (bukan FIFO masuk!)
        if len(seen) > 20:
            _oldest = sorted(seen.items(), key=lambda kv: kv[1])[:len(seen) - 20]
            for k, _ in _oldest:
                seen.pop(k, None)
        st["seen_chats"] = seen
        with open(config.STATE_FILE, "w") as f:
            _json.dump(st, f)
    except Exception:
        pass

def _save_state(enabled_val: bool, disabled: set, self_trigger_val: bool = None) -> None:
    try:
        import json as _json
        with open(config.STATE_FILE, "w") as f:
            _json.dump({
                "enabled": enabled_val,
                "disabled_users": sorted(disabled),
                "self_trigger": bool(self_trigger_val) if self_trigger_val is not None else self_trigger,
            }, f)
    except Exception:
        pass

def _save_self_trigger(v: bool) -> None:
    global self_trigger
    self_trigger = v
    _save_state(enabled, disabled_users, v)

enabled = _load_enabled()
disabled_users = _load_disabled_users()
self_trigger = _load_self_trigger()

# ── MERGE WINDOW BUFFER: Telegram auto-split pesan panjang jadi beberapa
# bubble (<1 detik antar bubble). Buffer per (chat_id, user_id): bubble
# berurutan dalam MERGE_WINDOW detik digabung jadi SATU prompt utuh, biar
# agent lihat pesan lengkap bukan potongan (dan bubble 2/3 tanpa wake word
# nggak ke-skip). Flush = sleep window → gabung parts → panggil _run_agent
# dengan teks gabungan.
_merge_buf = {}  # (chat_id, user_id) -> {"parts": [...], "last_event": event}


async def _flush_merge(mk):
    try:
        await asyncio.sleep(config.MERGE_WINDOW)
        item = _merge_buf.pop(mk, None)
        if not item:
            return
        parts = [p for p in item["parts"] if p]
        if not parts:
            return
        ev = item["last_event"]
        merged = "\n\n".join(parts)
        try:
            await _run_agent(ev, _merged_text=merged)
        except Exception:
            pass  # error di-handle di dalam _run_agent
    except Exception:
        pass


async def _is_reply_to_media(event) -> bool:
    """True kalau event ini REPLY ke pesan yang punya media (gambar/file/
    voice). Dipakai buat SKIP merge window — reply-to-media diproses
    LANGSUNG (jangan ditunda 2.5s, jangan ke-merge sama bubble lain)."""
    try:
        if not getattr(event, "is_reply", False):
            return False
        _rep = await event.get_reply_message()
        if _rep is None:
            return False
        return (_is_image_media(_rep) or _is_document_media(_rep)
                or _is_voice(_rep))
    except Exception:
        return False

def _save_enabled(v: bool) -> None:
    _save_state(v, disabled_users)

def _sender_id(event) -> int:
    """Resolve sender id dari event (PeerUser/PeerChat aman)."""
    sid = getattr(event, "sender_id", None)
    if sid is None:
        return 0
    return getattr(sid, "user_id", sid)

@client.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    global enabled, disabled_users
    # ── /switch_on / /switch_off — toggle akses userbot dari chat (wajib di-cek
    # SEBELUM gate enabled biar bisa nyalain lagi dari kondisi OFF)
    text_raw = (event.raw_text or "").strip().lower()
    if text_raw in ("/switch_on", "/switch_off"):
        if event.out:
            return
        sid = _sender_id(event)
        if text_raw == "/switch_on":
            disabled_users.discard(sid)
            await event.respond(
                "✅ **Userbot ON untuk lu** — SONNET aktif. Kirim `SONNET ...` buat chat sama agent."
            )
        else:
            disabled_users.add(sid)
            await event.respond(
                "⏸ **Userbot OFF untuk lu** — pesan lu bakal di-ignore. "
                "User lain tetep bisa pake. Kirim `/switch_on` buat aktifin lagi."
            )
        _save_state(enabled, disabled_users)
        return
    if not enabled:
        return
    if event.out:
        return
    if _sender_id(event) in disabled_users:
        return

    return await _run_agent(event)

async def _run_agent(event, _merged_text=None):
    """Flow agent lengkap (resolve sender → media → hermes_ask → kirim reply).
    Dipanggil dari on_incoming (pesan masuk) ATAU on_self_command
    (outgoing SONNET dari akun sendiri — self-trigger).
    _merged_text: dipanggil dari _flush_merge — teks sudah gabungan bubble,
    langsung proses (skip wake word + merge logic)."""
    _run_t0 = time.monotonic()
    _chat_id = getattr(event, "chat_id", None)
    # Dulu typing di-line 909 — SETELAH event.get_sender() (line 827 — API
    # resolve bisa 1-3s!) → typing telat. Sekarang: cek wake word DULU (dari
    # event.raw_text — TANPA API!) → typing langsung start → baru resolve
    # sender/media/hermes_ask (ketutup typing).
    _text_raw = event.raw_text or ""
    _typing_task = None
    if config.REQUIRE_PREFIX:
        _stripped = _text_raw.strip()
        if _stripped.lower().startswith(config.REQUIRE_PREFIX.lower()):
            _typing_task = asyncio.create_task(_keep_typing(event.chat_id))
    _record_seen_chat(event.chat_id)  # catat chat aktif → pre-warm restart
    try:
        sender = await event.get_sender()
        if sender is None:
            return  # service message
        if getattr(sender, "bot", False):
            return  # jangan bales bot (hindari loop)
    except Exception:
        return  # channel private / banned / entity gagal — skip aja

    me = await client.get_me()

    # ── CLARIFY: kalau agent lagi nanya (pending), pesan user ini = JAWABAN.
    # Di-inject langsung ke session (bukan prompt baru). Jawaban nggak perlu
    # wake word — ini lanjutan dari pertanyaan agent.
    if _pending_clarify.get(event.chat_id):
        _pending_clarify.pop(event.chat_id, None)
        try:
            await hermes_bridge.answer_clarify(
                event.chat_id, getattr(sender, "id", 0), event.raw_text or ""
            )
            log.info("clarify answer di-inject dari user=%s chat=%s", getattr(sender, "id", 0), event.chat_id)
        except Exception as e:
            log.warning("answer_clarify gagal: %s", e)
        return

    text = event.raw_text or ""

    # ── STOP: "/stop" / "sonnet stop" / "stop" → cancel run yang lagi jalan
    # (konsep kayak Hermes: stop = interrupt run, session tetep hidup)
    _stop_text = text.strip().lower().rstrip(".!")
    if _stop_text in ("/stop", "stop", "berhenti", "cancel", "/cancel"):
        try:
            _stopped = await hermes_bridge.cancel_session(
                event.chat_id, getattr(sender, "id", 0))
            _pending_clarify.pop(event.chat_id, None)
        except Exception:
            _stopped = False
        try:
            await event.reply(
                "⏹️ **Stopped.** Run yang lagi jalan di-cancel (konteks session tetep)."
                if _stopped else
                "⚠️ Nggak ada run yang lagi jalan buat di-stop."
            )
        except Exception:
            pass
        return

    if not text.strip() and not _is_image_media(event) and not _is_voice(event) and not _is_document_media(event):
        # pesan tanpa teks (sticker, dsb) — log biar ketauan kalau ada yang ke-skip
        if getattr(event, "sticker", None) is not None:
            log.info("skip: sticker dari user=%s chat=%s", getattr(sender, "id", 0), event.chat_id)
        return  # sticker/service tanpa teks & bukan gambar — skip
    if text.startswith(config.CMD_PREFIX):
        return  # perintah admin, ditangani handler lain

    # ── MERGE WINDOW (private chat only): kalau ini bubble LANJUTAN dari
    # pesan panjang yang ke-split Telegram → append ke buffer, tunggu flush.
    # Bubble pertama tetap harus lolos wake word (di bawah) sebelum masuk
    # buffer. Media (gambar/voice/doc) TIDAK di-merge — proses langsung.
    # Reply-to-media JUGA di-skip (proses langsung, jangan ditunda).
    _merge_key = (event.chat_id, getattr(sender, "id", 0))
    _skip_merge = await _is_reply_to_media(event)
    if (_merged_text is None and config.MERGE_WINDOW > 0
            and event.chat_id > 0 and not _skip_merge
            and not _is_image_media(event) and not _is_voice(event)
            and not _is_document_media(event)):
        _pending = _merge_buf.get(_merge_key)
        if _pending is not None:
            # bubble 2/3 — append, jangan proses sendiri
            _pending["parts"].append(event.raw_text or "")
            _pending["last_event"] = event
            log.info("merge: bubble lanjutan di-append (total %s parts) chat=%s user=%s",
                     len(_pending["parts"]), event.chat_id, getattr(sender, "id", 0))
            return

    # ── Observed history: SEMUA pesan grup di-record ke history.md
    # (rolling 50) — agent tau obrolan grup walau bukan sonnet. Sonnet =
    # request; non-sonnet = observed context (inject delta pas hermes_ask).
    try:
        if event.chat_id < 0:
            _hname = getattr(sender, "first_name", "") or getattr(sender, "title", "") or "?"
            _append_history(event.chat_id, _hname, event.raw_text or "")
    except Exception:
        pass

    # ── Mode wake word: cuma bales kalau pesan DIAWALI prefix (misal "SONNET")
    _do_sticker_typing = False
    if config.REQUIRE_PREFIX and _merged_text is None:
        stripped = text.strip()
        if not stripped.lower().startswith(config.REQUIRE_PREFIX.lower()):
            return  # bukan wake word — diem
        # buang prefix-nya, agent cuma lihat isi permintaannya
        text = stripped[len(config.REQUIRE_PREFIX):].lstrip(": ,\t")
        if not text.strip():
            return
        called = True
        # ── MERGE: bubble PERTAMA lolos wake word → masuk buffer + schedule
        # flush. Flush (setelah MERGE_WINDOW) gabung semua bubble → panggil
        # _run_agent(_merged_text=...) → agent lihat pesan UTUH.
        if (config.MERGE_WINDOW > 0 and event.chat_id > 0 and not _skip_merge
                and not _is_image_media(event) and not _is_voice(event)
                and not _is_document_media(event)):
            _merge_buf[_merge_key] = {"parts": [text], "last_event": event}
            asyncio.create_task(_flush_merge(_merge_key))
            log.info("merge: bubble pertama masuk buffer chat=%s user=%s (flush %ss)",
                     event.chat_id, getattr(sender, "id", 0), config.MERGE_WINDOW)
            return
        _do_sticker_typing = True
    elif _merged_text is not None:
        # pemanggilan dari flush — teks sudah gabungan
        text = _merged_text
        called = True
        _do_sticker_typing = True
    else:
        # ── Aturan lama: "Apakah dipanggil?"
        typing_task = _typing_task or asyncio.create_task(_keep_typing(event.chat_id))
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
            typing_task.cancel()
            return  # entity gagal — skip
        if not called:
            typing_task.cancel()
            return

    if _do_sticker_typing:
        # mark session busy SEBELUM media processing (gambar/voice download+OCR)
        # biar status queue/redirect muncul kalau user chat lagi pas proses media.
        try:
            hermes_bridge.note_busy(event.chat_id, getattr(sender, "id", 0))
        except Exception:
            pass

        # ── AUTO MAP sticker pack keyword supaya agent tidak accidentally
        # kirim favorite sticker saat user minta pack tertentu.
        # Contoh: 'sticker meme' → inject instruction eksplisit STICKER_PACK:memev2
        # Mapping keyword → pack di-load dari sticker_packs.json (user baru
        # tinggal tambah keywords di json, tanpa edit kode).
        _pack_aliases = _load_pack_aliases()
        _sticker_match = re.search(r'(?i)\b(sticker|stiker)\s+([a-z0-9]+)\b', text)
        if _sticker_match:
            _kw = _sticker_match.group(2).lower()
            _pack = _pack_aliases.get(_kw)
            if _pack:
                text = f'Kirim stiker pack {_pack}. Gunakan marker STICKER_PACK:{_pack} di jawaban, JANGAN stiker favorit.'
        # typing indicator MULAI dari sini — nutup fase media processing
        # (download gambar/voice + OCR + whisper) yang bisa 30-90 detik.
        # FIX: kalau _typing_task sudah cancelled/done, bikin task baru
        # (tanpa ini, typing nggak restart setelah cancel → kedip/ilang)
        if _typing_task is None or _typing_task.done():
            typing_task = asyncio.create_task(_keep_typing(event.chat_id))
        else:
            typing_task = _typing_task

    # ── Media dari REPLY: kalau event ini REPLY ke pesan yang punya file/
    # gambar/voice, resolve pesan aslinya — agent bisa lihat file yang
    # di-reply user ("sonnet coba lihat file ini" + reply ke file).
    _media_src = event  # sumber media default: event itu sendiri
    _media_from_reply = False
    if (not _is_image_media(event) and not _is_document_media(event)
            and not _is_voice(event)):
        try:
            if getattr(event, "is_reply", False):
                _rep = await event.get_reply_message()
                if _rep is not None:
                    _has_m = (_is_image_media(_rep) or _is_document_media(_rep)
                              or _is_voice(_rep))
                    if _has_m:
                        _media_src = _rep
                        _media_from_reply = True
                        log.info("media dari reply pesan id=%s (chat=%s)",
                                 getattr(_rep, "id", 0), event.chat_id)
        except Exception as e:
            log.warning("resolve media dari reply gagal: %s", e)

    # ── Media gambar → OCR + path buat VISION agent (kayak Hermes asli: coba liat dulu)
    if _is_image_media(_media_src):
        # 1) simpen di sandbox konteks biar agent bisa LIAT gambarnya (vision attachment)
        sandbox_path = None
        try:
            sender_id = getattr(event.sender_id, "id", event.sender_id) or event.chat_id
            ctx = hermes_bridge._resolve_context(event.chat_id, sender_id)
            folder = hermes_bridge._user_home(ctx)
            os.makedirs(folder, exist_ok=True)
            dl = await asyncio.wait_for(
                _media_src.download_media(file=os.path.join(folder, f"img_{event.id}.jpg")),
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
        ocr = await _ocr_event_image(_media_src)
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

    # ── Dokumen/file (zip, .py, .exe, pdf, dll) → download ke sandbox konteks
    # biar agent bisa BACA/INSTALL. Path di-append ke prompt.
    if _is_document_media(_media_src):
        try:
            sender_id = getattr(event.sender_id, "id", event.sender_id) or event.chat_id
            ctx = hermes_bridge._resolve_context(event.chat_id, sender_id)
            folder = hermes_bridge._user_home(ctx)
            os.makedirs(folder, exist_ok=True)
            doc = getattr(_media_src, "document", None)
            name = None
            for a in (getattr(doc, "attributes", None) or []):
                if getattr(a, "file_name", None):
                    name = a.file_name
                    break
            if not name:
                name = f"file_{getattr(_media_src, 'id', event.id)}"
            dest = os.path.join(folder, os.path.basename(name))
            dl = await asyncio.wait_for(_media_src.download_media(file=dest), timeout=120)
            if dl and os.path.exists(dl):
                import pwd
                try:
                    ubox_uid = pwd.getpwnam(config.SANDBOX_USER).pw_uid
                    os.chown(dl, ubox_uid, ubox_uid)
                except Exception:
                    pass
                # ZIP → extract ke projects/<nama-zip>/ (TERISOLASI per project,
                # nggak nimpa file proyek lain / persona override di root)
                if dl.lower().endswith(".zip"):
                    proj_dir = os.path.join(folder, "projects", os.path.basename(dl)[:-4])
                    os.makedirs(proj_dir, exist_ok=True)
                    _extract_zip_safe(dl, proj_dir)
                text = f"{text}\n\n[File dari user: {dl}]".strip()
                log.info("dokumen user ke-download: %s", dl)
            else:
                log.warning("download dokumen gagal (dl kosong)")
        except Exception as e:
            log.warning("download dokumen ke sandbox gagal: %s", e)

    # ── Voice note → STT transkrip (kayak Hermes: faster-whisper local).
    # Voice note + caption SONNET → di-transkrip, teksnya masuk konteks agent.
    if _is_voice(_media_src):
        trans = await _transcribe_event_voice(_media_src)
        if trans:
            text = (text + f"\n\n[Transkrip voice note dari user:\n{trans}]").strip()
        else:
            text = (text + "\n\n[Voice note — transkrip gagal/kosong]").strip()

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
        typing_task.cancel()
        return

    cooldown = config.HERMES_COOLDOWN if config.UB_MODE == "hermes" else config.COOLDOWN
    sender_id = getattr(sender, "id", 0) or 0
    if on_cooldown(chat_id, sender_id, cooldown):
        log.info("cooldown, skip chat=%s user=%s", chat_id, sender_id)
        typing_task.cancel()
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
                    typing_task.cancel()
                    return
            else:
                text_for_agent = text

            # Tool bubble messages — dihapus pas run selesai (kayak Hermes
            # cleanup_progress: progress keliatan LIVE, tapi chat nggak penuh
            # sampah tool bubble; reply final edit in-place di bubble streaming)
            tool_msgs: list = []

            async def on_tool(emoji: str, tool_name: str, preview: str):
                nonlocal tool_msgs, thinking_msg, hb_msg
                # ── latency instrumentation: tool call start ──
                _tool_t0 = time.monotonic()
                _tool_name_log = tool_name
                # ── FIX: run MULAI (tool pertama jalan!) → bubble PRE WARM/
                # THINKING DIHAPUS (bukan nunggu reply content — telat!)
                if thinking_msg is not None:
                    try:
                        await thinking_msg.delete()
                    except Exception:
                        pass
                    thinking_msg = None
                # ── FIX (19:30): heartbeat TIDAK di-delete pas tool jalan —
                # dulu di-delete + None → heartbeat berikutnya BIKIN BUBBLE
                # BARU (SPAM — "heartbeat dibuat" tiap 45s!). Sekarang: bubble
                # "⏳ Working" TETAP SATU + di-edit (⏳ Working — N min) —
                # user liat SATU bubble progress, bukan spam!
                # (hapus tetap terjadi pas reply mulai — on_update!)
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
                    tb = await asyncio.wait_for(
                        event.reply(
                            f"{emoji} {tool_name}\n<pre>{safe}</pre>",
                            link_preview=False, parse_mode="html",
                        ),
                        timeout=15,
                    )
                    try:
                        tool_msgs.append(tb.id)
                    except Exception:
                        pass
                except Exception:
                    pass
                log.info("tool chat=%s tool=%s elapsed=%.3fs", chat_id, _tool_name_log, time.monotonic() - _tool_t0)

            async def on_update(partial: str, new_segment: bool = False):
                nonlocal msg, last_emitted, hb_msg, thinking_msg
                if not partial:
                    return
                # ── reply bubble MULAI → hapus indikator "💭 THINKING..." +
                # heartbeat (kayak Hermes: progress bubble ilang pas reply jalan)
                if thinking_msg is not None:
                    try:
                        await client.delete_messages(chat_id, thinking_msg.id)
                    except Exception:
                        pass
                    thinking_msg = None
                if hb_msg is not None:
                    try:
                        await client.delete_messages(chat_id, hb_msg.id)
                    except Exception:
                        pass
                    hb_msg = None
                last_emitted = partial
                # ── FIX (20:10): STRIP UI CLI (status bar/banner/prompt echo)
                # DI SINI — streaming bubble TIDAK lewat send_reply, jadi filter
                # di send_reply GAK KE-APPLY ke jalur streaming! (temuan subagent)
                partial = hermes_bridge._strip_cli_ui(partial)
                # ── FIX: STICKER marker harus di-strip di streaming juga, supaya
                # marker gak muncul di chat meskipun sticker terkirim terpisah.
                partial = re.sub(r'\s*STICKER:\s*\S*', '', partial).strip()
                partial = re.sub(r'\s*STICKER_PACK:\s*\S+', '', partial).strip()
                if not partial:
                    return
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
            # typing_task udah dibuat di atas (before media) — jangan buat lagi
            async with client.action(event.chat_id, "typing"):
                # ── Status queue/redirect/interrupt (kayak Hermes gateway
                # busy-ack): tampilkan ke user, auto-delete pas agent mulai.
                status_msg = None
                try:
                    owner = hermes_bridge.busy_owner_for(chat_id, getattr(sender, "id", 0))
                    if owner is not None:
                        _st = hermes_bridge.busy_status_for(chat_id, getattr(sender, "id", 0))
                        _st = f" ({_st})" if _st else ""
                        if owner == getattr(sender, "id", 0):
                            if hermes_bridge.can_steer(chat_id, getattr(sender, "id", 0)):
                                status_msg = await event.respond(
                                    f"↪ Redirected current run{_st}. I'll adjust using your correction.")
                            else:
                                status_msg = await event.respond(
                                    f"⚡ Interrupting current task{_st}. I'll respond to your message shortly.")
                        else:
                            status_msg = await event.respond(
                                f"⏳ Queued for the next turn{_st}. I'll respond once the current task finishes.")
                except Exception:
                    status_msg = None

                async def _on_start():
                    if status_msg is not None:
                        try:
                            await client.delete_messages(chat_id, status_msg.id)
                        except Exception:
                            pass

                # ── HEARTBEAT "⏳ Working — N min": pesan status yang di-edit
                # in-place tiap ~45 detik, dihapus pas reply mulai streaming.
                hb_msg = None
                thinking_msg = None  # bubble "💭 THINKING..." — dihapus pas reply masuk

                # ── STATE STATUS: relay kondisi internal CLI (kayak gateway).
                # CLI print state-nya (compacting, stream error, fallback...)
                # → bridge deteksi → dikirim ke user sebagai status singkat.
                _STATE_TEXTS = {
                    "compressing": "⏳ Compressing context — nunggu sebentar, agent lagi rapiin memori...",
                    "stream_error": "⚠️ Stream error — agent lagi recovery (kontinuasi), sabar ya...",
                    "thinking_exhausted": "💭 Reasoning exhausted output budget — agent nyoba lagi...",
                    "truncated": "⚠️ Response truncated — agent lagi lanjutin reply-nya...",
                    "fallback": "🔄 Provider fail — switch ke fallback provider...",
                    "content_filter": "🛡️ Content filter — agent switch fallback...",
                    "self_improve": "💾 Self-improvement review — agent rapiin skill/memory...",
                    "thinking": "💭 THINKING...",
                    "prewarm": "🧠 PRE WARM...",
                }

                async def _on_state(state_key: str, detail: str = None):
                    nonlocal thinking_msg
                    try:
                        _txt = _STATE_TEXTS.get(state_key)
                        # self-improve: kirim LINE ASLI dari CLI (bukan paraphrase)
                        if detail and state_key == "self_improve":
                            _txt = detail
                        if _txt:
                            # ── FIX: hanya skip PRE WARM — yang lain boleh
                            # muncul ke user.
                            if state_key == "prewarm":
                                return
                            _m = await event.respond(_txt, link_preview=False)
                            # ── FIX (18:54): SEMUA status bubble (termasuk 💾
                            # self-improve!) auto-delete 12 detik — _delete_later
                            # harus dikasi _m (bubble NYA) — dulu nge-delete
                            # status_msg (bubble global — SALAH!) → 💾 nempel!
                            asyncio.create_task(_delete_later(12, _m))
                    except Exception:
                        pass

                async def _on_heartbeat(text: str):
                    # ── FIX: heartbeat cuma LOG, JANGAN kirim/edit message ke user.
                    # Status internal seperti ⏳ Working.. harusnya tidak muncul
                    # di chat — itu info debugging, bukan user-facing.
                    log.debug("heartbeat chat=%s: %s", chat_id, text)

                async def _delete_later(secs: float, _msg=None):
                    await asyncio.sleep(secs)
                    _target = _msg if _msg is not None else status_msg
                    if _target is not None:
                        try:
                            await client.delete_messages(chat_id, _target.id)
                        except Exception:
                            pass

                async def _on_clarify(question: str):
                    # Agent nanya → relay pertanyaan ke user + tandain pending:
                    # pesan berikutnya dari user = jawaban clarify (bukan prompt baru).
                    try:
                        _pending_clarify[chat_id] = True
                        await event.respond(f"❓ {question}")
                    except Exception:
                        pass

                # watchdog: apa pun yang hang di dalem (Telegram send, tmux,
                # API) → ke-cut di sini, session di-kill, bot tetep responsif
                reply_text = ""
                try:
                    # hard timeout 0 = nggak ada batas (kayak Hermes asli —
                    # CLI recovery jalan sampe kelar). Kalau di-set → +60 buffer.
                    _hard_t = (
                        None if config.HERMES_HARD_TIMEOUT <= 0
                        else config.HERMES_HARD_TIMEOUT + 60
                    )

                    # ── HISTORY PROVIDER: fetch N pesan terakhir (grup + private)
                    # buat session baru. Format dengan timestamp + reply thread
                    # (agent tau siapa reply ke siapa). Termasuk pesan akun
                    # userbot sendiri (biar agent tau reply-nya sendiri dulu).
                    _admin_cache: dict = {}  # chat_id → (timestamp, header)
                    _ADMIN_TTL = 600  # cache admin/owner 10 menit

                    async def _fetch_admin_header(_chat_id: int) -> str:
                        # cache biar nggak fetch ulang tiap session spawn
                        import time as _t
                        _now = _t.time()
                        if _chat_id in _admin_cache and _now - _admin_cache[_chat_id][0] < _ADMIN_TTL:
                            return _admin_cache[_chat_id][1]
                        try:
                            chat_ent = await client.get_entity(_chat_id)
                            is_group = getattr(chat_ent, "participants_count", None) is not None or getattr(chat_ent, "megagroup", False)
                            if not is_group:
                                _admin_cache[_chat_id] = (_now, "")
                                return ""
                            from telethon.tl.types import ChannelParticipantsAdmins
                            admins = await client.get_participants(
                                _chat_id, filter=ChannelParticipantsAdmins())
                            owner_name, admin_list = "", []
                            for a in admins:
                                aname = getattr(a, "first_name", "") or str(getattr(a, "id", ""))[:8]
                                rank = ""
                                try:
                                    rank = getattr(a.participant, "rank", "") or ""
                                except Exception:
                                    pass
                                try:
                                    part = a.participant
                                    is_creator = type(part).__name__.endswith("Creator")
                                except Exception:
                                    is_creator = False
                                if is_creator:
                                    owner_name = aname
                                else:
                                    admin_list.append(f"{aname}{(' [' + rank + ']') if rank else ''}")
                            parts = []
                            if owner_name:
                                parts.append(f"👑 Owner grup: {owner_name}")
                            if admin_list:
                                parts.append("🛡️ Admin: " + ", ".join(admin_list))
                            res = "\n".join(parts)
                            _admin_cache[_chat_id] = (_now, res)
                            return res
                        except Exception:
                            return ""

                    _obs_cursor: dict = {}  # chat_id → jumlah baris history.md yang udah di-inject

                    async def _fetch_history(_chat_id: int) -> str:
                        try:
                            n = config.GROUP_BACKFILL
                            if n <= 0:
                                return ""
                            # ── FASE 2 (observed delta): chat yang UDAH di-backfill
                            # → inject cuma baris history.md yang BARU (sejak
                            # interaksi terakhir) — marked observed, bukan request.
                            if _chat_id in _obs_cursor:
                                try:
                                    hp = _history_path(_chat_id)
                                    obs_lines = []
                                    if hp and os.path.exists(hp):
                                        with open(hp, encoding="utf-8") as _f:
                                            obs_lines = _f.read().splitlines()
                                    _cur = _obs_cursor.get(_chat_id, 0)
                                    new_lines = obs_lines[_cur:]
                                    _obs_cursor[_chat_id] = len(obs_lines)
                                    if new_lines:
                                        body = "\n".join(new_lines[-40:])
                                        return ("[Observed group context - context only, not requests]\n"
                                                + body)
                                    return ""
                                except Exception:
                                    return ""
                            # ── FASE 1 (backfill): session start → snapshot API
                            _msgs, _admin_head = await asyncio.gather(
                                asyncio.wait_for(client.get_messages(_chat_id, limit=n), timeout=25),
                                _fetch_admin_header(_chat_id),
                                return_exceptions=True,
                            )
                            # ── header owner/admin/title DI-TULIS ke history.md
                            # (top — nggak ke-roll!) → agent baca file dapet
                            # title member (kayak playa [kocokers]!) — fix 17:12.
                            if isinstance(_admin_head, str) and _admin_head:
                                try:
                                    _hp = _history_path(_chat_id)
                                    if _hp:
                                        with open(_hp, encoding="utf-8") as _f:
                                            _l = _f.read().splitlines()
                                        _hb = [x for x in _l if x.startswith(("👑", "🛡️"))]
                                        _hb = [x for x in _hb if x not in _admin_head.splitlines()]
                                        _admin_head = "\n".join(_hb + _admin_head.splitlines())
                                        with open(_hp, "w", encoding="utf-8") as _f:
                                            _f.write(_admin_head + "\n" + "\n".join(_l[len(_hb):]) + "\n")
                                except Exception:
                                    pass
                            if isinstance(_msgs, Exception):
                                return ""
                            msgs = _msgs
                            senders = {}
                            lines = []
                            _me_id = getattr(await client.get_me(), "id", 0)
                            for m in reversed(msgs):  # kronologis
                                if not m or m.message is None:
                                    continue
                                txt = (m.message or "").strip()
                                if not txt:
                                    continue
                                who = getattr(m.sender_id, "id", None) or getattr(m.sender_id, "", m.sender_id) if not isinstance(m.sender_id, int) else m.sender_id
                                # resolve sender name
                                name = "?"
                                try:
                                    ent = await client.get_entity(who) if who else None
                                    name = getattr(ent, "first_name", "") or getattr(ent, "title", "") or str(who)[:8]
                                except Exception:
                                    name = str(who)[:8]
                                senders[m.id] = name
                            for m in reversed(msgs):
                                if not m or m.message is None:
                                    continue
                                txt = (m.message or "").strip()
                                if not txt:
                                    continue
                                who = m.sender_id
                                name = senders.get(m.id, "?")
                                # reply thread: "B (reply ke A): ..."
                                rep = ""
                                try:
                                    if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
                                        rid = m.reply_to.reply_to_msg_id
                                        if rid in senders:
                                            rep = f" (reply ke {senders[rid]})"
                                except Exception:
                                    pass
                                ts = _fmt_ts(m.date)
                                if len(txt) > 200:
                                    txt = txt[:197] + "..."
                                lines.append(f"[{ts}]{rep} {name}: {txt}")
                            if not lines:
                                return ""
                            # ── HEADER: owner + admin + custom title (grup doang)
                            admin_head = _admin_head if not isinstance(_admin_head, Exception) else ""
                            head = f"📜 History {n} pesan terakhir chat ini (konteks awal):"
                            if admin_head:
                                head = admin_head + "\n" + head
                            # baseline cursor: observed delta mulai SETELAH backfill
                            try:
                                hp = _history_path(_chat_id)
                                if hp and os.path.exists(hp):
                                    with open(hp, encoding="utf-8") as _f:
                                        _obs_cursor[_chat_id] = len(_f.read().splitlines())
                                else:
                                    _obs_cursor[_chat_id] = 0
                            except Exception:
                                _obs_cursor[_chat_id] = 0
                            return head + "\n" + "\n".join(lines)
                        except Exception:
                            return ""

                    async def _group_history_provider(_chat_id: int) -> str:
                        """History provider HANYA untuk grup. Private chat: skip."""
                        if _chat_id >= 0:
                            return ""
                        return await _fetch_history(_chat_id)

                    reply_text = await asyncio.wait_for(
                        hermes_bridge.hermes_ask(
                            chat_id, sender_name, chat_title, text_for_agent,
                            on_update=stream_update, on_tool=on_tool,
                            user_id=getattr(sender, "id", 0),
                            msg_date=event.date,
                            on_start=_on_start,
                            on_clarify=_on_clarify,
                            on_heartbeat=_on_heartbeat,
                            on_state=_on_state,
                            history_provider=_group_history_provider,
                        ),
                        timeout=_hard_t,
                    )
                    log.info("phase chat=%s phase=agent_done elapsed=%.3fs", _chat_id, time.monotonic() - _run_t0)
                except asyncio.TimeoutError:
                    log.error("HERMES watchdog timeout — session di-kill")
                    await hermes_bridge.reset_sessions()
                    hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))
                    # heartbeat nyangkut juga ke-bersihin (bukan cuma run normal)
                    if hb_msg is not None:
                        try:
                            await client.delete_messages(chat_id, hb_msg.id)
                            log.info("heartbeat msg=%s dihapus (timeout path)", hb_msg.id)
                        except Exception as _he:
                            log.warning("heartbeat delete timeout-path gagal: %s", _he)
                    try:
                        await event.reply(
                            "⏱️ Proses terlalu lama (watchdog) — session di-reset. Kirim ulang ya."
                        )
                    except Exception:
                        pass
                    except Exception:
                        pass
                    _pending_clarify.pop(chat_id, None)
                    typing_task.cancel()
                    return
                # run kelar (atau timeout) → pending clarify dibersihin biar
                # pesan berikutnya nggak ke-makan sebagai jawaban clarify.
                _pending_clarify.pop(chat_id, None)
                if reply_text == hermes_bridge._STEERED:
                    # redirect /steer diterima — reply yang lagi jalan bakal
                    # incorporate koreksi ini; status redirect dihapus setelah 10s.
                    log.info("steer diterima, skip bubble dobel")
                    hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))
                    if status_msg is not None:
                        asyncio.create_task(_delete_later(10))
                    typing_task.cancel()
                    return

            typing_task.cancel()

            # heartbeat "⏳ Working" dihapus pas run kelar — reply bubble gantiin
            if hb_msg is not None:
                try:
                    await client.delete_messages(chat_id, hb_msg.id)
                    log.info("heartbeat msg=%s dihapus di chat=%s", hb_msg.id, chat_id)
                except Exception as e:
                    log.warning("heartbeat DELETE GAGAL msg=%s di chat=%s: %s", hb_msg.id, chat_id, e)

            # ── Single-VN: kalau user minta voice note, reply jadi 1 VN
            if is_vn and reply_text:
                if await _send_vn(event, chat_id, reply_text):
                    hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))
                    return

            # file yang agent sebut (MEDIA:/path) — pisahin dari teks, kirim belakangan
            reply_text, media_paths = _extract_media(reply_text)

            # STICKER marker: "STICKER:" / "STICKER: <emoji>" → kirim stiker
            # favorit sebagai PESAN TERPISAH + strip dari teks bubble (jangan
            # nongol di reply — kayak MEDIA:)
            # STICKER_PACK: <pack_name> → kirim stiker dari pack tertentu
            _st_m = re.search(r"STICKER:\s*(\S*)", reply_text)
            _pack_m = re.search(r"STICKER_PACK:\s*(\S+)", reply_text)
            if _pack_m:
                reply_text = re.sub(r"\s*STICKER_PACK:\s*\S+", "", reply_text).strip()
                pack_name = _pack_m.group(1)
                if not await _send_sticker_pack(event, pack_name):
                    await _send_faved_sticker(event, "")
            elif _st_m:
                reply_text = re.sub(r"\s*STICKER:\s*\S*", "", reply_text).strip()
                await _send_faved_sticker(event, _st_m.group(1))

            # ── AUTO-FIX: kalau agent kirim URL grup tanpa msg_id, append msg
            # terbaru dari chat ini supaya link bisa langsung dibuka.
            try:
                if chat_id and event.message:
                    _fix_url = re.sub(
                        r'(https://t\.me/c/\d+)(?!/)',
                        r"/" + str(event.message.id),
                        reply_text,
                    )
                    if _fix_url != reply_text:
                        reply_text = _fix_url
            except Exception:
                pass

            # edit final biar persis sama hasil agent (kayak Hermes: edit
            # streaming preview in-place — bukan delete + kirim ulang).
            if msg is not None and hermes_bridge.was_steered(chat_id, getattr(sender, "id", 0)):
                # ── FIX BUG: run ke-steer (user lain inject mid-run) — final
                # reply jangan nge-EDIT bubble stream user sebelumnya (bikin
                # reply user A keganti reply user B). Kirim sebagai pesan BARU.
                log.info("run ke-steer — final reply pesan baru (skip edit bubble lama)")
                try:
                    await client.delete_messages(chat_id, msg.id)
                except Exception:
                    pass
                msg = None
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

            # busy di-clear SETELAH reply terkirim penuh — pesan mid-run
            # berikutnya dianggap run baru (bukan redirect lagi)
            hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))

            # ── CLEANUP tool bubbles (kayak Hermes cleanup_progress): progress
            # udah kelihatan LIVE pas proses, tapi pas run selesai dihapus biar
            # chat nggak penuh sampah tool + urutannya bersih (cuma reply final).
            if tool_msgs:
                try:
                    await client.delete_messages(chat_id, tool_msgs)
                    log.info("cleanup %d tool bubble di chat=%s", len(tool_msgs), chat_id)
                except Exception as e:
                    log.warning("cleanup tool bubble gagal chat=%s: %s", chat_id, e)
            log.info("phase chat=%s phase=reply_sent elapsed=%.3fs", _chat_id, time.monotonic() - _run_t0)
        else:
            async with client.action(event.chat_id, "typing"):
                reply_text = await generate_reply(chat_id, text)
            await send_reply(event, reply_text)
            hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))
    except hermes_bridge.AgentError as e:
        # provider gagal setelah retry — kasih tau user (jangan diem), session
        # tetap hidup, user bisa ketik "continue" buat nyoba lagi
        log.warning("AgentError: %s", e)
        hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))
        try:
            typing_task.cancel()
        except Exception:
            pass
        try:
            _cont_cmd = (
                f"{config.REQUIRE_PREFIX} continue" if config.REQUIRE_PREFIX else "continue"
            )
            _why = "provider" if "provider" in str(e).lower() else "pengiriman/sesi"
            await event.reply(
                f"⚠️ Ada kendala ({_why}) pas proses — detail udah ke-log. "
                f"Session tetap hidup. Ketik '{_cont_cmd}' buat nyoba lagi ya.",
                link_preview=False,
            )
        except Exception:
            pass
        return
    except Exception as e:
        log.exception("%s error, lapor ke user", config.UB_MODE.upper())
        hermes_bridge.clear_busy(chat_id, getattr(sender, "id", 0))
        try:
            typing_task.cancel()
        except Exception:
            pass
        # ── FIX (audit): jangan DIAM — kasih tau user + solusi (kirim ulang /
        # continue). Error yang nggak ke-cover except lain (RuntimeError dll)
        # sebelumnya "staying silent" = user nggak tau apa-apa.
        try:
            _cont_cmd = (
                f"{config.REQUIRE_PREFIX} continue" if config.REQUIRE_PREFIX else "continue"
            )
            await event.reply(
                "⚠️ Ada kendala internal pas proses (udah ke-log). "
                f"Ketik '{_cont_cmd}' atau kirim ulang ya — session tetap hidup.",
                link_preview=False,
            )
        except Exception:
            pass
        return

@client.on(events.NewMessage(outgoing=True))
async def on_self_command(event):
    global enabled, self_trigger
    text = event.raw_text or ""
    stripped = text.strip().lower()
    # /switch_on / /switch_off — SELF-TRIGGER (akun sendiri bisa manggil agent
    # pake "SONNET ..." — outgoing). Diketik dari akun userbot.
    if stripped in ("/switch_on", "/switch_off"):
        self_trigger = stripped == "/switch_on"
        _save_self_trigger(self_trigger)
        if self_trigger:
            await event.edit("✅ **Self-trigger ON** — akun ini bisa manggil agent pake `SONNET ...`.")
        else:
            await event.edit("⏸ **Self-trigger OFF** — `SONNET ...` dari akun sendiri di-ignore.")
        return
    # self-trigger: outgoing "SONNET ..." → proses kayak pesan masuk (agent balas)
    if self_trigger and text.strip().upper().startswith((config.REQUIRE_PREFIX or "SONNET").upper()):
        log.info("self-trigger: outgoing SONNET di-chat=%s", event.chat_id)
        await _run_agent(event)
        return
    if not text.startswith(config.CMD_PREFIX):
        return
    cmd = text[len(config.CMD_PREFIX):].strip().lower()
    if cmd == "on":
        enabled = True
        _save_enabled(True)
        await event.edit("✅ Userbot ON")
    elif cmd == "off":
        enabled = False
        _save_enabled(False)
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

async def _session_reaper() -> None:
    """Kill session CLI yang idle lama (> SESSION_IDLE_KILL) — free RAM.
    Session ke-spawn ulang on-demand pas ada pesan berikutnya."""
    if config.SESSION_IDLE_KILL <= 0:
        return
    try:
        sessions = await hermes_bridge._run("list-sessions", "-F", "#{session_name}")
        for line in (sessions[0] or "").splitlines():
            name = line.strip()
            if not name.startswith("ub_"):
                continue
            try:
                pane = await hermes_bridge._run("capture-pane", "-p", "-S", "-30", "-t", name)
                import re as _re
                m = _re.search(r"✓\s+(\d+)s", pane[0] or "")
                idle = int(m.group(1)) if m else 0
                if idle > config.SESSION_IDLE_KILL:
                    await hermes_bridge._run("kill-session", "-t", name)
                    log.info("session %s idle %ds > %d — di-kill (free RAM)", name, idle, config.SESSION_IDLE_KILL)
            except Exception:
                continue
    except Exception as e:
        log.warning("session reaper gagal: %s", e)

async def _sweep_stale_heartbeats(limit_chats: int = 10, per_chat: int = 25) -> None:
    """Bersihin heartbeat '⏳ Working' yang nyangkut dari run sebelumnya
    (crash/timeout/restart). Scan chat aktif + hapus pesan yang match."""
    try:
        removed = 0
        async for dialog in client.iter_dialogs(limit=limit_chats):
            if not dialog.is_channel and not dialog.is_group and not dialog.is_user:
                continue
            try:
                async for msg in client.iter_messages(dialog.id, limit=per_chat):
                    t = (msg.text or "").strip()
                    if t.startswith("⏳ Working") or t.startswith("⏳ Working"):
                        await client.delete_messages(dialog.id, msg.id)
                        removed += 1
                        log.info("sweep: heartbeat nyangkut msg=%s di chat=%s dihapus", msg.id, dialog.id)
            except Exception as e:
                log.warning("sweep chat %s err: %s", dialog.id, e)
        if removed:
            log.info("sweep heartbeat selesai: %d pesan dihapus", removed)
    except Exception as e:
        log.warning("sweep heartbeat gagal: %s", e)

async def main():
    await client.start()
    me = await client.get_me()
    log.info("✅ Login sebagai %s (@%s)", me.first_name, me.username)
    # ── FIX: pending retry di-persist — survive restart (prompt gagal yang
    # belum di-"continue" tetep ada abis restart)
    try:
        hermes_bridge._load_pending_retry()
        log.info("pending retry di-load dari file")
    except Exception:
        pass
    # bersihin sesi tmux sisa dari run sebelumnya
    try:
        await hermes_bridge.reset_sessions()
    except Exception as e:
        log.warning("cleanup sesi gagal: %s", e)
    # bersihin heartbeat '⏳ Working' yang nyangkut dari run lama
    try:
        await _sweep_stale_heartbeats()
    except Exception as e:
        log.warning("sweep heartbeat startup gagal: %s", e)

    # ── PRE-WARM sessions (BACKGROUND task — nggak nge-block startup):
    # spawn CLI session grup/private yang aktif → pesan pertama nggak bayar
    # boot CLI. Jalan di background biar bot langsung listen.
    async def _prewarm_all():
        try:
            _pw_env = os.environ.get("UB_PREWARM_CHATS", "").strip()
            _seen = []
            try:
                import json as _json
                with open(config.STATE_FILE) as _f:
                    _st = _json.load(_f)
                _sc = _st.get("seen_chats", {}) or {}
                if isinstance(_sc, dict):
                    # {chat_id: ts} — sort by RECENCY (paling baru duluan)
                    _seen = [str(k) for k, _ in sorted(_sc.items(), key=lambda kv: kv[1], reverse=True)]
                else:
                    _seen = [str(c) for c in _sc]
            except Exception:
                pass
            _combined = []
            for _s in (list(_pw_env.split(",")) if _pw_env else []) + [str(c) for c in _seen]:
                _s = _s.strip()
                if _s and _s not in _combined:
                    _combined.append(_s)
            for _cid_s in _combined[:12]:  # max 12 session (RAM ~180MB/session — 2.2GB aman)
                # ── RAM SAFETY: free RAM < 1.5GB → STOP pre-warm (jangan bikin
                # VPS tekor — prioritas: RAM user aman!)
                try:
                    with open("/proc/meminfo") as _f:
                        for _l in _f:
                            if _l.startswith("MemAvailable:"):
                                _avail_mb = int(_l.split()[1]) // 1024
                                break
                    if _avail_mb < 1500:
                        log.warning("free RAM %sMB < 1.5GB — pre-warm stop (safety)", _avail_mb)
                        break
                except Exception:
                    pass
                for _att in range(2):  # retry 1× — boot kadang timeout pas beban
                    try:
                        # ── (20:05) balikin ke AWAL: pre-warm cuma "chat"
                        # (user 0) — TANPA format chat:user (user minta revert —
                        # banner di-HIDE via filter reply, bukan ubah session!)
                        _cid = int(_cid_s)
                        # ── FIX: pre-warm区分 private vs group context
                        # Private chat -> user_id = chat_id (session sandbox per-user)
                        # Group -> user_id = 0 (session group-wide)
                        _is_group = str(_cid).startswith('-100')
                        _ctx = hermes_bridge._resolve_context(_cid, 0 if _is_group else _cid)
                        _name = hermes_bridge._session_name(_cid, _ctx)
                        if not await hermes_bridge._has_session(_name):
                            await hermes_bridge._spawn_session(_cid, _ctx)
                            log.info("pre-warm session %s OK", _name)
                        break
                    except Exception as e:
                        if _att == 0:
                            log.warning("pre-warm %s gagal (retry): %s", _cid_s, e)
                            await asyncio.sleep(3)
                        else:
                            log.warning("pre-warm %s gagal final: %s", _cid_s, e)
        except Exception as e:
            log.warning("pre-warm loop gagal: %s", e)
    asyncio.create_task(_prewarm_all())

    # ── HISTORY SEARCH API (tool buat agent sandbox): agent jalanin
    # /srv/ubox/scripts/history_search.py '<query>' <chat_id> → endpoint ini
    # nyari history chat via Telethon → balikin hasil JSON.
    async def _start_history_api():
        try:
            from aiohttp import web as _web

            async def _hs_handler(request):
                q = (request.query.get("q") or "").strip()
                chat_raw = (request.query.get("chat") or "0").strip()
                limit = int(request.query.get("limit", "20"))
                try:
                    chat_id = int(chat_raw)
                except Exception:
                    return _web.json_response({"error": "chat wajib (integer)"}, status=400)
                if not q:
                    return _web.json_response({"error": "q wajib"}, status=400)
                # ── chat type + username (buat bikin link yang BENAR)
                chat_type, chat_uname = "group", ""
                try:
                    _ce = await client.get_entity(chat_id)
                    chat_type = "user" if getattr(_ce, "bot", None) is not None or (getattr(_ce, "first_name", None) is not None) else "group"
                    chat_uname = getattr(_ce, "username", "") or ""
                    if not chat_uname and hasattr(_ce, "usernames"):
                        try:
                            chat_uname = _ce.usernames[0].username if _ce.usernames else ""
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    # ── FIX (19:10): MULTI-TERM OR search — Claude-style.
                    # q bisa berisi beberapa istilah dipisah '|' (misal
                    # "archimedez|aldo|bang aldo") — tiap term di-search
                    # sendiri (Telethon search = literal), hasil digabung +
                    # dedupe by msg_id + diurutkan kronologis. Ini bikin
                    # agent bisa cari variasi nama/julukan/sinonim dalam
                    # SATU panggilan API, bukan cuma 1 kata kunci literal.
                    # ── FIX (19:35): KONTEKS SEKITAR MATCH (Claude-style).
                    # Tiap match di-expand: 2 pesan sebelum + 2 sesudah
                    # (window ±2) biar agent liat ALUR obrolan, bukan
                    # potongan terisolasi. Kalau match itu reply → sertakan
                    # pesan induknya (reply-chain expansion). Fetch konteks
                    # paralel + dibatasi max 12 match (anti rate-limit).
                    _terms = [t.strip() for t in q.split("|") if t.strip()] or [q]
                    _per_term = max(1, int(limit / len(_terms)) + 1)
                    _found = {}  # msg_id -> line
                    for _t in _terms:
                        try:
                            _msgs = await asyncio.wait_for(
                                client.get_messages(chat_id, search=_t, limit=_per_term),
                                timeout=20)
                        except Exception:
                            continue
                        for m in reversed(_msgs or []):
                            if not m or m.message is None:
                                continue
                            _found[m.id] = m
                    # ── ambil konteks sekitar tiap match (paralel, cap 12)
                    async def _ctx_window(mid: int) -> dict:
                        """2 sebelum + 2 sesudah + parent reply."""
                        out = {"before": [], "after": [], "parent": None}
                        try:
                            _w = await asyncio.wait_for(
                                client.get_messages(chat_id, min_id=mid - 3, max_id=mid + 3),
                                timeout=15)
                            for _m in (_w or []):
                                if not _m or _m.id == mid or _m.message is None:
                                    continue
                                _line = (_m.message or "").strip()[:150]
                                if not _line:
                                    continue
                                _nm = "?"
                                try:
                                    _ent = await client.get_entity(_m.sender_id) if _m.sender_id else None
                                    _nm = (getattr(_ent, "first_name", "") or getattr(_ent, "title", "") or str(_m.sender_id))[:16]
                                except Exception:
                                    _nm = str(_m.sender_id)[:8]
                                if _m.id < mid:
                                    out["before"].append(f"[{_fmt_ts(_m.date)}] {_nm}: {_line}")
                                else:
                                    out["after"].append(f"[{_fmt_ts(_m.date)}] {_nm}: {_line}")
                        except Exception:
                            pass
                        return out

                    async def _parent_msg(m: object) -> str:
                        """Kalau m itu reply, ambil teks pesan induknya."""
                        try:
                            _pid = m.reply_to.reply_to_msg_id if m.reply_to else None
                            if not _pid:
                                return ""
                            _p = await asyncio.wait_for(
                                client.get_messages(chat_id, ids=_pid), timeout=15)
                            if not _p or _p.message is None:
                                return ""
                            _pt = (_p.message or "").strip()[:150]
                            if not _pt:
                                return ""
                            _nm = "?"
                            try:
                                _ent = await client.get_entity(_p.sender_id) if _p.sender_id else None
                                _nm = (getattr(_ent, "first_name", "") or getattr(_ent, "title", "") or str(_p.sender_id))[:16]
                            except Exception:
                                _nm = str(_p.sender_id)[:8]
                            return f"[{_fmt_ts(_p.date)}] {_nm}: {_pt}"
                        except Exception:
                            return ""

                    _ids = sorted(_found.keys())
                    _ctx_targets = _ids[:_ctx_max] if (_ctx_max := 12) >= 0 else _ids
                    _ctxs = {}
                    if _ctx_targets:
                        _res = await asyncio.gather(
                            *[_ctx_window(i) for i in _ctx_targets],
                            return_exceptions=True)
                        _pres = await asyncio.gather(
                            *[_parent_msg(_found[i]) for i in _ctx_targets],
                            return_exceptions=True)
                        for _i, _mid in enumerate(_ctx_targets):
                            _c = _res[_i] if not isinstance(_res[_i], Exception) else {"before": [], "after": [], "parent": None}
                            _c["parent"] = _pres[_i] if not isinstance(_pres[_i], Exception) else ""
                            _ctxs[_mid] = _c

                    results = []
                    for _mid in _ids:
                        m = _found[_mid]
                        txt = (m.message or "").strip()[:200]
                        if not txt:
                            continue
                        name = "?"
                        try:
                            ent = await client.get_entity(m.sender_id) if m.sender_id else None
                            name = (getattr(ent, "first_name", "") or getattr(ent, "title", "") or str(m.sender_id))[:20]
                        except Exception:
                            name = str(m.sender_id)[:8]
                        ts = _fmt_ts(m.date)
                        rep = ""
                        try:
                            if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
                                rep = " (reply)"
                        except Exception:
                            pass
                        line = f"[{ts}]{rep} {name} (id={m.id}): {txt}"
                        _c = _ctxs.get(_mid)
                        if _c:
                            if _c["parent"]:
                                line += f"\n    ↳ REPLY KE: {_c['parent']}"
                            for _b in _c["before"][-2:]:
                                line += f"\n    ↖ SEBELUM: {_b}"
                            for _a in _c["after"][:2]:
                                line += f"\n    ↳ SESUDAH: {_a}"
                        results.append(line)
                    results = results[:limit]
                except Exception as e:
                    return _web.json_response({"error": f"search gagal: {e}"}, status=500)
                return _web.json_response({
                    "query": q, "count": len(results), "results": results,
                    "chat_type": chat_type, "chat_username": chat_uname,
                })

            _hs_app = _web.Application()
            _hs_app.router.add_get("/history_search", _hs_handler)
            _hs_runner = _web.AppRunner(_hs_app)
            await asyncio.wait_for(_hs_runner.setup(), timeout=10)
            _hs_site = _web.TCPSite(_hs_runner, "127.0.0.1", 9101)
            await asyncio.wait_for(_hs_site.start(), timeout=10)
            log.info("history-search API @ http://127.0.0.1:9101/history_search")
        except Exception as e:
            log.warning("history-search API gagal: %s", e)
    asyncio.create_task(_start_history_api())

    log.info("Mendengarkan... ketik '%son' / '%soff' buat toggle", config.CMD_PREFIX, config.CMD_PREFIX)

    # ── IDLE SESSION REAPER: kill session nganggur > SESSION_IDLE_KILL
    # (free RAM kalau banyak grup/private chat) — spawn ulang on-demand.
    async def _reaper_loop():
        while True:
            try:
                await asyncio.sleep(300)  # tiap 5 menit
                await _session_reaper()
            except Exception:
                await asyncio.sleep(300)
    asyncio.create_task(_reaper_loop())
    log.info("session reaper aktif (idle kill %ds)", config.SESSION_IDLE_KILL)

    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nbye")
