"""Store photography (app/imagery.py) and how it reaches a page.

The point of this module is not that a store gets a picture — it is that a store
never gets the WRONG picture. A plausible-but-irrelevant stock photo on a product
card is a false claim about what is for sale, made by us, on the merchant's page.
So most of what follows is about refusal: nothing relevant found, provider down, no
key, a body that is not a JPEG, a budget spent. Every one of those must end with a
store that renders on its generative texture and says nothing.

`requests` is stubbed rather than respx-mocked because this module uses requests
(the suite's respx fixtures cover httpx, which chain.py uses).
"""

import json
import pathlib

import pytest

from app import imagery, render

JPEG = b"\xff\xd8\xff" + b"\x00" * 512
# The published feed contract, pinned the same way tests/test_protocol_goldens.py
# pins it: a photographed store's feed must validate against the schema peers read.
FEED_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "docs" / "openapi.feed.yaml"
)


def photo(pid: int, alt: str, url: str = "") -> dict:
    """A Pexels photo object, exactly the fields the real API returns."""
    return {
        "id": pid,
        "alt": alt,
        "url": url or f"https://www.pexels.com/photo/{pid}/",
        "photographer": f"Photographer {pid}",
        "photographer_url": f"https://www.pexels.com/@p{pid}",
        "src": {
            "large": f"https://images.pexels.com/{pid}-large.jpg",
            "landscape": f"https://images.pexels.com/{pid}-landscape.jpg",
        },
    }


