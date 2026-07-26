"""Store imagery: real photographs, chosen for accuracy, fetched once, self-hosted.

Before this module a generated store had no photography at all. Its only visuals
were the two seeded ``<canvas>`` textures in ``themes/*.html`` — deterministic
tinted squares. A gym store rendered a headline, a blurb, a price, and abstract
blocks. Whatever the merchant described, the page looked the same kind of empty.

The hard part is NOT fetching a picture, it is fetching the RIGHT picture. A
plausible-but-wrong photo is worse than none: a photo of a yoga mat on a store
selling dumbbells is a false product claim, made by us, on the merchant's page.
So selection is fail-closed in the same way settlement detection is — an image is
only used when it can be shown to match, and when nothing matches the store keeps
its generative texture and says nothing.

Three things make a match defensible:

1. The QUERY is per product and written by the model (:mod:`app.engine` asks for
   it), because "black athletic leggings on a woman in a gym" finds the product
   and "gym" finds a mood board.
2. The SUBJECT terms are declared separately by the model and checked against the
   provider's own description of each candidate photo (Pexels returns ``alt``).
   The FIRST term is the product's own noun and is REQUIRED, not counted — a photo
   of "black fabric with intricate folds" matches two of "leggings fabric fold"
   while containing no leggings, which is exactly the failure this prevents.
   Coverage of the remaining terms is the score; :data:`MIN_SUBJECT_HITS` the floor.
3. RANKING is coverage first, then how early the product is named in the caption,
   because containing the product and being a photograph OF it are not the same
   thing. Remaining ties break on the store's own seed, so one slug always resolves
   to one photo and a re-render never silently changes a live store's look.

Everything here fails OPEN with respect to store creation and CLOSED with respect
to accuracy. ``create-store`` is x402-paid: an imagery failure (no key, provider
outage, timeout, oversized body, nothing relevant) must never fail the store or
change fund flow. It returns empty imagery and the store renders as it did before
this module existed.

Photographs are DOWNLOADED into the store's own directory rather than hotlinked.
Self-hosting is what the Pexels licence permits and what the architecture wants:
nginx already serves ``STORES_DIR`` at ``/s/`` (see :mod:`app.config`), so the
bytes cost nothing per request, need no ``img-src`` CSP change, leak no merchant's
buyers to a third party, and keep working if the provider ever goes away.

Attribution is not optional. The Pexels API terms require crediting the
photographer and linking back, so every :class:`StoreImage` carries the
photographer, their page, and the photo's page, and the themes render them.

Honest limitation, stated once: these photographs are provider-curated, NOT
Warden-screened. Warden screens the generated copy. What this module guarantees
about a photo is provenance (every id/url/photographer is persisted with the
store, so any image can be audited or swapped) and topical relevance to the
merchant's own description — not editorial review.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence

import requests
from pydantic import BaseModel, Field

logger = logging.getLogger("tilla")

# ---------------------------------------------------------------- provider wiring
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
# Absent key = imagery disabled, silently and safely. Read at call time (not import)
# so a key added to the VPS .env takes effect on restart without a code change, and
# so tests can set it per-case.
KEY_ENV = "TILLA_IMAGE_KEY"

# Which pre-sized variant to store per slot. Pexels serves fixed renditions and the
# app has no Pillow, so the right size is CHOSEN, never resampled locally:
#   landscape 1200x627 — hero and lifestyle bands, wide crops
#   large      940x650 — product cards, the largest a card ever displays
_VARIANT = {"hero": "landscape", "lifestyle": "landscape", "product": "large"}
# Nominal rendition geometry, used for width/height attributes so the browser
# reserves the right box and the page never shifts as photos load.
_GEOMETRY = {"landscape": (1200, 627), "large": (940, 650)}

# ------------------------------------------------------------------------ budgets
# A create-store is a paid request on a shared box; imagery gets a hard ceiling on
# every axis rather than a best-effort promise.
SEARCH_TIMEOUT = (5, 15)  # (connect, read) seconds — provider JSON
FETCH_TIMEOUT = (5, 20)  # (connect, read) seconds — image bytes
MAX_SEARCHES = 6  # provider calls per store (200/hr quota => ~33 stores/hr)
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # one photo
MAX_TOTAL_BYTES = 8 * 1024 * 1024  # all photos for one store
CANDIDATES_PER_SEARCH = 24  # how many to score before picking (max 80)
MAX_LIFESTYLE = 3
MAX_ALT_LEN = 200  # provider-supplied text, capped before it reaches a template

# ------------------------------------------------------------------- relevance bar
# A candidate must contain at least this many of the model's declared subject terms
# in the provider's own description of the photo. One is deliberate, not lax: the
# terms are already specific nouns ("leggings", "dumbbell", "espresso"), stock
# descriptions are short, and demanding two would reject correct photos far more
# often than it would catch wrong ones. Zero would accept anything the search
# returned, which is the failure mode this module exists to prevent.
MIN_SUBJECT_HITS = 1
# Terms this short are matched whole-word only, so "tea" cannot hit "steam".
_SHORT_TERM = 4
# Subject words carrying no visual information — they match everything and would
# let a candidate clear the floor on nothing.
_STOPWORDS = frozenset(
    """a an and the of for with without in on at to from by is are be this that these
    those it its our your my their his her new best top premium quality product
    products item items thing things set kit pack bundle style styles design designs
    made make custom original classic modern
    """.split()
)


# The on-disk name of a store photograph, as _Provider.fetch writes it: a digest of
# the bytes. Anchored and character-exact. This is the ONE shape allowed to reach an
# <img src>, a machine feed, or the image route, and app.render / app.main both match
# against it — kept here so those three can never drift apart.
IMAGE_PATH = re.compile(r"img/[0-9a-f]{8,64}\.jpg")


def product_image_url(product: Mapping, store_url: str) -> str | None:
    """The absolute URL of a product's photograph, or None.

    For the machine surfaces — ``feed.json``, the OpenAI product feed, Google
    Merchant ``g:image_link``. A buying agent should see the same photograph a person
    does; a catalog entry with no picture is worth strictly less to an agent deciding
    what to buy. Re-validates the path rather than trusting persisted content, on the
    same reasoning as :func:`app.render._safe_image`.
    """
    if not isinstance(product, Mapping):
        return None
    image = product.get("image")
    if not isinstance(image, Mapping):
        return None
    path = image.get("path")
    if not isinstance(path, str) or not IMAGE_PATH.fullmatch(path):
        return None
    return f"{store_url.rstrip('/')}/{path}"


class StoreImage(BaseModel):
    """One resolved photograph, as persisted with the store and read by a theme.

    ``path`` is always a repo-relative ``img/<hex>.jpg`` built by THIS module from a
    digest of the bytes — never a provider string and never model output, so nothing
    attacker-influenced can reach an ``<img src>`` or escape the store directory."""

    path: str
    width: int
    height: int
    alt: str = ""
    credit: str = ""  # photographer name — licence requires the credit
    credit_url: str = ""  # photographer's page
    source_url: str = ""  # the photo's own page
    subject_hits: int = 0  # how many declared subject terms the photo matched


class StoreImagery(BaseModel):
    """A store's whole photographic set. SERVER-OWNED: :mod:`app.engine` overwrites
    this wholesale after generation, exactly as ``resolve_design`` overwrites
    ``design_dna``. The model never supplies a member of this structure — it only
    supplies search text — which is what keeps a model-authored path out of an
    ``<img src>``.

    ``products`` is POSITIONAL, aligned one-to-one with
    ``GeneratedContent.products``, with ``None`` wherever nothing verifiably depicted
    that item. It is deliberately not an index MAP: :func:`apply` merges each entry
    into the product's own dict, because ``engine.resync_catalog`` rebuilds
    ``content['products']`` from the live Product rows after a dashboard edit — so a
    stored index would survive a deletion and then point a photograph at the wrong
    product, which is precisely the false claim this module exists to prevent.
    """

    hero: StoreImage | None = None
    lifestyle: list[StoreImage] = Field(default_factory=list)
    products: list[StoreImage | None] = Field(default_factory=list)


def apply(content: dict, resolved: StoreImagery) -> None:
    """Write `resolved` into `content` in the shape the themes and the catalog
    resync both read: each product's own photograph under its ``image`` key, and the
    store-level hero and lifestyle bands under ``content['imagery']``.

    Any pre-existing ``image`` key is deleted first, unconditionally. ``image`` is
    not a declared field on ``ProductContent`` — pydantic drops it at validation —
    but clearing it here means the invariant holds no matter how the content dict
    reached this function, so no path the server did not compute can ever appear in
    an ``<img src>``.
    """
    products = content.get("products") or []
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            continue
        product.pop("image", None)
        image = resolved.products[index] if index < len(resolved.products) else None
        if image is not None:
            product["image"] = image.model_dump()
    content["imagery"] = {
        "hero": resolved.hero.model_dump() if resolved.hero is not None else None,
        "lifestyle": [image.model_dump() for image in resolved.lifestyle],
    }


def credits(content: Mapping) -> list[dict]:
    """Every distinct photograph in a store, for the attribution block a theme
    renders. Deduplicated on the photo's own page URL, because the licence asks for
    one credit per photographer per photo, not one per placement."""
    store_level = content.get("imagery") or {}
    candidates: list[Mapping] = []
    hero = store_level.get("hero") if isinstance(store_level, Mapping) else None
    if isinstance(hero, Mapping):
        candidates.append(hero)
    for product in content.get("products") or []:
        if isinstance(product, Mapping) and isinstance(product.get("image"), Mapping):
            candidates.append(product["image"])
    if isinstance(store_level, Mapping):
        # Same cap the renderer applies to the band itself, so the credit list names
        # the photographs the page actually shows and never more.
        for image in (store_level.get("lifestyle") or [])[:MAX_LIFESTYLE]:
            if isinstance(image, Mapping):
                candidates.append(image)
    out: list[dict] = []
    seen: set[str] = set()
    for image in candidates:
        key = str(image.get("source_url") or image.get("path") or "")
        if not key or key in seen or not image.get("credit"):
            continue
        seen.add(key)
        out.append(
            {
                "credit": str(image.get("credit") or ""),
                "credit_url": str(image.get("credit_url") or ""),
                "source_url": str(image.get("source_url") or ""),
            }
        )
    return out


def enabled() -> bool:
    """Whether imagery can run at all. Checked at call time so the key can be added
    to the VPS ``.env`` without a code change."""
    return bool(os.environ.get(KEY_ENV, "").strip())


# ----------------------------------------------------------------------- relevance
def _terms(text: str) -> list[str]:
    """Lowercased alphanumeric words of `text`, minus visual no-ops."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _matches(term: str, haystack: str) -> bool:
    """Whether `term` appears in `haystack`. Long terms match as substrings so
    "legging" catches "leggings"; short ones match whole words only so "tea" cannot
    claim a photo described as "steam"."""
    if len(term) < _SHORT_TERM:
        return re.search(rf"\b{re.escape(term)}\b", haystack) is not None
    return term in haystack


