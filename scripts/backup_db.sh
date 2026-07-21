#!/usr/bin/env bash
# Nightly WAL-safe SQLite backup with keep-7 rotation, plus an accumulating mirror of
# the immutable deliverable files. Never a plain `cp` — `.backup` is a consistent
# snapshot even while the API is mid-write in WAL mode.
#
# Install on the VPS as a root crontab line (once):
#   15 4 * * * /opt/tilla/scripts/backup_db.sh >> /var/log/tilla-backup.log 2>&1
set -euo pipefail

DB="${TILLA_DB_PATH:-/opt/tilla/tilla.db}"
BACKUP_DIR="$(dirname "$DB")/backups"
DELIVERABLES="${TILLA_FILES_DIR:-/opt/tilla/deliverables}"
TG_CONF="/etc/solvent/telegram-alerts"

# On any error (set -e trips the ERR trap) alert via the SOLVENT bot creds, so a
# silently failing nightly backup can no longer go unnoticed. Skip if creds absent.
tg_alert() {
  local msg="$1"
  [ -r "$TG_CONF" ] || return 0
  # shellcheck disable=SC1090
  . "$TG_CONF"
  local token="${TELEGRAM_BOT_TOKEN:-${BOT_TOKEN:-${TG_TOKEN:-}}}"
  local chat="${TELEGRAM_CHAT_ID:-${CHAT_ID:-${TG_CHAT:-}}}"
  [ -n "$token" ] && [ -n "$chat" ] || return 0
  curl -fsS -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=[tilla-backup] ${msg}" >/dev/null || true
}
trap 'tg_alert "nightly backup FAILED (line $LINENO) — DB snapshot may be missing"' ERR

mkdir -p "$BACKUP_DIR"
DEST="$BACKUP_DIR/tilla-$(date +%F).db"

sqlite3 "$DB" ".backup '$DEST'"
sqlite3 "$DEST" 'PRAGMA integrity_check' >/dev/null

# keep only the 7 most recent dated backups
ls -1t "$BACKUP_DIR"/tilla-*.db 2>/dev/null | tail -n +8 | xargs -r rm -f

# Mirror the sha256-named, immutable deliverable files (accumulating, NO --delete: a
# deliberately deleted deliverable lingering in the backup is acceptable and documented).
# stores/ is intentionally NOT backed up — rerender_stores() rebuilds it from the DB.
if [ -d "$DELIVERABLES" ]; then
  mkdir -p "$BACKUP_DIR/deliverables"
  rsync -a "$DELIVERABLES/" "$BACKUP_DIR/deliverables/"
fi

echo "backup ok: $DEST"
