"""Tiny media metadata helpers for draft generation.

Only what the prompts need: pixel dimensions for still images, parsed
from the file header bytes - no imaging library, no full decode.
Anything unrecognized or truncated yields None, never an exception:
a missing dimension is a weaker prompt, not a failed entry.
"""

from __future__ import annotations

import struct


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return (width, height) if width and height else None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    # Only the VP8X (extended) header carries plain dimensions; the
    # lossy/lossless variants pack them into the bitstream, which is
    # more parsing than a prompt hint justifies.
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if data[12:16] != b"VP8X":
        return None
    width = int.from_bytes(data[24:27], "little") + 1
    height = int.from_bytes(data[27:30], "little") + 1
    return (width, height)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    sof_markers = {0xC0, 0xC1, 0xC2}
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker in sof_markers:
            if offset + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return (width, height) if width and height else None
        # Standalone markers (RSTn, TEM) carry no length field.
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:
            offset += 2
            continue
        segment_length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if segment_length < 2:
            return None
        offset += 2 + segment_length
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) for PNG/JPEG/GIF/WebP(VP8X) bytes, else None."""
    for probe in (_png_dimensions, _gif_dimensions, _webp_dimensions, _jpeg_dimensions):
        result = probe(data)
        if result is not None:
            return result
    return None
