#!/usr/bin/env bash
# Deploy Tilla to the VPS through a fully staged, consistency-preserving update.
# Never a full-directory clobber: stores/, deliverables/, tilla.db*, and .env are
# server-owned (the www/ pages listed below are repo-owned; the rest of www/ stays
# server-owned).
set -euo pipefail

VPS="root@75.119.153.252"
REMOTE="/opt/tilla"
BASE="https://tilla.gudman.xyz"
# The service's own interpreter env (verify with `systemctl cat tilla-api` which
# python runs uvicorn). Alembic MUST run from this env so it hits the same
# SQLAlchemy the app uses — a PATH-resolved `alembic` could be a different python.
VENV="$REMOTE/.venv"

cd "$(dirname "$0")/.."

# The app package + themes + migration tooling — GENERATED from git so the
# manifest can never go stale. (The old hand-maintained list silently omitted
# 9 modules that app/main.py imports plus migrations 0011-0029: a fresh-host
# deploy would have ImportError'd at boot, and any edit to an omitted module
# produced a torn deploy — new main.py, stale dependency.) git only tracks
# repo-owned paths, so `git ls-files` is exactly the ship set; stores/,
# tilla.db*, .env and everything else server-owned is untracked by design.
# deploy.sh itself is excluded (nothing on the server executes it).
#
# scripts/ ships BOTH halves. 'scripts/*.sh' alone was the same hand-picked-glob
# bug this comment already describes: the ops scripts meant to be RUN on the box
# (backfill, remediations, the delivery-text repair) are python, so they silently
# never arrived and `python -m scripts.x` failed with "No module named" on a host
# where the file was tracked, reviewed and merged.
mapfile -t FILES < <(git ls-files \
  'app/*.py' \
  'assets/embed.js' \
  'pyproject.toml' \
  'alembic.ini' 'alembic/env.py' 'alembic/script.py.mako' 'alembic/versions/*.py' \
  'scripts/*.sh' 'scripts/*.py' \
  'themes/*' \
  'www/*' \
  'sidecar/*' | grep -v '^scripts/deploy\.sh$')

# A running Python or Node process must never observe a mixture of old and new
# modules. This is deliberately *not* called an atomic deploy: both services are
# stopped only after the complete release has reached the VPS, then started once the
# code, dependencies, migrations, and import have succeeded. The short maintenance
# window is safer than pretending file-by-file replacement is atomic.
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short=12 HEAD)"
STAGE="$REMOTE/.deploy-stage-$RELEASE_ID"
BACKUP="$REMOTE/.deploy-backup-$RELEASE_ID"
MANIFEST="$STAGE/.manifest"
DEPLOYING=0
SIDECAR_WAS_ACTIVE=0
SIDECAR_SHOULD_START=0

cleanup_stage() {
  ssh "$VPS" "rm -rf -- '$STAGE'" || true
}

rollback_code() {
  local status=$?
  trap - ERR
  if [ "$DEPLOYING" = 1 ]; then
    echo "deploy failed while services were stopped; restoring staged files" >&2
    ssh "$VPS" "bash -s -- '$REMOTE' '$STAGE' '$BACKUP'" <<'REMOTE_ROLLBACK' || true
set -euo pipefail
remote=$1
stage=$2
backup=$3
while IFS= read -r file; do
  target="$remote/$file"
  previous="$backup/files/$file"
  if [ -e "$previous" ]; then
    mkdir -p "$(dirname "$target")"
    cp -p -- "$previous" "$target"
  else
    rm -f -- "$target"
  fi
done < "$stage/.manifest"
REMOTE_ROLLBACK
    ssh "$VPS" "systemctl start tilla-api" || true
    if [ "$SIDECAR_WAS_ACTIVE" = 1 ]; then
      ssh "$VPS" "systemctl start tilla-sidecar" || true
    fi
  fi
  exit "$status"
}

trap cleanup_stage EXIT
trap rollback_code ERR

