#!/usr/bin/env bash
# =============================================================================
# diagnose_rclone.sh — Diagnose why notes aren't syncing to Google Drive
# Run this on the HOST machine (the VPS running the bot), not inside Docker.
# Usage: bash diagnose_rclone.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS="✅"
FAIL="❌"
WARN="⚠️"

echo "============================================"
echo "  Rclone / Vault Sync Diagnostic"
echo "============================================"
echo ""

# --- 1. rclone installation ---
echo "--- 1. rclone installation ---"
if command -v rclone &>/dev/null; then
    echo -e "${PASS} rclone found: $(which rclone)"
    rclone version | head -1
else
    echo -e "${FAIL} rclone NOT found in PATH"
fi
echo ""

# --- 2. rclone remote ---
echo "--- 2. rclone remote ---"
if rclone listremotes 2>/dev/null | grep -q .; then
    echo -e "${PASS} Remotes configured:"
    rclone listremotes | sed 's/^/  /'
else
    echo -e "${FAIL} No rclone remotes configured"
fi
echo ""

# --- 3. Vault host path ---
echo "--- 3. Vault host path ---"
HOST_PATH="${OBSIDIAN_VAULT_HOST_PATH:-/srv/obsidian-vault}"
echo "Expected host path: $HOST_PATH"
if [ -d "$HOST_PATH" ]; then
    echo -e "${PASS} Directory exists"
    echo "  Items: $(ls -1 "$HOST_PATH" 2>/dev/null | wc -l)"
    echo "  Latest:"
    ls -lt "$HOST_PATH" 2>/dev/null | head -5 | sed 's/^/    /'
else
    echo -e "${FAIL} Directory does NOT exist"
fi
# --- 4. Is vault an rclone mount? ---
echo "--- 4. Is vault an rclone mount? ---"
if mount | grep -q "$HOST_PATH"; then
    echo -e "${PASS} $HOST_PATH is a mount point:"
    mount | grep "$HOST_PATH" | sed 's/^/  /'
else
    echo "${WARN} NOT a mount point (may use cron sync)"
fi
echo ""

# --- 5. rclone process ---
echo "--- 5. rclone process ---"
if pgrep -fa rclone 2>/dev/null | grep -q .; then
    echo -e "${PASS} rclone running:"
    pgrep -fa rclone | sed 's/^/  /'
else
    echo "${WARN} No rclone process (normal if using cron)"
fi
echo ""

# --- 6. Write test on host ---
echo "--- 6. Write test on host ---"
TEST_FILE="$HOST_PATH/.diag_test_$(date +%s).txt"
if echo "test" > "$TEST_FILE" 2>/dev/null; then
    echo -e "${PASS} Can write to $HOST_PATH"
    rm -f "$TEST_FILE"
else
    echo -e "${FAIL} CANNOT write to $HOST_PATH"
fi
echo ""
# --- 7. Docker container ---
echo "--- 7. Docker container ---"
if docker ps --format '{{.Names}}' | grep -q obsidian-agent; then
    echo -e "${PASS} Container 'obsidian-agent' running"
else
    echo -e "${FAIL} Container NOT running"
fi
echo ""

# --- 8. Bind mount inside container ---
echo "--- 8. Bind mount inside container ---"
if docker exec obsidian-agent ls /data/vault &>/dev/null; then
    echo -e "${PASS} /data/vault accessible in container"
    echo "  Items: $(docker exec obsidian-agent ls -1 /data/vault 2>/dev/null | wc -l)"
    echo "  Latest:"
    docker exec obsidian-agent ls -lt /data/vault 2>/dev/null | head -5 | sed 's/^/    /'
else
    echo -e "${FAIL} /data/vault NOT accessible in container"
fi
echo ""

# --- 9. Write test inside container ---
echo "--- 9. Write test inside container ---"
TEST_F=".diag_test_$(date +%s).txt"
if docker exec obsidian-agent bash -c "echo test > /data/vault/$TEST_F" 2>/dev/null; then
    echo -e "${PASS} Container can write to /data/vault"
    if [ -f "$HOST_PATH/$TEST_F" ]; then
        echo -e "${PASS} File visible on host (bind mount OK)"
    else
        echo -e "${FAIL} File NOT visible on host (bind mount BROKEN)"
    fi
    docker exec obsidian-agent rm -f "/data/vault/$TEST_F" 2>/dev/null
else
    echo -e "${FAIL} Container CANNOT write to /data/vault"
fi
echo ""
# --- 10. Disk space ---
echo "--- 10. Disk space ---"
echo "Host:"
df -h "$HOST_PATH" 2>/dev/null | tail -1 | awk '{print "  Used: "$3"/"$2" ("$5")  Free: "$4}'
echo "Container:"
docker exec obsidian-agent df -h /data/vault 2>/dev/null | tail -1 | awk '{print "  Used: "$3"/"$2" ("$5")  Free: "$4}'
echo ""

# --- 11. Recent vault activity ---
echo "--- 11. Recent activity (last 24h) ---"
echo "Files modified on host (24h):"
find "$HOST_PATH" -type f -mtime -1 2>/dev/null | wc -l | sed 's/^/  /'
echo "Most recent file:"
find "$HOST_PATH" -type f -printf '%T+ %p\n' 2>/dev/null | sort -r | head -1 | sed 's/^/  /'
echo ""

# --- 12. rclone logs ---
echo "--- 12. rclone logs ---"
if [ -f /var/log/rclone.log ]; then
    echo "Last 10 lines of /var/log/rclone.log:"
    tail -10 /var/log/rclone.log | sed 's/^/  /'
elif [ -f /var/log/rclone-sync.log ]; then
    echo "Last 10 lines of /var/log/rclone-sync.log:"
    tail -10 /var/log/rclone-sync.log | sed 's/^/  /'
else
    echo "${WARN} No rclone log in /var/log/"
    echo "  Try: journalctl -u rclone --since '1 hour ago'"
fi
echo ""

# --- 13. Test remote connection ---
echo "--- 13. Test remote connection ---"
REMOTE=$(rclone listremotes 2>/dev/null | head -1)
if [ -n "$REMOTE" ]; then
    echo "Testing: $REMOTE"
    if rclone lsd "$REMOTE" &>/dev/null; then
        echo -e "${PASS} Can connect to remote"
    else
        echo -e "${FAIL} CANNOT connect (auth/network issue)"
    fi
else
    echo "${WARN} No remote to test"
fi
echo ""
# --- 14. Bot logs (write errors) ---
echo "--- 14. Bot logs (write errors) ---"
if docker exec obsidian-agent cat /app/logs/bot.log 2>/dev/null | grep -iE "failed to write|error.*vault|permission denied" | tail -5 | sed 's/^/  /'; then
    echo "  ^ Last 5 write-related errors"
else
    echo -e "${PASS} No write errors in bot.log"
fi
echo ""

echo "============================================"
echo "  Diagnostic complete"
echo "============================================"
echo ""
echo "Quick fixes:"
echo "  rclone mount dead:   rclone mount gdrive: /srv/obsidian-vault --daemon"
echo "  bind mount broken:   docker compose restart"
echo "  disk full:           df -h && clean up"
echo "  permissions:         chown -R 1000:1000 /srv/obsidian-vault"
echo "  force sync:          rclone sync /srv/obsidian-vault gdrive:Vault"
echo ""
echo ""