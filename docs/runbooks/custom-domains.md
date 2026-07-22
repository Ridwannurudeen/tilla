# Custom domains — operator runbook (Phase 4)

Tilla builds custom domains **up to the edge of the external gate**: a merchant claims a
hostname, proves ownership via a DNS TXT challenge, and the app serves that store on the
verified host. **The app never touches DNS, TLS, or nginx.** Pointing the hostname at the
VPS and provisioning the vhost + certificate is **USER-GATED operator work**, documented
here.

---

## 1. What the app does on its own (no operator action)

1. **Claim** — `POST /api/merchant/stores/{slug}/custom-domain {domain}` (merchant-session
   gated). The domain is validated as a strict public hostname; a fresh TXT challenge
   token is minted; the claim is stored **UNVERIFIED**. One hostname → one store (a domain
   already held by another store is `409`).
2. **Verify** — the owner publishes the returned TXT record, then calls
   `POST /api/merchant/stores/{slug}/custom-domain/verify`. The app does a **read-only DNS
   TXT lookup** and, on a match, marks the domain verified.
3. **Serve** — once verified, a request whose `Host` header equals the domain is served
   that store (canonical/OG point at the custom domain). **Fail-closed:** an unverified or
   unclaimed domain serves nothing (`404`).

The DNS record the merchant must publish (echoed by the claim + `GET …/custom-domain`):

```
Type:  TXT
Name:  _tilla-challenge.<domain>
Value: tilla-domain-verification=<token>
```

---

## 2. USER-GATED operator step (the external gate)

The app resolves a store by `Host` **only if a vhost routes that host's requests to the
Tilla app**. That vhost + its TLS certificate is the operator's job on the VPS. Do this
**per verified domain** (nothing here is automated by the app):

1. **Confirm the merchant pointed the domain at the VPS** — an `A`/`AAAA` (or `CNAME` to
   `tilla.gudman.xyz`) record for `<domain>` resolving to `75.119.153.252`. And confirm
   the app already reports the domain **verified** (`GET /api/merchant/stores/{slug}/custom-domain`
   → `"verified": true`). Do not provision a vhost for an unverified domain.
2. **Add an nginx server block** for the domain that proxies to the app (mirrors the main
   Tilla vhost's proxy, `server_name` swapped). A tilla-only file — never edit another
   project's vhost:

   ```nginx
   server {
       listen 443 ssl;
       server_name shop.example.com;                 # the verified custom domain

       # TLS via certbot (step 3) fills in ssl_certificate / ssl_certificate_key.

       location / {
           proxy_pass http://127.0.0.1:8040;
           proxy_set_header Host $host;              # REQUIRED — the app resolves the
                                                     # store from this Host header
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

   `proxy_set_header Host $host` is **mandatory** — the app keys store resolution off the
   original Host. The `location /` block covers the store page **and** its
   `/og.png` / `/og.svg` card (both served by the app for the matched host).

3. **Provision TLS** with certbot (per domain — TLS is out-of-band, the app never issues
   certs):

   ```bash
   certbot --nginx -d shop.example.com
   nginx -t && systemctl reload nginx     # reload, never restart, on the shared box
   ```

   A wildcard (`*.example.com`, DNS-01) is an option when a merchant maps many subdomains,
   but the default is one cert per claimed domain.

4. **Smoke:** `curl -fsS https://shop.example.com/` returns the store page with
   `<link rel="canonical" href="https://shop.example.com/">`.

**Rollback:** `rm` the tilla-only server block, `nginx -t && systemctl reload nginx`, and
(app side) `DELETE /api/merchant/stores/{slug}/custom-domain`.

---

## 3. Security notes

- **Hostname validation** (`app/domains.normalize_domain`): lowercased, trailing-dot
  stripped, ≤253 chars; rejects schemes/ports/paths/whitespace, IP literals, `localhost`,
  single-label names, malformed DNS labels, and the platform's own host (so it can never
  be re-pointed to a merchant store). The domain is merchant input — it is stored only
  after validation and always rendered through the autoescaped Jinja env (never
  `innerHTML`; the dashboard writes it via `textContent`).
- **No SSRF via the verify lookup:** verification is a **DNS TXT query only** — it never
  opens a connection to the claimed host or any merchant-controlled host/port, so there is
  no request-forgery surface. IP literals / localhost are rejected before any lookup.
- **Fail-closed serving:** host resolution matches on
  `custom_domain_verified_at IS NOT NULL AND status='live'`; an unverified domain, a
  released domain, or a non-live store serves nothing.
- **No hijack:** `UNIQUE(custom_domain)` (index `uq_stores_custom_domain`) enforces one
  hostname → one store; a claim on a domain another store already holds is `409`.
- **Non-custodial / no attribution changes:** this feature moves no funds and touches no
  payment path.
