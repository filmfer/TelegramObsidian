#!/usr/bin/env bash
#
# setup_rclone.sh — Automates rclone Google Drive mount for the Obsidian vault
# on a headless Ubuntu server (no window manager / browser).
#
# Usage:
#   sudo bash setup_rclone.sh
#
# What it does:
#   1. Installs rclone + fuse
#   2. Guides you through headless OAuth (paste a link into your local browser)
#   3. Creates the mount point /srv/obsidian-vault
#   4. Creates + enables a systemd service so the mount survives reboots
#
# After running, set OBSIDIAN_VAULT_HOST_PATH=/srv/obsidian-vault in .env
# and run: docker compose up -d --build
#
set -euo pipefail

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
REMOTE_NAME="gdrive"
MOUNT_POINT="/srv/obsidian-vault"
# Your Google Drive folder ID (the vault). Change if needed.
DRIVE_FOLDER_ID="1kVr0_tbGmyQWzKcwhDddZrzOjc9XtlFY"
SERVICE_NAME="rclone-gdrive"

# ---------------------------------------------------------------
# 1. Install rclone + fuse
# ---------------------------------------------------------------
echo "==> [1/5] Installing rclone and fuse..."
if ! command -v rclone >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y rclone fuse3
else
    echo "    rclone already installed: $(rclone version | head -1)"
fi

# ---------------------------------------------------------------
# 2. Configure rclone (headless OAuth)
# ---------------------------------------------------------------
echo ""
echo "==> [2/5] Configuring rclone remote '$REMOTE_NAME' (headless)..."
echo ""
echo "If the remote '$REMOTE_NAME' already exists, we skip configuration."
echo "To re-configure later, run:  rclone config"
echo ""

if rclone listremotes 2>/dev/null | grep -q "^${REMOTE_NAME}:$"; then
    echo "    Remote '$REMOTE_NAME' already exists — skipping."
else
    # Build the config non-interactively where possible.
    # NOTE: OAuth still requires a browser on YOUR local machine.
    # We create the remote with a placeholder token, then run
    # 'rclone config reconnect' to do the headless OAuth flow.
    cat >> "$HOME/.config/rclone/rclone.conf" <<EOF

[$REMOTE_NAME]
type = drive
scope = drive
root_folder_id = $DRIVE_FOLDER_ID
EOF
    echo "    Remote '$REMOTE_NAME' created with root_folder_id=$DRIVE_FOLDER_ID"
    echo ""
    echo "    Now we need to authorize. A URL will be printed."
    echo "    Open it in your LOCAL browser, log in, authorize,"
    echo "    then paste the verification code back here."
    echo ""
    rclone config reconnect "$REMOTE_NAME":
fi

# ---------------------------------------------------------------
# 3. Create mount point
# ---------------------------------------------------------------
echo ""
echo "==> [3/5] Creating mount point $MOUNT_POINT..."
sudo mkdir -p "$MOUNT_POINT"
sudo chown "$USER:$USER" "$MOUNT_POINT"

# ---------------------------------------------------------------
# 4. Test mount (manual, foreground for a few seconds)
# ---------------------------------------------------------------
echo ""
echo "==> [4/5] Testing mount..."
echo "    Mounting for 5 seconds to verify access..."
timeout 5 rclone mount "$REMOTE_NAME": "$MOUNT_POINT" --vfs-cache-mode writes || true
echo "    (If you saw no errors above, the mount works.)"

# ---------------------------------------------------------------
# 5. Create + enable systemd service
# ---------------------------------------------------------------
echo ""
echo "==> [5/5] Creating systemd service '$SERVICE_NAME'..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=rclone mount Google Drive vault
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/rclone mount $REMOTE_NAME: $MOUNT_POINT --vfs-cache-mode writes --buffer-size 64M --vfs-read-ahead 128M --dir-cache-time 1m
ExecStop=/bin/fusermount -u $MOUNT_POINT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo ""
echo "=============================================================="
echo " Done!"
echo "=============================================================="
echo ""
echo " Check status:"
echo "   systemctl status $SERVICE_NAME"
echo "   ls $MOUNT_POINT"
echo ""
echo " Then set in your .env:"
echo "   OBSIDIAN_VAULT_HOST_PATH=$MOUNT_POINT"
echo ""
echo " And rebuild the container:"
echo "   cd TelegramAgent/bot && docker compose up -d --build"
echo ""
echo " Verify inside the container:"
echo "   docker exec obsidian-agent ls /data/vault"
echo "=============================================================="