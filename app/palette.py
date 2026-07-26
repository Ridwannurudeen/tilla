"""Derive a store's palette instead of accepting four unrelated hex values.

WHY this module exists. Before it, the LLM handed back four free hex colours and
``render._safe_hex`` checked only that each was *syntactically* a colour. Nothing
checked that they worked together: an accent could sit at 1.4:1 against the
background and simply vanish, and body text could land well under the WCAG floor.
Four independently-chosen colours look sampled. A palette derived from one hue by
a stated relationship looks decided — that difference is most of what separates a
designed storefront from a generated one.

So the LLM now contributes only what it is actually good at: the *hue* that suits
what is being sold (coffee is not the same brown as a fintech blue) and, if it
likes, a harmony and a mood. Everything else is computed here, and every returned
colour is checked against a contrast floor before it is allowed out:

    text vs bg      >= 7.0:1   WCAG AAA for body copy
    primary vs bg   >= 3.0:1   a button must be findable
    accent vs bg    >= 3.0:1   so must an accent

If a nominal colour misses its floor it is walked toward the far end of the
lightness range until it clears, so the guarantee holds for every hue rather than
for the ones that happen to be lucky. The output is exactly the four keys
``render._palette_ctx`` already reads, so nothing downstream knows this ran, and a
store whose content predates it keeps whatever hexes it was created with.
"""

from __future__ import annotations

from collections.abc import Mapping

# Reused rather than reimplemented: the WCAG luminance maths already lives in
# render.py and having two copies of it is how the two drift apart.
from app.render import _relative_luminance

# Accent hue offset per harmony. Mono differs by lightness and saturation alone.
HARMONIES: dict[str, int] = {
    "mono": 0,
    "analogous": 32,
    "complementary": 180,
    "triadic": 120,
}

# A mood fixes the ground and the ink, plus how saturated the brand colours sit.
# `midnight` reproduces the shipped default look (near-black ground, light ink).
MOODS: dict[str, dict] = {
    "midnight": {
        "bg_l": 0.055,
        "bg_s": 0.14,
        "text_l": 0.96,
        "text_s": 0.05,
        "primary_s": 0.72,
        "primary_l": 0.62,
        "accent_s": 0.78,
        "accent_l": 0.52,
    },
    "ink": {
        "bg_l": 0.10,
        "bg_s": 0.05,
        "text_l": 0.93,
        "text_s": 0.02,
        "primary_s": 0.45,
        "primary_l": 0.66,
        "accent_s": 0.50,
        "accent_l": 0.58,
    },
    "paper": {
        "bg_l": 0.975,
        "bg_s": 0.06,
        "text_l": 0.11,
        "text_s": 0.20,
        "primary_s": 0.70,
        "primary_l": 0.40,
        "accent_s": 0.72,
        "accent_l": 0.34,
    },
    "bone": {
        "bg_l": 0.945,
        "bg_s": 0.18,
        "text_l": 0.16,
        "text_s": 0.28,
        "primary_s": 0.48,
        "primary_l": 0.36,
        "accent_s": 0.52,
        "accent_l": 0.30,
    },
}

MOOD_NAMES = tuple(sorted(MOODS))
HARMONY_NAMES = tuple(sorted(HARMONIES))

# Contrast floors. Nothing leaves this module below them.
MIN_TEXT_CONTRAST = 7.0
MIN_BRAND_CONTRAST = 3.0


