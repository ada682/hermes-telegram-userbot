# Telegram Userbot — Auto-reply LLM

Userbot yang dengerin chat lu dan auto-balas pakai backend OpenAI-compatible
(default: backend yang sama kayak Hermes — key-nya dipakai otomatis).

## Setup (sekali doang)

```bash
cd /root/telegram-userbot
pip3 install -r requirements.txt
```

1. Buka **https://my.telegram.org** → login pake akun Telegram lu
2. Masuk menu **API development tools** → bikin app baru
3. Salin **api_id** dan **api_hash** ke file `.env`:

```
TG_API_ID=123456
TG_API_HASH=abcdef...
```

4. Login bikin session (sekali aja, bakal minta nomor + kode OTP):

```bash
python3 login.py
```

5. Jalanin bot:

```bash
python3 main.py
```

## Cara kerja

- **DM pribadi** → semua chat dibales otomatis
- **Grup** → cuma bales kalau lu di-*mention* (@username), di-reply,
  atau username lu disebut
- Ada **cooldown** per chat biar nggak kelihatan kayak spam bot
- Bot **nggak bales bot lain** (hindari loop)
- Bot **nggak bales pesan sendiri**

## Mode jawaban

| Mode | Fungsi |
|---|---|
| `hermes` (default) | Tiap pesan diteruskan ke **agent Hermes penuh** — pake memory, skills, dan web search. Balas natural gaya pemilik akun, dan **jujur kalau ditanya "lu bot?"**. Session per-chat di-resume, jadi konteks obrolan nyambung. |
| `llm` | Raw API call ke model (lebih cepat, tapi tanpa memory/skills Hermes) |

**Keamanan mode hermes:** agent jalan sebagai **user OS terpisah** (`ubox`) yang
cuma punya akses ke `/srv/ubox` — permission Linux yang ngejagain, bukan cuma
prompt. `/root` (project lu), docker, dan file sistem lain **nggak kebaca**.
Toolset: `skills,memory,web,todo,session_search,terminal,file`.

## Sandbox folder

- Agent Hermes di userbot jalan sebagai user `ubox` (bukan root)
- Satu-satunya folder yang bisa dia baca/tulis: **`/srv/ubox`**
- User di chat bisa minta agent **bikin bot/script dan jalanin** — tapi hasilnya
  cuma di `/srv/ubox`, nggak bisa nyentuh luar
- **Docker lu aman**: `ubox` nggak di grup docker, socket nggak bisa diakses
- Skill di-copy dari profile utama ke `/srv/ubox/.hermes/skills/` — sync manual:
  `bash /usr/local/bin/sync-ubox-skills.sh`
- Agent punya memory/profile sendiri yang terpisah (nggak bisa baca memory utama
  yang isinya kredensial VPS lu)

## Perintah (diketik sendiri dari akun lu)

| Perintah | Fungsi |
|---|---|
| `.ub on` | aktifkan auto-reply |
| `.ub off` | matikan auto-reply |
| `.ub status` | cek status + model |
| `.ub reset` | bersihkan konteks chat |

## Konfigurasi (opsional, di `.env`)

| Var | Default | Fungsi |
|---|---|---|
| `UB_MODE` | `hermes` | `hermes` = agent Hermes penuh \| `llm` = raw API |
| `UB_MODEL` | `deepseek/deepseek-v4-flash-0731` | model LLM (mode `llm`) |
| `UB_BASE_URL` | `https://inference-api.nousresearch.com/v1` | endpoint OpenAI-compatible |
| `UB_API_KEY` | (otomatis pakai key Hermes) | key API sendiri kalau mau ganti |
| `UB_HERMES_TOOLSETS` | `skills,memory,web,todo,session_search,terminal,file` | tools yang boleh dipakai agent |
| `UB_SANDBOX_USER` | `ubox` | user OS terpisah buat jalanin agent |
| `UB_SANDBOX_HOME` | `/srv/ubox` | satu-satunya folder yang bisa diakses agent |
| `UB_HERMES_TIMEOUT` | `180` | detik maksimal nunggu agent jawab |
| `UB_HERMES_COOLDOWN` | `45` | detik jeda per chat (mode hermes) |
| `UB_COOLDOWN` | `30` | detik jeda per chat (mode llm) |
| `UB_HISTORY` | `12` | jumlah pesan konteks (mode llm) |
| `UB_PERSONA` | (default) | prompt kepribadian (mode llm) |
| `UB_MAX_TOKENS` | `800` | panjang maksimal balasan (mode llm) |
| `UB_DANGER_PHRASE` | `gua tolak jing kontoljuga lu ya` | balasan kalau ada yang nyuruh hal berbahaya |

## Proteksi server

Kalau ada orang di chat yang nyuruh bot ngejalanin perintah destruktif
(`rm -rf /`, `shutdown`, fork bomb, `dd` ke disk, `docker rm -f`, dsb),
bot **nolak otomatis** — nggak diterusin ke agent — dan bales dengan
`UB_DANGER_PHRASE`.

Selain itu ada filter **exfil**: minta baca/kirim data sensitif
(`.env`, API key, seed phrase wallet, `.ssh`, password, dsb) juga di-block.
Ditambah lagi di mode `hermes`, agent nggak punya akses terminal/file sama sekali.

## Catatan

- Userbot = akun lu yang jalan otomatis. Kalau dipakai spam, **akun bisa kena
  limit/ban** sama Telegram. Makanya ada cooldown — jangan kecilin ke 0 kalau
  nggak mau akun di-flag.
- Jangan jalanin userbot di grup yang nggak lu kenal/rawan report.
- Session file (`*.session`) itu rahasia — kalau bocor, orang bisa masuk akun lu.
