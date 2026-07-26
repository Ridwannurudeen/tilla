#!/bin/bash
# certbot --manual-auth-hook for the *.tilla.gudman.xyz DNS-01 challenge.
#
# certbot calls this with CERTBOT_VALIDATION set. There is no DNS API configured
# (verified: no registrar credentials on the box, and certbot has no DNS plugin),
# so the record has to be published by hand at the registrar. Rather than have
# certbot fail while that happens, this records the value for the operator and then
# WAITS for the record to actually appear in DNS before returning success.
#
# Polls the authoritative nameservers directly, not a resolver — a cached negative
# answer from a recursive resolver would otherwise keep reporting "not there yet"
# long after the record was added.
set -uo pipefail

NAME="_acme-challenge.tilla.gudman.xyz"
OUT=/root/dns01-challenge.txt
AUTH_NS=dns1.registrar-servers.com
TIMEOUT_SECS=1800   # 30 minutes: enough to log into a registrar unhurried
SLEEP_SECS=15

{
  echo "record_name=$NAME"
  echo "record_type=TXT"
  echo "record_value=$CERTBOT_VALIDATION"
  echo "waiting_since=$(date -u +%FT%TZ)"
} > "$OUT"

echo "[hook] need TXT $NAME = $CERTBOT_VALIDATION"
echo "[hook] polling $AUTH_NS for up to $((TIMEOUT_SECS / 60)) minutes"

deadline=$(( $(date +%s) + TIMEOUT_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  # +short returns the value(s) quoted; strip quotes before comparing
  if dig +short "TXT" "$NAME" "@$AUTH_NS" 2>/dev/null \
       | tr -d '"' | grep -qxF "$CERTBOT_VALIDATION"; then
    echo "[hook] record is live at the authoritative NS"
    echo "status=published" >> "$OUT"
    sleep 5   # small settle margin before Let's Encrypt queries it
    exit 0
  fi
  sleep "$SLEEP_SECS"
done

echo "[hook] TIMED OUT after $((TIMEOUT_SECS / 60)) minutes - record never appeared"
echo "status=timeout" >> "$OUT"
exit 1
