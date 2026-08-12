import os


def load_dotenv(path=".env"):
    """Mini dotenv loader — no extra dependency."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip()
            if not val:
                continue  # skip placeholder kosong
            os.environ.setdefault(key.strip(), val)


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Telegram API credentials (ambil dari https://my.telegram.org → API development tools)
API_ID = os.environ.get("TG_API_ID", "")  # set via env
API_HASH = os.environ.get("TG_API_HASH", "")  # set via env
SESSION_FILE = os.environ.get("UB_SESSION", "ubot")  # set via env

# ── LLM backend (OpenAI-compatible). Default: reuse backend Hermes.
BASE_URL = os.environ.get("UB_BASE_URL", "https://inference-api.nousresearch.com/v1").rstrip("/")
API_KEY = os.environ.get("UB_API_KEY", "")  # set via env
    os.environ.get("UB_API_KEY")
    or os.environ.get("HERMES_CUSTOM_INFERENCE_API_NOUSRESEARCH_COM_API_KEY")
    or ""
)
MODEL = os.environ.get("UB_MODEL", "deepseek/deepseek-v4-flash-0731")
MAX_TOKENS = int(os.environ.get("UB_MAX_TOKENS", "800"))
TEMPERATURE = float(os.environ.get("UB_TEMPERATURE", "0.7"))
API_TIMEOUT = float(os.environ.get("UB_TIMEOUT", "60"))

# ── Behavior
UB_MODE = os.environ.get("UB_MODE", "hermes")            # hermes (agent penuh) | llm (raw API)
HERMES_TOOLSETS = os.environ.get(
    "UB_HERMES_TOOLSETS",
    "skills,memory,web,todo,session_search,terminal,file,browser,cronjob,delegation,"
    "vision,image_gen,tts,code_execution,kanban,clarify",
)
HERMES_TIMEOUT = float(os.environ.get("UB_HERMES_TIMEOUT", "120"))  # detik IDLE (nggak ada aktivitas) sebelum dianggap macet
# HARD TIMEOUT: 0 = NONAKTIF (sama kayak Hermes asli — CLI recovery
# kontinuasi/fallback/synthesizing JALAN SAMPE KELAR, nggak ada yang nge-cut).
HERMES_HARD_TIMEOUT = float(os.environ.get("UB_HERMES_HARD_TIMEOUT", "0"))
HERMES_COOLDOWN = float(os.environ.get("UB_HERMES_COOLDOWN", "0"))  # detik jeda per user (0 = tanpa delay)
COOLDOWN = int(os.environ.get("UB_COOLDOWN", "30"))       # detik jeda antar balasan per chat (mode llm)
MAX_MSG_CHARS = int(os.environ.get("UB_MAX_MSG_CHARS", "3500"))  # pecah reply panjang jadi beberapa pesan
MSG_DELAY = float(os.environ.get("UB_MSG_DELAY", "0.8"))   # jeda antar bubble (biar kayak orang ngetik)
SHOW_TOOL_BUBBLES = os.environ.get("UB_SHOW_TOOLS", "1") == "1"  # kirim bubble terpisah tiap tool jalan (Hermes style)
HIDE_TOOL_BUBBLES = set(
    os.environ.get("UB_HIDE_TOOLS", "session_search,todo").split(",")
)  # tool internal yang nggak usah dimunculin (spam) — memory SEKARANG DIMUNCULIN
GROUP_BACKFILL = int(os.environ.get("UB_GROUP_BACKFILL", "50"))  # history grup yang di-inject pas session baru (0 = mati)
HISTORY_ROLLING = int(os.environ.get("UB_HISTORY_ROLLING", "100"))  # rolling window history.md (baris)
SESSION_IDLE_KILL = int(os.environ.get("UB_SESSION_IDLE_KILL", "1800"))  # session CLI di-kill kalau idle > N detik (0 = mati) — biar RAM aman kalau banyak chat
REQUIRE_PREFIX = os.environ.get("UB_REQUIRE_PREFIX", "").strip()  # wake word: cuma bales kalau pesan DIAWALI kata ini (misal SONNET). Kosong = bales semua (aturan lama).
# state file buat persist toggle /switch_on / /switch_off (dan .on/.off)
STATE_FILE = os.environ.get("UB_STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))

# ── Voice note (ElevenLabs TTS): kalau user minta "voice note", reply-nya
# dikonversi jadi VN (.ogg) pake API ini.
ELEVENLABS_API_KEY = os.environ.get("UB_API_KEY", "")  # set via env
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "cgSgspJ2msm6clMCkdW9")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")
ELEVENLABS_VOLUME = float(os.environ.get("ELEVENLABS_VOLUME", "0.85"))  # 1.0 = asli, 0.85 = agak pelan
ELEVENLABS_SPEED = float(os.environ.get("ELEVENLABS_SPEED", "0.85"))  # via ffmpeg atempo (API speed di-ignore model flash). 1.0=kenceng, 0.85=santai, 0.5=slowmo
# Intonasi natural (nada naik-turun kayak orang asli): stability RENDAH = makin
# ekspresif/bervariasi, style = makin berasa dramatis, speaker_boost = suara nempel
ELEVENLABS_STABILITY = float(os.environ.get("ELEVENLABS_STABILITY", "0.2"))
ELEVENLABS_STYLE = float(os.environ.get("ELEVENLABS_STYLE", "0.6"))
ELEVENLABS_SPEAKER_BOOST = os.environ.get("ELEVENLABS_SPEAKER_BOOST", "1") == "1"
VN_KEYWORDS = ("voice note", "vn", "suara", "vn dong", "vn dong bang", "bikin vn", "voice")  # keyword user → reply jadi VN

# ── Sandbox: agent hermes jalan sebagai user terpisah, cuma bisa akses folder ini
SANDBOX_USER = os.environ.get("UB_SANDBOX_USER", "ubox")
SANDBOX_HOME = os.environ.get("UB_SANDBOX_HOME", "/srv/ubox")
SANDBOX_USERS = os.environ.get("UB_SANDBOX_USERS", "/srv/ubox/users")  # per-user sandbox: tiap user punya folder + HERMES_HOME sendiri (memory/session/cron sendiri)
SANDBOX_GROUPS = os.environ.get("UB_SANDBOX_GROUPS", "/srv/ubox/groups")  # per-grup sandbox: session + memory SHARED antar user di grup yang sama
HISTORY_LEN = int(os.environ.get("UB_HISTORY", "12"))     # jumlah pesan konteks yang diingat per chat
CMD_PREFIX = os.environ.get("UB_CMD_PREFIX", ".ub")       # perintah dari akun sendiri
STICKER_PACKS = {
    "scuba": ["scuba", "diving", "diver", "selam"],
    "cewe": ["girl", "woman", "cewek", "cewe", "cantik", "beautiful", "cute"],
    "kucing": ["cat", "kucing", "meow", "kitty", "kitten"],
    "kucingv2": ["cat", "kucing", "meow", "kitty", "kitten"],
    "spongebob": ["spongebob", "bob", "sponge", "patrick", "starfish"],
    "kucingv3": ["cat", "kucing", "meow", "kitty", "kitten", "tiktok"],
    "crypto": ["crypto", "bitcoin", "money", "monkey", "ape", "diamond"],
    "meme": ["meme", "funny", "lol", "haha", "joke", "comedy"],
    "memev2": ["meme", "funny", "lol", "haha", "joke", "comedy"],
}

DANGER_PHRASE = os.environ.get(
    "UB_DANGER_PHRASE",
    "gua tolak jing kontoljuga lu ya",
)  # balasan kalau ada yang nyuruh hal berbahaya ke server
PERSONA = os.environ.get(
    "UB_PERSONA",
    "You are a helpful assistant that replies as the Telegram account owner. "
    "Reply in the same language and tone as the incoming message (use casual Indonesian slang if the message is Indonesian). "
    "Keep replies short and natural, like a real person texting. "
    "Never mention that you are an AI or a bot. Stay helpful and direct.",
)
