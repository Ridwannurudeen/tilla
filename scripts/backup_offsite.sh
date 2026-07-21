#!/usr/bin/env bash
# Off-VPS backup: push the local backups/ dir (the 7 DB snapshots + the deliverables
# mirror) to a USER-provided remote. A safe no-op (exit 0) until the target is set, so
# same-disk keep-7 stays the honest default until the user provides a destination.
#
# Reads /etc/tilla-backup.env (root:600, SEPARATE from /opt/tilla/.env so the app never
# loads backup creds):
#   TILLA_BACKUP_REMOTE="user@host:/path/to/tilla-backups"   (required to activate)
#   TILLA_BACKUP_SSH_KEY="/root/.ssh/tilla_backup"           (optional)
#
# Cron (after the 4:15 DB backup):
#   35 4 * * * /opt/tilla/scripts/backup_offsite.sh >> /var/log/tilla-offsite.log 2>&1
set -euo pipefail

ENV_FILE="/etc/tilla-backup.env"
DB="${TILLA_DB_PATH:-/opt/tilla/tilla.db}"
BACKUP_DIR="$(dirname "$DB")/backups"
TG_CONF="/etc/solvent/telegram-alerts"

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
    --data-urlencode "text=[tilla-offsite] ${msg}" >/dev/null || true
}
trap 'tg_alert "offsite backup FAILED (line $LINENO)"' ERR

if [ ! -r "$ENV_FILE" ]; then
  echo "offsite not configured ($ENV_FILE absent) — skipping"
  exit 0
fi
# shellcheck disable=SC1090
. "$ENV_FILE"
if [ -z "${TILLA_BACKUP_REMOTE:-}" ]; then
  echo "offsite not configured (TILLA_BACKUP_REMOTE unset) — skipping"
  exit 0
fi

SSH_CMD="ssh -o StrictHostKeyChecking=accept-new"
if [ -n "${TILLA_BACKUP_SSH_KEY:-}" ]; then
  SSH_CMD="ssh -i ${TILLA_BACKUP_SSH_KEY} -o StrictHostKeyChecking=accept-new"
fi

rsync -az --partial -e "$SSH_CMD" "$BACKUP_DIR/" "$TILLA_BACKUP_REMOTE"
echo "offsite ok: pushed $BACKUP_DIR/ -> $TILLA_BACKUP_REMOTE"
