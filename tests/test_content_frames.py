"""Tests for video frame extraction and its vision wiring.

Three layers, mirroring the video render tests: the extraction
unit tests fake subprocess (no ffmpeg, no video files), the
generator tests use a stub client + fake frames + fake vision
(no Ollama - the model calls are faked exactly as the rest of
the content suite fakes them), and one skipUnless smoke class
runs the real ffmpeg on a generated clip.

The contract being pinned: a video media item with a frame
extractor configured gets a 'vision' description built from its
frames, in the same shape a photo's description takes; every
failure - fetch, extraction, vision - degrades the item to
metadata-only and never fails the entry.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from freetalon.content import frames
from freetalon.content.generator import ContentGenerator
from freetalon.content.media_info import image_dimensions
from freetalon.content.queue_client import ContentQueueError

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=""
    )


class TimestampTests(unittest.TestCase):
    def test_unknown_duration_earns_first_frame_only(self):
        self.assertEqual(frames.frame_timestamps(None), [0.0])
        self.assertEqual(frames.frame_timestamps(0.0), [0.0])
        self.assertEqual(frames.frame_timestamps(-3.0), [0.0])

    def test_evenly_spaced_first_plus_samples(self):
        self.assertEqual(
            frames.frame_timestamps(40.0), [0.0, 10.0, 20.0, 30.0]
        )

    def test_last_sample_never_at_the_very_end(self):
        stamps = frames.frame_timestamps(9.0)
        self.assertEqual(len(stamps), frames.MAX_FRAMES)
        self.assertLess(stamps[-1], 9.0)
        self.assertEqual(stamps[0], 0.0)

    def test_max_frames_argument_respected(self):
        self.assertEqual(frames.frame_timestamps(10.0, max_frames=2), [0.0, 5.0])
        self.assertEqual(frames.frame_timestamps(10.0, max_frames=1), [0.0])


class ExtractCommandTests(unittest.TestCase):
    def test_command_shape(self):
        command = frames.build_extract_command(
            "in.mp4", 10.0, "out.jpg", ffmpeg="ffmpeg"
        )
        self.assertEqual(command[:3], ["nice", "-n", "10"])
        joined = " ".join(command)
        self.assertIn("-ss 10.000", joined)
        self.assertIn("-frames:v 1", joined)
        self.assertIn("min(1280,iw)", joined)
        self.assertIn("-f image2", joined)
        self.assertEqual(command[-1], "out.jpg")
        # The seek must precede the input (fast seek).
        self.assertLess(command.index("-ss"), command.index("-i"))


class ProbeDurationTests(unittest.TestCase):
    def test_parses_format_duration(self):
        with mock.patch(
            "subprocess.run",
            return_value=_completed(stdout='{"format": {"duration": "12.5"}}'),
        ):
            self.assertEqual(frames.probe_duration("x.mp4"), 12.5)

    def test_failures_degrade_to_none(self):
        cases = [
            _completed(returncode=1),
            _completed(stdout="not json"),
            _completed(stdout='{"format": {"duration": "0"}}'),
            _completed(stdout='{"format": {}}'),
        ]
        for result in cases:
            with mock.patch("subprocess.run", return_value=result):
                self.assertIsNone(frames.probe_duration("x.mp4"))
        with mock.patch("subprocess.run", side_effect=OSError("no ffprobe")):
            self.assertIsNone(frames.probe_duration("x.mp4"))


def _fake_run(probe_stdout='{"format": {"duration": "40.0"}}', grab_behaviors=None):
    """A subprocess.run stand-in: answers the ffprobe call with
    `probe_stdout`; each ffmpeg grab consults grab_behaviors by
    call index ('ok' writes a JPEG, 'fail' exits 1, 'junk' writes
    non-JPEG bytes)."""
    state = {"grabs": 0}

    def run(command, **kwargs):
        if "-show_format" in command:
            return _completed(stdout=probe_stdout)
        index = state["grabs"]
        state["grabs"] += 1
        behavior = (grab_behaviors or ["ok"] * 8)[index]
        if behavior == "fail":
            return _completed(returncode=1)
        payload = _JPEG if behavior == "ok" else b"not a jpeg at all........"
        with open(command[-1], "wb") as fh:
            fh.write(payload)
        return _completed()

    return run


class ExtractFramesFakeTests(unittest.TestCase):
    def test_empty_input_rejected_without_subprocess(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(ValueError):
                frames.extract_frames(b"")
        run.assert_not_called()

    def test_extracts_all_samples_in_order(self):
        with mock.patch("subprocess.run", side_effect=_fake_run()):
            result = frames.extract_frames(b"video-bytes")
        self.assertEqual(len(result), frames.MAX_FRAMES)
        self.assertTrue(all(f[:2] == b"\xff\xd8" for f in result))

    def test_failed_sample_costs_only_that_frame(self):
        behaviors = ["ok", "fail", "ok", "ok"]
        with mock.patch(
            "subprocess.run", side_effect=_fake_run(grab_behaviors=behaviors)
        ):
            result = frames.extract_frames(b"video-bytes")
        self.assertEqual(len(result), 3)

    def test_non_jpeg_output_is_a_failed_sample(self):
        behaviors = ["junk", "ok", "ok", "ok"]
        with mock.patch(
            "subprocess.run", side_effect=_fake_run(grab_behaviors=behaviors)
        ):
            result = frames.extract_frames(b"video-bytes")
        self.assertEqual(len(result), 3)

    def test_no_frames_at_all_raises(self):
        with mock.patch(
            "subprocess.run",
            side_effect=_fake_run(grab_behaviors=["fail"] * 8),
        ):
            with self.assertRaises(RuntimeError):
                frames.extract_frames(b"video-bytes")

    def test_unprobable_duration_still_tries_first_frame(self):
        with mock.patch(
            "subprocess.run", side_effect=_fake_run(probe_stdout="garbage")
        ) as run:
            result = frames.extract_frames(b"video-bytes")
        self.assertEqual(len(result), 1)


# ---------- Generator wiring ----------


class StubClient:
    """In-memory stand-in for ContentQueueClient: fetch_media
    serves the bytes registered per filename (or raises the
    registered exception)."""

    def __init__(self, entries, media):
        self._entries = entries
        self._media = media
        self.claimed = []
        self.failed = {}
        self.submitted = {}
        self.fetches = []

    def list_received(self):
        return self._entries

    def claim(self, entry_id):
        self.claimed.append(entry_id)
        return {}

    def fail(self, entry_id, error):
        self.failed[entry_id] = error
        return {}

    def submit_drafts(self, entry_id, drafts):
        self.submitted[entry_id] = drafts
        return {}

    def fetch_media(self, entry_id, filename):
        self.fetches.append(filename)
        result = self._media[filename]
        if isinstance(result, Exception):
            raise result
        return result


class FakeChat:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    def __call__(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        return self._responses.pop(0)


class FakeVision:
    """One scripted result per call, in call order; an Exception
    result is raised instead of returned."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, raw):
        self.calls.append(raw)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeFrames:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def __call__(self, raw):
        self.calls.append(raw)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


