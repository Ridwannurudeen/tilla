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
4. The chosen photograph is LOOKED AT before it is kept (:func:`_verify`), because
   every check above reads the caption and a caption cannot report what it does not
   mention. Candidates are walked in rank order until one survives inspection, so a
   rejection hands the card to the runner-up instead of emptying it.

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

import base64
import hashlib
import json
import logging
import os
import pathlib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

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

# ------------------------------------------------------------- visual verification
# Caption scoring answers "does the provider SAY this is the product". It cannot
# answer "what is actually in the frame", and the gap between those is where the
# damaging failures live: a store selling handcrafted maple watches was given a
# photograph of a Casio watch beside a Canon camera and a pair of AirPods — three
# competitors' trademarks on a card offering the merchant's own product for sale —
# and not one of those brand names appears anywhere in the caption. No amount of
# text matching can catch that, so the chosen photograph is LOOKED AT before it is
# kept. This is also why a stronger text model would not have helped: the model
# never sees the candidates, and the query that produced the Casio photo was
# perfectly good.
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
VERIFY_KEY_ENV = "TILLA_LLM_KEY"  # the same key generation uses, read independently
VERIFY_MODEL_ENV = "TILLA_LLM_MODEL"
VERIFY_MODEL_DEFAULT = "claude-haiku-4-5"
VERIFY_TIMEOUT = (5, 30)
# How far down the ranked candidates to walk when one is rejected. Rejecting and
# stopping would empty the card; rejecting and trying the runner-up keeps the page
# rich while still refusing the bad photograph. Each step costs one vision call
# (~815 tokens for a 940x650 image, so a fraction of a cent).
MAX_VERIFY_PER_SLOT = 3
# How many deterministic seeds the generation fallback may try when a rendered image
# fails the branding check. One seed proved brittle in production (see _slot).
MAX_GENERATE_ATTEMPTS = 3

# ---------------------------------------------------------------- generated art
# Stock photography has a hard ceiling on branded goods: most photographs of a hot
# sauce or a wristwatch ARE somebody's branded product, so the branding check
# correctly refuses them and those stores go without a hero. Generation fills that
# specific hole — and ONLY that hole.
#
# Deliberately restricted to the hero and the lifestyle band. A product CARD sits
# above a Buy button and asserts "this is the item you will receive"; a generated
# photorealistic bottle with an invented label would be a fabricated product image,
# which is a worse failure than the one this module spent its existence removing. A
# hero asserts nothing about the goods, so a generated one is atmosphere, not a claim.
#
# Keyless by design: no account, no card, no key to rotate, and no signup that a
# challenged carrier IP can block. Seeded from the store's own PRNG so a slug always
# yields the same image, exactly like its palette and layout.
GENERATE_URL = "https://image.pollinations.ai/prompt/"
GENERATE_ENV = "TILLA_IMAGE_GEN"  # unset/0 => generation off, stock only
GENERATE_TIMEOUT = (5, 90)  # generation is slow; the caller is already fail-open
# Appended to every prompt. "no text" matters: generators love to invent signage and
# labels, and invented label text on a storefront is exactly what we are avoiding.
#
# The anti-branding half was widened after measuring it. Asked for a wooden watch on a
# workbench, the short prompt produced something the branding check REFUSED; the same
# scene with "plain unbranded / no emblem / no crest" passed. That single change is the
# difference between a store having a hero and not having one. It does NOT rescue
# everything: a football-kit store fails on both prompts, because a jersey without a
# crest stops reading as a jersey — that category is a genuine ceiling, not a bug.
_GENERATE_STYLE = (
    ", photographic, natural light, plain unbranded subject, no text, no words, "
    "no logos, no brand marks, no emblem, no crest, no sponsor, no watermark"
)


