<div align="center">

# 🤖 Telegram Userbot — Hermes Auto-Reply

**A full agent-powered Telegram userbot: streaming replies, tool bubbles, sandboxed multi-agent, voice notes, file sharing — running on the Hermes agent.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Telethon](https://img.shields.io/badge/Telethon-1.34%2B-2CA5E0?logo=telegram&logoColor=white)](https://github.com/LonamiWebs/Telethon)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Hermes](https://img.shields.io/badge/Powered%20by-Hermes%20Agent-8B5CF6)](https://hermes-agent.nousresearch.com)

</div>

---

## 🧠 What is this?

This userbot turns your Telegram account into an **AI agent on autopilot**. Instead of a dumb keyword-reply bot, it runs a **full Hermes agent** (the same agent you'd talk to here) inside a secure OS sandbox — so it can actually *do* things: analyze tokens, write scripts, run commands, spawn sub-agents, and send you the files it made. It streams replies like a human typing, shows tool bubbles as it works, and answers in the same language you write.

**Built for:** crypto group analysis, personal assistant, automation, or just having an agent on your Telegram.

---

## ✨ Features

| | |
|---|---|
| 🤖 **Full Hermes agent** | Runs `hermes --cli` in an isolated tmux session per context — real tool use, not canned replies |
| 💬 **Hermes-style streaming** | Reply types itself bubble-by-bubble + separate bubbles per tool |
| 🧵 **Per-context sessions** | Private = per-user; **groups share one context** (everyone in the group sees the same thread) |
| 🔀 **Multi-agent delegation** | Say *"use multiple agents"* → spawns up to 2 parallel sub-agents |
| 🛠️ **Full Hermes toolset** | Same toolsets as Hermes CLI: web, terminal, file, browser, skills, memory, delegation + vision, image_gen, tts, code_execution, kanban, **clarify** |
| 🏜️ **OS-level sandbox** | Agent runs as a separate `ubox` user — can ONLY touch `/srv/ubox`, never your server |
| 🎙️ **Voice notes** | Ask for a "vn" → reply is TTS'd (ElevenLabs); quoted text is read verbatim |
| 🎧 **Speech-to-text** | User sends a voice note + caption → transcribed locally (faster-whisper) into the agent's context |
| 📁 **File delivery** | Agent writes a file + `MEDIA:/path` → file lands in your chat |
| 🖼️ **Image vision** | User sends a screenshot → downloaded, attached to the agent as a vision image + OCR fallback (eng+ind) |
| 🔀 **Redirect / steer / queue** | Mid-run correction → `↪ Redirected current run` (like Hermes); other users' messages queue with a status bubble, auto-deleted when processing starts |
| ⏳ **Working heartbeat** | Runs longer than 60s → `⏳ Working — N min — iteration X/150` status bubble (edited in place), removed when the reply streams |
| ❓ **Clarify relay** | Agent asks a multiple-choice question → relayed to your chat; your next message answers it (no wake word needed) |
| 🎭 **Persona switch** | `SONNET persona breach` / `persona default` → `/personality` applied live to the session |
| ⏰ **Time-aware** | Every message is timestamped (WIB + morning/noon/evening/night) for the agent |
| 🌍 **Multilingual** | Agent auto-replies in the language of the incoming message |
| 🛡️ **Danger filter** | Destructive requests blocked before they ever reach the agent |
| ⏱️ **Auto-retry** | Provider hiccups (503) retried 3×, error bubble + "continue"; watchdog kills hung sessions with a friendly notice |

---

## 🏗️ Architecture

```
Telegram ──▶ main.py (Telethon)
                │
                ▼
         hermes_bridge.py ──▶ tmux session (ub_<chat>)
                │                │
                │                ▼
                │      hermes --cli --yolo  (as user "ubox", HERMES_HOME per context)
                │                │
                └── poll pane ───┘
                │
                ▼
        streaming + tool bubbles + files ──▶ Telegram
```

---

## 📋 Prerequisites

- Linux VPS/server (tested on Ubuntu 22.04)
- Python 3.10+
- **Hermes CLI** — [hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs) + a provider API key (Nous Research / DeepSeek / OpenAI-compatible)
- Telegram API ID/Hash from [my.telegram.org/apps](https://my.telegram.org/apps)
- (Optional) ElevenLabs API key for voice notes
- (Optional) `faster-whisper` for voice-note transcription (`pip3 install faster-whisper`)

---

## 🚀 Setup

### 1. Install

```bash
git clone <your-repo-url> && cd telegram-userbot
pip3 install -r requirements.txt
```

### 2. Create the sandbox user (required — agent runs isolated)

```bash
sudo bash setup.sh
```

Or manually:

```bash
sudo useradd -r -m -d /srv/ubox ubox
sudo mkdir -p /srv/ubox/users /srv/ubox/groups /srv/ubox/.hermes/memories
sudo chown -R ubox:ubox /srv/ubox
sudo cp USER.md.example /srv/ubox/.hermes/memories/USER.md   # optional persona
sudo cp SOUL.md.example /srv/ubox/.hermes/SOUL.md            # optional system prompt
```

### 3. Configure the sandbox Hermes profile

`/srv/ubox/.hermes/config.yaml` — point it at **your** model provider:

```yaml
model:
  default: deepseek/deepseek-v4-flash-0731   # ← any model you like
  provider: custom
  base_url: https://inference-api.nousresearch.com/v1
  api_key: ${HERMES_CUSTOM_INFERENCE_API_NOUSRESEARCH_COM_API_KEY}
```

> 💡 Swap `default`/`base_url`/`api_key` for OpenAI, Gemini, Claude, Grok, etc. — the project is provider-agnostic.

### 4. Configure `.env`

```bash
cp .env.example .env
nano .env   # fill in TG_API_ID, TG_API_HASH, provider key, ...
```

### 5. Login to Telegram

```bash
python3 login.py   # enter your number + OTP code
```

### 6. Run

```bash
python3 main.py
```

Or as a systemd service:

```bash
sudo cp userbot.service.example /etc/systemd/system/userbot.service
sudo nano /etc/systemd/system/userbot.service   # adjust User/WorkingDirectory/EnvironmentFile
sudo cp .env /etc/userbot.env
sudo systemctl daemon-reload
sudo systemctl enable --now userbot
```

---

## 🕹️ Usage

| Action | How |
|---|---|
| **Wake word** | Bot replies only to messages starting with `SONNET` (config: `UB_REQUIRE_PREFIX`) |
| **Admin commands** | `.ub on` / `.ub off` / `.ub status` / `.ub reset` |
| **Deep token analysis** | `SONNET bedah total CA ini 0x... pake beberapa agent` |
| **Multi-agent** | `SONNET <anything> use multiple agents` |
| **Voice note** | `SONNET vn dong <ask>` or `SONNET bikin vn ini teksnya "..."` |
| **Transcribe a voice note** | send a voice note with caption `SONNET <anything>` → audio is transcribed + included |
| **Get a file** | ask for a script/report → agent writes it → `MEDIA:/path` delivers it |
| **Switch persona** | `SONNET persona breach` (any personality in the sandbox config) — `persona default` resets |
| **Answer an agent question** | when you see `❓ ...`, just reply with the number or text — no wake word needed |
| **Long-running tasks** | watch for `⏳ Working — N min` while the agent works |

---

## 🌍 Language

- Agent **auto-matches the language of the incoming message** — no config needed.
- Wake word `SONNET` is language-neutral.
- Force a language by adding an instruction to the context's `USER.md` (e.g. *"Always reply in English"*).
- Admin strings are English; `DANGER_PHRASE` is configurable via `UB_DANGER_PHRASE`.
- VN keywords (`vn`, `voice note`, `voice`, `suara`) are in `config.py` — add your language's words there.

---

## 📁 Project Layout

```
main.py             — Telegram handler (streaming, VN, filters, cooldown, media)
hermes_bridge.py    — spawn/poll Hermes CLI sessions, tool bubbles, HTML formatting
config.py           — all configuration via env vars
login.py            — create the Telegram session
.env.example        — config template (all placeholders, zero credentials)
SOUL.md.example     — sandbox agent system-prompt template
USER.md.example     — per-context persona template
userbot.service.example — systemd unit template
setup.sh            — one-shot installer (sandbox user + .env + seed templates)
.gitignore          — protects .env / *.session / __pycache__
```

---

## 🔒 Security Notes

- **Never commit** `.env`, `*.session`, or credential files — `.gitignore` has you covered.
- The agent runs as the `ubox` user — it **cannot** touch `/root`, docker, or system services.
- `hermes_bridge.py` carries HARD LIMITS in its prompt: the agent must never damage the host server or leak credentials into group chats (adjustable).
- A `is_dangerous`/`is_exfil` filter blocks destructive requests before they reach the agent.

---

## 📄 License

MIT — use it freely, modify it freely, you're responsible for what you do with it.