# Git owns this manifest, but reject unusual paths before placing one inside a
# remote shell command. Persistent server-owned paths can never enter it.
for file in "${FILES[@]}"; do
  case "$file" in
    "" | /* | *..* | *[!A-Za-z0-9._/-]*)
      echo "unsafe deploy path: $file" >&2
      exit 2
      ;;
  esac
done

ssh "$VPS" "test ! -e '$STAGE' && test ! -e '$BACKUP' && mkdir -p '$STAGE'"
tar -cf - "${FILES[@]}" | ssh "$VPS" "tar -xf - -C '$STAGE'"
ssh "$VPS" "cd '$STAGE' && find . -type f -printf '%P\\n' | LC_ALL=C sort > '$MANIFEST'"
remote_count=$(ssh "$VPS" "wc -l < '$MANIFEST'")
[ "$remote_count" = "${#FILES[@]}" ] || {
  echo "staged file count mismatch: expected ${#FILES[@]}, got $remote_count" >&2
  exit 1
}

# The stage is complete before downtime. Compile only; importing app.main could
# make network-backed startup work while a production process is still live.
ssh "$VPS" "'$VENV/bin/python' -m compileall -q '$STAGE/app'"

PYTHON_DEPS_CHANGED=0
SIDECAR_DEPS_CHANGED=0
ssh "$VPS" "cmp -s '$STAGE/pyproject.toml' '$REMOTE/pyproject.toml'" || PYTHON_DEPS_CHANGED=1
ssh "$VPS" "cmp -s '$STAGE/sidecar/package-lock.json' '$REMOTE/sidecar/package-lock.json'" || SIDECAR_DEPS_CHANGED=1
if ssh "$VPS" "systemctl is-enabled tilla-sidecar >/dev/null 2>&1"; then
  SIDECAR_SHOULD_START=1
fi
if ssh "$VPS" "systemctl is-active --quiet tilla-sidecar"; then
  SIDECAR_WAS_ACTIVE=1
fi

# Save precisely the repo-owned files that will be replaced. The rollback never
# touches .env, database/WAL files, stores/, deliverables/, or any other VPS data.
ssh "$VPS" "bash -s -- '$REMOTE' '$STAGE' '$BACKUP'" <<'REMOTE_BACKUP'
set -euo pipefail
remote=$1
stage=$2
backup=$3
mkdir -p "$backup/files"
: > "$backup/.existing"
while IFS= read -r file; do
  source="$remote/$file"
  if [ -e "$source" ]; then
    target="$backup/files/$file"
    mkdir -p "$(dirname "$target")"
    cp -p -- "$source" "$target"
    printf '%s\n' "$file" >> "$backup/.existing"
  fi
done < "$stage/.manifest"
REMOTE_BACKUP

ssh "$VPS" "systemctl stop tilla-api"
if [ "$SIDECAR_WAS_ACTIVE" = 1 ]; then
  ssh "$VPS" "systemctl stop tilla-sidecar"
fi
DEPLOYING=1

ssh "$VPS" "bash -s -- '$REMOTE' '$STAGE'" <<'REMOTE_APPLY'
set -euo pipefail
remote=$1
stage=$2
while IFS= read -r file; do
  target="$remote/$file"
  mkdir -p "$(dirname "$target")"
  cp -p -- "$stage/$file" "$target"
done < "$stage/.manifest"
REMOTE_APPLY

# The shell scripts must stay executable — cron runs backup_db.sh/backup_offsite.sh and
# the watchdog timer execs watchdog.sh directly (scp does not preserve the +x bit).
ssh "$VPS" "chmod +x '$REMOTE'/scripts/*.sh"

# The project manifest is itself shipped. Reconcile Python packages only when it
# changed, then verify the resulting virtualenv before migration/restart. Likewise
# `npm ci` follows a package-lock change rather than leaving a stale sidecar tree.
if [ "$PYTHON_DEPS_CHANGED" = 1 ]; then
  ssh "$VPS" "cd '$REMOTE' && '$VENV/bin/pip' install --upgrade ."
fi
ssh "$VPS" "'$VENV/bin/pip' check"
if [ "$SIDECAR_DEPS_CHANGED" = 1 ] || ! ssh "$VPS" "test -d '$REMOTE/sidecar/node_modules'"; then
  ssh "$VPS" "cd '$REMOTE/sidecar' && npm ci --omit=dev"
fi

# Migrate BEFORE restart so new code never meets an old schema. Runs without the
# systemd EnvironmentFile; do not source .env over ssh (keeps secrets out of argv).
if [ -d alembic ]; then
  ssh "$VPS" "cd '$REMOTE' && '$VENV/bin/alembic' upgrade head"
fi

# Import any on-disk stores that have no DB row yet (idempotent — re-runs are
# no-ops). Runs before restart so a live store's checkout never 404s.
ssh "$VPS" "cd '$REMOTE' && '$VENV/bin/python' -m app.import_stores"

ssh "$VPS" "systemctl start tilla-api"
if [ "$SIDECAR_SHOULD_START" = 1 ]; then
  ssh "$VPS" "systemctl start tilla-sidecar"
fi
DEPLOYING=0
trap - ERR

# Smoke: health is up, both live stores still render (nginx serves them
# statically — unaffected by the restart), and an unpaid create-store is still
# x402-gated.
#
# A failing check RECORDS and continues rather than exiting on the spot. The old
# script aborted at the first hard failure, so a slow boot did not just report
# "smoke failed" — it also skipped the store renders, /ready, the 402 gate and
# every growth check below it. That is the worst outcome available: a scary
# message AND less verification than a passing run. Failures are summarised at
# the end and the script still exits non-zero.
SMOKE_FAILURES=()
fail() { echo "smoke FAILED: $*" >&2; SMOKE_FAILURES+=("$1"); }

# Poll rather than single-shot: the restart (and any watchdog bounce while uvicorn
# boots and rerender_stores runs) briefly 502s at the edge.
#
# The budget was 40s against a boot measured at 38s and 61s on the same day — a
# coin flip that failed a perfectly healthy deploy. Boot is roughly process start
# plus rerender_stores, and rerender scales with the store count, so the budget
# has to have real headroom rather than track today's number. Elapsed time is
# printed on success so the creep is visible while it is still cheap.
health_start=$SECONDS
health_ok=0
for _ in $(seq 1 90); do
  if curl -fsS "$BASE/health" >/dev/null 2>&1; then health_ok=1; break; fi
  sleep 2
done
if [ "$health_ok" = 1 ]; then
  echo "smoke: /health up after $((SECONDS - health_start))s"
else
  fail "/health did not reach 200 within ~180s"
fi
for slug in invoice-flow billable; do
  curl -fsS "$BASE/s/$slug/" >/dev/null || fail "store /s/$slug/ did not render"
done

# /ready may briefly 503 right after restart until the first sweeper tick stamps its
# heartbeats, so poll before treating it as a failure.
ready_ok=0
for _ in $(seq 1 30); do
  if curl -fsS "$BASE/ready" >/dev/null 2>&1; then ready_ok=1; break; fi
  sleep 2
done
if [ "$ready_ok" != 1 ]; then
  curl -s "$BASE/ready" >&2 || true
  fail "/ready did not reach 200 within ~60s"
fi
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/create-store" \
  -H 'content-type: application/json' -d '{"description":"deploy smoke"}')
[ "$code" = "402" ] || fail "unpaid POST /create-store returned $code, expected 402"

# M13 growth smoke. External feeds + embed.js are read-only/static and must 200
# (these routes require the nginx-growth.snippet locations to be applied first —
# the orchestrator applies nginx, so on a pre-nginx deploy they may 404 at the edge
# while answering 200 directly on 127.0.0.1:8040). Waitlist stores an email (silent
# {ok:true}). The ACP expectation is flag-aware — see the case below.
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
# 503 only while the rail is DORMANT. Prod runs TILLA_ACP_ENABLED=1 with a signing
# secret, where an unsigned POST is correctly refused at the signature gate (401)
# before any DB write — so a flat 503 expectation printed a WARN on EVERY deploy and
# trained the reader to ignore smoke output. Accept either shape; never smoke the
# enabled rail with a real signed create.
case "$acp" in
  503) : ;;              # dormant: router mounted, rail off
  401|400|422) : ;;      # enabled: mounted and gated, unsigned request refused
  *) echo "smoke WARN: ACP create returned $acp (expected 503 dormant, or 401/4xx gated)" >&2 ;;
esac

# Growth-kit is merchant-gated: an UNAUTH POST must 401 (proves the route is mounted
# and gated without spending an LLM call or moving funds). /api/ is already proxied by
# nginx (no new location needed). Generating a real kit needs the store's manage key
# and is a user-gated smoke step (one haiku call), not run here.
gk=$(smoke_code -X POST "$BASE/api/stores/invoice-flow/growth-kit")
[ "$gk" = "401" ] || echo "smoke WARN: growth-kit unauth POST returned $gk (expected 401)" >&2

# Agentic surfaces (deploy/nginx-agentic-surfaces.snippet must be applied at the
# edge or every one of these serves the static HTML 404 while working on
# 127.0.0.1:8040 — the exact regression that hid the whole agent surface once).
# Each must answer app JSON, whatever the status code its own gating returns.
for path in "mcp" "s/invoice-flow/reviews" "s/invoice-flow/quote"; do
  ctype=$(curl -s -o /dev/null -w '%{content_type}' "$BASE/$path")
  case "$ctype" in
    application/json*) : ;;
    *) echo "smoke WARN: GET /$path answered '$ctype' not JSON (check nginx-agentic-surfaces.snippet)" >&2 ;;
  esac
done
mpp=$(smoke_code -X POST "$BASE/s/invoice-flow/mpp/open" -H 'content-type: application/json' -d '{}')
[ "$mpp" != "404" ] || echo "smoke WARN: POST /s/…/mpp/open returned 404 (nginx not exposing mpp/*)" >&2
sub=$(smoke_code -X POST "$BASE/s/invoice-flow/subscribe" -H 'content-type: application/json' -d '{}')
[ "$sub" != "404" ] || echo "smoke WARN: POST /s/…/subscribe returned 404 (nginx not exposing subscribe*)" >&2

if [ ${#SMOKE_FAILURES[@]} -ne 0 ]; then
  echo "" >&2
  echo "deploy FAILED ${#SMOKE_FAILURES[@]} smoke check(s):" >&2
  for f in "${SMOKE_FAILURES[@]}"; do echo "  - $f" >&2; done
  echo "Every check above still ran; the files are already deployed and the" >&2
  echo "service restarted, so this reports what is broken, not what was skipped." >&2
  exit 1
fi

ssh "$VPS" "rm -rf -- '$BACKUP'"
echo "deploy ok: health up, ready 200, live stores render, create-store gated (402)"