class _Response:
    def __init__(self, payload=None, blob=None, ctype="image/jpeg", status=200):
        self._payload = payload
        self._blob = blob
        self.headers = {"content-type": ctype}
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            import requests

            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self._payload

    def iter_content(self, size):
        for i in range(0, len(self._blob), size):
            yield self._blob[i : i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Stands in for requests.Session: canned search results, canned image bytes."""

    def __init__(self, results=None, blob=JPEG, ctype="image/jpeg", search_status=200):
        self.headers = {}
        self.results = results if results is not None else {}
        self.blob = blob
        self.ctype = ctype
        self.search_status = search_status
        self.searched: list[str] = []
        self.fetched: list[str] = []

    def get(self, url, params=None, timeout=None, stream=False):
        if stream:
            self.fetched.append(url)
            # Distinct bytes per URL unless a test pins `blob` to something specific:
            # files are content-addressed, so identical bytes legitimately collapse to
            # one file and would mask whether two DIFFERENT photos were chosen.
            blob = self.blob
            if blob == JPEG:
                blob = JPEG + url.encode()
            return _Response(blob=blob, ctype=self.ctype)
        query = (params or {}).get("query", "")
        self.searched.append(query)
        return _Response(
            payload={"photos": self.results.get(query, [])}, status=self.search_status
        )


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(imagery.KEY_ENV, "test-image-key")


def install(monkeypatch, session):
    monkeypatch.setattr(imagery.requests, "Session", lambda: session)
    return session


def seeded():
    """A deterministic stand-in for the store's slug-seeded PRNG."""
    return lambda: 0.0


# ----------------------------------------------------------------- the accuracy bar
def test_a_photo_that_does_not_depict_the_product_is_refused(
    monkeypatch, keyed, tmp_path
):
    """The whole module in one test. A search for gym wear returns a yoga mat and a
    smoothie; neither depicts leggings, so the product gets NO image rather than a
    decorative stand-in."""
    session = install(
        monkeypatch,
        FakeSession(
            {
                "black athletic leggings on woman": [
                    photo(1, "Person meditating on a yoga mat at sunrise"),
                    photo(2, "Close up of a green smoothie in a glass"),
                ]
            }
        ),
    )
    content = {
        "products": [
            {
                "name": "Compression Leggings",
                "image_query": "black athletic leggings on woman",
                "image_subject": "leggings woman",
            }
        ]
    }
    result = imagery.resolve(content, tmp_path, seeded())
    assert result.products == [None]
    assert session.fetched == [], "nothing irrelevant may be downloaded"


def test_the_most_relevant_candidate_wins(monkeypatch, keyed, tmp_path):
    """Ranked on how many declared subject nouns the PROVIDER's own description
    contains — so the photo that shows the most of the product wins, regardless of
    the order the provider returned them in."""
    install(
        monkeypatch,
        FakeSession(
            {
                "q": [
                    photo(1, "A dumbbell on a gym floor"),
                    photo(
                        2, "Woman in black leggings squatting with a dumbbell in a gym"
                    ),
                    photo(3, "Empty gym interior"),
                ]
            }
        ),
    )
    content = {
        "products": [
            {"image_query": "q", "image_subject": "leggings dumbbell gym woman"}
        ]
    }
    result = imagery.resolve(content, tmp_path, seeded())
    assert result.products[0] is not None
    assert result.products[0].subject_hits == 4
    assert "Photographer 2" == result.products[0].credit


def test_an_empty_query_means_the_model_declined_and_is_honoured(
    monkeypatch, keyed, tmp_path
):
    """A template, an ebook, a service: the model is told to return empty image
    fields for anything with no honest photograph. That must not be second-guessed by
    falling back to the product NAME, which is a brand word and finds anything."""
    session = install(monkeypatch, FakeSession({}))
    content = {"products": [{"name": "Notion Productivity Template"}]}
    result = imagery.resolve(content, tmp_path, seeded())
    assert result.products == [None]
    assert session.searched == [], "an unphotographable product must not be searched"


def test_subject_terms_carrying_no_visual_information_cannot_match(
    monkeypatch, keyed, tmp_path
):
    """ "The best premium quality product" would otherwise let any candidate clear
    the bar on a stopword."""
    install(
        monkeypatch,
        FakeSession({"q": [photo(1, "The best premium quality product, top item")]}),
    )
    content = {
        "products": [{"image_query": "q", "image_subject": "the best premium quality"}]
    }
    assert imagery.resolve(content, tmp_path, seeded()).products == [None]


def test_short_subject_terms_match_whole_words_only():
    """ "tea" must not claim a photograph described as "steam"."""
    assert not imagery._matches("tea", "hot steam rising from a cup")
    assert imagery._matches("tea", "a cup of tea")
    # Longer terms match as substrings so singular/plural both hit.
    assert imagery._matches("legging", "black leggings on a model")


def test_one_store_never_repeats_a_photograph(monkeypatch, keyed, tmp_path):
    """The same photo appearing as the hero, a card and a lifestyle frame reads as a
    padded page."""
    shared = [
        photo(1, "Man lifting a dumbbell in a gym"),
        photo(2, "Dumbbell rack in a gym"),
    ]
    install(monkeypatch, FakeSession({"a": list(shared), "b": list(shared)}))
    content = {
        "products": [
            {"image_query": "a", "image_subject": "dumbbell gym"},
            {"image_query": "b", "image_subject": "dumbbell gym"},
        ]
    }
    result = imagery.resolve(content, tmp_path, seeded())
    ids = [image.path for image in result.products if image is not None]
    assert len(ids) == len(set(ids)) == 2


# --------------------------------------------------------------------- failing open
def test_no_key_disables_photography_entirely(monkeypatch, tmp_path):
    monkeypatch.delenv(imagery.KEY_ENV, raising=False)
    assert not imagery.enabled()
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "dumbbell"}]},
        tmp_path,
        seeded(),
    )
    assert result.hero is None and result.products == [] and result.lifestyle == []


def test_a_provider_outage_still_yields_a_store(monkeypatch, keyed, tmp_path):
    """create-store is x402-paid. A 500 from the image provider must cost the store
    its photographs and nothing else — never the store, never the payment."""
    install(
        monkeypatch, FakeSession({"q": [photo(1, "dumbbell gym")]}, search_status=500)
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "dumbbell gym"}]},
        tmp_path,
        seeded(),
    )
    assert result.products == [None]


def test_a_body_that_is_not_a_jpeg_is_refused(monkeypatch, keyed, tmp_path):
    """Declared content-type is not trusted; the magic bytes decide."""
    install(
        monkeypatch,
        FakeSession(
            {"q": [photo(1, "a dumbbell in a gym")]}, blob=b"<html>nope</html>"
        ),
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "dumbbell gym"}]},
        tmp_path,
        seeded(),
    )
    assert result.products == [None]
    assert not list(tmp_path.glob("img/*")), "no file may survive a rejected body"


def test_a_non_image_content_type_is_refused(monkeypatch, keyed, tmp_path):
    install(
        monkeypatch,
        FakeSession({"q": [photo(1, "a dumbbell in a gym")]}, ctype="text/html"),
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "dumbbell gym"}]},
        tmp_path,
        seeded(),
    )
    assert result.products == [None]


