# Hermes Telegram Userbot - Update

## Perubahan Terbaru

### Sticker Packs via `sticker_packs.json`

Sticker packs sekarang dikonfigurasi via file `sticker_packs.json` (bukan hardcoded).

Pack yang tersedia:
- `scuba` → `itstank_by_fStikBot`
- `cewe` → `Vicidior`
- `kucing` → `hahahihi567_by_fStikBot`
- `kucingv2` → `enjaberryys_by_fStikBot`
- `spongebob` → `YangBroRasakanJrC1p0y`
- `kucingv3` → `TikTok_cats_animals`
- `crypto` → `Swagsmonke`
- `patrick` → `Gokujawa_by_fStikBot`
- `meme` → `video1725002678_by_Marin_Roxbot`
- `memev2` → `Pler437`
- `nsfw` → `Bbgwithddk`

### Cara Pakai

User kirim: `sonnet sticker meme`
Agent otomatis mapping ke pack yang sesuai via keyword alias.

### Cara Menambah Pack Baru

1. Buka `sticker_packs.json`
2. Tambahkan entry baru:
   ```json
   "nama_alias": "shortname_pack_telegram"
   ```
3. Restart userbot
4. User langsung bisa pake: `sonnet sticker nama_alias`

### Fix Lainnya

- Typing indicator instant + tidak ilang saat proses agent
- Hapus fallback ke favorite sticker untuk explicit pack request
- Strip `STICKER_PACK:` marker di streaming path
- `GetStickerSetRequest` pakai `hash=0` untuk compatibility
- Auto map keyword `sticker meme` → pack `memev2`, dsb
- Reset sessions bersih semua state saat startup
- Format reply konsisten bold/monospace/italic
