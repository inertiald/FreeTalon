"""Tests for the content pipeline generated video (YouTube V1).

Pins for the deterministic filter graph and command line, the
Pillow title card, the queue client's video upload, and the
generator wiring: the video renders from the PLATE-treated stills
in media order, uploads before the drafts submit, and is advisory
throughout - a render or upload failure never holds an entry's
text drafts or treated photos back.
"""

import io
import json
import shutil
import unittest

from PIL import Image

from freetalon.content import video
from freetalon.content.generator import ContentGenerator
from freetalon.content.photos import TreatedPhoto
from freetalon.content.queue_client import ContentQueueClient, ContentQueueError
from freetalon.content.video import RenderedVideo


def _jpeg_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _solid(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", size, color)


class FilterGraphTests(unittest.TestCase):
    def test_expected_duration(self):
        self.assertEqual(video.expected_duration(1), 8.5)
        self.assertEqual(video.expected_duration(3), 18.5)
        self.assertEqual(video.expected_duration(5), 28.5)
        with self.assertRaises(ValueError):
            video.expected_duration(0)

    def test_graph_shape_for_three_stills(self):
        graph = video.build_filter_graph(3)
        # One xfade per segment join (title->s1, s1->s2, s2->s3),
        # offsets land where each preceding segment's clear time ends.
        self.assertEqual(graph.count("xfade=transition=fade:duration=1.0"), 3)
        self.assertIn("offset=2.50", graph)
        self.assertIn("offset=7.50", graph)
        self.assertIn("offset=12.50", graph)
        # Ken Burns on every still, alternating push-in / pull-back.
        self.assertEqual(graph.count("zoompan"), 3)
        self.assertIn("z='1+0.15*on/179'", graph)
        self.assertIn("z='1.15-0.15*on/179'", graph)
        # Head/tail fades and the silent audio trim to full length.
        self.assertIn("fade=t=in:st=0:d=0.5", graph)
        self.assertIn("fade=t=out:st=17.50:d=1.0", graph)
        self.assertIn("[4:a]atrim=0:18.50", graph)
        self.assertTrue(graph.endswith("[aout]") or "[vout]" in graph)

    def test_graph_rejects_zero_stills(self):
        with self.assertRaises(ValueError):
            video.build_filter_graph(0)

    def test_command_shape(self):
        command = video.build_command(
            "card.png", ["a.jpg", "b.jpg"], "out.mp4", ffmpeg="ffmpeg"
        )
        self.assertEqual(command[:3], ["nice", "-n", "10"])
        joined = " ".join(command)
        self.assertIn("libx264", joined)
        self.assertIn("-crf 20", joined)
        self.assertIn("-maxrate 12M", joined)
        self.assertIn("anullsrc=channel_layout=stereo:sample_rate=44100", joined)
        self.assertIn("-map [vout]", joined)
        self.assertIn("-map [aout]", joined)
        self.assertIn("+faststart", joined)


class TitleCardTests(unittest.TestCase):
    def test_card_is_a_dark_1080p_png(self):
        data = video.render_title_card("Midnight at Daikoku Futo")
        self.assertEqual(data[:4], b"\x89PNG")
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.size, (1920, 1080))
        # The plate register: near-black overall.
        pixels = image.resize((48, 27)).convert("L")
        mean = sum(pixels.getdata()) / (48 * 27)
        self.assertLess(mean, 40)

    def test_card_is_deterministic(self):
        self.assertEqual(
            video.render_title_card("Same Title"), video.render_title_card("Same Title")
        )

    def test_long_title_wraps_without_error(self):
        data = video.render_title_card(
            "A Very Long Generated Title About Driving the Mountain "
            "Roads Above the City at Night With Friends"
        )
        self.assertEqual(Image.open(io.BytesIO(data)).size, (1920, 1080))


class VideoUploadClientTests(unittest.TestCase):
    def _client(self, status=200, body=b'{"entry": {}}'):
        calls = []

        def transport(method, url, headers, payload):
            calls.append((method, url, headers, payload))
            return status, body

        client = ContentQueueClient("http://site.test", "sekrit", transport=transport)
        return client, calls

    def test_put_shape_and_metadata_headers(self):
        client, calls = self._client()
        client.submit_video(
            "e1",
            "inertiald-video.mp4",
            b"\x00\x00\x00\x18ftyp",
            duration_seconds=18.5,
            width=1920,
            height=1080,
        )
        (method, url, headers, payload), = calls
        self.assertEqual(method, "PUT")
        self.assertEqual(
            url, "http://site.test/api/internal/content/e1/video/inertiald-video.mp4"
        )
        self.assertEqual(headers["Authorization"], "Bearer " + "sekrit")
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["X-Video-Duration-Seconds"], "18.50")
        self.assertEqual(headers["X-Video-Width"], "1920")
        self.assertEqual(headers["X-Video-Height"], "1080")
        self.assertEqual(payload, b"\x00\x00\x00\x18ftyp")

    def test_error_status_carries_server_message(self):
        body = json.dumps({"error": "entry is not processing"}).encode()
        client, _ = self._client(status=409, body=body)
        with self.assertRaises(ContentQueueError) as ctx:
            client.submit_video("e1", "inertiald-video.mp4", b"x")
        self.assertIn("409", str(ctx.exception))
        self.assertIn("not processing", str(ctx.exception))


