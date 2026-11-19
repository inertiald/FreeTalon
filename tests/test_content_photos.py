"""Tests for the content pipeline photo standard (slice 1d).

Pixel-level pins for the four treatments (expectations derived from
the published treatment parameters, not from the implementation),
plus process_photo structure/orientation/determinism, the queue
client's treated-media upload, and the generator wiring: vision
still sees the original bytes, a treatment failure ships the photo
untreated, and an upload failure never holds the drafts back.
"""

import io
import json
import unittest

from PIL import Image, ImageStat

from freetalon.content import photos
from freetalon.content.generator import ContentGenerator, treated_filename
from freetalon.content.photos import TreatedPhoto
from freetalon.content.queue_client import ContentQueueClient, ContentQueueError


def _jpeg_bytes(image: Image.Image, **save_kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95, **save_kwargs)
    return buffer.getvalue()


def _solid(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", size, color)


def _plate_spec_gray(v: int) -> tuple[int, int, int]:
    """Expected PLATE output for a gray pixel, computed straight
    from the treatment parameters: black-point 14 levels, S-curve
    a=0.35, saturation x0.80 (a no-op on gray), then the highlight
    warm shift scaled by (lum/255)^2."""
    level = max(0, min(255, round((v - 14) * 255.0 / (255 - 14))))
    t = level / 255.0
    lut = max(0, min(255, round((t + 0.35 * t * (1 - t) * (t - 0.5) * 4) * 255)))
    w = round(255 * (lut / 255.0) ** 2)
    add_r = round(w * 5 / 255.0)
    add_g = round(w * 1 / 255.0)
    sub_b = round(w * 5 / 255.0)
    return (min(255, lut + add_r), min(255, lut + add_g), max(0, lut - sub_b))


class PlateTreatmentTests(unittest.TestCase):
    def test_gray_ramp_matches_spec(self):
        for v in (0, 14, 15, 64, 128, 200, 255):
            out = photos.plate(_solid((3, 3), (v, v, v))).getpixel((1, 1))
            self.assertEqual(out, _plate_spec_gray(v), f"gray {v}")

    def test_mid_gray_darkens(self):
        r, g, b = photos.plate(_solid((3, 3), (128, 128, 128))).getpixel((1, 1))
        self.assertLess((r + g + b) / 3, 128)

    def test_saturation_reduced(self):
        r, g, b = photos.plate(_solid((3, 3), (255, 0, 0))).getpixel((1, 1))
        self.assertLess(r, 255)
        self.assertEqual(g, b)
        self.assertGreater(g, 0)


class CleanTreatmentTests(unittest.TestCase):
    def test_uniform_gray_unchanged(self):
        out = photos.documentary_clean(_solid((6, 6), (100, 100, 100)))
        self.assertEqual(out.getpixel((3, 3)), (100, 100, 100))

    def test_gray_world_balances_channel_means(self):
        image = Image.new("RGB", (10, 10))
        image.paste((200, 100, 50), (0, 0, 5, 10))
        image.paste((100, 50, 200), (5, 0, 10, 10))

        def spread(im: Image.Image) -> float:
            means = ImageStat.Stat(im).mean
            return max(means) - min(means)

        self.assertLess(spread(photos.documentary_clean(image)), spread(image))


class WarmFilmTreatmentTests(unittest.TestCase):
    def test_black_lifts_to_16(self):
        out = photos.warm_film(_solid((3, 3), (0, 0, 0))).getpixel((1, 1))
        self.assertEqual(out, (16, 16, 16))

    def test_white_warms(self):
        r, g, b = photos.warm_film(_solid((3, 3), (255, 255, 255))).getpixel((1, 1))
        self.assertGreater(r, b)


class MonoTreatmentTests(unittest.TestCase):
    def test_output_is_grayscale(self):
        image = Image.new("RGB", (4, 4))
        image.paste((200, 30, 90), (0, 0, 2, 4))
        image.paste((10, 220, 130), (2, 0, 4, 4))
        out = photos.mono(image)
        for x in range(4):
            for y in range(4):
                r, g, b = out.getpixel((x, y))
                self.assertEqual(r, g)
                self.assertEqual(g, b)

    def test_pure_red_maps_to_spec_luma(self):
        # L(255,0,0) = 76 (ITU-R 601-2); stretch (76-8)*255/239 = 73;
        # a uniform frame's contrast step is a no-op.
        out = photos.mono(_solid((3, 3), (255, 0, 0))).getpixel((1, 1))
        self.assertEqual(out, (73, 73, 73))


class CropTests(unittest.TestCase):
    def test_square_crops_at_subject_anchor(self):
        image = Image.new("RGB", (1000, 1000), (10, 10, 10))
        image.paste((250, 250, 250), (60, 0, 61, 1000))  # marker column
        out = photos.crop_4x5(image)
        self.assertEqual(out.size, (800, 1000))
        # Window starts at round(0.46*1000 - 400) = 60: the marker
        # column lands on the crop's left edge.
        self.assertEqual(out.getpixel((0, 500)), (250, 250, 250))

    def test_tall_frame_crops_vertically(self):
        out = photos.crop_4x5(Image.new("RGB", (600, 1200)))
        self.assertEqual(out.size, (600, 750))


class ProcessPhotoTests(unittest.TestCase):
    def test_structure_and_encoding(self):
        data = _jpeg_bytes(_solid((2000, 1000), (120, 80, 40)))
        result = photos.process_photo(data)
        self.assertIsInstance(result, TreatedPhoto)
        self.assertEqual(
            list(result.alternates.keys()),
            ["clean", "warm-film", "mono", "plate-4x5"],
        )
        for payload in [result.standard, *result.alternates.values()]:
            self.assertEqual(payload[:2], b"\xff\xd8")  # JPEG magic
        standard = Image.open(io.BytesIO(result.standard))
        self.assertEqual(standard.size, (1600, 800))  # max-edge cap
        crop = Image.open(io.BytesIO(result.alternates["plate-4x5"]))
        self.assertAlmostEqual(crop.size[0] / crop.size[1], 0.8, places=2)

    def test_deterministic(self):
        data = _jpeg_bytes(_solid((64, 48), (90, 140, 200)))
        first = photos.process_photo(data)
        second = photos.process_photo(data)
        self.assertEqual(first.standard, second.standard)
        self.assertEqual(first.alternates, second.alternates)

    def test_exif_orientation_applied(self):
        exif = Image.Exif()
        exif[274] = 6  # stored sideways; viewers rotate 90 CW
        data = _jpeg_bytes(_solid((4, 2), (10, 20, 30)), exif=exif.tobytes())
        self.assertEqual(photos.load_photo(data).size, (2, 4))
        standard = Image.open(io.BytesIO(photos.process_photo(data).standard))
        self.assertEqual(standard.size, (2, 4))

    def test_undecodable_input_raises(self):
        with self.assertRaises(Exception):
            photos.process_photo(b"this is not an image")


class TreatedFilenameTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(treated_filename("IMG_4824.jpeg"), "IMG_4824.jpg")
        self.assertEqual(treated_filename("photo.png"), "photo.jpg")
        self.assertEqual(treated_filename("shakedown.jpg"), "shakedown.jpg")
        self.assertEqual(treated_filename("archive.tar.jpg"), "archive.tar.jpg")
        self.assertEqual(treated_filename("noext"), "noext.jpg")


class TreatedUploadClientTests(unittest.TestCase):
    def _client(self, status=200, body=b'{"entry": {}}'):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, headers, payload))
            return status, body

        client = ContentQueueClient("http://site.test", "sekrit", transport=transport)
        return client, calls

    def test_put_shape(self):
        client, calls = self._client()
        client.submit_treated_media("e1", "plate-4x5", "IMG_4824.jpg", b"\xff\xd8data")
        (method, url, headers, payload), = calls
        self.assertEqual(method, "PUT")
        self.assertEqual(
            url,
            "http://site.test/api/internal/content/e1/treated/plate-4x5/IMG_4824.jpg",
        )
        self.assertEqual(headers["Authorization"], "Bearer sekrit")
        self.assertEqual(headers["Content-Type"], "image/jpeg")
        self.assertEqual(payload, b"\xff\xd8data")

    def test_error_status_carries_server_message(self):
        body = json.dumps({"error": "Cannot add treated media at 'received'"}).encode()
        client, _ = self._client(status=409, body=body)
        with self.assertRaises(ContentQueueError) as ctx:
            client.submit_treated_media("e1", "standard", "a.jpg", b"x")
        self.assertIn("409", str(ctx.exception))
        self.assertIn("Cannot add treated media", str(ctx.exception))