def generation_enabled() -> bool:
    """Whether generated imagery may fill an empty hero or lifestyle slot. Read at
    call time so it can be switched on the VPS without a code change; default OFF so
    the paid create path gains no external dependency unless it is asked for."""
    return os.environ.get(GENERATE_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _generate(prompt: str, seed: int, width: int, height: int) -> bytes | None:
    """One generated image, or None. Fail-open: any failure simply leaves the slot
    empty and the store renders on its generative texture, as it did before."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    try:
        r = requests.get(
            GENERATE_URL + quote(prompt + _GENERATE_STYLE, safe=""),
            params={
                "width": width,
                "height": height,
                "nologo": "true",
                "seed": seed,
            },
            timeout=GENERATE_TIMEOUT,
        )
        r.raise_for_status()
        blob = r.content
    except requests.RequestException as exc:
        logger.warning("imagery: generation failed for %r: %s", prompt[:60], exc)
        return None
    if len(blob) > MAX_IMAGE_BYTES or not blob.startswith(b"\xff\xd8\xff"):
        logger.warning(
            "imagery: generated body was not a usable JPEG (%d bytes)", len(blob)
        )
        return None
    return blob


_VERIFY_PROMPT = (
    "This photograph is a candidate for the product card of an online store "
    'selling: "{description}".\n\n'
    'Answer ONLY with JSON: {{"depicts": true|false, "branded": true|false}}\n\n'
    "depicts — true only if the MAIN SUBJECT of the photograph is that kind of "
    "product. False if the product is incidental, in the background, or absent, or "
    "if the frame is dominated by other objects.\n"
    "branded — true if ANY third-party brand name, logo, trademark or recognisable "
    "branded product is visible ANYWHERE in the image, including on labels, "
    "packaging, clothing, devices or in the background.\n\n"
    "A product card showing another company's product misrepresents what is for "
    "sale. Be strict: if you are unsure whether something is branded, answer "
    '"branded": true.'
)

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
    # True when this is generated art rather than a photograph. Persisted so the
    # themes can label it: unlabelled synthetic imagery on a commerce page is the
    # thing this module exists to avoid, whatever it cost to make.
    generated: bool = False
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


def _verify(blob: bytes, description: str, require_depicts: bool = True) -> bool:
    """Whether this photograph may be used for `description`.

    Two different questions, because the slots make two different claims. A product
    CARD asserts "this is the thing you are buying", so the photograph must actually
    depict it AND carry no third-party branding. A hero or lifestyle band asserts
    nothing about the goods — it is the world around them, a gym floor or a lit
    workshop — so demanding the product be its main subject is the wrong test and
    merely throws away good photographs. `require_depicts=False` keeps the branding
    rule, which applies everywhere, and drops the subject rule, which does not.

    FAIL-CLOSED on every uncertainty — an API error, a timeout, a malformed answer
    or a missing key all return False, so the photograph is dropped and the store
    falls back to its generative plate. That is the same trade the rest of this
    module makes, and it costs nothing extra in availability terms: store creation
    already depends on this API for generation, so a store that could not verify a
    photograph is a store that was never generated either.
    """
    key = os.environ.get(VERIFY_KEY_ENV, "").strip()
    if not key or not blob:
        logger.warning("imagery: cannot verify (no %s), refusing photo", VERIFY_KEY_ENV)
        return False
    payload = {
        "model": os.environ.get(VERIFY_MODEL_ENV) or VERIFY_MODEL_DEFAULT,
        "max_tokens": 100,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.standard_b64encode(blob).decode(),
                        },
                    },
                    {
                        "type": "text",
                        "text": _VERIFY_PROMPT.format(description=description),
                    },
                ],
            }
        ],
    }
    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=VERIFY_TIMEOUT,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            logger.warning("imagery: verifier returned no JSON: %s", text[:120])
            return False
        answer = json.loads(match.group(0))
    except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
        logger.warning("imagery: verification failed, refusing photo: %s", exc)
        return False
    ok = answer.get("branded") is False and (
        answer.get("depicts") is True or not require_depicts
    )
    if not ok:
        logger.info(
            "imagery: rejected on sight (depicts=%s branded=%s require_depicts=%s) %r",
            answer.get("depicts"),
            answer.get("branded"),
            require_depicts,
            description[:60],
        )
    return ok


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

    def fetch_bytes(self, photo: dict, variant: str) -> bytes | None:
        """Download one rendition WITHOUT writing it. Separated from :meth:`store` so
        a candidate can be looked at before anything lands in the store directory —
        a rejected photograph then leaves no orphan file behind."""
        url = (photo.get("src") or {}).get(variant)
        if not isinstance(url, str) or not url.startswith("https://"):
            return None
        if self.bytes_left <= 0:
            logger.info("imagery: byte budget spent, skipping %s", url)
            return None
        try:
            return self._download(url)
        except (requests.RequestException, OSError) as exc:
            logger.warning("imagery: fetch failed for %s: %s", url, exc)
            return None

    def store(self, blob: bytes, photo: dict, variant: str) -> StoreImage | None:
        """Write a verified photograph into the store directory and describe it."""
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


def _rank(
    photos: Iterable[dict],
    subject_terms: Sequence[str],
    used: set[int],
    rand: Callable[[], float],
    require_head: bool = True,
) -> list[tuple[dict, int]]:
    """Every defensible candidate, best first, as (photo, hits).

    Scored on provider-described subject coverage; anything below
    :data:`MIN_SUBJECT_HITS` is discarded rather than downgraded, which is what
    makes "no photo" the outcome for a store stock photography cannot honestly
    illustrate. Ranking is coverage, then how early the product is named, then a
    draw from the store's own seed — so the choice is varied across stores and
    fixed for any one store.

    A LIST rather than a single winner because the caller looks at each candidate
    before keeping it: when the best one turns out to show a competitor's product,
    the runner-up should get the card, not nothing.

    `used` holds already-taken photo ids so one store does not repeat a photograph
    across its hero, its cards and its lifestyle band.
    """
    scored: list[tuple[int, int, float, dict]] = []
    for photo in photos:
        pid = photo.get("id")
        if not isinstance(pid, int) or pid in used:
            continue
        hits = _score(photo, subject_terms, require_head)
        if hits >= MIN_SUBJECT_HITS:
            # The seed draw is folded in as a per-candidate sort key so it breaks
            # ties exactly as the single-winner version did, while still yielding a
            # full ordering to walk.
            scored.append((hits, _primacy(photo, subject_terms), rand(), photo))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [(photo, hits) for hits, _, _, photo in scored]


def _slot(
    provider: _Provider,
    query: str,
    subject: str,
    kind: str,
    used: set[int],
    rand: Callable[[], float],
) -> StoreImage | None:
    """Resolve one image slot end to end: search, rank, then look at each candidate
    in turn and keep the first that survives being looked at.

    Walking the ranking rather than taking only the winner is what keeps the page
    rich: the best-captioned candidate is often the one showing a competitor's
    product, and rejecting it should hand the card to the runner-up, not empty it.
    """
    subject_terms = _terms(subject) or _terms(query)
    if not subject_terms:
        return None
    orientation = "landscape" if kind in ("hero", "lifestyle") else "landscape"
    photos = provider.search(query, orientation)
    # The lifestyle band has no curated subject list — its subject IS its query —
    # so the head-noun requirement does not apply to it.
    ranked = _rank(photos, subject_terms, used, rand, require_head=kind != "lifestyle")
    if not ranked:
        # No early return: an empty ranking is the MAIN case generation exists for —
        # stock having nothing usable is exactly when a hero needs filling. Falling
        # through to the generation block below is the point.
        logger.info("imagery: nothing relevant for %r (subject=%r)", query, subject)
    for photo, hits in ranked[:MAX_VERIFY_PER_SLOT]:
        blob = provider.fetch_bytes(photo, _VARIANT[kind])
        if blob is None:
            continue
        # Only a product card claims "this is what you are buying"; the hero and the
        # lifestyle band are the world around the goods, so they are held to the
        # branding rule alone.
        if not _verify(blob, subject or query, require_depicts=kind == "product"):
            continue
        image = provider.store(blob, photo, _VARIANT[kind])
        if image is None:
            continue
        image.subject_hits = hits
        used.add(photo["id"])
        if not image.alt:
            # Provider descriptions are occasionally empty. The declared subject is
            # a truthful description of what was asked for, and an empty alt on a
            # content image is an accessibility defect.
            image.alt = subject[:MAX_ALT_LEN]
        return image
    # Stock had nothing usable. For a hero or a lifestyle frame — never a product
    # card — fall back to generated atmosphere, held to the same branding check.
    #
    # SEVERAL seeds, not one, and the reason is a real store: iron-built's gym hero
    # verified fine at an arbitrary seed, yet the backfill failed it — the ONE seed
    # its PRNG produced happened to render branded apparel, the check refused it,
    # and because the seed is deterministic the store could never recover on any
    # re-run. Walking a few successive draws keeps determinism (same slug, same
    # sequence, same final image) while removing the single-unlucky-seed failure —
    # the exact courtesy the stock path already extends to its runner-up candidates.
    if kind != "product" and generation_enabled() and provider.bytes_left > 0:
        width, height = _GEOMETRY.get(_VARIANT[kind], (1200, 627))
        for _ in range(MAX_GENERATE_ATTEMPTS):
            blob = _generate(query or subject, int(rand() * 2**31), width, height)
            if blob is None:
                continue
            if not _verify(blob, subject or query, require_depicts=False):
                continue
            image = provider.store(blob, {"alt": subject or query}, _VARIANT[kind])
            if image is not None:
                image.generated = True
                logger.info("imagery: generated a %s for %r", kind, query)
                return image
    logger.info("imagery: no candidate survived inspection for %r", query)
    return None


def _generated_hero(
    provider: _Provider,
    prompt: str,
    rand: Callable[[], float],
) -> StoreImage | None:
    """A hero for a store whose goods cannot honestly be photographed at all.

    Software, templates, memberships and services get an EMPTY hero_image_query by
    design — a stranger's laptop standing in for a Notion template is the exact false
    claim the rest of this module exists to refuse — so :func:`_slot` never runs for
    them and they render on their generative texture alone. That is honest but it is
    also why 8 of the live stores had no imagery whatsoever.

    Generation closes that specific hole, on one condition that makes it honest: the
    prompt is NEVER sent to the stock provider. A searched photograph of a real desk
    is a photograph OF something, and putting it on a software storefront asserts a
    thing that is not true. A generated frame depicts nothing real and asserts
    nothing about the goods — the same standard the photographed stores' heroes are
    already held to (``require_depicts=False``; a gym floor is a fine hero for a
    gym-wear store). It is still held to the BRANDING rule, so invented logos and
    label text are refused exactly as they are everywhere else, and the themes
    disclose it as generated illustration.

    Hero only. A product card is a claim about the item for sale, and no amount of
    atmosphere makes a generated frame a truthful picture of a thing being sold.
    """
    if not prompt or not generation_enabled() or provider.bytes_left <= 0:
        return None
    width, height = _GEOMETRY[_VARIANT["hero"]]
    # Same several-seeds courtesy the photographed path gets: one unlucky
    # deterministic draw must not be a permanent failure for that slug.
    for _ in range(MAX_GENERATE_ATTEMPTS):
        blob = _generate(prompt, int(rand() * 2**31), width, height)
        if blob is None:
            continue
        if not _verify(blob, prompt, require_depicts=False):
            continue
        image = provider.store(blob, {"alt": prompt}, _VARIANT["hero"])
        if image is not None:
            image.generated = True
            logger.info("imagery: generated an atmosphere hero for %r", prompt[:60])
            return image
    logger.info("imagery: no generated hero survived inspection for %r", prompt[:60])
    return None


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
        else:
            # Nothing photographable, or nothing that survived inspection. The model
            # supplies hero_art_prompt only when the goods have no physical form, so
            # for every photographed store this is empty and the branch is a no-op.
            imagery.hero = _generated_hero(
                provider, str(content.get("hero_art_prompt") or ""), rand
            )

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
