# Telegram Userbot — Auto-reply LLM (Hermes)

Userbot yang dengerin chat lu dan auto-balas pakai agent Hermes penuh
(memory, skills, web search) atau raw LLM API — jalan sebagai akun lu sendiri.

## Fitur

- **Agent Hermes per-chat**: tiap DM/grup punya session sendiri, konteks nyambung
- **Ganti model per-chat**: `.ub models <nomor>` — tiap chat bisa beda model
- **Ganti model semua**: `.ub models all <nomor>` — satu perintah buat seluruh fleet
- **Moderasi otomatis**: deteksi promo (Mt5, XyloMarket, Password Trading, dll) →
  mute + hapus pesan, tanpa agent (0 credit)
- **Deteksi forward**: pesan forward di-tag `[PESAN FORWARD dari: ...]` biar agent tau
- **Self-trigger**: ketik `SONNET ...` dari akun lu → agent bales (optional)
- **Staff watchdog**: hapus/kelola pesan staff grup otomatis
- **Session reaper**: session idle di-kill (hemat resource)

## Setup (sekali doang)

```bash
cd /root/telegram-userbot
pip3 install -r requirements.txt
```

1. Buka **https://my.telegram.org** → login pake akun Telegram lu
2. Masuk menu **API development tools** → bikin app baru
3. Salin **api_id** dan **api_hash** ke file `.env` (contoh: `.env.example`):

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
- Bot **nggak bales bot lain** (hindari loop) dan **nggak bales pesan sendiri**
- Pesan **forward** di-tag biar agent tau itu kiriman, bukan teks asli sender
- Pesan promo yang kena rule **moderasi** → mute + delete otomatis (tanpa agent)

## Mode jawaban

| Mode | Fungsi |
|---|---|
| `hermes` (default) | Tiap pesan diteruskan ke **agent Hermes penuh** — pake memory, skills, dan web search. Balas natural gaya pemilik akun, dan **jujur kalau ditanya "lu bot?"**. Session per-chat di-resume, jadi konteks obrolan nyambung. |
| `llm` | Raw API call ke model (lebih cepat, tapi tanpa memory/skills Hermes) |

**Keamanan mode hermes:** agent jalan sebagai **user OS terpisah** (`ubox`) yang
cuma punya akses ke `/srv/ubox` — permission Linux yang ngejagain, bukan cuma
prompt. `/root` (project lu), docker, dan file sistem lain **nggak kebaca**.

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

## Perintah (diketik sendiri dari akun lu — cuma owner yang bisa)

| Perintah | Fungsi |
|---|---|
| `.ub on` / `.ub off` | aktifkan / matikan auto-reply |
| `.ub status` | cek status + model aktif di chat ini |
| `.ub models` | list model + tandain yang aktif di chat ini |
| `.ub models <nomor>` | ganti model chat ini doang (per-chat override) |
| `.ub models all <nomor>` | ganti model SEMUA user + grup |
| `.ub reset` | bersihkan konteks chat |
| `.ub tools active/inactive` | tampilkan / sembunyikan bubble progress tool |
| `.ub help` | list semua command |

Model list (`config.py` → `MODEL_LIST`):
1. `deepseek-v4-pro` — DeepSeek V4 Pro
2. `deepseek-v4-flash` — DeepSeek V4 Flash (default)

Override per-chat disimpan di `chat_models.json` + di-patch ke
`config.yaml` sandbox chat itu (biar model beneran kepake, bukan cuma env).

## Konfigurasi (opsional, di `.env` — lihat `.env.example`)

| Var | Default | Fungsi |
|---|---|---|
| `UB_MODE` | `hermes` | `hermes` = agent Hermes penuh \| `llm` = raw API |
| `UB_MODEL` | `deepseek-v4-flash` | model default agent |
| `UB_BASE_URL` | (dari env hermes) | endpoint OpenAI-compatible |
| `UB_API_KEY` | (otomatis pakai key Hermes) | key API sendiri kalau mau ganti |
| `UB_HERMES_TOOLSETS` | `skills,memory,web,todo,session_search,terminal,file` | tools yang boleh dipakai agent |
| `UB_SANDBOX_USER` | `ubox` | user OS terpisah buat jalanin agent |
| `UB_SANDBOX_HOME` | `/srv/ubox` | satu-satunya folder yang bisa diakses agent |
| `UB_HERMES_TIMEOUT` | `180` | detik maksimal nunggu agent jawab |
| `UB_HERMES_HARD_TIMEOUT` | `600` | detik hard timeout (kill paksa) |
| `UB_HERMES_COOLDOWN` | `45` | detik jeda per chat (mode hermes) |
| `UB_COOLDOWN` | `30` | detik jeda per chat (mode llm) |
| `UB_HISTORY` | `12` | jumlah pesan konteks (mode llm) |
| `UB_PREWARM_CHATS` | (kosong) | chat yang di-prewarm pas boot (cepat respon) |
| `UB_GROUP_BACKFILL` | `false` | backfill history grup |
| `UB_MAX_TOKENS` | `800` | panjang maksimal balasan (mode llm) |
| `UB_DANGER_PHRASE` | (default) | balasan kalau ada yang nyuruh hal berbahaya |

## Moderasi otomatis

Rules di `mod_rules.json` (master) / `/srv/ubox/groups/*/mod/mod_rules.json`:

```json
{"pattern": "XyloMarket", "action": ["mute", "delete_all"], "scope": "chat_id_grup", "forward_from_channel": true}
```

- `pattern` = kata kunci (case-insensitive, substring match)
- `action` = `mute` (mute user) + `delete_all` (hapus semua pesan promo)
- `forward_from_channel: true` = rule cuma jalan untuk pesan yang di-forward dari channel
- Owner (akun userbot) gak pernah kena moderasi

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
  Jangan commit ke repo.