class _StubClient:
    """Queue-client double recording every call in order."""

    def __init__(self, entries, media: bytes, treated_error: Exception | None = None):
        self._entries = entries
        self._media = media
        self._treated_error = treated_error
        self.calls: list[tuple] = []
        self.submitted: dict[str, dict] = {}
        self.failed: dict[str, str] = {}

    def list_received(self):
        return self._entries

    def claim(self, entry_id):
        self.calls.append(("claim", entry_id))
        return {}

    def fail(self, entry_id, error):
        self.failed[entry_id] = error
        return {}

    def submit_drafts(self, entry_id, drafts):
        self.calls.append(("submit", entry_id))
        self.submitted[entry_id] = drafts
        return {}

    def fetch_media(self, entry_id, filename):
        self.calls.append(("fetch", entry_id, filename))
        return self._media

    def submit_treated_media(self, entry_id, look, filename, data):
        self.calls.append(("treated", entry_id, look, filename, data))
        if self._treated_error is not None:
            raise self._treated_error
        return {}


def _entry(entry_id="e1", kind="image", filename="shakedown.png"):
    return {
        "id": entry_id,
        "blurb": "Test entry.",
        "outputs": ["blog"],
        "media": [
            {
                "kind": kind,
                "filename": filename,
                "originalName": filename,
                "size": 1234,
            }
        ],
    }