def test_an_oversized_image_is_abandoned_mid_stream(monkeypatch, keyed, tmp_path):
    """The cap is enforced per chunk, so a lying or absent Content-Length cannot
    beat it."""
    install(
        monkeypatch,
        FakeSession(
            {"q": [photo(1, "a dumbbell in a gym")]},
            blob=b"\xff\xd8\xff" + b"\x00" * (imagery.MAX_IMAGE_BYTES + 1024),
        ),
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "dumbbell gym"}]},
        tmp_path,
        seeded(),
    )
    assert result.products == [None]


def test_the_search_budget_is_capped_per_store(monkeypatch, keyed, tmp_path):
    """A store cannot burn the hourly provider quota on its own catalog."""
    session = install(monkeypatch, FakeSession({}))
    content = {
        "products": [
            {"image_query": f"query-{i}", "image_subject": "dumbbell gym"}
            for i in range(imagery.MAX_SEARCHES + 4)
        ]
    }
    imagery.resolve(content, tmp_path, seeded())
    assert len(session.searched) == imagery.MAX_SEARCHES


def test_repeated_queries_do_not_each_cost_a_search(monkeypatch, keyed, tmp_path):
    session = install(
        monkeypatch, FakeSession({"same": [photo(1, "a dumbbell in a gym")]})
    )
    content = {
        "products": [
            {"image_query": "same", "image_subject": "dumbbell gym"},
            {"image_query": "same", "image_subject": "dumbbell gym"},
            {"image_query": "SAME", "image_subject": "dumbbell gym"},
        ]
    }
    imagery.resolve(content, tmp_path, seeded())
    assert session.searched == ["same"]


def test_resolve_never_raises_even_on_absurd_content(monkeypatch, keyed, tmp_path):
    install(monkeypatch, FakeSession({}))
    for content in (
        {},
        {"products": None},
        {"products": [None, 7, "x"]},
        {"products": [{}], "lifestyle_queries": [None, 5]},
    ):
        result = imagery.resolve(content, tmp_path, seeded())
        assert isinstance(result, imagery.StoreImagery)


# ------------------------------------------------------------- shape on the way out
def test_files_are_content_addressed_and_named_by_this_module(
    monkeypatch, keyed, tmp_path
):
    """The name is a digest of the bytes THIS module received — never a
    provider-supplied string — so nothing attacker-influenced reaches an <img src>,
    and two identical photographs are stored once."""
    install(monkeypatch, FakeSession({"q": [photo(1, "a dumbbell in a gym")]}))
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "dumbbell gym"}]},
        tmp_path,
        seeded(),
    )
    image = result.products[0]
    assert render._IMAGE_PATH.fullmatch(image.path), image.path
    assert (tmp_path / image.path).read_bytes().startswith(b"\xff\xd8\xff")


def test_apply_clears_any_image_the_model_tried_to_supply(tmp_path):
    """`image` is not a declared field on ProductContent, so pydantic drops a
    model-supplied one — but apply() clears it unconditionally so the invariant holds
    however the content dict arrived."""
    content = {"products": [{"name": "X", "image": {"path": "../../etc/passwd"}}]}
    imagery.apply(content, imagery.StoreImagery(products=[None]))
    assert "image" not in content["products"][0]


def test_apply_is_positional_and_survives_json(tmp_path):
    image = imagery.StoreImage(path="img/" + "a" * 16 + ".jpg", width=940, height=650)
    content = {"products": [{"name": "A"}, {"name": "B"}]}
    imagery.apply(content, imagery.StoreImagery(products=[None, image]))
    assert "image" not in content["products"][0]
    assert content["products"][1]["image"]["path"] == image.path
    # store.json round trip
    assert json.loads(json.dumps(content)) == content


def test_credits_are_deduplicated_and_capped(tmp_path):
    def entry(n):
        return {
            "path": f"img/{n:016x}.jpg",
            "credit": f"P{n}",
            "credit_url": "https://www.pexels.com/@x",
            "source_url": f"https://www.pexels.com/photo/{n}",
        }

    same = entry(1)
    content = {
        "products": [{"image": same}],
        "imagery": {"hero": same, "lifestyle": [entry(i) for i in range(2, 9)]},
    }
    out = imagery.credits(content)
    # the hero and the product share one photograph -> one credit
    assert sum(1 for c in out if c["credit"] == "P1") == 1
    # and never more lifestyle credits than the band actually renders
    assert len(out) <= 1 + imagery.MAX_LIFESTYLE


