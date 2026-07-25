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
#  4. subscription sidecar (Node, :8790) /health -> alert only, throttled once/hour.
#                  A dead sidecar 503s every subscribe; nothing else notices it.
#  5. app-side silent degradation: the journal marker app/attest.py logs after a run of
#                  idle ticks -> alert only, throttled once/hour.
#  6. agent presence: the tilla-a2a daemon (agent #8333's XMTP heartbeat) and the
#                  tilla-openclaw-gateway it dispatches through -> alert only,
#                  throttled once/hour. A dead a2a daemon silently drops #8333 to
#                  offline on the OKX marketplace while the website stays 200 — that
#                  is exactly how a 23h outage went unnoticed on 2026-07-25.
#  7. heartbeat staleness: unit up but no "heartbeat sent" in the journal for 10min
#                  (creds/RPC failing under a live process) -> alert only.
# All extra probes ALERT ONLY — this script restarts exactly one unit, "tilla-api".
# Telegram uses the SOLVENT bot creds at /etc/solvent/telegram-alerts; if that file or
# its vars are absent, alerts are skipped and the restart logic still runs.
#
# NOT set -e: curl non-zero exits are the control flow and must be handled, not fatal.
set -uo pipefail

UNIT="tilla-api"
HEALTH="http://127.0.0.1:8040/health"
READY="http://127.0.0.1:8040/ready"
SIDECAR="http://127.0.0.1:8790/health"      # subscription sidecar liveness (no creds)
ATTEST_MARKER="attest: DEGRADED"            # app/attest.py logs this after idle ticks
STATE="/opt/tilla/.watchdog.state"          # restart epochs, one per line (last hour)
RL_MARK="/opt/tilla/.watchdog.ratelimited"  # mtime = last "rate-limited" alert
RD_MARK="/opt/tilla/.watchdog.ready"        # content = last-alerted /ready body
SC_MARK="/opt/tilla/.watchdog.sidecar"      # mtime = last sidecar-down alert
AT_MARK="/opt/tilla/.watchdog.attest"       # mtime = last attest-degraded alert
A2_UNIT="tilla-a2a"                         # agent #8333 XMTP daemon (heartbeat source)
OC_UNIT="tilla-openclaw-gateway"            # AI dispatch gateway the daemon calls
A2_MARK="/opt/tilla/.watchdog.a2a"          # mtime = last a2a-unit-down alert
OC_MARK="/opt/tilla/.watchdog.openclaw"     # mtime = last gateway-down alert
HB_MARK="/opt/tilla/.watchdog.heartbeat"    # mtime = last stale-heartbeat alert
MAX_RESTARTS=3                              # per rolling hour
STARTUP_GRACE=90                            # secs a just-started unit may boot in
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
  # Liveness FAILED. FIRST: is it merely still BOOTING? tilla-api takes ~50s to
  # finish startup (model/registry warmup), which is longer than this timer's 60s
  # period, so a naive restart-on-failed-health kills the process mid-boot and the
  # next tick kills it again — a restart loop that never lets it come up. Observed
  # in production 2026-07-25 after a deploy: three kills in ~90s, /health 502
  # throughout, recovering only when one boot happened to land between ticks.
  # A unit younger than STARTUP_GRACE is given time instead of a bounce.
  pid="$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)"
  age=0
  if [ -n "$pid" ] && [ "$pid" != "0" ]; then
    age="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ' || echo 0)"
  fi
  if [ -n "$age" ] && [ "$age" -lt "$STARTUP_GRACE" ] 2>/dev/null; then
    exit 0   # still inside its startup window: no restart, no alert
  fi
  # Genuinely down -> restart within budget.
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

# Subscription sidecar liveness. Alert only (this script restarts one unit, tilla-api):
# a dead sidecar makes every /s/*/subscribe 503, and nothing else surfaces it.
if curl -fsS -m 5 "$SIDECAR" >/dev/null 2>&1; then
  rm -f "$SC_MARK" 2>/dev/null || true
elif throttle_ok "$SC_MARK"; then
  tg_send "subscription sidecar NOT answering ${SIDECAR} — subscribes are failing closed"
  : > "$SC_MARK"
fi

# Silent app-side degradation: app/attest.py logs ATTEST_MARKER every tick once a live
# attester has idled for a run of ticks (RPC unreachable, wrong chain, schema missing).
if journalctl -u "$UNIT" --since "-10min" --no-pager 2>/dev/null | grep -qF "$ATTEST_MARKER"; then
  if throttle_ok "$AT_MARK"; then
    tg_send "${UNIT} attester DEGRADED (idling; no receipts being written) — check the journal"
    : > "$AT_MARK"
  fi
else
  rm -f "$AT_MARK" 2>/dev/null || true
fi

# Agent presence. ALERT ONLY — these are never restarted here. A unit stuck in the
# systemd auto-restart loop reports "activating", not "active" or "failed", so the
# check is "is it exactly active", not "has it failed". Re-probed once after a short
# pause so a legitimate 5s restart window is not reported as an outage.
unit_down() {
  local u="$1"
  [ "$(systemctl is-active "$u" 2>/dev/null)" = "active" ] && return 1
  sleep 8
  [ "$(systemctl is-active "$u" 2>/dev/null)" = "active" ] && return 1
  return 0
}

if unit_down "$A2_UNIT"; then
  if throttle_ok "$A2_MARK"; then
    tg_send "${A2_UNIT} is NOT running ($(systemctl is-active "$A2_UNIT" 2>/dev/null)) — agent #8333 is going offline on the OKX marketplace"
    : > "$A2_MARK"
  fi
else
  rm -f "$A2_MARK" 2>/dev/null || true
  # Unit is up: is it still heartbeating? The daemon logs "heartbeat sent" ~every
  # minute; silence for 10min means a live process that stopped reporting presence.
  if journalctl -u "$A2_UNIT" --since "-10min" --no-pager 2>/dev/null | grep -qF "heartbeat sent"; then
    rm -f "$HB_MARK" 2>/dev/null || true
  elif throttle_ok "$HB_MARK"; then
    tg_send "${A2_UNIT} is up but has sent NO heartbeat in 10min — #8333 presence is going stale"
    : > "$HB_MARK"
  fi
fi

if unit_down "$OC_UNIT"; then
  if throttle_ok "$OC_MARK"; then
    tg_send "${OC_UNIT} is NOT running ($(systemctl is-active "$OC_UNIT" 2>/dev/null)) — autonomous store-building on A2A tasks will not dispatch"
    : > "$OC_MARK"
  fi
else
  rm -f "$OC_MARK" 2>/dev/null || true
fi
