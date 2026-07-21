#!/usr/bin/env bash
# Deploy Tilla to the VPS by shipping changed files individually.
# Never a full-directory clobber: stores/, .env and www/ are server-owned.
set -euo pipefail

VPS="root@75.119.153.252"
REMOTE="/opt/tilla"
BASE="https://tilla.gudman.xyz"
# The service's own interpreter env (verify with `systemctl cat tilla-api` which
# python runs uvicorn). Alembic MUST run from this env so it hits the same
# SQLAlchemy the app uses — a PATH-resolved `alembic` could be a different python.
VENV="$REMOTE/.venv"

# The app package + themes + migration tooling. These are the only paths this
# script ever writes (stores/, tilla.db*, .env stay server-owned).
FILES=(
  app/__init__.py
  app/main.py
  app/engine.py
  app/payment.py
  app/config.py
  app/render.py
  app/screening.py
  app/db.py
  app/models.py
  app/chain.py
  app/checkout.py
  app/import_stores.py
  alembic.ini
  alembic/env.py
  alembic/script.py.mako
  alembic/versions/0001_persistence_core.py
  alembic/versions/0002_hardened_checkout.py
  scripts/backup_db.sh
  themes/original.html
  themes/bold.html
  themes/editorial.html
)

cd "$(dirname "$0")/.."

for f in "${FILES[@]}"; do
  ssh "$VPS" "mkdir -p '$REMOTE/$(dirname "$f")'"
  scp "$f" "$VPS:$REMOTE/$f"
done

# Migrate BEFORE restart so new code never meets an old schema. Runs without the
# systemd EnvironmentFile, so it relies on TILLA_DB_PATH being unset (default
# /opt/tilla/tilla.db); do not source .env over ssh (keeps secrets out of argv).
if [ -d alembic ]; then
  ssh "$VPS" "cd '$REMOTE' && '$VENV/bin/alembic' upgrade head"
fi

# Import any on-disk stores that have no DB row yet (idempotent — re-runs are
# no-ops). Runs before the restart so a live store's checkout never 404s.
ssh "$VPS" "cd '$REMOTE' && '$VENV/bin/python' -m app.import_stores"

ssh "$VPS" "systemctl restart tilla-api"

# Smoke: health is up, both live stores still render (nginx serves them
# statically — unaffected by the restart), and an unpaid create-store is still
# x402-gated.
curl -fsS "$BASE/health" >/dev/null
for slug in invoice-flow billable; do
  curl -fsS "$BASE/s/$slug/" >/dev/null
done
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/create-store" \
  -H 'content-type: application/json' -d '{"description":"deploy smoke"}')
if [ "$code" != "402" ]; then
  echo "smoke failed: unpaid POST /create-store returned $code, expected 402" >&2
  exit 1
fi
echo "deploy ok: health up, live stores render, create-store gated (402)"