# ============================================================ reaching a page safely
class TestRenderer:
    """What app/render.py will and will not put on a page. Content is persisted JSON
    that can also arrive from an imported store.json, so the renderer re-validates
    everything rather than trusting that its own writer produced it."""

    @staticmethod
    def _image(**over):
        base = {
            "path": "img/" + "a1b2c3d4e5f60718" + ".jpg",
            "width": 940,
            "height": 650,
            "alt": "A man in a black gym tank top",
            "credit": "Jane Doe",
            "credit_url": "https://www.pexels.com/@jane",
            "source_url": "https://www.pexels.com/photo/1",
        }
        base.update(over)
        return base

    @pytest.mark.parametrize(
        "path",
        [
            "../../../etc/passwd",
            "img/../../etc/passwd",
            "/etc/passwd",
            "https://evil.example/x.jpg",
            "img/short.jpg",
            "img/a1b2c3d4e5f60718.png",
            "img/A1B2C3D4E5F60718.jpg",
            "img/a1b2c3d4e5f60718.jpg/../x",
        ],
    )
    def test_only_a_digest_named_jpeg_can_reach_an_img_src(self, path):
        assert render._safe_image(self._image(path=path)) is None

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "http://insecure.example/@x",
            "//evil.example/@x",
        ],
    )
    def test_a_credit_link_that_is_not_https_is_dropped(self, url):
        """Autoescaping protects an attribute's delimiters, not its scheme — a
        javascript: URL in an href survives escaping intact."""
        image = render._safe_image(self._image(credit_url=url))
        assert image is not None and image["credit_url"] == ""

    def test_absurd_dimensions_fall_back_rather_than_reaching_the_markup(self):
        for bad in (0, -5, 99999, "940", None, 1.5):
            image = render._safe_image(self._image(width=bad))
            assert image["width"] == 1200

    @pytest.mark.parametrize("theme", ["original.html", "bold.html", "editorial.html"])
    def test_every_theme_renders_photography_when_present(self, theme):
        content = {
            "store_name": "Ironline",
            "hero_headline": "Train heavier.",
            "emoji": "\U0001f3cb️",
            "price_usdt": 48,
            "products": [
                {
                    "name": "Tank",
                    "blurb": "b",
                    "price_usdt": 48,
                    "cta_text": "Add",
                    "image": self._image(),
                },
                {
                    "name": "Gloves",
                    "blurb": "b",
                    "price_usdt": 32,
                    "cta_text": "Add",
                    "image": self._image(path="img/" + "b" * 16 + ".jpg"),
                },
            ],
            "imagery": {
                "hero": self._image(path="img/" + "c" * 16 + ".jpg"),
                "lifestyle": [self._image(path="img/" + "d" * 16 + ".jpg")],
            },
        }
        html = render.render(content, "0x" + "a" * 40, "ironline", theme)
        assert html.count("<img") == 4, theme
        # the licence requires the photographer credit and a link back
        assert "Jane Doe" in html and "pexels.com/@jane" in html

    @pytest.mark.parametrize("theme", ["original.html", "bold.html", "editorial.html"])
    def test_a_store_with_no_photography_renders_exactly_as_before(self, theme):
        """The un-photographable store — a template, a service — must keep its
        generative texture and gain nothing: no empty frames, no credit block."""
        content = {
            "store_name": "Notes",
            "hero_headline": "A system.",
            "emoji": "\U0001f4dd",
            "price_usdt": 9,
            "products": [
                {"name": "Template", "blurb": "b", "price_usdt": 9, "cta_text": "Buy"}
            ],
        }
        html = render.render(content, "0x" + "a" * 40, "notes", theme)
        assert "<img" not in html, theme
        assert 'class="credits"' not in html
        assert "plateField" in html, "the generative plate must still be drawn"

    def test_the_product_photograph_becomes_the_schema_org_image(self):
        """schema.org/Product `image` is read by search and agent shopping surfaces as
        a picture OF THE PRODUCT; the OG card is a typographic brand tile."""
        content = {
            "store_name": "S",
            "products": [{"name": "T", "price_usdt": 5, "image": self._image()}],
        }
        ctx = render._store_ctx(content, "0x" + "a" * 40, "s")
        assert ctx["PRODUCT_JSONLD"]["image"].endswith(self._image()["path"])
        # and falls back to the card when there is no photograph
        bare = render._store_ctx(
            {"store_name": "S", "products": [{"name": "T", "price_usdt": 5}]},
            "0x" + "a" * 40,
            "s",
        )
        assert bare["PRODUCT_JSONLD"]["image"].endswith("/og.png")


