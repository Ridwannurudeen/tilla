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
  app/dashboard.py
  app/webhooks.py
  app/refunds.py
  app/attest.py
  app/warden_hire.py
  app/affiliates.py
  app/external_feeds.py
  app/embed.py
  app/acp.py
  app/growth.py
  assets/embed.js
  alembic.ini
  alembic/env.py
  alembic/script.py.mako
  alembic/versions/0001_persistence_core.py
  alembic/versions/0002_hardened_checkout.py
  alembic/versions/0003_gated_delivery.py
  alembic/versions/0004_agent_commerce.py
  alembic/versions/0005_payment_methods.py
  alembic/versions/0006_merchant_platform.py
  alembic/versions/0007_marketplace_citizenship.py
  alembic/versions/0008_onchain_receipts.py
  alembic/versions/0009_growth.py
  scripts/backup_db.sh
  scripts/backup_offsite.sh
  scripts/restore_drill.sh
  scripts/watchdog.sh
  themes/original.html
  themes/bold.html
  themes/editorial.html
  themes/_fonts.html
  themes/_checkout.html
  themes/_dashboard.html
  themes/og.svg
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

# The shell scripts must stay executable — cron runs backup_db.sh/backup_offsite.sh and
# the watchdog timer execs watchdog.sh directly (scp does not preserve the +x bit).
ssh "$VPS" "chmod +x '$REMOTE'/scripts/*.sh"

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

# /ready may briefly 503 right after restart until the first sweeper tick stamps its
# heartbeats, so poll for up to ~30s before treating it as a failure.
ready_ok=0
for _ in $(seq 1 15); do
  if curl -fsS "$BASE/ready" >/dev/null 2>&1; then ready_ok=1; break; fi
  sleep 2
done
if [ "$ready_ok" != 1 ]; then
  echo "smoke failed: /ready did not reach 200 within ~30s" >&2
  curl -s "$BASE/ready" >&2 || true
  exit 1
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/create-store" \
  -H 'content-type: application/json' -d '{"description":"deploy smoke"}')
if [ "$code" != "402" ]; then
  echo "smoke failed: unpaid POST /create-store returned $code, expected 402" >&2
  exit 1
fi

# M13 growth smoke. External feeds + embed.js are read-only/static and must 200
# (these routes require the nginx-growth.snippet locations to be applied first —
# the orchestrator applies nginx, so on a pre-nginx deploy they may 404 at the edge
# while answering 200 directly on 127.0.0.1:8040). Waitlist stores an email (silent
# {ok:true}). ACP create must 503 while TILLA_ACP_ENABLED is unset (dormant mount);
# flip the flag in .env and re-run only after smoking the tx-hash complete path.
smoke_code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
for path in "s/invoice-flow/feed/openai.json" "s/invoice-flow/feed/google.xml" \
            "embed.js" "feeds/openai.json"; do
  c=$(smoke_code "$BASE/$path")
  if [ "$c" != "200" ]; then
    echo "smoke WARN: GET /$path returned $c (expected 200; check nginx-growth.snippet)" >&2
  fi
done
wl=$(smoke_code -X POST "$BASE/api/stores/invoice-flow/waitlist" \
  -H 'content-type: application/json' -d '{"email":"deploy-smoke@example.com"}')
[ "$wl" = "200" ] || echo "smoke WARN: waitlist returned $wl (expected 200)" >&2
acp=$(smoke_code -X POST "$BASE/s/invoice-flow/checkout_sessions" \
  -H 'content-type: application/json' -d '{}')
[ "$acp" = "503" ] || echo "smoke WARN: ACP create returned $acp (expected 503 while dormant)" >&2

# Growth-kit is merchant-gated: an UNAUTH POST must 401 (proves the route is mounted
# and gated without spending an LLM call or moving funds). /api/ is already proxied by
# nginx (no new location needed). Generating a real kit needs the store's manage key
# and is a user-gated smoke step (one haiku call), not run here.
gk=$(smoke_code -X POST "$BASE/api/stores/invoice-flow/growth-kit")
[ "$gk" = "401" ] || echo "smoke WARN: growth-kit unauth POST returned $gk (expected 401)" >&2

echo "deploy ok: health up, ready 200, live stores render, create-store gated (402)"
