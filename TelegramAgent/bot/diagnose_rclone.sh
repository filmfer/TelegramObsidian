#!/usr/bin/env bash
# =============================================================================
# diagnose_rclone.sh — Full diagnostic for rclone + Docker vault sync
# Run on the HOST (VPS). Checks mount, container, and Google Drive.
# Usage: bash diagnose_rclone.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PASS="✅"
FAIL="❌"
WARN="⚠️"
INFO="ℹ️"

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Rclone + Vault Sync Diagnostic${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""

ERRORS=0
WARNINGS=0

check_pass() { echo -e "${PASS} $1"; }
check_fail() { echo -e "${FAIL} $1"; ((ERRORS++)); }
check_warn() { echo -e "${WARN} $1"; ((WARNINGS++)); }
check_info() { echo -e "${INFO} $1"; }

# =============================================================================
# 1. rclone installation
# =============================================================================
echo "--- 1. rclone installation ---"
if command -v rclone &>/dev/null; then
    check_pass "rclone found: $(rclone version | head -1)"
else
    check_fail "rclone NOT installed"
fi
echo ""

# =============================================================================
# 2. rclone remote configured
# =============================================================================
echo "--- 2. rclone remote ---"
REMOTES=$(rclone listremotes 2>/dev/null || echo "")
if echo "$REMOTES" | grep -q .; then
    check_pass "Remotes: $(echo $REMOTES | tr '\n' ' ')"
else
    check_fail "No rclone remotes configured"
fi
echo ""

# =============================================================================
# 3. rclone mount process
# =============================================================================
echo "--- 3. rclone mount process ---"
RCLONE_PIDS=$(pgrep -f "rclone mount" 2>/dev/null || echo "")
if [ -n "$RCLONE_PIDS" ]; then
    check_pass "rclone mount running (PIDs: $(echo $RCLONE_PIDS | tr '\n' ' '))"
    # Extract mount points from /proc to avoid ps parsing issues
    MOUNT_POINTS=""
    for pid in $RCLONE_PIDS; do
        if [ -f "/proc/$pid/cmdline" ]; then
            CMD=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
            # Extract path after "rclone mount <remote>"
            MP=$(echo "$CMD" | sed -E 's/.*rclone mount [^ ]+ ([^ ]+).*/\1/' | tr -d '[:space:]')
            if [ -n "$MP" ]; then
                MOUNT_POINTS="$MOUNT_POINTS $MP"
            fi
        fi
    done
    MOUNT_POINTS=$(echo "$MOUNT_POINTS" | tr ' ' '\n' | sort -u | tr '\n' ' ')
    check_info "Mount points: $MOUNT_POINTS"
else
    check_fail "rclone mount NOT running"
    MOUNT_POINTS=""
fi
echo ""

# =============================================================================
# 4. Mount point verification
# =============================================================================
echo "--- 4. Mount point check ---"
MOUNT_INFO=$(mount | grep -E 'rclone|fuse.rclone' || echo "")
if echo "$MOUNT_INFO" | grep -q .; then
    check_pass "Mount(s) active:"
    echo "$MOUNT_INFO" | sed 's/^/  /'
else
    check_fail "No vault mount found"
fi
echo ""

# =============================================================================
# 5. Google Drive connectivity
# =============================================================================
echo "--- 5. Google Drive connection ---"
REMOTE=$(echo "$REMOTES" | head -1)
if [ -n "$REMOTE" ]; then
    if rclone lsd "$REMOTE" &>/dev/null; then
        check_pass "Can connect to $REMOTE"
    else
        check_fail "CANNOT connect to $REMOTE (auth/network issue)"
    fi
else
    check_warn "No remote to test"
fi
echo ""

# =============================================================================
# 6. Docker container status
# =============================================================================
echo "--- 6. Docker container ---"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q obsidian-agent; then
    check_pass "Container 'obsidian-agent' running"
else
    check_fail "Container NOT running"
fi
echo ""

# =============================================================================
# 7. Docker bind mount verification
# =============================================================================
echo "--- 7. Docker bind mount ---"
MOUNTS=$(docker inspect obsidian-agent --format '{{json .Mounts}}' 2>/dev/null || echo "[]")
if echo "$MOUNTS" | grep -q '/data/vault'; then
    VAULT_SRC=$(echo "$MOUNTS" | python3 -c "
import sys, json
m = json.load(sys.stdin)
for x in m:
    if x.get('Destination') == '/data/vault':
        print(x['Source'])
        break
" 2>/dev/null || echo "unknown")
    check_pass "Vault bind mount: $VAULT_SRC → /data/vault"
    
    # Check if Docker mount source matches any rclone mount point
    MOUNT_MATCH=0
    for mp in $MOUNT_POINTS; do
        if [ "$VAULT_SRC" = "$mp" ]; then
            MOUNT_MATCH=1
            break
        fi
    done
    
    if [ -n "$MOUNT_POINTS" ]; then
        if [ "$MOUNT_MATCH" -eq 1 ]; then
            check_pass "Paths match correctly ($VAULT_SRC)"
        else
            check_fail "PATH MISMATCH! Docker mount is '$VAULT_SRC' but rclone is at: $MOUNT_POINTS"
        fi
    else
        check_warn "Cannot verify path match (no rclone mount detected)"
    fi
else
    check_fail "No vault bind mount found in container"
fi
# =============================================================================
# 8. Container can read vault
# =============================================================================
echo "--- 8. Container read test ---"
VAULT_ITEMS=$(docker exec obsidian-agent ls -1 /data/vault/ 2>/dev/null | wc -l || echo "0")
if [ "$VAULT_ITEMS" -gt 0 ]; then
    check_pass "Vault accessible in container ($VAULT_ITEMS items)"
    echo "  Latest:"
    docker exec obsidian-agent ls -lt /data/vault/ 2>/dev/null | head -5 | sed 's/^/    /'
else
    check_fail "Vault empty or inaccessible in container"
fi
echo ""

# =============================================================================
# 9. Container write test
# =============================================================================
echo "--- 9. Container write test ---"
TEST_FILE=".diagnostic_$(date +%s).txt"
TEST_CONTENT="Vault sync test — $(date)"
if docker exec obsidian-agent bash -c "echo '$TEST_CONTENT' > /data/vault/$TEST_FILE" 2>/dev/null; then
    if docker exec obsidian-agent cat /data/vault/$TEST_FILE &>/dev/null; then
        check_pass "Container can WRITE to vault"
        docker exec obsidian-agent rm -f "/data/vault/$TEST_FILE" 2>/dev/null
    else
        check_fail "Write succeeded but cannot read back"
    fi
else
    check_fail "Container CANNOT write to vault (Permission denied)"
fi
echo ""

# =============================================================================
# 10. Host write test (via bind mount source)
# =============================================================================
echo "--- 10. Host write test ---"
if [ -n "${VAULT_SRC:-}" ] && [ -d "$VAULT_SRC" ]; then
    HOST_TEST_FILE=".host_test_$(date +%s).txt"
    if echo "host test" > "$VAULT_SRC/$HOST_TEST_FILE" 2>/dev/null; then
        check_pass "Host can write to $VAULT_SRC"
        rm -f "$VAULT_SRC/$HOST_TEST_FILE"
    else
        check_fail "Host CANNOT write to $VAULT_SRC"
    fi
else
    check_warn "Cannot test host write (path unknown)"
fi
echo ""

# =============================================================================
# 11. Disk space
# =============================================================================
echo "--- 11. Disk space ---"
echo "Host:"
df -h / 2>/dev/null | tail -1 | awk '{print "  Root: "$3"/"$2" ("$5")  Free: "$4}'
if [ -n "${VAULT_SRC:-}" ]; then
    df -h "$VAULT_SRC" 2>/dev/null | tail -1 | awk '{print "  Vault: "$3"/"$2" ("$5")  Free: "$4}'
fi
echo ""

# =============================================================================
# 12. Recent vault activity
# =============================================================================
echo "--- 12. Recent vault activity (24h) ---"
if [ -n "${VAULT_SRC:-}" ] && [ -d "$VAULT_SRC" ]; then
    RECENT=$(find "$VAULT_SRC" -type f -mtime -1 2>/dev/null | wc -l)
    check_info "Files modified (24h): $RECENT"
    LATEST=$(find "$VAULT_SRC" -type f -printf '%T+ %p\n' 2>/dev/null | sort -r | head -1)
    if [ -n "$LATEST" ]; then
        echo "  Most recent: $(echo "$LATEST" | cut -d' ' -f2-)"
    fi
else
    check_warn "Cannot check activity (path unknown)"
fi
echo ""

# =============================================================================
# 13. rclone mount log
# =============================================================================
echo "--- 13. rclone mount log ---"
for LOG in /var/log/rclone-mount.log /var/log/rclone.log; do
    if [ -f "$LOG" ]; then
        echo "Last 10 lines of $LOG:"
        tail -10 "$LOG" | sed 's/^/  /'
        break
    fi
done
echo ""

# =============================================================================
# 14. Bot logs (write errors)
# =============================================================================
echo "--- 14. Bot logs (last 5 write errors) ---"
BOT_LOG="/app/logs/bot.log"
ERRORS_FOUND=$(docker exec obsidian-agent cat "$BOT_LOG" 2>/dev/null | grep -iE "failed to write|error.*vault|permission denied|Transport endpoint" | tail -5 || echo "")
if [ -n "$ERRORS_FOUND" ]; then
    echo "$ERRORS_FOUND" | sed 's/^/  /'
    check_warn "Write errors found in bot.log"
else
    check_pass "No write errors in bot.log"
fi
echo ""

# =============================================================================
# Summary
# =============================================================================
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Diagnostic Summary${NC}"
echo -e "${BLUE}============================================${NC}"

if [ $ERRORS -eq 0 ] && [ $WARNINGS -eq 0 ]; then
    echo -e "${GREEN}  ✅ ALL CHECKS PASSED — Vault sync is healthy${NC}"
elif [ $ERRORS -eq 0 ]; then
    echo -e "${YELLOW}  ⚠️  $WARNINGS warning(s) — check above${NC}"
else
    echo -e "${RED}  ❌ $ERRORS error(s), $WARNINGS warning(s)${NC}"
fi

echo ""
echo "Quick fixes:"
echo "  Mount not running:  systemctl start rclone-mount"
echo "  Path mismatch:      Check docker-compose.yml mount path"
echo "  Permission denied:  chown -R 1000:1000 <vault-path>"
echo "  Auth failed:        rclone config reconnect gdrive:"
echo ""

exit $ERRORS
echo ""