def _chat(system_prompt, user_prompt):
    return "# A Post\n\nBody text."


class GeneratorPhotoWiringTests(unittest.TestCase):
    def setUp(self):
        self.raw = _jpeg_bytes(_solid((120, 90), (150, 60, 30)))

    def test_uploads_precede_submit_and_vision_sees_original(self):
        seen: dict[str, bytes] = {}

        def fake_vision(data: bytes) -> str:
            seen["vision"] = data
            return "A rust-red coupe on a driveway."

        def fake_photos(data: bytes) -> TreatedPhoto:
            seen["photos"] = data
            return TreatedPhoto(
                standard=b"STD",
                alternates={"clean": b"CLN", "mono": b"MON"},
            )

        client = _StubClient([_entry()], self.raw)
        generator = ContentGenerator(
            client, _chat, vision=fake_vision, photos=fake_photos
        )
        results = generator.run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        # Vision and treatment both worked from the fetched original.
        self.assertEqual(seen["vision"], self.raw)
        self.assertEqual(seen["photos"], self.raw)
        treated_calls = [c for c in client.calls if c[0] == "treated"]
        self.assertEqual(
            [(c[2], c[3]) for c in treated_calls],
            [
                ("standard", "shakedown.jpg"),
                ("clean", "shakedown.jpg"),
                ("mono", "shakedown.jpg"),
            ],
        )
        kinds = [c[0] for c in client.calls]
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "treated"),
                        kinds.index("submit"))

    def test_real_processor_end_to_end_shape(self):
        client = _StubClient([_entry()], self.raw)
        generator = ContentGenerator(client, _chat, photos=photos.process_photo)
        results = generator.run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        looks = [c[2] for c in client.calls if c[0] == "treated"]
        self.assertEqual(
            looks, ["standard", "clean", "warm-film", "mono", "plate-4x5"]
        )
        for call in client.calls:
            if call[0] == "treated":
                self.assertEqual(call[4][:2], b"\xff\xd8")

    def test_treatment_failure_ships_untreated(self):
        def broken_photos(data: bytes) -> TreatedPhoto:
            raise ValueError("cannot decode")

        client = _StubClient([_entry()], self.raw)
        generator = ContentGenerator(client, _chat, photos=broken_photos)
        results = generator.run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual([c for c in client.calls if c[0] == "treated"], [])
        self.assertIn("e1", client.submitted)
        self.assertEqual(client.failed, {})

    def test_upload_failure_never_blocks_drafts(self):
        def ok_photos(data: bytes) -> TreatedPhoto:
            return TreatedPhoto(standard=b"STD", alternates={})

        client = _StubClient(
            [_entry()], self.raw, treated_error=ContentQueueError("site said no")
        )
        generator = ContentGenerator(client, _chat, photos=ok_photos)
        results = generator.run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertIn("e1", client.submitted)
        self.assertEqual(client.failed, {})

    def test_videos_are_not_treated(self):
        calls: list[bytes] = []

        def spy_photos(data: bytes) -> TreatedPhoto:
            calls.append(data)
            return TreatedPhoto(standard=b"STD", alternates={})

        client = _StubClient(
            [_entry(kind="video", filename="run.mp4")], self.raw
        )
        generator = ContentGenerator(client, _chat, photos=spy_photos)
        results = generator.run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(calls, [])
        self.assertEqual([c for c in client.calls if c[0] == "fetch"], [])


if __name__ == "__main__":
    unittest.main()
