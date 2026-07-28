"""The palette engine's guarantees (app/palette.py).

These are exhaustive on purpose. The whole point of deriving a palette rather than
accepting four hex values is that the guarantees hold for EVERY hue, not for the
ones that happen to be lucky — so the tests sweep the hue circle instead of
sampling a few brands.
"""

import re

import pytest

from app import palette
from app.palette import (
    HARMONIES,
    MIN_ACCENT_SEPARATION,
    MIN_BRAND_CONTRAST,
    MIN_TEXT_CONTRAST,
    MOODS,
    contrast,
    delta_e,
    derive,
)

HEX = re.compile(r"^#[0-9a-f]{6}$")
COMBOS = [(h, m) for h in sorted(HARMONIES) for m in sorted(MOODS)]


@pytest.mark.parametrize("harmony,mood", COMBOS)
def test_every_hue_meets_every_floor(harmony, mood):
    """Text readable, brand colours findable, accent perceptibly its own colour —
    for all 360 degrees. A single failing hue means some merchant gets a storefront
    with invisible body copy.

    Every hue, not every third: 360 x 4 harmonies x 4 moods = the 5760 combinations
    README.md claims are verified. Sampling a third of them and publishing the whole
    space as covered is the gap this closes."""
    for hue in range(360):
        p = derive(hue, harmony, mood)
        assert contrast(p["text"], p["bg"]) >= MIN_TEXT_CONTRAST - 1e-9, (hue, p)
        assert contrast(p["primary"], p["bg"]) >= MIN_BRAND_CONTRAST - 1e-9, (hue, p)
        assert contrast(p["accent"], p["bg"]) >= MIN_BRAND_CONTRAST - 1e-9, (hue, p)
        # perceptual, not contrast: a red and a green of equal luminance have a
        # contrast ratio of 1.0 and are obviously different colours
        assert delta_e(p["accent"], p["primary"]) >= MIN_ACCENT_SEPARATION - 1e-9, (
            hue,
            p,
        )
        assert delta_e(p["accent"], p["text"]) >= MIN_ACCENT_SEPARATION - 1e-9, (hue, p)


@pytest.mark.parametrize("harmony,mood", COMBOS)
def test_output_is_always_four_strict_hex_values(harmony, mood):
    """render._safe_hex silently swaps anything that is not a strict hex for the
    platform default — which would quietly undo the whole derivation."""
    for hue in (0, 47, 128, 233, 359):
        p = derive(hue, harmony, mood)
        assert set(p) == {"primary", "accent", "bg", "text"}
        for key, value in p.items():
            assert HEX.match(value), (key, value)


def test_derive_is_pure_and_stable():
    for _ in range(3):
        assert derive(200, "triadic", "paper") == derive(200, "triadic", "paper")


def test_hue_wraps_rather_than_failing():
    assert derive(370, "mono", "ink") == derive(10, "mono", "ink")
    assert derive(-10, "mono", "ink") == derive(350, "mono", "ink")


def test_bogus_harmony_and_mood_fall_back_instead_of_raising():
    """Fail-closed, like _safe_hex and the DNA axes: a create-store is paid for, so
    a bad style value must never be the thing that fails it."""
    fallback = derive(120, "analogous", "midnight")
    assert derive(120, "not-a-harmony", "midnight") == fallback
    assert derive(120, "analogous", "not-a-mood")["bg"] == fallback["bg"]


def test_different_hues_give_different_palettes():
    seen = {
        tuple(sorted(derive(hue, "analogous", "midnight").items()))
        for hue in range(0, 360, 10)
    }
    assert len(seen) >= 30, f"only {len(seen)} distinct palettes across 36 hues"


def test_moods_split_into_light_and_dark_grounds():
    from app.palette import _relative_luminance

    assert _relative_luminance(derive(0, "mono", "midnight")["bg"]) < 0.1
    assert _relative_luminance(derive(0, "mono", "ink")["bg"]) < 0.15
    assert _relative_luminance(derive(0, "mono", "paper")["bg"]) > 0.8
    assert _relative_luminance(derive(0, "mono", "bone")["bg"]) > 0.8


# ------------------------------------------------------------------- resolve()
def _rng(values):
    """A stand-in for the slug-seeded PRNG, so seeded behaviour is assertable."""
    it = iter(values * 20)
    return lambda: next(it)


def test_resolve_uses_the_llm_brief_when_it_is_usable():
    content = {"brand": {"hue": 28, "harmony": "complementary", "mood": "bone"}}
    assert palette.resolve(content, _rng([0.5])) == derive(28, "complementary", "bone")


def test_resolve_seeds_each_missing_field_independently():
    """A partial brief must be topped up, not discarded — the model naming only a
    hue is a perfectly good contribution."""
    got = palette.resolve({"brand": {"hue": 300}}, _rng([0.0]))
    assert got == derive(300, sorted(HARMONIES)[0], sorted(MOODS)[0])


def test_resolve_ignores_a_bogus_brief_entirely():
    for bogus in [
        None,
        "nope",
        {"hue": "x", "harmony": "y", "mood": "z"},
        {"hue": None},
        [],
    ]:
        got = palette.resolve({"brand": bogus}, _rng([0.0]))
        assert set(got) == {"primary", "accent", "bg", "text"}
        for value in got.values():
            assert HEX.match(value), (bogus, value)


def test_resolve_with_no_brand_key_still_yields_a_valid_palette():
    got = palette.resolve({"store_name": "X"}, _rng([0.25, 0.75]))
    assert set(got) == {"primary", "accent", "bg", "text"}
    assert contrast(got["text"], got["bg"]) >= MIN_TEXT_CONTRAST - 1e-9
