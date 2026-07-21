#!/usr/bin/env bash
# Restore drill (M12 acceptance). Restores a backup DB into a TEMP dir — NEVER touches
# the /opt/tilla live paths — runs PRAGMA integrity_check, verifies the schema is at the
# expected migration head, prints stores/orders/deliverables row counts, and re-hashes up
# to 20 deliverable files against the DB's file_sha256. Exit 0 = PASS, non-zero = FAIL.
#
# Usage: restore_drill.sh <backup.db> [deliverables-dir]
#   e.g. restore_drill.sh /opt/tilla/backups/tilla-2026-07-21.db /opt/tilla/backups/deliverables
set -uo pipefail

BACKUP_DB="${1:-}"
DELIV_DIR="${2:-}"
EXPECTED_HEAD="0008_onchain_receipts"

fail() { echo "DRILL FAIL: $*" >&2; exit 1; }

if [ -z "$BACKUP_DB" ]; then
  echo "usage: restore_drill.sh <backup.db> [deliverables-dir]" >&2
  exit 2
fi
[ -r "$BACKUP_DB" ] || fail "backup not readable: $BACKUP_DB"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DEST="$TMP/restored.db"

# .backup restores a consistent copy even from a WAL-mode source snapshot.
sqlite3 "$BACKUP_DB" ".backup '$DEST'" || fail "restore copy failed"

ic="$(sqlite3 "$DEST" 'PRAGMA integrity_check;')"
[ "$ic" = "ok" ] || fail "integrity_check returned: $ic"

head="$(sqlite3 "$DEST" 'SELECT version_num FROM alembic_version;' 2>/dev/null)"
[ "$head" = "$EXPECTED_HEAD" ] || fail "migration head is '$head', expected '$EXPECTED_HEAD'"

stores="$(sqlite3 "$DEST" 'SELECT COUNT(*) FROM stores;')"
orders="$(sqlite3 "$DEST" 'SELECT COUNT(*) FROM orders;')"
delivs="$(sqlite3 "$DEST" 'SELECT COUNT(*) FROM deliverables;')"
echo "restored ok: stores=$stores orders=$orders deliverables=$delivs head=$head"

# Deliverables are content-addressed: on disk at <dir>/<sha[:2]>/<sha>, and each file's
# own sha256 must equal its DB file_sha256. Re-hash up to 20 to prove the mirror is intact.
if [ -n "$DELIV_DIR" ] && [ -d "$DELIV_DIR" ]; then
  checked=0
  mismatched=0
  while read -r fsha; do
    [ -n "$fsha" ] || continue
    f="$DELIV_DIR/${fsha:0:2}/$fsha"
    if [ ! -f "$f" ]; then
      echo "  MISSING $fsha"
      mismatched=$((mismatched + 1))
      continue
    fi
    actual="$(sha256sum "$f" | awk '{print $1}')"
    if [ "$actual" != "$fsha" ]; then
      echo "  MISMATCH $fsha (got $actual)"
      mismatched=$((mismatched + 1))
    else
      checked=$((checked + 1))
    fi
  done < <(sqlite3 "$DEST" \
    "SELECT file_sha256 FROM deliverables WHERE file_sha256 IS NOT NULL LIMIT 20;")
  echo "deliverable hash check: verified=$checked mismatched=$mismatched"
  [ "$mismatched" -eq 0 ] || fail "$mismatched deliverable hash mismatch(es)"
fi

echo "DRILL PASS"
