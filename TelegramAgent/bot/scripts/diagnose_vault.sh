#!/usr/bin/env bash
#
# diagnose_vault.sh — pinpoint where the vault "stops" between:
#   container (/data/vault) -> host mount -> rclone -> Google Drive
#
# Run ON THE SERVER, from the bot directory:
#   bash TelegramAgent/bot/scripts/diagnose_vault.sh
#
# Nothing is changed; it only prints findings.
set -uo pipefail

echo "════════ 1. Where is docker-compose.yml / .env running from?"
pwd
ls -la docker-compose.yml .env 2>&1 | head -5

echo ""
echo "════════ 2. What .env values does the CONTEXT use?"
grep -E "OBSIDIAN_VAULT_PATH|OBSIDIAN_VAULT_HOST_PATH" .env 2>/dev/null || echo "  (vars not found in ./.env)"

echo ""
echo "════════ 3. Are the containers running?"
docker compose ps 2>&1 | head -10

echo ""
echo "════════ 4. What is actually mounted WHERE in the container?"
CID=$(docker ps --format '{{.Names}}' | grep -i obsidian | head -1)
if [ -z "$CID" ]; then
  echo "  ✗ No running 'obsidian-agent' container found. Aborting."
  exit 1
fi
echo "  container: $CID"
docker inspect "$CID" --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'

echo ""
echo "════════ 5. Does the bot SEE files it writes? (proof of the write target)"
docker exec "$CID" sh -c 'echo "hello-$(date +%s)" > /data/vault/99_Templates/_diag_write.md && ls -la /data/vault/99_Templates/_diag_write.md' 2>&1

echo ""
echo "════════ 6. Does the HOST see that file? (host mount path = mount source from step 4)"
# Take the mount destination that ends with /data/vault
HOST_DIR=$(docker inspect "$CID" --format '{{range .Mounts}}{{if eq .Destination "/data/vault"}}{{.Source}}{{end}}{{end}}')
echo "  host mount source: ${HOST_DIR:-<none>}"
ls -la "${HOST_DIR}/99_Templates/_diag_write.md" 2>&1

echo ""
echo "════════ 7. What does rclone actually mount? (independent of Docker)"
echo "  systemd unit mount target (extracted, optimistic):"
grep -h "rclone.*mount\|MountPoint\|/srv/obsidian-vault\|/mnt/gdrive" /etc/systemd/system/rclone*.service 2>/dev/null
echo ""
echo "  Active systemd mounts (rclone / fuse):"
mount | grep -iE "rclone|fuse|gdrive" || echo "  (no rclone mount detected by 'mount')"
systemctl --type=mount --all | grep -iE "rclone|gdrive" -- "--no-pager" 2>/dev/null || true

echo ""
echo "════════ 8. Can host -> Drive see the test file? (must be INSIDE host dir from step 6)"
rclone lsl "$(basename "$HOST_DIR"):" --max-depth 1 2>&1 | grep -i diag_write || \
  rclone lsd "gdrive:" 2>&1 | head -5

echo ""
echo "════════ 9. Cleanup test file"
[ -d "$HOST_DIR" ] && rm -f "$HOST_DIR/99_Templates/_diag_write.md"

echo ""
echo "════════ INTERPRETATION GUIDE ════════"
echo "If step 5 OK but step 6 FAIL  -> docker-compose binds to the WRONG host dir."
echo "If step 5 and 6 OK but step 7 target != hostdir → rclone mounts elsewhere."
echo "If step 7 OK but step 8 fails  → rclone remote/access/token problem."
echo "Golden rule:  OBSIDIAN_VAULT_HOST_PATH  MUST equal the actual rclone mount dir."