# ================================================ surviving a merchant catalog edit
def test_a_deleted_product_does_not_shift_photographs_onto_the_wrong_items(make_store):
    """engine.resync_catalog rebuilds content['products'] from the live Product rows,
    so anything held by POSITION survives a deletion pointing at the wrong product.
    A photograph doing that would put a picture of one item on another item's card —
    the exact false claim this feature is built to avoid. Images therefore travel by
    product id.
    """
    from sqlalchemy import select

    from app import engine
    from app.db import SessionLocal
    from app.models import Product, Store

    make_store(slug="cat-shop")
    shot = {
        "path": "img/" + "f" * 16 + ".jpg",
        "width": 940,
        "height": 650,
        "alt": "second",
    }
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == "cat-shop"))
        first = session.scalar(select(Product).where(Product.store_id == store.id))
        second = Product(
            store_id=store.id, name="Second", price_micro=5_000_000, active=True
        )
        session.add(second)
        session.flush()
        # Only the SECOND product has a photograph.
        store.content = {
            "products": [
                {"id": first.id, "name": first.name, "price_usdt": 9, "blurb": "a"},
                {
                    "id": second.id,
                    "name": "Second",
                    "price_usdt": 5,
                    "blurb": "b",
                    "image": shot,
                    "image_query": "a second thing",
                },
            ]
        }
        session.commit()
        second_id = second.id

        # The merchant removes the first product; the second slides to index 0.
        first.active = False
        engine.resync_catalog(session, store)
        session.commit()

        products = store.content["products"]
        assert len(products) == 1
        assert products[0]["id"] == second_id
        assert products[0]["image"] == shot, "the photo must follow its own product"
        # the search text rides along too, so a later upgrade can re-resolve
        assert products[0]["image_query"] == "a second thing"


def test_a_catalog_edit_never_contacts_the_image_provider(make_store, monkeypatch):
    """Adding or deleting a product re-renders from the photographs already on disk.
    Reaching for the provider here would spend quota on every dashboard edit."""
    from sqlalchemy import select

    from app import engine
    from app.db import SessionLocal
    from app.models import Store

    make_store(slug="cat-noquota")

    def boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("resync_catalog must not resolve imagery")

    monkeypatch.setattr(imagery, "resolve", boom)
    with SessionLocal() as session:
        store = session.scalar(select(Store).where(Store.slug == "cat-noquota"))
        engine.resync_catalog(session, store)
        session.commit()


# ======================================== serving a photograph off the platform host
class TestImageRoute:
    """On the platform host nginx serves STORES_DIR at /s/, so these files never reach
    the app. On a store subdomain and on a custom domain the app serves everything,
    so without this route every photograph on those hosts 404s."""

    @staticmethod
    def _seed(make_store, slug, name="ab12cd34ef567890.jpg"):
        from app import config

        make_store(slug=slug)
        target = config.STORES_DIR / slug / "img" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(JPEG)
        return f"{slug}.{config.DOMAIN}", name

    def test_a_subdomain_store_serves_its_own_photograph(self, make_store):
        import app.main as main
        from fastapi.testclient import TestClient

        host, name = self._seed(make_store, "img-shop")
        r = TestClient(main.app).get(f"/img/{name}", headers={"host": host})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content == JPEG
        # content-addressed: these bytes can never change under this name
        assert "immutable" in r.headers.get("cache-control", "")

    @pytest.mark.parametrize(
        "name",
        [
            "../../../etc/passwd",
            "..%2f..%2fstore.json",
            "store.json",
            "og.png",
            "ab12cd34ef567890.png",
            "ab12CD34ef567890.jpg",
            "short.jpg",
        ],
    )
    def test_nothing_but_a_digest_named_jpeg_is_served(self, make_store, name):
        import app.main as main
        from fastapi.testclient import TestClient

        host, _ = self._seed(make_store, f"img-bad-{abs(hash(name)) % 9999}")
        r = TestClient(main.app).get(f"/img/{name}", headers={"host": host})
        assert r.status_code == 404, name

    def test_one_store_cannot_serve_another_stores_photograph(self, make_store):
        import app.main as main
        from fastapi.testclient import TestClient
        from app import config

        host_a, name = self._seed(make_store, "img-owner")
        make_store(slug="img-other")
        host_b = f"img-other.{config.DOMAIN}"
        client = TestClient(main.app)
        assert client.get(f"/img/{name}", headers={"host": host_a}).status_code == 200
        # the same filename, asked for on a different store's host
        assert client.get(f"/img/{name}", headers={"host": host_b}).status_code == 404

    def test_an_unknown_host_gets_nothing(self, make_store):
        import app.main as main
        from fastapi.testclient import TestClient

        _, name = self._seed(make_store, "img-host")
        r = TestClient(main.app).get(
            f"/img/{name}", headers={"host": "random.example.org"}
        )
        assert r.status_code == 404


