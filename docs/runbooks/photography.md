# Runbook — store photography

Generated stores carry real photographs: a hero image, a shot per product, and a
lifestyle band. Implementation and the reasoning behind each guarantee are in
[`app/imagery.py`](../../app/imagery.py); this is the operational side.

## Enabling it

One environment variable on the VPS, in `/opt/tilla/.env`:

```
TILLA_IMAGE_KEY=<pexels api key>
```

Get the key at <https://www.pexels.com/api/> — free, issued instantly to any Pexels
account, no card. Then `systemctl restart tilla-api` and poll `/ready` before
smoke-testing (**do not** use a fixed `sleep`; startup has taken ~40s).

The key is read at call time, so adding it needs no code change. Quota on the free
tier is **200 requests/hour and 20,000/month**; a store spends at most
`imagery.MAX_SEARCHES` (6) searches, so the hourly ceiling is roughly 33 stores.

## Disabling it

Remove or blank `TILLA_IMAGE_KEY` and restart. Photography is additive and fails
open: stores are created exactly as they were before it existed, rendering on their
seeded generative texture. Nothing about checkout, settlement or fund flow is
touched either way — a create-store during an image-provider outage still produces a
live store and still settles.

Stores already created keep the photographs already on disk.

## Where the files live

Inside the store's own directory, content-addressed:

```
/opt/tilla/stores/<slug>/img/<sha256[:16]>.jpg
```

Served two different ways, which is why both paths must keep working:

| Host | Served by |
|---|---|
| `tilla.gudman.xyz/s/<slug>/` | **nginx**, statically, via the existing `location /s/` alias |
| `<slug>.tilla.gudman.xyz` and custom domains | **the app**, `GET /img/{name}` in `app/main.py` |

The wildcard subdomain vhost proxies everything to the app, so the app route is not
redundant — without it every photograph on every subdomain store 404s. The route
resolves the store from the `Host` header and only ever reads that store's own
directory, so one store cannot serve another's asset.

Budgets per store: `MAX_IMAGE_BYTES` 2 MB per photo, `MAX_TOTAL_BYTES` 8 MB overall.
A ten-product catalogue is therefore bounded at 8 MB on disk.

## Checking what a store actually got

```sh
# what was resolved, and how strongly each photo matched
jq '.content.imagery, [.content.products[] | {name, image: .image.path, alt: .image.alt}]' \
  /opt/tilla/stores/<slug>/store.json

# the decision log for one create (relevance refusals included)
journalctl -u tilla-api --since '15 min ago' | grep 'imagery:'
```

`imagery: nothing relevant for …` is **not** an error. It means no candidate photo's
own description contained the nouns the product requires, so the store correctly
showed none rather than something misleading. Expect it for anything with no honest
photograph — templates, ebooks, subscriptions, services.

## Replacing a photograph

Everything needed to audit or swap one is persisted with the store: the photo's page,
the photographer, and their page. To change a product's shot, edit that product's
`image` block in `store.json`, drop the new JPEG into the store's `img/` directory
under a matching name, and re-render. `path` must stay in the
`img/<hex>.jpg` form or the renderer will refuse it (`app/render.py::_safe_image`).

## Licence obligation

Pexels requires the photographer be credited with a link back. Every theme renders a
credit block in its footer, built from the data persisted with each photo. Do not
strip it — it is the condition on which the images may be used.

## What this does and does not guarantee

Guaranteed: provenance (every photo's id, page and photographer are persisted) and
topical relevance to the merchant's own description, enforced by scoring the
provider's description of each candidate and refusing anything below the floor.

Not guaranteed: editorial review of the photographs. Warden screens the generated
copy, and the image search text is included in that same screening call — but the
photographs themselves are provider-curated, not Warden-screened.
