#!/usr/bin/env bash
# =============================================================================
# diagnose_deep.sh — Deep diagnostic when basic commands return nothing
# =============================================================================

echo "=== SHELL INFO ==="
echo "Shell: $SHELL"
echo "User: $(whoami)"
echo "PWD: $(pwd)"
echo ""

echo "=== HOST: /srv/obsidian-vault ==="
echo "--- stat ---"
stat /srv/obsidian-vault 2>&1
echo "--- ls ---"
ls -la /srv/obsidian-vault/ 2>&1
echo "--- tree (depth 1) ---"
find /srv/obsidian-vault -maxdepth 1 2>&1 | head -20
echo "--- count ---"
echo "Files: $(find /srv/obsidian-vault -type f 2>/dev/null | wc -l)"
echo ""

echo "=== HOST: /mnt/obsidian-vault ==="
echo "--- stat ---"
stat /mnt/obsidian-vault 2>&1
echo "--- ls ---"
ls -la /mnt/obsidian-vault/ 2>&1
echo "--- find ---"
find /mnt/obsidian-vault -maxdepth 2 2>&1 | head -30
echo "--- count ---"
echo "Files: $(find /mnt/obsidian-vault -type f 2>/dev/null | wc -l)"
echo ""

echo "=== DOCKER CONTAINER ==="
echo "--- processes ---"
docker exec obsidian-agent ps aux 2>&1
echo "--- root user test ---"
docker exec --user root obsidian-agent id 2>&1
echo "--- root ls ---"
docker exec --user root obsidian-agent ls -la /data/vault/ 2>&1
echo "--- root find ---"
docker exec --user root obsidian-agent find /data/vault -maxdepth 2 2>&1 | head -30
echo "--- mount info ---"
docker exec --user root obsidian-agent cat /proc/mounts | grep -E 'vault|data' 2>&1
echo ""

echo "=== DOCKER COMPOSE VOLUMES ---"
docker inspect obsidian-agent --format '{{json .Mounts}}' 2>&1 | python3 -m json.tool 2>/dev/null || docker inspect obsidian-agent --format '{{json .Mounts}}' 2>&1
echo ""

echo "=== RCLONE MOUNT PROCESS ==="
ps aux | grep rclone | grep -v grep
echo ""

echo "=== RCLONE MOUNT LOG (last 20 lines) ==="
tail -20 /var/log/rclone-mount.log 2>&1 || echo "No log file"
echo ""

echo "=== TEST: Create file in /mnt/obsidian-vault ==="
echo "test_$(date +%s)" > /mnt/obsidian-vault/.host_test.txt 2>&1 && echo "OK - created" || echo "FAILED"
ls -la /mnt/obsidian-vault/.host_test.txt 2>&1
echo ""

echo "=== TEST: Create file in /srv/obsidian-vault ==="
echo "test_$(date +%s)" > /srv/obsidian-vault/.host_test2.txt 2>&1 && echo "OK - created" || echo "FAILED"
ls -la /srv/obsidian-vault/.host_test2.txt 2>&1
echo ""

echo "=== TEST: Container write as root ==="
docker exec --user root obsidian-agent bash -c "echo test > /data/vault/.container_test.txt" 2>&1 && echo "OK - created" || echo "FAILED"
docker exec --user root obsidian-agent ls -la /data/vault/.container_test.txt 2>&1
echo ""

echo "=== TEST: Container write as appuser ==="
docker exec --user appuser obsidian-agent bash -c "echo test > /data/vault/.container_test2.txt" 2>&1 && echo "OK - created" || echo "FAILED"
docker exec --user appuser obsidian-agent ls -la /data/vault/.container_test2.txt 2>&1
echo ""

echo "=== GOOGLE DRIVE CONTENT (via rclone) ==="
rclone ls gdrive: 2>&1 | head -20
echo ""

echo "=== DONE ==="