# ============================================ the machine surfaces see it too
class TestMachineSurfaces:
    """Tilla's claim is that a store sells to humans AND agents from one build. A
    catalog entry with no picture is worth strictly less to an agent deciding what to
    buy, so the photograph is published on the machine surfaces as well."""

    @staticmethod
    def _seed_with_photo(make_store, slug):
        from sqlalchemy import select

        from app import config
        from app.db import SessionLocal
        from app.models import Product, Store

        make_store(slug=slug)
        name = "ab12cd34ef567890.jpg"
        target = config.STORES_DIR / slug / "img" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(JPEG)
        with SessionLocal() as session:
            store = session.scalar(select(Store).where(Store.slug == slug))
            product = session.scalar(
                select(Product).where(Product.store_id == store.id)
            )
            store.content = {
                "store_name": "Ironline",
                "products": [
                    {
                        "id": product.id,
                        "name": product.name,
                        "price_usdt": 9,
                        "blurb": "b",
                        "image": {
                            "path": f"img/{name}",
                            "width": 940,
                            "height": 650,
                            "alt": "a black gym tank top",
                            "credit": "Jane Doe",
                        },
                    }
                ],
            }
            session.commit()
        return f"img/{name}"

    def test_feed_json_publishes_the_photograph_and_still_validates(self, make_store):
        """The feed schema pins additionalProperties:false and also validates INBOUND
        federated peer feeds, so an undeclared key would make a photographed store's
        own feed invalid — and unusable by a peer."""
        import jsonschema
        import yaml
        from fastapi.testclient import TestClient

        import app.main as main

        path = self._seed_with_photo(make_store, "feed-photo")
        body = TestClient(main.app).get("/s/feed-photo/feed.json").json()
        product = body["products"][0]
        assert product["image_url"].endswith(path)
        assert product["image_url"].startswith("https://")

        schema = yaml.safe_load(FEED_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(body, schema)

    def test_a_store_with_no_photograph_omits_the_key_entirely(self, make_store):
        """Never an empty string or a placeholder: an agent must not have to tell
        "no picture" apart from "broken picture"."""
        from fastapi.testclient import TestClient

        import app.main as main

        make_store(slug="feed-nophoto")
        body = TestClient(main.app).get("/s/feed-nophoto/feed.json").json()
        assert "image_url" not in body["products"][0]

    def test_google_merchant_gets_a_required_image_link(self, make_store):
        from fastapi.testclient import TestClient

        import app.main as main

        path = self._seed_with_photo(make_store, "gm-photo")
        xml = TestClient(main.app).get("/s/gm-photo/feed/google.xml").text
        assert "<g:image_link>" in xml and path in xml

    def test_the_openai_feed_carries_it_too(self, make_store):
        from fastapi.testclient import TestClient

        import app.main as main

        path = self._seed_with_photo(make_store, "oa-photo")
        body = TestClient(main.app).get("/s/oa-photo/feed/openai.json").json()
        assert body["products"][0]["image_url"].endswith(path)

    def test_an_image_is_matched_to_its_product_by_id_not_position(self, make_store):
        """The agent-facing consequence of the resync problem: if this matched by
        index, a catalog edit would offer an agent a picture of a different item than
        the one it is paying for."""
        from sqlalchemy import select

        from app import agentic
        from app.db import SessionLocal
        from app.models import Product, Store

        make_store(slug="feed-idmatch")
        with SessionLocal() as session:
            store = session.scalar(select(Store).where(Store.slug == "feed-idmatch"))
            product = session.scalar(
                select(Product).where(Product.store_id == store.id)
            )
            # content lists a DIFFERENT product id first
            store.content = {
                "products": [
                    {
                        "id": product.id + 500,
                        "name": "Other",
                        "image": {"path": "img/" + "e" * 16 + ".jpg"},
                    },
                    {
                        "id": product.id,
                        "name": product.name,
                        "image": {"path": "img/" + "f" * 16 + ".jpg"},
                    },
                ]
            }
            session.commit()
            url = agentic.product_image_url(store, product, "https://x/s/feed-idmatch/")
        assert url.endswith("f" * 16 + ".jpg")


# ==================================================== the whole pipeline, end to end
def test_create_store_produces_a_photographed_page(tmp_path, monkeypatch, keyed):
    """Prompt -> generated copy WITH search text -> verified photographs on disk ->
    a rendered page that shows them. The one test that would catch a break anywhere
    along the chain."""
    import json as _json

    import respx
    from httpx import Response

    import app.engine as engine
    from app.config import WARDEN_SCREEN_URL

    monkeypatch.setattr(engine, "STORES_DIR", tmp_path)

    generated = {
        "store_name": "Ironline",
        "tagline": "Built for the set",
        "hero_headline": "Train heavier.",
        "hero_subcopy": "Kit that survives the rack.",
        "emoji": "\U0001f3cb️",
        "hero_image_query": "woman lifting dumbbells in a gym",
        "hero_image_subject": "dumbbell gym woman",
        "lifestyle_queries": ["barbell rack in a gym"],
        "products": [
            {
                "name": "Compression Leggings",
                "blurb": "Squat-proof.",
                "price_usdt": 48,
                "cta_text": "Add",
                "image_query": "black athletic leggings on a woman",
                "image_subject": "leggings woman",
            },
            {
                "name": "Studio Notes",
                "blurb": "A digital training log.",
                "price_usdt": 9,
                "cta_text": "Buy",
                # The model judged this one unphotographable and said so.
                "image_query": "",
                "image_subject": "",
            },
        ],
        "brand": {"hue": 25},
    }

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": _json.dumps(generated)}]}

    monkeypatch.setattr("app.engine.requests.post", lambda *a, **k: FakeResp())
    install(
        monkeypatch,
        FakeSession(
            {
                "woman lifting dumbbells in a gym": [
                    photo(10, "Woman lifting heavy dumbbells in a gym")
                ],
                "barbell rack in a gym": [photo(11, "A barbell rack in a gym")],
                "black athletic leggings on a woman": [
                    photo(12, "Woman wearing black leggings in a studio"),
                    photo(13, "A bowl of fruit"),
                ],
            }
        ),
    )

    with respx.mock:
        respx.post(WARDEN_SCREEN_URL).mock(
            return_value=Response(200, json={"verdict": "ALLOW"})
        )
        result = engine.create_store("I sell gym wear", "0x" + "b" * 40)

    slug = result["slug"]
    store_dir = tmp_path / slug
    meta = _json.loads((store_dir / "store.json").read_text(encoding="utf-8"))
    content = meta["content"]

    # The photographable product got a VERIFIED photograph; the digital one got none.
    assert content["products"][0]["image"]["path"].startswith("img/")
    assert "image" not in content["products"][1]
    assert content["imagery"]["hero"] is not None
    assert len(content["imagery"]["lifestyle"]) == 1

    # The files are really on disk, under the store's own directory.
    for image in [
        content["products"][0]["image"],
        content["imagery"]["hero"],
        *content["imagery"]["lifestyle"],
    ]:
        written = store_dir / image["path"]
        assert written.is_file() and written.read_bytes().startswith(b"\xff\xd8\xff")

    # And the page shows them, with the credits the licence requires.
    html = (store_dir / "index.html").read_text(encoding="utf-8")
    assert html.count("<img") == 3, "hero + one product + one lifestyle frame"
    assert content["products"][0]["image"]["path"] in html
    assert "Photographer 12" in html
    # The fruit bowl was returned by the search and correctly refused.
    assert "A bowl of fruit" not in html


def test_of_two_photos_containing_the_product_the_one_it_is_ABOUT_wins(
    monkeypatch, keyed, tmp_path
):
    """Coverage answers "does this photo contain the product"; it does not answer "is
    the product what the photo is OF". Both descriptions below name leggings, but one
    is a photograph of shoes that happens to include them. Stock captions are written
    subject-first, so the earlier the product is named the more likely it is the
    subject — used only to rank candidates that tie on coverage.
    """
    install(
        monkeypatch,
        FakeSession(
            {
                "q": [
                    photo(1, "Sportswoman wearing sneakers and black leggings"),
                    photo(2, "Black leggings on a model against a white background"),
                ]
            }
        ),
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "leggings"}]},
        tmp_path,
        seeded(),
    )
    assert result.products[0].alt.startswith("Black leggings")


