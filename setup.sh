#!/bin/bash
# setup.sh — installer Telegram Userbot (Hermes auto-reply)
# Jalanin sebagai root: sudo bash setup.sh
set -e

echo "==> Install dependencies"
pip3 install -r requirements.txt || pip install -r requirements.txt

echo "==> Setup sandbox user 'ubox'"
if ! id ubox >/dev/null 2>&1; then
    useradd -r -m -d /srv/ubox ubox
fi
mkdir -p /srv/ubox/users /srv/ubox/groups /srv/ubox/.hermes/memories
chown -R ubox:ubox /srv/ubox

echo "==> Seed template persona + SOUL (opsional — lewatin kalau nggak mau)"
[ -f USER.md.example ] && cp -n USER.md.example /srv/ubox/.hermes/memories/USER.md || true
[ -f SOUL.md.example ] && cp -n SOUL.md.example /srv/ubox/.hermes/SOUL.md || true

echo "==> .env"
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    .env dibuat dari .env.example — ISI dulu: nano .env"
else
    echo "    .env udah ada, skip"
fi

echo
echo "Langkah berikutnya:"
echo "  1. Isi .env  (TG_API_ID, TG_API_HASH, provider key)"
echo "  2. Install Hermes CLI: https://hermes-agent.nousresearch.com/docs"
echo "  3. Bikin /srv/ubox/.hermes/config.yaml (model + provider) — liat README"
echo "  4. python3 login.py   → login Telegram"
echo "  5. python3 main.py    → jalan"
echo "  (atau pakai userbot.service.example buat systemd)"