class _StubClient:
    """Queue-client double recording every call in order, with
    per-filename media bytes so media order is observable."""

    def __init__(self, entries, media_by_name, video_error=None):
        self._entries = entries
        self._media_by_name = media_by_name
        self._video_error = video_error
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
        return self._media_by_name[filename]

    def submit_treated_media(self, entry_id, look, filename, data):
        self.calls.append(("treated", entry_id, look, filename, data))
        return {}

    def submit_video(self, entry_id, filename, data, **metadata):
        self.calls.append(("video", entry_id, filename, data, metadata))
        if self._video_error is not None:
            raise self._video_error
        return {}


def _entry(outputs, filenames=("one.png", "two.png")):
    return {
        "id": "e1",
        "blurb": "Test entry.",
        "outputs": outputs,
        "media": [
            {"kind": "image", "filename": name, "originalName": name, "size": 10}
            for name in filenames
        ],
    }


def _chat(system_prompt, user_prompt):
    return "Shakedown Run\n\nThe description body."


def _echo_photos(data: bytes) -> TreatedPhoto:
    return TreatedPhoto(standard=b"STD:" + data)


class GeneratorVideoWiringTests(unittest.TestCase):
    def setUp(self):
        self.media = {"one.png": b"raw-one", "two.png": b"raw-two"}

    def _generator(self, client, videos):
        return ContentGenerator(client, _chat, photos=_echo_photos, videos=videos)

    def test_video_renders_from_treated_stills_in_order_and_uploads_first(self):
        seen = {}

        def fake_videos(stills, title):
            seen["stills"] = stills
            seen["title"] = title
            return RenderedVideo(
                data=b"MP4DATA", duration_seconds=13.5, width=1920, height=1080
            )

        client = _StubClient([_entry(["youtube"])], self.media)
        results = self._generator(client, fake_videos).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(seen["stills"], [b"STD:raw-one", b"STD:raw-two"])
        self.assertEqual(seen["title"], "Shakedown Run")
        video_calls = [c for c in client.calls if c[0] == "video"]
        self.assertEqual(len(video_calls), 1)
        _, _, filename, data, metadata = video_calls[0]
        self.assertEqual(filename, "inertiald-video.mp4")
        self.assertEqual(data, b"MP4DATA")
        self.assertEqual(metadata["duration_seconds"], 13.5)
        kinds = [c[0] for c in client.calls]
        self.assertLess(kinds.index("video"), kinds.index("submit"))

    def test_render_failure_never_holds_the_entry_back(self):
        def broken_videos(stills, title):
            raise RuntimeError("ffmpeg exploded")

        client = _StubClient([_entry(["youtube"])], self.media)
        results = self._generator(client, broken_videos).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertIn("youtube", client.submitted["e1"])
        self.assertEqual([c for c in client.calls if c[0] == "video"], [])
        # The treated photos still uploaded.
        self.assertTrue([c for c in client.calls if c[0] == "treated"])

    def test_upload_failure_never_holds_the_entry_back(self):
        def ok_videos(stills, title):
            return RenderedVideo(data=b"MP4DATA")

        client = _StubClient(
            [_entry(["youtube"])],
            self.media,
            video_error=ContentQueueError("site said no"),
        )
        results = self._generator(client, ok_videos).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertIn("youtube", client.submitted["e1"])

    def test_no_youtube_output_no_video(self):
        called = []

        def spy_videos(stills, title):
            called.append((stills, title))
            return RenderedVideo(data=b"MP4DATA")

        client = _StubClient([_entry(["blog"])], self.media)
        results = self._generator(client, spy_videos).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(called, [])

    def test_no_treated_stills_no_video(self):
        called = []

        def spy_videos(stills, title):
            called.append((stills, title))
            return RenderedVideo(data=b"MP4DATA")

        client = _StubClient([_entry(["youtube"])], self.media)
        generator = ContentGenerator(client, _chat, videos=spy_videos)  # no photos fn
        results = generator.run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(called, [])


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not on PATH"
)
class RealRenderTests(unittest.TestCase):
    def test_render_two_stills_probes_to_spec(self):
        stills = [
            _jpeg_bytes(_solid((1600, 1200), (120, 60, 30))),
            _jpeg_bytes(_solid((1200, 1600), (30, 60, 120))),
        ]
        result = video.render_video(stills, "Shakedown Run")
        self.assertEqual(result.filename, "inertiald-video.mp4")
        self.assertEqual((result.width, result.height), (1920, 1080))
        self.assertAlmostEqual(
            result.duration_seconds, video.expected_duration(2), delta=1.0
        )
        self.assertGreater(len(result.data), 50_000)  # a real MP4, not a stub

    def test_render_rejects_no_stills(self):
        with self.assertRaises(ValueError):
            video.render_video([], "Nothing")


if __name__ == "__main__":
    unittest.main()
