#!/usr/bin/env bash
# =============================================================================
# sync_immediate.sh - Watchdog de sync IMEDIATO para o Google Drive.
#
# Envia, imediatamente apos escrita, cada nota/ficheiro do vault (mount FUSE
# do rclone) para o Google Drive, usando rclone copyto.
#
# PORQUE polling e nao inotify?  O inotifywait NAO e fiavel em mounts FUSE do
# rclone (nao dispara eventos de escrita de forma consistente). O polling a
# cada 1s que compara mtimes e 100% fiavel e testado em producao (2026-09-14).
#
# Uso (servidor, como root):
#   chmod +x /usr/local/bin/sync_immediate.sh
#   cp scripts/sync-immediate.service /etc/systemd/system/
#   systemctl daemon-reload
#   systemctl enable --now sync-immediate
#
# Estado:
#   systemctl status sync-immediate
#   tail -f /var/log/sync-immediate.log
# =============================================================================
set -u

VAULT="/mnt/obsidian-vault"              # mount FUSE do rclone (gdrive)
LOG="/var/log/sync-immediate.log"
DB="/tmp/sync-immediate.db"              # snapshot de mtime+path

log() { echo "$(date +%Y-%m-%dT%H:%M:%S) $*" >> "$LOG"; }

# Envia UMA nota/ficheiro para o Drive, preservando a arvore de pastas.
sync_file() {
  local rel="$1"
  [ -z "$rel" ] && return
  # Ignora internos do Obsidian/Git e ficheiros de teste.
  case "$rel" in
    */.git/*|.git/*|*/.obsidian/*|*/.space/*|.test_*|.inotify_*) return;;
  esac
  local src="$VAULT/$rel"
  [ -f "$src" ] || return
  rclone copyto "$src" "gdrive:$rel" --config /root/.config/rclone/rclone.conf \
    >>"$LOG" 2>&1 && log "SYNCED: $rel"
}

log "=== sync-immediate (polling) START ==="

# Snapshot inicial (so cria se nao existir, para nao varrer tudo no boot).
[ -f "$DB" ] || find "$VAULT" -type f -printf "%T@ %P\n" 2>/dev/null | sort > "$DB"

while true; do
  CUR=$(mktemp)
  find "$VAULT" -type f -printf "%T@ %P\n" 2>/dev/null | sort > "$CUR"
  while read -r mtime rel; do
    [ -n "$rel" ] || continue
    if grep -qF -- " $rel" "$DB"; then
      old=$(grep -F -- " $rel" "$DB" | head -1 | cut -d" " -f1)
      [ "$mtime" = "$old" ] && continue   # inalterado
    fi
    sync_file "$rel"                       # novo ou modificado -> envia ja
  done < "$CUR"
  mv "$CUR" "$DB"
  sleep 1
done