#!/usr/bin/env bash
# triage_vault.sh — diagnose & recover "Transport endpoint is not connected"
# (Errno 107) on the vault bind mount, e.g. after /organize.
#
# Run ON THE HOST (Hetzner VM), as root:
#   sudo bash triage_vault.sh                 # 1. read-only diagnosis (default)
#   sudo bash triage_vault.sh backup          # 2. save rclone VFS cache (unuploaded writes)
#   sudo bash triage_vault.sh fix             # 3. remount rclone + recreate container
#   sudo bash triage_vault.sh organize        # 4. inspect what /organize left behind
#
# Override detection with env vars if needed:
#   HOST_PATH=/srv/obsidian-vault UNIT=rclone-gdrive REMOTE=gdrive \
#   COMPOSE_DIR=/root/TelegramObsidian/TelegramAgent/bot sudo -E bash triage_vault.sh
#
# Every filesystem access is wrapped in `timeout`: on a dead FUSE mount a
# plain `ls` blocks forever in uninterruptible sleep.
set -u

UNIT="${UNIT:-rclone-gdrive}"
REMOTE="${REMOTE:-gdrive}"
CONTAINER="${CONTAINER:-obsidian-agent}"
T=8  # seconds before a filesystem probe is declared hung

R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[34m'; N=$'\e[0m'
ok()   { echo "  ${G}✔${N} $*"; }
bad()  { echo "  ${R}✘${N} $*"; }
warn() { echo "  ${Y}!${N} $*"; }
hdr()  { echo; echo "${B}== $* ==${N}"; }

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)."; exit 1; }

# ---------------------------------------------------------------- detect ---
detect() {
  if [[ -z "${COMPOSE_DIR:-}" ]]; then
    COMPOSE_DIR=$(docker inspect "$CONTAINER" \
      --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null)
  fi
  if [[ -z "${HOST_PATH:-}" ]]; then
    HOST_PATH=$(docker inspect "$CONTAINER" \
      --format '{{range .Mounts}}{{if eq .Destination "/data/vault"}}{{.Source}}{{end}}{{end}}' 2>/dev/null)
  fi
  ENV_HOST_PATH=""
  [[ -n "${COMPOSE_DIR:-}" && -f "$COMPOSE_DIR/.env" ]] && \
    ENV_HOST_PATH=$(grep -E '^OBSIDIAN_VAULT_HOST_PATH=' "$COMPOSE_DIR/.env" | tail -1 | cut -d= -f2-)
  UNIT_MOUNT=$(systemctl cat "$UNIT" 2>/dev/null | grep -oP 'rclone mount \S+ \K\S+' | head -1)
  CACHE_DIR=$(systemctl cat "$UNIT" 2>/dev/null | grep -oP -- '--cache-dir[= ]\K\S+' | head -1)
  UNIT_USER=$(systemctl show "$UNIT" -p User --value 2>/dev/null)
  if [[ -z "$CACHE_DIR" ]]; then
    home=$(getent passwd "${UNIT_USER:-root}" | cut -d: -f6)
    CACHE_DIR="${home:-/root}/.cache/rclone"
  fi
}

probe() {  # probe <path> — 0 healthy, 1 error, 124 hung
  timeout "$T" stat -t "$1" >/dev/null 2>&1
}

# ------------------------------------------------------------- diagnose ---
diagnose() {
  hdr "1. Paths (all three must be identical)"
  echo "  compose dir           : ${COMPOSE_DIR:-<not found>}"
  echo "  container mount source: ${HOST_PATH:-<not found>}"
  echo "  .env HOST_PATH        : ${ENV_HOST_PATH:-<unset>}"
  echo "  rclone unit mounts at : ${UNIT_MOUNT:-<unit not found>}"
  if [[ -n "$HOST_PATH" && "$HOST_PATH" == "$UNIT_MOUNT" ]]; then ok "container ↔ rclone path match"
  else bad "PATH MISMATCH — container binds '$HOST_PATH' but rclone mounts '$UNIT_MOUNT'"; fi
  [[ -z "$ENV_HOST_PATH" || "$ENV_HOST_PATH" == "$HOST_PATH" ]] || \
    warn ".env differs from the running container → container was created with an older .env"

  hdr "2. rclone service"
  systemctl is-active --quiet "$UNIT" && ok "$UNIT active" || bad "$UNIT NOT active"
  systemctl show "$UNIT" -p ActiveEnterTimestamp -p NRestarts -p ExecMainStatus --no-pager | sed 's/^/  /'
  echo "  -- last errors (7 days):"
  journalctl -u "$UNIT" --since "7 days ago" --no-pager 2>/dev/null \
    | grep -iE 'error|fatal|panic|killed|signal|429|403|rateLimit|quota|transport|vfs cache' \
    | tail -25 | sed 's/^/    /'

  hdr "3. Kernel: OOM kills / FUSE"
  journalctl -k --since "7 days ago" --no-pager 2>/dev/null \
    | grep -iE 'out of memory|oom-kill|killed process|fuse' | tail -10 | sed 's/^/    /' \
    || true
  free -h | sed 's/^/  /'
  swapon --show 2>/dev/null | sed 's/^/  /' || true

  hdr "4. Host mount"
  if mountpoint -q "$HOST_PATH" 2>/dev/null; then ok "$HOST_PATH is a mountpoint"
  else bad "$HOST_PATH is NOT a mountpoint (or probe hung)"; fi
  grep -E " $HOST_PATH " /proc/mounts | sed 's/^/  /'
  probe "$HOST_PATH"; rc=$?
  case $rc in
    0)   ok "host can stat the vault";
         echo "  top-level: $(timeout $T ls "$HOST_PATH" 2>&1 | head -15 | tr '\n' ' ')";;
    124) bad "host stat HUNG > ${T}s — rclone is wedged";;
    *)   bad "host stat failed: $(timeout $T stat "$HOST_PATH" 2>&1 | tail -1)";;
  esac

  hdr "5. Container view"
  docker ps -a --filter "name=^${CONTAINER}$" --format '  {{.Names}}  {{.Status}}  (created {{.CreatedAt}})'
  echo "  rclone started : $(systemctl show "$UNIT" -p ActiveEnterTimestamp --value)"
  echo "  container start: $(docker inspect "$CONTAINER" --format '{{.State.StartedAt}}' 2>/dev/null)"
  warn "if the container started BEFORE rclone's last start, it holds a stale FUSE handle"
  out=$(timeout $((T+5)) docker exec "$CONTAINER" timeout "$T" ls /data/vault 2>&1); rc=$?
  if [[ $rc -eq 0 ]]; then ok "container can list /data/vault"
  else bad "container cannot read /data/vault (rc=$rc): $(echo "$out" | tail -1)"; fi
  echo "  -- processes stuck in D state (uninterruptible I/O) inside container:"
  pids=$(docker top "$CONTAINER" -eo pid,stat,args 2>/dev/null | awk 'NR>1 && $2 ~ /D/')
  [[ -n "$pids" ]] && echo "$pids" | sed 's/^/    /' || echo "    none"

  hdr "6. Bot log around /organize"
  docker logs --since 168h "$CONTAINER" 2>&1 \
    | grep -E 'organize|Move failed|Sub-folder|Git commit|Transport endpoint|Errno 107|Traceback|CRITICAL' \
    | tail -30 | sed 's/^/    /'

  hdr "7. Google Drive reachable directly (bypasses FUSE)"
  if timeout 30 rclone lsd "$REMOTE": --max-depth 1 >/tmp/_lsd 2>&1; then
    ok "rclone API access OK"; sed 's/^/    /' /tmp/_lsd | head -25
  else bad "rclone API access failed:"; tail -5 /tmp/_lsd | sed 's/^/    /'; fi
  timeout 30 rclone about "$REMOTE": 2>/dev/null | sed 's/^/    /'

  hdr "8. rclone VFS write cache (files not yet uploaded)"
  vfs="$CACHE_DIR/vfs/$REMOTE"
  if [[ -d "$vfs" ]]; then
    n=$(find "$vfs" -type f 2>/dev/null | wc -l)
    echo "  $vfs: $n file(s), $(du -sh "$vfs" 2>/dev/null | cut -f1)"
    [[ $n -gt 0 ]] && warn "Run 'backup' BEFORE 'fix' — these may be notes never uploaded" \
      && find "$vfs" -type f -newermt '-7 days' 2>/dev/null | head -20 | sed 's/^/    /'
  else warn "no VFS cache at $vfs (check --cache-dir)"; fi

  hdr "Verdict"
  echo "  • Container rc≠0 but host OK          → run: fix   (stale bind mount, just recreate)"
  echo "  • Host stat hung/failed                → run: backup, then fix"
  echo "  • Section 3 shows OOM kill of rclone   → add swap / --vfs-cache-max-size, see README"
  echo "  • Section 7 fails                      → token expired: rclone config reconnect $REMOTE:"
}

