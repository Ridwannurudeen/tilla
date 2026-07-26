# Tilla — Operations runbook (M12)

Live: <https://tilla.gudman.xyz> · systemd unit `tilla-api` (root, uvicorn `127.0.0.1:8040 --proxy-headers`) · code at `/opt/tilla` on the shared Contabo VPS `root@75.119.153.252`. The box hosts other live projects (Warden, groundtruth, solvent, …) — every op here touches **only** tilla-owned paths/units.

Deploy is per-file via `scripts/deploy.sh` (never a directory clobber). `tilla.db`, `stores/`, `deliverables/`, `.env` are **server-owned** and never written by deploy.

---

## 1. Health vs readiness

| Endpoint | Meaning | Cost | Used by |
|---|---|---|---|
| `GET /health` | pure liveness — no DB, no network, byte-identical to pre-M12 | trivial | deploy smoke, watchdog liveness probe |
| `GET /ready` | readiness — DB `SELECT 1`, migration head == `0030_creation_block_floor`, sweeper + RPC heartbeats fresh | one sqlite `SELECT` + in-memory reads, **no network call** | watchdog (every ~60s) |

`/ready` is unauthenticated, unthrottled, and **never raises** — always JSON, `200` when ready else `503` naming the failing component:

```json
{"ready": true, "checks": {"db":"ok","migrations":"0030_creation_block_floor","sweeper":"ok","rpc":"ok"}}
```

