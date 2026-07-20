#!/usr/bin/env bash
# Nightly WAL-safe SQLite backup with keep-7 rotation. Never a plain `cp` —
# `.backup` is a consistent snapshot even while the API is mid-write in WAL mode.
#
# Install on the VPS as a root crontab line (once):
#   15 4 * * * /opt/tilla/scripts/backup_db.sh >> /var/log/tilla-backup.log 2>&1
set -euo pipefail

DB="${TILLA_DB_PATH:-/opt/tilla/tilla.db}"
BACKUP_DIR="$(dirname "$DB")/backups"
mkdir -p "$BACKUP_DIR"
DEST="$BACKUP_DIR/tilla-$(date +%F).db"

sqlite3 "$DB" ".backup '$DEST'"
sqlite3 "$DEST" 'PRAGMA integrity_check' >/dev/null

# keep only the 7 most recent dated backups
ls -1t "$BACKUP_DIR"/tilla-*.db 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "backup ok: $DEST"
