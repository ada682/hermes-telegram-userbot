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