- `sweeper`/`rpc` report `disabled` when `TILLA_SWEEP_ENABLED=0` (dev/tests). In prod they read heartbeats the sweeper stamps each tick: `sweeper` stale after `READY_SWEEP_STALE_SEC` (default 180s), `rpc` after `READY_RPC_STALE_SEC` (default 300s). No per-probe RPC call — an RPC outage surfaces via the sweeper's piggybacked head read, so the once-a-minute watchdog never burns `eth_blockNumber` quota.
- `migrations` `unknown` (couldn't compute the expected head) is non-fatal.
- Right after a restart `/ready` may briefly `503` (`sweeper: stale`) until the first tick — expected; the deploy smoke polls up to ~30s.

---

## 2. Self-healing coverage (what the box detects and fixes on its own)

| Failure | Detection → action | Alert |
|---|---|---|
| Process crash-exit | systemd `Restart=always RestartSec=3` (drop-in) → ~3s | — |
| Wedged/hung process, dead port 8040 | watchdog `/health` fail → `systemctl restart tilla-api` (≤3/hr) → re-probe | Telegram (down→up / restart-failed) |
| Restart budget exhausted | watchdog: **no restart** (never storm the shared box) | Telegram once/hr |
| Degraded readiness (DB, migration drift, sweeper dead, RPC stale) | watchdog `/ready` 503 → **no restart** | Telegram, throttled once/hr per state |
| Nightly backup failure | `backup_db.sh` `trap ERR` | Telegram |
| Data | 7 on-disk DB snapshots + deliverables mirror (+ off-VPS copy once configured) | — |

Restarts are reserved for dead/wedged liveness; a stale RPC/readiness issue is **not** fixed by a bounce, so `/ready` 503 alerts but never restarts.

### Honest residuals (NOT covered — no HA is claimed)
1. **The single VPS is THE SPOF.** A host/disk/network outage takes the site, the watchdog, and Telegram egress down together, so **no alert fires**. The only real fix is an **external** probe — e.g. UptimeRobot free tier on `https://tilla.gudman.xyz/health` with Telegram/email notify. **USER-GATED** (account creation).
2. **nginx-down-but-uvicorn-up** is invisible to the watchdog (it probes `127.0.0.1:8040` directly). The external probe above covers it too.
3. **Off-VPS backup RPO is 24h** (nightly).
4. External dependencies stay SPOFs with defined degraded modes, not guarantees: **Anthropic** (create/upgrade-store → 503 + `Retry-After`; serving/checkout unaffected), **X Layer RPC** (payment detection stalls, `/ready` flags it, orders confirm on recovery — funds are on-chain, nothing is lost), **OKX facilitator** (agent buys fail closed; web checkout unaffected).

---

## 3. Watchdog

Files: `scripts/watchdog.sh` (shipped to `/opt/tilla/scripts/`), `deploy/tilla-watchdog.service` (oneshot), `deploy/tilla-watchdog.timer` (`OnBootSec=2min`, `OnUnitActiveSec=60s`). The script's **only** `systemctl` target is the literal `tilla-api` — no globs, never nginx, never another project.

Telegram reuses the SOLVENT bot creds at `/etc/solvent/telegram-alerts` (the send function tolerates a few common var names; if the file or vars are absent, alerts are skipped and the restart logic still runs — everything lands in journald). State: `/opt/tilla/.watchdog.state` (restart epochs, last hour) + `.watchdog.ratelimited` / `.watchdog.ready` throttle markers.

Install (orchestrator, VPS):
```bash
cp deploy/tilla-watchdog.service /etc/systemd/system/
cp deploy/tilla-watchdog.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl start tilla-watchdog.service     # run once, read journal: probes pass, no restart, no alert
journalctl -u tilla-watchdog.service -n 30 --no-pager
systemctl enable --now tilla-watchdog.timer
```
Rollback: `systemctl disable --now tilla-watchdog.timer; rm /etc/systemd/system/tilla-watchdog.{service,timer}; systemctl daemon-reload`.

---

## 4. systemd hardening drop-in

`deploy/tilla-api-override.conf` → `/etc/systemd/system/tilla-api.service.d/override.conf`. A **drop-in** — the base unit and `User=root` are untouched. Adds `Restart=always`/`RestartSec=3` plus a sandbox set chosen so **root file access is unaffected**: `ProtectSystem=full` makes only `/usr /boot /efi /etc` read-only, so `/opt` (DB+WAL, stores/, deliverables/, backups/, the sweeper) stays writable; `/opt/tilla/.env` loads via `EnvironmentFile` at unit start; `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` keeps TCP egress (RPC/facilitator/Anthropic/Telegram).

Apply:
```bash
mkdir -p /etc/systemd/system/tilla-api.service.d
cp deploy/tilla-api-override.conf /etc/systemd/system/tilla-api.service.d/override.conf
systemctl daemon-reload && systemctl restart tilla-api
# smoke: /health, /ready, one live store page, unpaid POST /create-store == 402
```
Rollback: `rm -r /etc/systemd/system/tilla-api.service.d && systemctl daemon-reload && systemctl restart tilla-api`.

**Deliberately excluded** (higher mid-session break risk for marginal gain): `ProtectSystem=strict`+`ReadWritePaths`, `MemoryDenyWriteExecute` (native wheels/cffi may need W+X), `CapabilityBoundingSet` trimming, and any `User=` change. The full unprivileged-user migration is the follow-up in §9.

---

## 5. Backups

Same-disk **keep-7** is the operating default.

- `scripts/backup_db.sh` (cron `15 4 * * *` → `/opt/tilla/backups/backup.log` — path corrected
  2026-07-26; this doc said `/var/log/tilla-backup.log`, which does not exist. Note
  `/etc/logrotate.d/tilla` still rotates the non-existent path, so the real log is unrotated): WAL-safe `.backup` + `PRAGMA integrity_check` + keep-7, then `rsync -a` of `deliverables/` into `backups/deliverables/` (accumulating mirror, **no `--delete`** — deliverables are sha256-named + immutable, so a deleted file lingering in backup is acceptable). On any error a Telegram alert fires (`trap ERR`).
- **`stores/` is NOT backed up** — `index.html`/`store.json` are regenerated from the DB by `rerender_stores()` on every restart, so they are derived, not source.
- The API logs to **journald** (no file, no rotation). journald's default 10%-of-disk cap applies box-wide (shared config not touched). Manual trim if ever needed: `journalctl --vacuum-time=30d`.

### Off-VPS copy (USER-GATED)
`scripts/backup_offsite.sh` (cron `35 4 * * *` → `/var/log/tilla-offsite.log`) reads `/etc/tilla-backup.env` (root:600, **separate** from `/opt/tilla/.env` so the app never loads backup creds):
```
TILLA_BACKUP_REMOTE="user@host:/path/to/tilla-backups"   # required to activate
TILLA_BACKUP_SSH_KEY="/root/.ssh/tilla_backup"           # optional
```
Unset → logs `offsite not configured` and exits 0 (safe no-op). Set → `rsync -az --partial` ships the 7 DB snapshots + the deliverables mirror. **The destination (a second box, a home pull, or an rclone remote) is user-provided** — until then same-disk keep-7 is the honest default and submission claims say so.

---

## 6. Log rotation

`deploy/logrotate-tilla` → `/etc/logrotate.d/tilla` (a new tilla-only file — cannot affect other projects). Covers exactly `/var/log/tilla-backup.log` and `/var/log/tilla-offsite.log`: weekly, rotate 8, compress, delaycompress, missingok, notifempty, copytruncate. Validate: `logrotate -d /etc/logrotate.d/tilla`. Rollback: `rm /etc/logrotate.d/tilla`.

---

## 7. nginx rate limiting (conf.d-gated, may be DEFERRED)

`deploy/nginx-m12.snippet`. **Pre-flight gate:** `nginx -T | grep 'include /etc/nginx/conf.d/\*.conf'`. If absent → **DEFER** (editing the shared `nginx.conf` isn't worth it on a many-project box; slowapi already enforces per-route limits) and record the deferral here. If present: add `/etc/nginx/conf.d/tilla-limit-req.conf` (zone defs only — `tilla_api` 120r/m, `tilla_create` 12r/m) and `limit_req` lines on the tilla vhost only, `nginx -t`, `systemctl reload nginx` (never restart). Rollback: restore the vhost backup, `rm` the conf.d file, `nginx -t`, reload.

---

## 8. Restore runbook + drill

### Restore drill (read-only, safe anytime)
`scripts/restore_drill.sh <backup.db> [deliverables-dir]` restores into a **mktemp dir (never `/opt/tilla` live paths)**, runs `PRAGMA integrity_check`, asserts the migration head is `0030_creation_block_floor`, prints stores/orders/deliverables counts, and re-hashes up to 20 deliverables against their DB `file_sha256`. PASS/FAIL exit code.
```bash
/opt/tilla/scripts/restore_drill.sh /opt/tilla/backups/tilla-$(date +%F).db /opt/tilla/backups/deliverables
```

### Full restore (real recovery)
```bash
systemctl stop tilla-api
cp /opt/tilla/backups/tilla-<date>.db /opt/tilla/tilla.db     # restore DB
rsync -a /opt/tilla/backups/deliverables/ /opt/tilla/deliverables/   # restore files
cd /opt/tilla && .venv/bin/alembic current                    # confirm head == 0030_creation_block_floor
systemctl start tilla-api
# stores/ rebuilds itself: rerender_stores() runs at startup from the DB
curl -fsS https://tilla.gudman.xyz/health && curl -fsS https://tilla.gudman.xyz/ready
```

### Drill log
| Date | Source | Result | Notes |
|---|---|---|---|
| _pending_ | on-VPS `backups/tilla-<date>.db` | _run at deploy_ | orchestrator records PASS here |
| _pending_ | **off-VPS** pulled copy | _blocked_ | runs once the user provides `TILLA_BACKUP_REMOTE` (M12 acceptance's off-VPS drill) |

---

## 9. Daily checks & follow-ups

- **LLM spend:** `journalctl -u tilla-api --since today | grep 'llm usage'` — each create/upgrade logs `model=… in=<tok> out=<tok>` (also in `event_log` on `store.created`/`store.upgraded`). Cost at claude-haiku-4-5 pricing.
- **Follow-up — unprivileged user (staged, own runbook):** create a `tilla` user, `chown -R tilla:tilla /opt/tilla`, switch the unit `User=`, tighten to `ProtectSystem=strict` + an exhaustive `ReadWritePaths` (DB+WAL, stores/, deliverables/, backups/). Deferred here because a mid-session uid change on a shared box is risky.
- **Follow-up — external uptime probe** (§2 residual #1) and **off-VPS backup destination** (§5) are the two USER-GATED items that close the remaining coverage gaps.