def _hsl_to_hex(hue: float, sat: float, light: float) -> str:
    """HSL (hue in degrees, sat/light in 0..1) to a #rrggbb string."""
    hue = hue % 360
    sat = min(max(sat, 0.0), 1.0)
    light = min(max(light, 0.0), 1.0)
    c = (1 - abs(2 * light - 1)) * sat
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = light - c / 2
    if hue < 60:
        rgb = (c, x, 0.0)
    elif hue < 120:
        rgb = (x, c, 0.0)
    elif hue < 180:
        rgb = (0.0, c, x)
    elif hue < 240:
        rgb = (0.0, x, c)
    elif hue < 300:
        rgb = (x, 0.0, c)
    else:
        rgb = (c, 0.0, x)
    return "#" + "".join(f"{round((v + m) * 255):02x}" for v in rgb)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two validated hex colours."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _to_lab(hex_color: str) -> tuple[float, float, float]:
    """sRGB hex to CIE L*a*b* (D65). Needed because WCAG contrast is a LUMINANCE
    ratio and therefore useless for "are these two colours distinguishable" — a red
    and a green of equal luminance score 1.0 while looking nothing alike. Telling
    an accent apart from the text is a perceptual question, not a contrast one."""
    digits = hex_color[1:]
    if len(digits) in (3, 4):
        digits = "".join(d * 2 for d in digits)
    srgb = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb]
    x = (rgb[0] * 0.4124 + rgb[1] * 0.3576 + rgb[2] * 0.1805) / 0.95047
    y = rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722
    z = (rgb[0] * 0.0193 + rgb[1] * 0.1192 + rgb[2] * 0.9505) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a: str, b: str) -> float:
    """CIE76 colour difference. Roughly: under ~2.3 is imperceptible, ~10 is a
    clear difference, ~25 reads as a different colour entirely."""
    la, aa, ba = _to_lab(a)
    lb, ab, bb = _to_lab(b)
    return ((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2) ** 0.5


# An accent that a viewer cannot tell from the primary or from the body text is not
# an accent. Enforced perceptually, via delta_e above.
MIN_ACCENT_SEPARATION = 22.0


def _fit_contrast(
    hue: float, sat: float, light: float, against: str, floor: float
) -> str:
    """The colour at this hue/sat whose lightness is closest to `light` while still
    clearing `floor` against `against`.

    Walks lightness away from the background rather than giving up, so the floor is
    met for every hue instead of only the fortunate ones. Steps toward whichever
    end of the range is further from the background, and falls back to flat
    black/white if even the extreme cannot clear the floor (possible for a mid-grey
    background, where no hue at any lightness would)."""
    if contrast(_hsl_to_hex(hue, sat, light), against) >= floor:
        return _hsl_to_hex(hue, sat, light)
    going_darker = _relative_luminance(against) > 0.5
    step = -0.02 if going_darker else 0.02
    probe = light
    for _ in range(50):
        probe += step
        if not 0.0 <= probe <= 1.0:
            break
        candidate = _hsl_to_hex(hue, sat, probe)
        if contrast(candidate, against) >= floor:
            return candidate
    return "#000000" if going_darker else "#ffffff"


def derive(hue: float, harmony: str, mood: str) -> dict[str, str]:
    """Build a full palette from one hue and two named relationships.

    Returns the four keys ``render._palette_ctx`` reads. Every value is a strict
    ``#rrggbb`` and every contrast floor above is satisfied."""
    if harmony not in HARMONIES:
        harmony = "analogous"
    if mood not in MOODS:
        mood = "midnight"
    m = MOODS[mood]
    hue = float(hue) % 360
    accent_hue = (hue + HARMONIES[harmony]) % 360
    if harmony == "mono":
        # No hue separation to lean on, so separate the accent by lightness instead
        # or primary and accent would be nearly the same colour.
        accent_l = m["accent_l"] + (0.16 if m["bg_l"] < 0.5 else -0.16)
    else:
        accent_l = m["accent_l"]

    bg = _hsl_to_hex(hue, m["bg_s"], m["bg_l"])
    text = _fit_contrast(hue, m["text_s"], m["text_l"], bg, MIN_TEXT_CONTRAST)
    primary = _fit_contrast(hue, m["primary_s"], m["primary_l"], bg, MIN_BRAND_CONTRAST)
    accent = _separated_accent(
        accent_hue, m["accent_s"], accent_l, bg, primary, text, harmony
    )
    return {"primary": primary, "accent": accent, "bg": bg, "text": text}


def _separated_accent(
    hue: float,
    sat: float,
    light: float,
    bg: str,
    primary: str,
    text: str,
    harmony: str,
) -> str:
    """An accent that clears the contrast floor against the ground AND is
    perceptually distinct from both the primary and the body text.

    Without this, some hues produced an accent a viewer simply could not tell from
    the text — worst case delta_e 1.0, i.e. the same colour. Candidates are tried in
    order of how little they disturb the intended design: the nominal accent first,
    then lightness steps either side, then (for `mono`, which has no hue separation
    to fall back on) a widening hue offset. The best candidate is kept even if none
    fully clears, so this can never fail to return a usable colour."""
    steps = [0.0]
    for delta in (0.10, 0.18, 0.26, 0.34, 0.42):
        steps.extend((delta, -delta))
    hue_offsets = (0, 16, -16, 30, -30) if harmony == "mono" else (0,)

    best, best_score = None, -1.0
    for h_off in hue_offsets:
        for step in steps:
            candidate = _fit_contrast(
                hue + h_off, sat, light + step, bg, MIN_BRAND_CONTRAST
            )
            score = min(delta_e(candidate, primary), delta_e(candidate, text))
            if score >= MIN_ACCENT_SEPARATION:
                return candidate
            if score > best_score:
                best, best_score = candidate, score
    return best


def resolve(content: Mapping, rnd) -> dict[str, str]:
    """The palette for a store being generated: honour what the LLM chose where it
    is usable, seed the rest.

    ``rnd`` is the store's slug-seeded PRNG, so two stores never land on the same
    colours by accident and one store always resolves the same way on re-render.
    The LLM's hue is kept whenever it is a number in range — that judgement is
    genuinely semantic (what colour is this product) unlike the layout axes, where
    it was measured collapsing to one answer. Harmony and mood are taken when named
    validly and seeded otherwise."""
    brand = content.get("brand") if isinstance(content, Mapping) else None
    brand = brand if isinstance(brand, Mapping) else {}

    hue = brand.get("hue")
    try:
        hue = float(hue)
    except (TypeError, ValueError):
        hue = rnd() * 360
    if not 0 <= hue < 360:
        hue = hue % 360

    harmony = brand.get("harmony")
    if harmony not in HARMONIES:
        harmony = HARMONY_NAMES[int(rnd() * len(HARMONY_NAMES)) % len(HARMONY_NAMES)]
    else:
        rnd()

    mood = brand.get("mood")
    if mood not in MOODS:
        mood = MOOD_NAMES[int(rnd() * len(MOOD_NAMES)) % len(MOOD_NAMES)]
    else:
        rnd()

    return derive(hue, harmony, mood)