def test_primacy_can_never_promote_a_less_relevant_photograph(
    monkeypatch, keyed, tmp_path
):
    """The subject-position tie-break ranks WITHIN a coverage level. A photo naming
    the product first but showing less of it must still lose to one that depicts
    more, or the tie-break would quietly become the primary ranking."""
    install(
        monkeypatch,
        FakeSession(
            {
                "q": [
                    # names the product first, but covers one term
                    photo(1, "Leggings folded on a shelf"),
                    # names it later, but covers three
                    photo(2, "A woman in a gym wearing black leggings"),
                ]
            }
        ),
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "leggings woman gym"}]},
        tmp_path,
        seeded(),
    )
    assert result.products[0].subject_hits == 3
    assert "gym" in result.products[0].alt


def test_supporting_nouns_alone_cannot_qualify_a_photo(monkeypatch, keyed, tmp_path):
    """Regression, from a real live store. The model described its leggings as
    "leggings fabric fold"; the provider returned "High-quality black fabric with
    intricate folds and texture", which matched two of three terms and contained no
    leggings. A store selling leggings showed a photograph of cloth.

    The first subject term is the product's own noun and is required, so supporting
    detail can raise a score but can never create one.
    """
    install(
        monkeypatch,
        FakeSession(
            {
                "q": [
                    photo(1, "High-quality black fabric with intricate folds and texture")
                ]
            }
        ),
    )
    result = imagery.resolve(
        {"products": [{"image_query": "q", "image_subject": "leggings fabric fold"}]},
        tmp_path,
        seeded(),
    )
    assert result.products == [None]


