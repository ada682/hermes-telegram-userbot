---
name: tts-voice-note
description: Use when a user asks for a VOICE NOTE / VN / voice / suara (TTS). The user's request text gets read by a TTS engine — you must reply with EXACTLY the text to be spoken, nothing else.
---

# TTS Voice Note — aturan bikin teks yang bakal dibacain suara

Pas user minta VN/voice note/suara, reply lo = **ISI VN-nya** (teks yang bakal
dibacain TTS kata demi kata). Bukan laporan, bukan konfirmasi, bukan deskripsi.

## Aturan wajib

1. **User nyebut teks spesifik** ("vn i love you", "vn in aku goodnight sayang",
   "vn bilang selamat pagi", "vn yang ...") → balas HANYA teks itu, PERSIS,
   tanpa nambahin kata apa pun. Kata sambung kayak "in/isi/bilang/yang" di
   antara "vn" dan teksnya BUKAN bagian teks.
2. **User gak nyebut teks spesifik** ("kirim vn ngaji", "vn dong", "vn buat grup")
   → bikin teks singkat natural sesuai konteks: 2-3 kalimat maksimal, bahasa
   santai, enak dibacain.
3. **JANGAN PERNAH** nulis di reply: "vn nya udah gw bikin", "file ada di",
   "MEDIA:", "udah gw kirim", "nanti gw kirim" — semua itu bakal KEBACA di VN.
   Reply = isi VN, bukan status proses.
4. Gak usah format HTML/markdown/bold — teks polos aja.
5. Kalau user minta terjemahan/ngaji/doa/dll via VN — teksnya yang di-TTS
   (terjemahan/doanya), bukan komentar lo.
6. Bahasa NGIKUT USER: user chat Indonesia → VN Indonesia santai (kayak
   biasa); user chat English → English. Gaya bahasa gak berubah — yang
   diatur cuma ISI (jangan laporan, jangan nambahin kalimat).

## Contoh

- User: "sonnet vn i love you" → reply: `i love you`
- User: "vn in aku goodnight sayang" → reply: `aku goodnight sayang`
- User: "kirim vn ngaji untuk grup ini" → reply: teks dzikir/doa singkat
- User: "vn bilang selamat pagi semua" → reply: `selamat pagi semua`
- SALAH: "oke udah gw bikin vn nya ya" ← ini yang ke-dibacain TTS!

## Pitfalls

- `_extract_quoted_text` di userbot otomatis nangkep teks spesifik (quote/
  setelah "vn") — kalau ke-detect, TTS langsung tanpa nunggu agent. Tapi
  tetep ikutin aturan di atas kalo agent yang bikin teksnya.
- Jangan nambahin "katanya", "bilangnya", dll — teks lo PERSIS yang dibacain.