def _score(photo: dict, subject_terms: Sequence[str], require_head: bool = True) -> int:
    """How many distinct subject terms the PROVIDER's description of this photo
    contains — or 0 if it does not contain the HEAD term, whatever else it matched.

    The score comes from the provider's account of what the photo shows, never from
    the query we sent, so a search that returns something off-topic scores zero.

    The head-term rule exists because counting alone was measurably not enough. Asked
    for compression leggings, the model supplied the subject "leggings fabric fold" —
    the product plus two supporting details — and the provider offered "High-quality
    black fabric with intricate folds and texture". That matched `fabric` and `fold`,
    scored two out of three, and contained no leggings whatsoever: a photograph of
    cloth on a store selling leggings. Supporting nouns describe the shot; only the
    head noun names the thing being sold, so it is required rather than counted.

    `require_head` is True for the slots that carry a curated subject list — products
    and the hero. It is False for the lifestyle band, whose "subject" is just its own
    query text, where the leading word is as likely to be `person` or `athlete` as the
    product; requiring it there would reject correct photographs captioned `man` or
    `woman` for no gain.
    """
    terms = list(dict.fromkeys(subject_terms))
    if not terms:
        return 0
    haystack = f"{photo.get('alt', '')} {photo.get('url', '')}".lower()
    if require_head and not _matches(terms[0], haystack):
        return 0
    return sum(1 for t in terms if _matches(t, haystack))


