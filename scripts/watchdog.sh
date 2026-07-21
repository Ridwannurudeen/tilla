#!/usr/bin/env bash
# Tilla liveness + readiness watchdog. Run by tilla-watchdog.timer (~every 60s) as
# a oneshot unit, so all output lands in journald.
#
# Touches ONLY the literal systemd unit "tilla-api" (the single systemctl target in
# this file — no globs, never nginx, never any other project on the shared box):
#  1. liveness  = curl /health direct to uvicorn on :8040 (proves the port too).
#  2. down      -> restart within a budget (max 3/hour), re-probe, Telegram either way;
#                  budget exhausted -> alert at most once/hour, NO restart (no storm).
#  3. up + /ready 503 -> Telegram the failing checks, throttled once/hour per state,
#                  NO restart (a stale RPC/readiness issue isn't fixed by a bounce).
# Telegram uses the SOLVENT bot creds at /etc/solvent/telegram-alerts; if that file or
# its vars are absent, alerts are skipped and the restart logic still runs.
#
# NOT set -e: curl non-zero exits are the control flow and must be handled, not fatal.
set -uo pipefail

UNIT="tilla-api"
HEALTH="http://127.0.0.1:8040/health"
READY="http://127.0.0.1:8040/ready"
STATE="/opt/tilla/.watchdog.state"          # restart epochs, one per line (last hour)
RL_MARK="/opt/tilla/.watchdog.ratelimited"  # mtime = last "rate-limited" alert
RD_MARK="/opt/tilla/.watchdog.ready"        # content = last-alerted /ready body
MAX_RESTARTS=3                              # per rolling hour
TG_CONF="/etc/solvent/telegram-alerts"

now() { date +%s; }

# Source the SOLVENT creds if present; skip silently otherwise. Tolerates a few common
# variable names — the orchestrator confirms the exact names at install and adapts.
tg_send() {
  local msg="$1"
  if [ ! -r "$TG_CONF" ]; then
    echo "watchdog: telegram creds absent, alert skipped: $msg"
    return 0
  fi
  # shellcheck disable=SC1090
  . "$TG_CONF"
  local token="${SOLVENT_TG_BOT_TOKEN:-${TELEGRAM_BOT_TOKEN:-${BOT_TOKEN:-${TG_TOKEN:-}}}}"
  local chat="${SOLVENT_TG_CHAT_ID:-${TELEGRAM_CHAT_ID:-${CHAT_ID:-${TG_CHAT:-}}}}"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    echo "watchdog: telegram vars missing in $TG_CONF, alert skipped: $msg"
    return 0
  fi
  curl -fsS -m 10 "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=[tilla-watchdog] ${msg}" >/dev/null \
    || echo "watchdog: telegram send failed: $msg"
}

# Allowed if the marker is absent or was last touched more than 60 minutes ago.
throttle_ok() { [ -z "$(find "$1" -mmin -60 2>/dev/null)" ]; }

prune_restarts() {
  local cutoff
  cutoff=$(( $(now) - 3600 ))
  if [ -f "$STATE" ]; then
    awk -v c="$cutoff" '$1 ~ /^[0-9]+$/ && $1 >= c' "$STATE" > "$STATE.tmp" 2>/dev/null || true
    mv -f "$STATE.tmp" "$STATE" 2>/dev/null || true
  fi
}

restart_count() {
  if [ -f "$STATE" ]; then wc -l < "$STATE" | tr -d ' '; else echo 0; fi
}

if curl -fsS -m 5 "$HEALTH" >/dev/null 2>&1; then
  # Liveness OK -> check readiness (one call captures body + status code).
  resp="$(curl -sS -m 5 -w $'\n%{http_code}' "$READY" 2>/dev/null)"
  code="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  if [ "$code" = "503" ]; then
    last=""
    [ -f "$RD_MARK" ] && last="$(cat "$RD_MARK" 2>/dev/null)"
    # Alert if the failing state changed, or once/hour for the same state.
    if [ "$body" != "$last" ] || throttle_ok "$RD_MARK"; then
      tg_send "${UNIT} readiness DEGRADED (503): ${body}"
      printf '%s' "$body" > "$RD_MARK"
    fi
  else
    # Ready again -> clear the degraded marker so a future degrade re-alerts.
    rm -f "$RD_MARK" 2>/dev/null || true
  fi
else
  # Liveness FAILED -> restart within budget.
  prune_restarts
  n="$(restart_count)"
  if [ "$n" -lt "$MAX_RESTARTS" ]; then
    now >> "$STATE"
    systemctl restart "$UNIT"
    sleep 10
    if curl -fsS -m 5 "$HEALTH" >/dev/null 2>&1; then
      tg_send "${UNIT} was DOWN — restarted, now UP"
    else
      tg_send "${UNIT} restart FAILED, still down — manual attention needed"
    fi
  else
    # Budget exhausted: never storm the shared box. Alert at most once/hour.
    if throttle_ok "$RL_MARK"; then
      tg_send "${UNIT} DOWN and restart-rate-limited (${n} restarts in the last hour) — manual attention needed"
      : > "$RL_MARK"
    fi
  fi
fi
