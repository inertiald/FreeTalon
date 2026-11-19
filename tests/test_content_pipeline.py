"""Tests for the content pipeline slice 1b (freetalon.content).

The queue client is exercised against a fake transport (no network),
the generator against a stub client + fake chat (no Ollama): the live
wiring is proven by the on-box end-to-end run, these tests pin the
shapes and the failure behavior.
"""

from __future__ import annotations

import json
import struct
import unittest

from freetalon.content.generator import (
    ContentGenerator,
    clean_draft,
    parse_youtube_draft,
)
from freetalon.content.media_info import image_dimensions
from freetalon.content.prompts import blog_prompt, entry_context
from freetalon.content.queue_client import ContentQueueClient, ContentQueueError


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def _jpeg_bytes(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"\x00" * 14
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0


class FakeTransport:
    """Pops scripted (status, payload) responses; records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        status, payload = self._responses.pop(0)
        if isinstance(payload, (bytes, bytearray)):
            return status, bytes(payload)
        return status, json.dumps(payload).encode("utf-8")


class QueueClientTests(unittest.TestCase):
    def test_list_received_sends_token_and_parses(self):
        transport = FakeTransport([(200, {"entries": [{"id": "abc"}]})])
        client = ContentQueueClient("http://site:3000/", "tok123", transport=transport)
        entries = client.list_received()
        self.assertEqual(entries, [{"id": "abc"}])
        method, url, headers, _ = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://site:3000/api/internal/content")
        self.assertEqual(headers["Authorization"], "Bearer tok123")

    def test_claim_fail_submit_payloads(self):
        transport = FakeTransport([(200, {}), (200, {}), (200, {})])
        client = ContentQueueClient("http://site:3000", "tok", transport=transport)
        client.claim("e1")
        client.fail("e1", "boom")
        client.submit_drafts("e1", {"blog": "text"})
        bodies = [json.loads(call[3].decode("utf-8")) for call in transport.calls]
        self.assertEqual(bodies[0], {"action": "claim"})
        self.assertEqual(bodies[1], {"action": "fail", "error": "boom"})
        self.assertEqual(bodies[2], {"action": "submit", "drafts": {"blog": "text"}})
        for call in transport.calls:
            self.assertEqual(call[0], "PATCH")
            self.assertTrue(call[1].endswith("/api/internal/content/e1"))

    def test_401_raises(self):
        transport = FakeTransport([(401, {"error": "Unauthorized"})])
        client = ContentQueueClient("http://site:3000", "wrong", transport=transport)
        with self.assertRaises(ContentQueueError):
            client.list_received()

    def test_error_status_carries_server_message(self):
        transport = FakeTransport([(409, {"error": "Cannot claim"})])
        client = ContentQueueClient("http://site:3000", "tok", transport=transport)
        with self.assertRaises(ContentQueueError) as ctx:
            client.claim("e1")
        self.assertIn("Cannot claim", str(ctx.exception))

    def test_fetch_media_quotes_filename(self):
        transport = FakeTransport([(200, b"\x89PNG")])
        client = ContentQueueClient("http://site:3000", "tok", transport=transport)
        self.assertEqual(client.fetch_media("e1", "my photo.jpg"), b"\x89PNG")
        self.assertTrue(transport.calls[0][1].endswith("/media/my%20photo.jpg"))

    def test_empty_token_rejected(self):
        with self.assertRaises(ContentQueueError):
            ContentQueueClient("http://site:3000", "")


class MediaInfoTests(unittest.TestCase):
    def test_png(self):
        self.assertEqual(image_dimensions(_png_bytes(4032, 3024)), (4032, 3024))

    def test_jpeg(self):
        self.assertEqual(image_dimensions(_jpeg_bytes(1920, 1080)), (1920, 1080))

    def test_gif(self):
        data = b"GIF89a" + struct.pack("<HH", 480, 360) + b"\x00" * 8
        self.assertEqual(image_dimensions(data), (480, 360))

    def test_garbage_and_truncated(self):
        self.assertIsNone(image_dimensions(b"not an image at all"))
        self.assertIsNone(image_dimensions(b"\x89PNG\r\n\x1a\n\x00\x00"))
        self.assertIsNone(image_dimensions(b""))


class DraftShapingTests(unittest.TestCase):
    def test_clean_draft_strips_fence(self):
        self.assertEqual(clean_draft("```markdown\n# Title\n\nBody\n```"), "# Title\n\nBody")
        self.assertEqual(clean_draft("\n\n  plain text  \n\n"), "plain text")

    def test_parse_youtube_with_label(self):
        draft = parse_youtube_draft("TITLE: Night Run\n\nDesc line one.\n\nDesc two.")
        self.assertEqual(draft["title"], "Night Run")
        self.assertTrue(draft["description"].startswith("Desc line one."))

    def test_parse_youtube_without_label(self):
        draft = parse_youtube_draft("Plain Title\n\nThe description.")
        self.assertEqual(draft["title"], "Plain Title")
        self.assertEqual(draft["description"], "The description.")

    def test_parse_youtube_rejects_missing_description(self):
        with self.assertRaises(ValueError):
            parse_youtube_draft("TITLE: Only a title")

    def test_prompt_carries_blurb_media_and_dimensions(self):
        entry = {
            "blurb": "Shakedown at the home track day, new tires fitted.",
            "media": [
                {
                    "kind": "image",
                    "originalName": "shakedown.jpg",
                    "filename": "shakedown.jpg",
                    "size": 2_500_000,
                    "dimensions": (4032, 3024),
                }
            ],
            "outputs": ["blog"],
        }
        prompt = blog_prompt(entry)
        self.assertIn("Shakedown at the home track day", prompt)
        self.assertIn("shakedown.jpg", prompt)
        self.assertIn("4032x3024", prompt)
        self.assertIn("ONLY the facts", prompt)

    def test_context_without_blurb_or_media(self):
        context = entry_context({"blurb": "", "media": []})
        self.assertIn("(no notes provided)", context)


class StubClient:
    """In-memory stand-in for ContentQueueClient."""

    def __init__(self, entries, claim_errors=None):
        self._entries = entries
        self._claim_errors = claim_errors or {}
        self.claimed = []
        self.failed = {}
        self.submitted = {}

    def list_received(self):
        return self._entries

    def claim(self, entry_id):
        if entry_id in self._claim_errors:
            raise ContentQueueError(self._claim_errors[entry_id])
        self.claimed.append(entry_id)
        return {}

    def fail(self, entry_id, error):
        self.failed[entry_id] = error
        return {}

    def submit_drafts(self, entry_id, drafts):
        self.submitted[entry_id] = drafts
        return {}

    def fetch_media(self, entry_id, filename):
        return _png_bytes(4032, 3024)


class FakeChat:
    """Returns queued responses in call order; records prompts."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.prompts = []

    def __call__(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _entry(entry_id, outputs, **overrides):
    entry = {
        "id": entry_id,
        "blurb": "Shakedown at the home track day, new tires fitted.",
        "outputs": outputs,
        "media": [
            {
                "kind": "image",
                "filename": "shakedown.jpg",
                "originalName": "shakedown.jpg",
                "size": 2_500_000,
            }
        ],
    }
    entry.update(overrides)
    return entry


class GeneratorTests(unittest.TestCase):
    def test_full_entry_drafts_ready(self):
        client = StubClient([_entry("e1", ["blog", "instagram", "youtube"])])
        chat = FakeChat(
            [
                "# Shakedown Day\n\nThe post body.",
                "Caption text.\n\n#cars #trackday",
                "TITLE: Shakedown Day\n\nWhat we ran and what we learned.",
            ]
        )
        results = ContentGenerator(client, chat).run_once()
        self.assertEqual([r.outcome for r in results], ["drafts-ready"])
        drafts = client.submitted["e1"]
        self.assertTrue(drafts["blog"].startswith("# Shakedown Day"))
        self.assertIn("#trackday", drafts["instagram"])
        self.assertEqual(drafts["youtube"]["title"], "Shakedown Day")
        self.assertIn("what we learned", drafts["youtube"]["description"])
        # The blog prompt saw the image dimensions from the media fetch.
        self.assertIn("4032x3024", chat.prompts[0])
        self.assertEqual(client.failed, {})

    def test_only_selected_outputs_generated(self):
        client = StubClient([_entry("e1", ["instagram"])])
        chat = FakeChat(["Just a caption. #one"])
        ContentGenerator(client, chat).run_once()
        self.assertEqual(list(client.submitted["e1"].keys()), ["instagram"])
        self.assertEqual(len(chat.prompts), 1)

    def test_failure_returns_entry_and_queue_moves_on(self):
        entries = [_entry("bad", ["blog"]), _entry("good", ["blog"])]
        client = StubClient(entries)
        chat = FakeChat([RuntimeError("ollama died"), "# Good Post\n\nBody."])
        results = ContentGenerator(client, chat).run_once()
        self.assertEqual([r.outcome for r in results], ["failed", "drafts-ready"])
        self.assertIn("ollama died", client.failed["bad"])
        self.assertIn("good", client.submitted)

    def test_claim_conflict_skips_without_fail(self):
        client = StubClient(
            [_entry("e1", ["blog"])], claim_errors={"e1": "409: already claimed"}
        )
        chat = FakeChat([])
        results = ContentGenerator(client, chat).run_once()
        self.assertEqual([r.outcome for r in results], ["skipped"])
        self.assertEqual(client.failed, {})
        self.assertEqual(client.submitted, {})


if __name__ == "__main__":
    unittest.main()