# --------------------------------------------------------------- backup ---
backup() {
  vfs="$CACHE_DIR/vfs/$REMOTE"
  [[ -d "$vfs" ]] || { warn "no VFS cache at $vfs — nothing to back up"; return; }
  out="/root/rclone-vfs-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$out" -C "$CACHE_DIR/vfs" "$REMOTE" && ok "saved → $out ($(du -h "$out" | cut -f1))"
  echo "  Restore a file later with: tar -xzf $out -C /tmp && ls /tmp/$REMOTE"
}

# ------------------------------------------------------------------ fix ---
fix() {
  [[ -n "${COMPOSE_DIR:-}" && -f "$COMPOSE_DIR/docker-compose.yml" ]] || \
    { bad "compose dir not found — set COMPOSE_DIR=..."; exit 1; }
  read -rp "Stop bot, remount $UNIT, recreate container? Did you run 'backup'? [y/N] " a
  [[ "$a" == [yY] ]] || { echo "aborted"; exit 0; }

  hdr "Stop the bot (releases its handles on the dead mount)"
  (cd "$COMPOSE_DIR" && timeout 60 docker compose stop) || \
    { warn "graceful stop hung — killing"; docker kill "$CONTAINER"; }

  hdr "Remount rclone"
  systemctl stop "$UNIT"
  fusermount -uz "$HOST_PATH" 2>/dev/null || umount -l "$HOST_PATH" 2>/dev/null || true
  sleep 2
  systemctl start "$UNIT"
  for i in $(seq 1 15); do probe "$HOST_PATH" && mountpoint -q "$HOST_PATH" && break; sleep 2; done
  if probe "$HOST_PATH" && mountpoint -q "$HOST_PATH"; then ok "host mount healthy"
  else bad "host mount still broken — see: journalctl -u $UNIT -n 50"; exit 1; fi
  t="$HOST_PATH/.triage_write_$$"
  timeout "$T" sh -c "echo ok > '$t' && rm -f '$t'" && ok "host write test OK" || bad "host write test FAILED"

  hdr "Recreate container (fresh bind mount + re-reads .env)"
  (cd "$COMPOSE_DIR" && docker compose up -d --force-recreate)
  sleep 5
  if timeout $((T+5)) docker exec "$CONTAINER" sh -c \
      "timeout $T touch /data/vault/.triage_c && rm -f /data/vault/.triage_c"; then
    ok "container write test OK — send /vault in Telegram to confirm"
  else bad "container still cannot write — re-run diagnose"; fi
  docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/    /'
}