def _primacy(photo: dict, subject_terms: Sequence[str]) -> int:
    """How early the product is named in the provider's description, as a negative
    character offset (higher is better, 0 is best).

    Coverage alone answers "does this photo contain the product". It does not answer
    "is the product what the photo is OF" — and those come apart in practice. Asked
    for leggings, the provider offered "Unrecognizable sportswoman wearing sneakers
    and black leggings": two subject terms matched, but the frame is mostly shoes.
    Stock captions are written subject-first, so the position of the first matched
    term is a usable proxy for what the photograph is actually about, and it costs
    nothing extra — the description is already in hand.

    Used only to rank candidates that tie on coverage; it can never promote a photo
    over one that depicts more of the product, and it can never rescue a photo below
    :data:`MIN_SUBJECT_HITS`.
    """
    haystack = f"{photo.get('alt', '')}".lower()
    offsets = []
    for term in dict.fromkeys(subject_terms):
        if len(term) < _SHORT_TERM:
            found = re.search(rf"\b{re.escape(term)}\b", haystack)
            if found:
                offsets.append(found.start())
        else:
            at = haystack.find(term)
            if at >= 0:
                offsets.append(at)
    return -min(offsets) if offsets else -9999


class _Provider:
    """Thin Pexels client. One instance per store so the search budget, the byte
    budget and the HTTP connection are shared across every slot."""

    def __init__(self, key: str, store_dir: pathlib.Path):
        self.store_dir = store_dir
        self.searches_left = MAX_SEARCHES
        self.bytes_left = MAX_TOTAL_BYTES
        self.session = requests.Session()
        self.session.headers["Authorization"] = key
        # Cache by (query, orientation): repeated or near-duplicate product queries
        # must not each burn a slot of the provider quota.
        self._cache: dict[tuple[str, str], list[dict]] = {}

    def search(self, query: str, orientation: str) -> list[dict]:
        """Candidate photos for `query`, or an empty list on any failure. Never
        raises: every caller is inside the paid create-store path."""
        query = (query or "").strip()
        if not query:
            return []
        cache_key = (query.lower(), orientation)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if self.searches_left <= 0:
            logger.info("imagery: search budget spent, skipping %r", query)
            return []
        self.searches_left -= 1
        try:
            r = self.session.get(
                PEXELS_SEARCH_URL,
                params={
                    "query": query,
                    "orientation": orientation,
                    "per_page": CANDIDATES_PER_SEARCH,
                },
                timeout=SEARCH_TIMEOUT,
            )
            r.raise_for_status()
            photos = r.json().get("photos") or []
        except (requests.RequestException, ValueError) as exc:
            # Includes a bad key (401) and quota exhaustion (429). Imagery is
            # cosmetic; the store must still be created.
            logger.warning("imagery: search failed for %r: %s", query, exc)
            photos = []
        photos = [p for p in photos if isinstance(p, dict)]
        self._cache[cache_key] = photos
        return photos

    def fetch(self, photo: dict, variant: str) -> StoreImage | None:
        """Download one rendition into the store directory and describe it. Returns
        None on any failure, so a dead byte-fetch costs the slot and nothing else."""
        url = (photo.get("src") or {}).get(variant)
        if not isinstance(url, str) or not url.startswith("https://"):
            return None
        if self.bytes_left <= 0:
            logger.info("imagery: byte budget spent, skipping %s", url)
            return None
        try:
            blob = self._download(url)
        except (requests.RequestException, OSError) as exc:
            logger.warning("imagery: fetch failed for %s: %s", url, exc)
            return None
        if blob is None:
            return None
        # Content-addressed name: identical photos reused across slots are stored
        # once, the name can never contain provider-controlled characters, and the
        # bytes on disk always match the name.
        digest = hashlib.sha256(blob).hexdigest()[:16]
        rel = f"img/{digest}.jpg"
        target = self.store_dir / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        except OSError as exc:
            logger.warning("imagery: could not write %s: %s", target, exc)
            return None
        self.bytes_left -= len(blob)
        width, height = _GEOMETRY.get(variant, (1200, 627))
        return StoreImage(
            path=rel,
            width=width,
            height=height,
            alt=str(photo.get("alt") or "")[:MAX_ALT_LEN],
            credit=str(photo.get("photographer") or "")[:120],
            credit_url=str(photo.get("photographer_url") or "")[:300],
            source_url=str(photo.get("url") or "")[:300],
        )

    def _download(self, url: str) -> bytes | None:
        """Stream `url` under a running byte budget. A lying or absent
        Content-Length cannot beat the cap because the budget is enforced per
        chunk, the same discipline the upload route uses. Rejects anything that is
        not actually a JPEG."""
        cap = min(MAX_IMAGE_BYTES, self.bytes_left)
        with self.session.get(url, timeout=FETCH_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
            if ctype not in ("image/jpeg", "image/jpg"):
                logger.warning("imagery: refusing %s content-type %r", url, ctype)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(64 * 1024):
                total += len(chunk)
                if total > cap:
                    logger.warning("imagery: %s exceeded %d bytes", url, cap)
                    return None
                chunks.append(chunk)
        blob = b"".join(chunks)
        # Magic bytes, not the declared type: a JPEG starts FF D8 FF. Trusting the
        # header alone would let a mislabelled body land on disk as .jpg.
        if not blob.startswith(b"\xff\xd8\xff"):
            logger.warning("imagery: %s was not a JPEG", url)
            return None
        return blob


def _pick(
    photos: Iterable[dict],
    subject_terms: Sequence[str],
    used: set[int],
    rand: Callable[[], float],
    require_head: bool = True,
) -> tuple[dict, int] | None:
    """The best defensible candidate, or None when none clears the bar.

    Scored on provider-described subject coverage; anything below
    :data:`MIN_SUBJECT_HITS` is discarded rather than downgraded, which is what
    makes "no photo" the outcome for a store stock photography cannot honestly
    illustrate. Among equal-scoring candidates the choice is drawn from the store's
    own seed, so it is varied across stores and fixed for any one store.

    `used` holds already-taken photo ids so one store does not repeat a photograph
    across its hero, its cards and its lifestyle band.
    """
    ranked: list[tuple[int, int, dict]] = []
    for photo in photos:
        pid = photo.get("id")
        if not isinstance(pid, int) or pid in used:
            continue
        hits = _score(photo, subject_terms, require_head)
        if hits >= MIN_SUBJECT_HITS:
            ranked.append((hits, _primacy(photo, subject_terms), photo))
    if not ranked:
        return None
    # Coverage first, then how early the product is named — so of two photos that
    # both contain the product, the one the photo is actually ABOUT wins.
    best = max((h, p) for h, p, _ in ranked)
    tied = [photo for h, p, photo in ranked if (h, p) == best]
    best = best[0]
    # Deterministic index from the store seed; the provider's own ordering is not
    # used, so two stores with the same query still differ.
    chosen = tied[int(rand() * len(tied)) % len(tied)]
    return chosen, best


def _slot(
    provider: _Provider,
    query: str,
    subject: str,
    kind: str,
    used: set[int],
    rand: Callable[[], float],
) -> StoreImage | None:
    """Resolve one image slot end to end: search, score, pick, download."""
    subject_terms = _terms(subject) or _terms(query)
    if not subject_terms:
        return None
    orientation = "landscape" if kind in ("hero", "lifestyle") else "landscape"
    photos = provider.search(query, orientation)
    # The lifestyle band has no curated subject list — its subject IS its query —
    # so the head-noun requirement does not apply to it.
    picked = _pick(photos, subject_terms, used, rand, require_head=kind != "lifestyle")
    if picked is None:
        logger.info("imagery: nothing relevant for %r (subject=%r)", query, subject)
        return None
    photo, hits = picked
    image = provider.fetch(photo, _VARIANT[kind])
    if image is None:
        return None
    image.subject_hits = hits
    used.add(photo["id"])
    if not image.alt:
        # Provider descriptions are occasionally empty. The declared subject is a
        # truthful description of what was asked for, and an empty alt on a
        # content image is an accessibility defect.
        image.alt = subject[:MAX_ALT_LEN]
    return image


def resolve(
    content: dict,
    store_dir: pathlib.Path,
    rand: Callable[[], float],
) -> StoreImagery:
    """Resolve a store's whole photographic set from its generated content.

    `content` is the validated generation dict; the only fields read are the search
    text the model was asked for (``hero_image_query`` / ``hero_image_subject``,
    ``lifestyle_queries``, and each product's ``image_query`` / ``image_subject``).
    `rand` is the store's seeded PRNG, passed in rather than built here so this
    module stays independent of :mod:`app.engine` (which imports it).

    Returns an empty :class:`StoreImagery` when imagery is disabled, the provider
    is unreachable, or nothing clears the relevance bar. Never raises.
    """
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        return StoreImagery()
    try:
        provider = _Provider(key, store_dir)
        used: set[int] = set()
        imagery = StoreImagery()

        # Products first, deliberately. They carry the accuracy burden — a card is a
        # claim about a thing for sale — so they get first call on the search and
        # byte budgets, and the hero takes what is left.
        products = content.get("products") or []
        for product in products:
            if not isinstance(product, dict):
                imagery.products.append(None)
                continue
            # No fall back to the product NAME as a query. A name is a brand word
            # ("Summit Tee"), not a photographable subject, and searching it returns
            # whatever the provider happens to associate with the word — the exact
            # random-photo failure this module rejects. An empty query means the
            # model judged the item unphotographable, and that judgement is kept.
            imagery.products.append(
                _slot(
                    provider,
                    product.get("image_query") or "",
                    product.get("image_subject") or "",
                    "product",
                    used,
                    rand,
                )
            )

        hero = _slot(
            provider,
            content.get("hero_image_query") or "",
            content.get("hero_image_subject") or "",
            "hero",
            used,
            rand,
        )
        if hero is not None:
            imagery.hero = hero

        for query in (content.get("lifestyle_queries") or [])[:MAX_LIFESTYLE]:
            if not isinstance(query, str):
                continue
            image = _slot(provider, query, query, "lifestyle", used, rand)
            if image is not None:
                imagery.lifestyle.append(image)

        logger.info(
            "imagery: hero=%s products=%d/%d lifestyle=%d searches_used=%d bytes=%d",
            imagery.hero is not None,
            sum(1 for image in imagery.products if image is not None),
            len(products),
            len(imagery.lifestyle),
            MAX_SEARCHES - provider.searches_left,
            MAX_TOTAL_BYTES - provider.bytes_left,
        )
        return imagery
    except Exception:
        # Deliberately total. This runs inside a paid create-store; no imagery bug
        # may ever cost a merchant their store or alter fund flow.
        logger.exception("imagery: resolve failed, continuing without photography")
        return StoreImagery()
