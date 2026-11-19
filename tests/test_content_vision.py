"""Tests for the content pipeline slice 1c vision pass.

The vision callable is exercised against an injected post_json (no
Ollama), prompt assembly against plain dicts, and the generator
against a stub client + fake chat + fake vision: the live wiring is
proven by the on-box end-to-end run, these tests pin the payload
shape, the prompt contract, and the failure behavior.
"""

from __future__ import annotations

import base64
import struct
import unittest

from freetalon.content.generator import ContentGenerator
from freetalon.content.prompts import blog_prompt, entry_context, instagram_prompt
from freetalon.content.queue_client import ContentQueueError
from freetalon.content.vision import (
    MAX_DESCRIPTION_WORDS,
    cap_words,
    clean_description,
    make_ollama_vision,
)
from freetalon.orchestrator.intake import LLMSettings


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


class FakePostJson:
    """Stands in for intake._post_json: records calls, replays one
    scripted response (or raises it)."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def __call__(self, url, payload, timeout_seconds=None):
        self.calls.append((url, payload, timeout_seconds))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _vision_settings() -> LLMSettings:
    return LLMSettings(model="gemma3:27b", timeout_seconds=600.0, temperature=0.2, num_ctx=8192)


class VisionCallTests(unittest.TestCase):
    def test_payload_shape_and_description(self):
        post = FakePostJson({"message": {"content": "A red coupe on a track."}})
        describe = make_ollama_vision(_vision_settings(), post_json=post)
        raw = _png_bytes(64, 48)
        self.assertEqual(describe(raw), "A red coupe on a track.")
        url, payload, timeout = post.calls[0]
        self.assertTrue(url.endswith("/api/chat"))
        self.assertEqual(payload["model"], "gemma3:27b")
        self.assertNotIn("format", payload)
        self.assertNotIn("think", payload)
        self.assertEqual(payload["options"]["temperature"], 0.2)
        user = payload["messages"][-1]
        self.assertEqual(user["images"], [base64.b64encode(raw).decode("ascii")])
        # The prompt demands visible-only facts and forbids invention.
        self.assertIn("only what is", user["content"])
        self.assertIn("Do not invent", user["content"])
        self.assertEqual(timeout, 600.0)

    def test_word_cap_applies(self):
        long_text = " ".join(f"word{i}" for i in range(200))
        post = FakePostJson({"message": {"content": long_text}})
        describe = make_ollama_vision(_vision_settings(), post_json=post)
        result = describe(_png_bytes(8, 8))
        self.assertEqual(len(result.split()), MAX_DESCRIPTION_WORDS)
        self.assertTrue(result.startswith("word0 word1"))

    def test_cap_words_helper(self):
        self.assertEqual(cap_words("one two three", limit=2), "one two")
        self.assertEqual(cap_words("one two", limit=5), "one two")

    def test_clean_description_strips_fence(self):
        self.assertEqual(clean_description("```\nA blue sedan.\n```"), "A blue sedan.")

    def test_empty_content_raises(self):
        post = FakePostJson({"message": {"content": "   "}})
        describe = make_ollama_vision(_vision_settings(), post_json=post)
        with self.assertRaises(ContentQueueError):
            describe(_png_bytes(8, 8))

    def test_missing_content_raises(self):
        post = FakePostJson({"message": {}})
        describe = make_ollama_vision(_vision_settings(), post_json=post)
        with self.assertRaises(ContentQueueError):
            describe(_png_bytes(8, 8))


def _entry_with(media, blurb="Shakedown day at the home track."):
    return {"id": "e1", "blurb": blurb, "outputs": ["blog"], "media": media}


class PromptAssemblyTests(unittest.TestCase):
    def test_vision_block_rendered_when_present(self):
        media = [
            {
                "kind": "image",
                "filename": "stored-1.jpg",
                "originalName": "pit-lane.jpg",
                "vision": "A red coupe parked in a pit lane.",
            }
        ]
        prompt = blog_prompt(_entry_with(media))
        self.assertIn("What the photos show (from vision):", prompt)
        self.assertIn("- pit-lane.jpg: A red coupe parked in a pit lane.", prompt)
        self.assertIn("do not extend them", prompt)

    def test_prompts_byte_identical_without_vision(self):
        plain = [
            {"kind": "image", "filename": "a.jpg", "originalName": "a.jpg", "size": 1024}
        ]
        with_none = [dict(plain[0], vision=None)]
        with_empty = [dict(plain[0], vision="   ")]
        for variant in (with_none, with_empty):
            self.assertEqual(blog_prompt(_entry_with(plain)), blog_prompt(_entry_with(variant)))
            self.assertEqual(
                instagram_prompt(_entry_with(plain)), instagram_prompt(_entry_with(variant))
            )
            self.assertEqual(entry_context(_entry_with(plain)), entry_context(_entry_with(variant)))
        self.assertNotIn("from vision", blog_prompt(_entry_with(plain)))

    def test_multiple_images_each_keyed_by_name(self):
        media = [
            {"kind": "image", "filename": "s1.jpg", "originalName": "one.jpg", "vision": "First."},
            {"kind": "image", "filename": "s2.jpg", "originalName": "two.jpg", "vision": "Second."},
            {"kind": "video", "filename": "s3.mp4", "originalName": "three.mp4"},
        ]
        context = entry_context(_entry_with(media))
        self.assertIn("- one.jpg: First.", context)
        self.assertIn("- two.jpg: Second.", context)
        self.assertNotIn("three.mp4:", context.split("What the photos show")[1])


class StubClient:
    """In-memory stand-in for ContentQueueClient (mirrors the 1b
    test stub; fetch_media serves real PNG bytes)."""

    def __init__(self, entries):
        self._entries = entries
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
        return _png_bytes(4032, 3024)


class FakeChat:
    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    def __call__(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        return self._responses.pop(0)


class FakeVision:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def __call__(self, raw):
        self.calls.append(raw)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _gen_entry(media, outputs=("blog",)):
    return {
        "id": "e1",
        "blurb": "Shakedown day at the home track.",
        "outputs": list(outputs),
        "media": media,
    }


_IMAGE = {"kind": "image", "filename": "car.jpg", "originalName": "car.jpg", "size": 2048}


class GeneratorVisionTests(unittest.TestCase):
    def test_vision_description_reaches_prompt(self):
        client = StubClient([_gen_entry([dict(_IMAGE)])])
        chat = FakeChat(["# Post\n\nBody."])
        vision = FakeVision("A bright red coupe under pit-lane lights.")
        results = ContentGenerator(client, chat, vision=vision).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(len(vision.calls), 1)
        self.assertIn("A bright red coupe under pit-lane lights.", chat.prompts[0])
        self.assertIn("What the photos show (from vision):", chat.prompts[0])
        # Dimensions enrichment still happens alongside vision.
        self.assertIn("4032x3024", chat.prompts[0])

    def test_vision_failure_falls_back_without_failing_entry(self):
        client = StubClient([_gen_entry([dict(_IMAGE)])])
        chat = FakeChat(["# Post\n\nBody."])
        vision = FakeVision(RuntimeError("vision model unavailable"))
        with self.assertLogs("freetalon.content", level="WARNING") as logs:
            results = ContentGenerator(client, chat, vision=vision).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(client.failed, {})
        self.assertNotIn("from vision", chat.prompts[0])
        self.assertTrue(any("drafting from metadata only" in line for line in logs.output))

    def test_vision_empty_description_falls_back(self):
        client = StubClient([_gen_entry([dict(_IMAGE)])])
        chat = FakeChat(["# Post\n\nBody."])
        vision = FakeVision("   ")
        results = ContentGenerator(client, chat, vision=vision).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertNotIn("from vision", chat.prompts[0])

    def test_video_media_never_triggers_vision(self):
        video = {"kind": "video", "filename": "lap.mp4", "originalName": "lap.mp4", "size": 4096}
        client = StubClient([_gen_entry([video])])
        chat = FakeChat(["# Post\n\nBody."])
        vision = FakeVision("should never be seen")
        results = ContentGenerator(client, chat, vision=vision).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(vision.calls, [])
        self.assertEqual(client.fetches, [])

    def test_no_vision_configured_prompts_unchanged(self):
        client = StubClient([_gen_entry([dict(_IMAGE)])])
        chat = FakeChat(["# Post\n\nBody."])
        results = ContentGenerator(client, chat).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertNotIn("from vision", chat.prompts[0])
        self.assertIn("4032x3024", chat.prompts[0])

    def test_multiple_images_described_each(self):
        media = [
            dict(_IMAGE),
            {"kind": "image", "filename": "wheel.jpg", "originalName": "wheel.jpg", "size": 1024},
        ]
        client = StubClient([_gen_entry(media)])
        chat = FakeChat(["# Post\n\nBody."])
        vision = FakeVision("A wheel close-up.")
        results = ContentGenerator(client, chat, vision=vision).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        self.assertEqual(len(vision.calls), 2)
        self.assertIn("- car.jpg: A wheel close-up.", chat.prompts[0])
        self.assertIn("- wheel.jpg: A wheel close-up.", chat.prompts[0])


if __name__ == "__main__":
    unittest.main()
