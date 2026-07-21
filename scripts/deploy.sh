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
  app/delivery.py
  app/limiter.py
  app/agentic.py
  app/mpp.py
  app/subscriptions.py
  app/import_stores.py
  alembic.ini
  alembic/env.py
  alembic/script.py.mako
  alembic/versions/0001_persistence_core.py
  alembic/versions/0002_hardened_checkout.py
  alembic/versions/0003_gated_delivery.py
  alembic/versions/0004_agent_commerce.py
  alembic/versions/0005_payment_methods.py
  scripts/backup_db.sh
  themes/original.html
  themes/bold.html
  themes/editorial.html
)

# Subscription sidecar (Node). node_modules is server-owned (installed via
# `npm ci --omit=dev`), never scp'd. The systemd unit is copied once by hand
# (see deploy/tilla-sidecar.service header); here we only refresh the JS.
SIDECAR_FILES=(
  sidecar/server.js
  sidecar/sample-buyer.js
  sidecar/package.json
  sidecar/package-lock.json
  sidecar/README.md
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

# Refresh the subscription sidecar JS (dormant unless TILLA_SUBSCRIPTIONS_ENABLED
# + OKX creds; bound to 127.0.0.1:8790, never nginx-exposed). Only restart it if
# the unit is already installed — a missing unit is fine (subscribe proxy 503s).
for f in "${SIDECAR_FILES[@]}"; do
  ssh "$VPS" "mkdir -p '$REMOTE/$(dirname "$f")'"
  scp "$f" "$VPS:$REMOTE/$f"
done
ssh "$VPS" "test -d '$REMOTE/sidecar/node_modules' || (cd '$REMOTE/sidecar' && npm ci --omit=dev)"
ssh "$VPS" "systemctl is-enabled tilla-sidecar >/dev/null 2>&1 && systemctl restart tilla-sidecar || true"

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
