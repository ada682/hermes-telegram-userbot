# Changelog

## [2026-08-12] - Sticker Packs + Typing Fix

### Added
- Sticker packs configurable via `sticker_packs.json`
- Pack `patrick` (Gokujawa_by_fStikBot)
- Auto keyword mapping: `sticker meme` → `STICKER_PACK:memev2`

### Fixed
- Typing indicator instant start + 2s renew (tidak ilang)
- `GetStickerSetRequest` hash parameter
- Hapus fallback ke favorite sticker untuk explicit pack request
- Strip `STICKER_PACK:` marker di streaming path
- Reset sessions clear all state dicts on startup
- Format reply via `format_reply()` untuk bold/monospace/italic

## [2026-08-12] - Merge Window + Reply-to-Media + History Search v2

### Added
- **Merge window** (`UB_MERGE_WINDOW`, default 2.5s, private chat only): pesan panjang yang di-split Telegram jadi beberapa bubble digabung jadi SATU prompt utuh — bubble 2/3 tanpa wake word tidak di-skip lagi
- **Reply-to-media**: `SONNET coba lihat file ini` + reply ke file/gambar/voice → agent resolve & download media dari pesan yang di-reply (file path / OCR / transkrip masuk ke konteks)
- **History search multi-term**: query `a|b|c` dicari per istilah, hasil digabung + dedupe by msg_id + urut kronologis
- **History search konteks sekitar match**: window ±2 pesan (`SEBELUM`/`SESUDAH`) + reply-chain expansion (`REPLY KE`) — agent lihat alur obrolan, bukan potongan
- **Anti-nyerah-cepat**: agent wajib coba 3-6 variasi keyword (nama asli, panggilan, julukan, @username) sebelum menyimpulkan "nggak ada"
- **Delegation wajib tunggu hasil**: setelah `delegate_task`, agent TETAP di dalam turn sampai sub-agent selesai, baru rangkum + kirim — hasil tidak nyangkut di pane
- **Sticker pack fully configurable**: format baru `{"set": "...", "keywords": [...]}` di `sticker_packs.json` — user baru tambah pack tanpa edit kode (keyword auto-map dari json, format lama string tetap didukung)

### Fixed
- **Paste-buffer fix**: `tmux send-keys` drop prompt >16KB diam-diam (`command too long`) — teks panjang sekarang via `tmux load-buffer` (stdin pipe) + `paste-buffer`, fallback chunk 10KB
- **Probe semua session baru**: pre-warmed session yang masih welcome screen tidak menelan prompt (fix stuck 197s)
- **Cooldown bug**: `HERMES_COOLDOWN=0` dianggap falsy → jatuh ke default 30s — sekarang `if cooldown is None`
- **Steering gate**: private chat tanpa tmux session aktif tidak di-steer (diproses normal via hermes_ask)
- **UnboundLocalError `name`** di steering path
- **config.py release**: syntax error blok API_KEY diperbaiki, `SESSION_FILE` default di-sanitize ke `ubot`
- **Temp file rule**: BRIDGE_PROMPT instruksi temp script wajib di dalam sandbox (`{sandbox}/tmp/`), bukan `/tmp` sistem

### Changed
- Semua sandbox: model `deepseek-v4-flash` / provider `deepseek` / `api.deepseek.com` / max_tokens 16384 / reasoning max
- Template base sandbox ikut di-patch (grup baru auto-dapat model baru)