# ------------------------------------------------------------- organize ---
organize() {
  probe "$HOST_PATH" || { bad "vault not readable — run fix first"; exit 1; }
  V="$HOST_PATH"
  hdr "/organize log (what it intended)"
  if timeout $T test -f "$V/10_Categories/_organize_log.md"; then
    timeout $T tail -20 "$V/10_Categories/_organize_log.md" | sed 's/^/  /'
  else
    warn "no _organize_log.md — the run never reached the end (interrupted mid-move)"
  fi
  hdr "Duplicates created on name clashes (*-moved.md)"
  timeout 60 find "$V" -path "$V/.git" -prune -o -name '*-moved.md' -print 2>/dev/null | head -30 | sed 's/^/  /'
  hdr "Folders by note count (sparse leftovers = partially merged sources)"
  for d in "$V"/*/; do
    n=$(timeout 30 find "$d" -name '*.md' 2>/dev/null | wc -l); echo "  $n  $(basename "$d")"
  done | sort -n
  hdr "Git state inside the vault"
  if [[ -d "$V/.git" ]]; then
    [[ -e "$V/.git/index.lock" ]] && bad "stale .git/index.lock present — /organize's commit died; remove it from a PC: del .git\\index.lock" \
                                  || ok "no index.lock"
    warn "do NOT run git over the rclone mount — check 'git status' on a PC with Google Drive Desktop"
  else echo "  no .git in vault"; fi
}

detect
case "${1:-diagnose}" in
  diagnose) diagnose ;;
  backup)   backup ;;
  fix)      fix ;;
  organize) organize ;;
  *) echo "usage: $0 [diagnose|backup|fix|organize]"; exit 2 ;;
esac
