#!/usr/bin/env bash
# Deploy Tilla to the VPS by shipping changed files individually.
# Never a full-directory clobber: stores/, .env and www/ are server-owned.
set -euo pipefail

VPS="root@75.119.153.252"
REMOTE="/opt/tilla"
BASE="https://tilla.gudman.xyz"

# The app package + themes. These are the only paths this script ever writes.
FILES=(
  app/__init__.py
  app/main.py
  app/engine.py
  app/payment.py
  themes/original.html
  themes/bold.html
  themes/editorial.html
)

cd "$(dirname "$0")/.."

for f in "${FILES[@]}"; do
  ssh "$VPS" "mkdir -p '$REMOTE/$(dirname "$f")'"
  scp "$f" "$VPS:$REMOTE/$f"
done

# Migrations are part of every deploy from M2 on.
if [ -d alembic ]; then
  ssh "$VPS" "cd '$REMOTE' && alembic upgrade head"
fi

ssh "$VPS" "systemctl restart tilla-api"

# Smoke: health is up and an unpaid create-store is still x402-gated.
curl -fsS "$BASE/health" >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/create-store" \
  -H 'content-type: application/json' -d '{"description":"deploy smoke"}')
if [ "$code" != "402" ]; then
  echo "smoke failed: unpaid POST /create-store returned $code, expected 402" >&2
  exit 1
fi
echo "deploy ok: health up, create-store gated (402)"