_VIDEO_ITEM = {
    "kind": "video",
    "filename": "lap.mp4",
    "originalName": "lap.mp4",
    "size": 5_000_000,
}

_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128


def _entry(media, outputs=("blog",)):
    return {
        "id": "e1",
        "blurb": "Shakedown day at the home track.",
        "outputs": list(outputs),
        "media": media,
    }


def _run(media, frames_result, vision_results, media_map=None):
    entry = _entry(media)
    client = StubClient(
        [entry], media_map if media_map is not None else {"lap.mp4": _VIDEO_BYTES}
    )
    chat = FakeChat(["# Post\n\nBody."])
    vision = FakeVision(vision_results)
    frames_fn = FakeFrames(frames_result)
    generator = ContentGenerator(client, chat, vision=vision, frames=frames_fn)
    results = generator.run_once()
    return results, client, chat, vision, frames_fn


class GeneratorVideoVisionTests(unittest.TestCase):
    def test_video_frames_described_into_prompt(self):
        results, client, chat, vision, frames_fn = _run(
            [dict(_VIDEO_ITEM)],
            [b"frame-one", b"frame-two"],
            ["A red coupe leaving the pits.", "The same coupe mid-corner."],
        )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        # The extractor saw exactly the fetched video bytes; vision
        # saw exactly the extracted frames, in order.
        self.assertEqual(frames_fn.calls, [_VIDEO_BYTES])
        self.assertEqual(vision.calls, [b"frame-one", b"frame-two"])
        prompt = chat.prompts[0]
        self.assertIn("What the photos show (from vision):", prompt)
        self.assertIn(
            "- lap.mp4: A red coupe leaving the pits. The same coupe mid-corner.",
            prompt,
        )

    def test_extraction_failure_degrades_to_metadata_only(self):
        with self.assertLogs("freetalon.content", level="WARNING") as logs:
            results, client, chat, vision, frames_fn = _run(
                [dict(_VIDEO_ITEM)],
                RuntimeError("ffmpeg extracted no frames"),
                [],
            )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(client.failed, {})
        self.assertEqual(vision.calls, [])
        self.assertNotIn("from vision", chat.prompts[0])
        self.assertTrue(
            any("frame extraction failed" in line for line in logs.output)
        )

    def test_extractor_returning_no_frames_degrades(self):
        results, client, chat, vision, frames_fn = _run(
            [dict(_VIDEO_ITEM)], [], []
        )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(vision.calls, [])
        self.assertNotIn("from vision", chat.prompts[0])

    def test_vision_failure_on_every_frame_degrades(self):
        results, client, chat, vision, frames_fn = _run(
            [dict(_VIDEO_ITEM)],
            [b"frame-one", b"frame-two"],
            [RuntimeError("vision down"), RuntimeError("vision down")],
        )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertNotIn("from vision", chat.prompts[0])

    def test_partial_frame_descriptions_still_ground(self):
        results, client, chat, vision, frames_fn = _run(
            [dict(_VIDEO_ITEM)],
            [b"frame-one", b"frame-two"],
            [RuntimeError("vision down"), "A coupe mid-corner, tires loaded."],
        )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertIn("- lap.mp4: A coupe mid-corner, tires loaded.", chat.prompts[0])

    def test_fetch_failure_degrades_without_extracting(self):
        results, client, chat, vision, frames_fn = _run(
            [dict(_VIDEO_ITEM)],
            [b"frame-one"],
            ["unused"],
            media_map={"lap.mp4": ContentQueueError("media fetch -> 500")},
        )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(frames_fn.calls, [])
        self.assertEqual(vision.calls, [])
        self.assertNotIn("from vision", chat.prompts[0])

    def test_no_frames_configured_video_untouched(self):
        entry = _entry([dict(_VIDEO_ITEM)])
        client = StubClient([entry], {"lap.mp4": _VIDEO_BYTES})
        chat = FakeChat(["# Post\n\nBody."])
        vision = FakeVision(["should never be seen"])
        results = ContentGenerator(client, chat, vision=vision).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(client.fetches, [])
        self.assertEqual(vision.calls, [])
        self.assertNotIn("from vision", chat.prompts[0])

    def test_mixed_entry_describes_photo_and_video(self):
        image = {
            "kind": "image",
            "filename": "car.jpg",
            "originalName": "car.jpg",
            "size": 2048,
        }
        png_header = (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        )
        results, client, chat, vision, frames_fn = _run(
            [image, dict(_VIDEO_ITEM)],
            [b"frame-one"],
            ["A red coupe in the pits.", "The coupe out on track."],
            media_map={"lap.mp4": _VIDEO_BYTES, "car.jpg": png_header},
        )
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        prompt = chat.prompts[0]
        self.assertIn("- car.jpg: A red coupe in the pits.", prompt)
        self.assertIn("- lap.mp4: The coupe out on track.", prompt)


# ---------- Real ffmpeg smoke ----------


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe not on PATH",
)
class RealExtractionTests(unittest.TestCase):
    def _make_clip(self, duration=2):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clip.mp4")
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i",
                    f"testsrc=duration={duration}:size=320x240:rate=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", path,
                ],
                check=True, capture_output=True, timeout=120,
            )
            with open(path, "rb") as fh:
                return fh.read()

    def test_extracts_real_frames(self):
        result = frames.extract_frames(self._make_clip())
        self.assertEqual(len(result), frames.MAX_FRAMES)
        for frame in result:
            self.assertEqual(frame[:2], b"\xff\xd8")
            self.assertEqual(image_dimensions(frame), (320, 240))

    def test_corrupt_bytes_raise(self):
        with self.assertRaises(RuntimeError):
            frames.extract_frames(b"this is not a video file at all")


if __name__ == "__main__":
    unittest.main()
