"""Photo standard for content entries (slice 1d).

Pat picked the house look on 2026-11-19 from generated samples:
PLATE - muted, deep blacks. Every still image on a content entry is
now finished to that standard before the entry lands at drafts-ready,
and the alternates ride along as suggestions: Documentary Clean,
Warm Film, Mono, and a PLATE 4:5 crop.

The treatments are grade/finish only - deterministic Pillow
operations ported exactly from the samples script Pat approved
(/home/seeker/logs/photo-samples/make_samples.py on seeker88) - and
they never touch the original: the generator works on the bytes it
fetched, and the treated results upload as new files next to it.

Failure discipline matches the rest of the pipeline: process_photo
raises on undecodable input, and the generator degrades that one
photo to untreated (error noted in the log). The entry still drafts.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, ImageStat

# Output encoding, identical to the samples: JPEG quality 88, the
# longest edge capped at 1600 px with Lanczos resampling.
JPEG_QUALITY = 88
MAX_EDGE = 1600

# Alternate look keys, shared with the site's treated-media route.
LOOK_CLEAN = "clean"
LOOK_WARM_FILM = "warm-film"
LOOK_MONO = "mono"
LOOK_PLATE_4X5 = "plate-4x5"
ALTERNATE_LOOKS = (LOOK_CLEAN, LOOK_WARM_FILM, LOOK_MONO, LOOK_PLATE_4X5)

# The 4:5 crop window centres at 46% of the frame width - the
# subject anchor from the samples Pat approved.
CROP_CENTER_X = 0.46


@dataclass
class TreatedPhoto:
    """One photo finished to the standard, plus its alternates.

    All values are encoded JPEG bytes (quality 88, max edge 1600).
    """

    standard: bytes
    alternates: dict[str, bytes] = field(default_factory=dict)


# ---------- LUT builders (ported from the samples script) ----------


def _lut_levels_black(bp: int) -> list[int]:
    # Crush input <= bp to 0, stretch the rest to full range
    # (deepens blacks).
    return [max(0, min(255, round((x - bp) * 255.0 / (255 - bp)))) for x in range(256)]


def _lut_scurve(a: float) -> list[int]:
    # Gentle S-curve: s(t) = t + a * t*(1-t)*(t-0.5)*4, t in 0..1.
    out = []
    for x in range(256):
        t = x / 255.0
        v = t + a * t * (1 - t) * (t - 0.5) * 4
        out.append(max(0, min(255, round(v * 255))))
    return out


def _compose(lut_first: list[int], lut_second: list[int]) -> list[int]:
    return [lut_second[lut_first[x]] for x in range(256)]


def _apply_lut_rgb(im: Image.Image, lut: list[int]) -> Image.Image:
    return im.point(lut * 3)


def _lut_gain(g: float) -> list[int]:
    return [max(0, min(255, round(x * g))) for x in range(256)]


# ---------- Treatments (RGB image -> RGB image, full resolution) ----------


def plate(im: Image.Image) -> Image.Image:
    """PLATE, the house standard: black-point 14 levels into an
    a=0.35 S-curve (one composed LUT), saturation x0.80, and the
    highlights pulled slightly warm - R+5, G+1, B-5 at full
    highlight, scaled by (luminance/255)^2."""
    lut = _compose(_lut_levels_black(14), _lut_scurve(0.35))
    out = _apply_lut_rgb(im, lut)
    out = ImageEnhance.Color(out).enhance(0.80)
    lum = out.convert("L")
    w = lum.point([round(255 * (v / 255.0) ** 2) for v in range(256)])
    r, g, b = out.split()
    r = ImageChops.add(r, w.point(lambda v: round(v * 5 / 255.0)))
    g = ImageChops.add(g, w.point(lambda v: round(v * 1 / 255.0)))
    b = ImageChops.subtract(b, w.point(lambda v: round(v * 5 / 255.0)))
    return Image.merge("RGB", (r, g, b))


def documentary_clean(im: Image.Image) -> Image.Image:
    """DOCUMENTARY CLEAN: gray-world white balance, contrast x1.06,
    UnsharpMask radius 2 / 60% / threshold 2."""
    stat = ImageStat.Stat(im)
    means = stat.mean
    gray = sum(means) / 3.0
    gains = [gray / m if m else 1.0 for m in means]
    r, g, b = im.split()
    out = Image.merge(
        "RGB",
        (
            r.point(_lut_gain(gains[0])),
            g.point(_lut_gain(gains[1])),
            b.point(_lut_gain(gains[2])),
        ),
    )
    out = ImageEnhance.Contrast(out).enhance(1.06)
    return out.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))


def warm_film(im: Image.Image) -> Image.Image:
    """WARM FILM: channel gains R x1.05 / B x0.93, contrast x0.94,
    shadows lifted to an output black of 16, saturation x0.88."""
    r, g, b = im.split()
    out = Image.merge("RGB", (r.point(_lut_gain(1.05)), g, b.point(_lut_gain(0.93))))
    out = ImageEnhance.Contrast(out).enhance(0.94)
    lift = [round(16 + x * (255 - 16) / 255.0) for x in range(256)]
    out = _apply_lut_rgb(out, lift)
    return ImageEnhance.Color(out).enhance(0.88)


def mono(im: Image.Image) -> Image.Image:
    """MONO: ITU-R 601-2 luma (PIL's 'L' conversion), levels
    stretched 8 -> 0 / 247 -> 255, contrast x1.20."""
    out = im.convert("L")
    stretch = [max(0, min(255, round((x - 8) * 255.0 / (247 - 8)))) for x in range(256)]
    out = out.point(stretch)
    out = ImageEnhance.Contrast(out).enhance(1.20)
    return out.convert("RGB")


def crop_4x5(im: Image.Image) -> Image.Image:
    """The 4:5 portrait crop from the samples: full height, width =
    height * 4/5, window centred at 46% of the frame width. Frames
    too narrow for that window (portrait past 4:5) crop vertically
    instead - full width, height = width * 5/4, centred."""
    w, h = im.size
    crop_w = round(h * 4 / 5)
    if crop_w <= w:
        centre_x = CROP_CENTER_X * w
        left = round(centre_x - crop_w / 2)
        left = max(0, min(left, w - crop_w))
        return im.crop((left, 0, left + crop_w, h))
    crop_h = round(w * 5 / 4)
    top = max(0, (h - crop_h) // 2)
    return im.crop((0, top, w, top + crop_h))


# ---------- Loading + output encoding ----------


def load_photo(data: bytes) -> Image.Image:
    """Decode stored photo bytes to an RGB image with the EXIF
    orientation applied - the orientation the samples were graded
    in, and the one every viewer shows."""
    with Image.open(io.BytesIO(data)) as raw:
        return ImageOps.exif_transpose(raw).convert("RGB")


def _resize_max(im: Image.Image, maxedge: int = MAX_EDGE) -> Image.Image:
    w, h = im.size
    s = min(1.0, maxedge / max(w, h))
    if s >= 1.0:
        return im
    return im.resize((round(w * s), round(h * s)), Image.LANCZOS)


def _encode(im: Image.Image) -> bytes:
    buffer = io.BytesIO()
    _resize_max(im).save(buffer, "JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def process_photo(data: bytes) -> TreatedPhoto:
    """Finish one photo (raw stored bytes) to the PLATE standard
    plus the four alternates. Raises on undecodable input; the
    generator isolates that failure to this one photo."""
    base = load_photo(data)
    standard = plate(base)
    alternates = {
        LOOK_CLEAN: _encode(documentary_clean(base)),
        LOOK_WARM_FILM: _encode(warm_film(base)),
        LOOK_MONO: _encode(mono(base)),
        LOOK_PLATE_4X5: _encode(crop_4x5(standard)),
    }
    return TreatedPhoto(standard=_encode(standard), alternates=alternates)
