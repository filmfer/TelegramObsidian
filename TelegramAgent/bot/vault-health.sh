#!/usr/bin/env bash
# =============================================================================
# vault-health.sh — Verify vault sync health and auto-fix if needed
# Run on the HOST (VPS). Checks rclone mount, bind mount, and container write.
# Usage: bash vault-health.sh [--fix]
#
# With --fix, attempts to restart the container if bind mount is stale.
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

DO_FIX=0
if [[ "${1:-}" == "--fix" ]]; then
    DO_FIX=1
fi

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Vault Health Check${NC}"
if [ "$DO_FIX" -eq 1 ]; then
    echo -e "${BLUE}  (auto-fix enabled)${NC}"
fi
echo -e "${BLUE}============================================${NC}"
echo ""

ERRORS=0

check_pass() { echo -e "${PASS} $1"; }
check_fail() { echo -e "${FAIL} $1"; ((ERRORS++)); }
check_warn() { echo -e "${WARN} $1"; }
check_info() { echo -e "${INFO} $1"; }

# --- 1. rclone mount process ---
echo "--- 1. rclone mount ---"
RCLONE_PIDS=$(pgrep -f "rclone mount" 2>/dev/null || echo "")
if [ -n "$RCLONE_PIDS" ]; then
    check_pass "rclone mount running (PIDs: $(echo $RCLONE_PIDS | tr '\n' ' '))"
else
    check_fail "rclone mount NOT running"
    if command -v systemctl &>/dev/null; then
        echo "  Trying: systemctl start rclone-mount"
        systemctl start rclone-mount 2>/dev/null || true
    fi
fi
echo ""

# --- 2. Mount point active ---
echo "--- 2. mount point ---"
MOUNT_INFO=$(mount | grep -E 'rclone|fuse.rclone' | grep -v grep || echo "")
if echo "$MOUNT_INFO" | grep -q .; then
    check_pass "Mount(s) active:"
    echo "$MOUNT_INFO" | head -3 | sed 's/^/  /'
else
    check_fail "No rclone mount found"
fi
echo ""

# --- 3. Docker container ---
echo "--- 3. container ---"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q obsidian-agent; then
    check_pass "Container running"
else
    check_fail "Container NOT running"
fi
echo ""

# --- 4. Container can read vault ---
echo "--- 4. vault access ---"
VAULT_ITEMS=$(docker exec obsidian-agent ls -1 /data/vault/ 2>/dev/null | wc -l || echo "0")
if [ "$VAULT_ITEMS" -gt 0 ]; then
    check_pass "Vault accessible ($VAULT_ITEMS items)"
else
    check_fail "Vault inaccessible (Transport endpoint not connected?)"
    if [ "$DO_FIX" -eq 1 ]; then
        echo "  → Restarting container..."
        COMPOSE_DIR=$(find /root /home -name "docker-compose.yml" -path "*telegram*" 2>/dev/null | head -1 | xargs dirname 2>/dev/null || echo "")
        if [ -n "$COMPOSE_DIR" ]; then
            cd "$COMPOSE_DIR" && docker compose restart
            sleep 3
            # Re-test
            if docker exec obsidian-agent ls /data/vault/ &>/dev/null; then
                check_pass "Fixed! Vault accessible after restart"
                ERRORS=$((ERRORS - 1))
            else
                check_fail "Still inaccessible after restart"
            fi
        else
            check_warn "Cannot auto-fix: docker-compose.yml not found"
            echo "  Run: cd <compose-dir> && docker compose restart"
        fi
    else
        echo "  Run with --fix to auto-restart container"
    fi
fi
echo ""

# --- 5. Container write test ---
echo "--- 5. write test ---"
TEST_FILE=".health_$(date +%s).txt"
if docker exec obsidian-agent bash -c "echo ok > /data/vault/$TEST_FILE" 2>/dev/null; then
    docker exec obsidian-agent rm -f "/data/vault/$TEST_FILE" 2>/dev/null
    check_pass "Container can write to vault"
else
    check_fail "Container CANNOT write"
fi
echo ""

# --- 6. Google Drive connection ---
echo "--- 6. Google Drive ---"
REMOTE=$(rclone listremotes 2>/dev/null | head -1)
if [ -n "$REMOTE" ] && rclone lsd "$REMOTE" &>/dev/null; then
    check_pass "Can connect to $REMOTE"
else
    check_fail "Cannot connect to Google Drive"
fi
echo ""

# --- Summary ---
echo -e "${BLUE}============================================${NC}"
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}  ✅ HEALTHY — Vault sync working${NC}"
else
    echo -e "${RED}  ❌ $ERRORS issue(s) found${NC}"
    if [ "$DO_FIX" -eq 0 ]; then
        echo -e "${YELLOW}  Run with --fix to auto-repair${NC}"
    fi
fi
echo -e "${BLUE}============================================${NC}"

exit $ERRORS