def test_the_head_noun_is_required_only_where_a_subject_was_curated(
    monkeypatch, keyed, tmp_path
):
    """A lifestyle frame's "subject" is just its own query text, whose leading word is
    as likely to be `person` as the product. Requiring it there would reject correct
    photographs captioned `man` or `woman` and gain nothing."""
    install(
        monkeypatch,
        FakeSession(
            {
                "person wearing a training tank on a morning run": [
                    photo(1, "A man running at sunrise in a tank top")
                ]
            }
        ),
    )
    result = imagery.resolve(
        {"lifestyle_queries": ["person wearing a training tank on a morning run"]},
        tmp_path,
        seeded(),
    )
    assert len(result.lifestyle) == 1, "a correct lifestyle photo must not be rejected"


class TestHeadRequests:
    """nginx answers HEAD on the platform's static /s/ path, but a store subdomain and
    a custom domain proxy every request to the app — so these asset routes have to
    answer HEAD themselves. Social scrapers, link checkers and uptime monitors probe
    an asset with HEAD before fetching it, and a 405 reads as a broken asset."""

    @staticmethod
    def _client_and_host(make_store, slug, name="ab12cd34ef567890.jpg"):
        from fastapi.testclient import TestClient

        import app.main as main
        from app import config

        make_store(slug=slug)
        img = config.STORES_DIR / slug / "img" / name
        img.parent.mkdir(parents=True, exist_ok=True)
        img.write_bytes(JPEG)
        (config.STORES_DIR / slug / "og.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return TestClient(main.app), f"{slug}.{config.DOMAIN}", name

    def test_head_on_a_photograph_returns_headers_and_no_body(self, make_store):
        client, host, name = self._client_and_host(make_store, "head-img")
        r = client.head(f"/img/{name}", headers={"host": host})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["content-length"] == str(len(JPEG))
        assert "immutable" in r.headers.get("cache-control", "")
        assert r.content == b"", "HEAD must not carry a body"

    def test_head_on_the_og_card_works_too(self, make_store):
        client, host, _ = self._client_and_host(make_store, "head-og")
        r = client.head("/og.png", headers={"host": host})
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content == b""

    def test_head_still_refuses_what_get_refuses(self, make_store):
        """The method must not become a way around the path and host checks."""
        client, host, _ = self._client_and_host(make_store, "head-guard")
        assert client.head("/img/store.json", headers={"host": host}).status_code == 404
        assert (
            client.head(
                "/img/ab12cd34ef567890.jpg", headers={"host": "random.example.org"}
            ).status_code
            == 404
        )
