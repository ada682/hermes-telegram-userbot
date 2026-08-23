#!/bin/bash
# sync_mod.sh — sync sistem moderation (mod_actions.py + session + rules + skill)
# ke semua sandbox grup yang punya session agent aktif.
# Run: sudo bash /root/telegram-userbot/sync_mod.sh

MAIN_DIR="/root/telegram-userbot"
ACTIVE=$(tmux -S /tmp/tmux-997/default ls 2>/dev/null | awk -F: '{print $1}' | sed 's/^ub_//; s/_/ /g' | awk '{print $1}' | sort -u)

echo "session aktif: $ACTIVE"

for id in $ACTIVE; do
    SBX="/srv/ubox/groups/$id"
    [ -d "$SBX" ] || continue
    mkdir -p "$SBX/mod"
    cp "$MAIN_DIR/mod_actions.py" "$SBX/mod/mod_actions.py"
    cp "$MAIN_DIR/kinji_userbot_mod.session" "$SBX/mod/kinji_userbot_mod.session" 2>/dev/null
    if [ ! -f "$SBX/mod/mod_rules.json" ]; then
        cp "$MAIN_DIR/mod_rules.json" "$SBX/mod/mod_rules.json"
    fi
    chown -R ubox:ubox "$SBX/mod" 2>/dev/null
    # skill
    SKILL_DIR="$SBX/.hermes/skills/group-moderation"
    mkdir -p "$SKILL_DIR"
    cp "$MAIN_DIR/skill-group-moderation.md" "$SKILL_DIR/SKILL.md"
    chown -R ubox:ubox "$SKILL_DIR" 2>/dev/null
    echo "synced: $id (mod + skill)"
done
echo "DONE"
