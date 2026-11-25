"""Representative frames for content videos (vision follow-on).

Slice 1c's vision pass describes an entry's stills, but a video
entry carried no stills: its media stayed metadata-only (filename,
size), so a video submission's drafts were grounded in Pat's blurb
alone. This module closes that gap at the source: it pulls a
small set of representative frames out of the video - the first
frame plus evenly spaced samples - as JPEG stills, and the
generator runs them through the same vision callable a photo
gets, so the video item ends up carrying a 'vision' description
in exactly the shape a photo's does.

Extraction is ffmpeg, like the V1 render, and under the same
discipline: CPU-only, nice -n 10, frames downscaled to a sane
width for a vision payload. Failure discipline matches the rest
of the pipeline: extract_frames raises on total failure (corrupt
file, unsupported codec, ffmpeg error) and the generator treats
that as 'no frames this pass' - the entry drafts from metadata
alone, exactly as video entries did before this module existed.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

# How many frames stand in for a video: the first frame plus
# evenly spaced samples. Four is enough for the vision pass to
# say what the video shows without spending a photo album's
# worth of vision calls on one media item.
MAX_FRAMES = 4

# Frames are vision payloads, not deliverables: cap the width so
# a 4K clip does not base64 a 4K still into the vision request.
MAX_FRAME_WIDTH = 1280

_PROBE_TIMEOUT_SECONDS = 60.0
_EXTRACT_TIMEOUT_SECONDS = 120.0


def _ffmpeg_bin() -> str:
    return os.environ.get("FREETALON_FFMPEG", "ffmpeg")


def _ffprobe_bin() -> str:
    return os.environ.get("FREETALON_FFPROBE", "ffprobe")


def frame_timestamps(
    duration: float | None, max_frames: int = MAX_FRAMES
) -> list[float]:
    """Where in the video the sample frames come from: the first
    frame, then evenly spaced samples across the runtime. The
    last sample lands at (n-1)/n of the duration, never at the
    very end, where a seek can run off the stream. An unknown or
    non-positive duration earns the first frame only."""
    if duration is None or duration <= 0 or max_frames <= 1:
        return [0.0]
    return [duration * i / max_frames for i in range(max_frames)]


def build_extract_command(
    input_path: str,
    timestamp: float,
    out_path: str,
    ffmpeg: str | None = None,
) -> list[str]:
    """One frame grab: nice'd CPU ffmpeg, the seek placed before
    the input (fast, and frame-accurate for a single-frame grab),
    downscale to MAX_FRAME_WIDTH at most, JPEG out."""
    return [
        "nice", "-n", "10",
        ffmpeg or _ffmpeg_bin(),
        "-v", "error",
        "-ss", f"{timestamp:.3f}",
        "-i", input_path,
        "-frames:v", "1",
        "-vf", f"scale=w='min({MAX_FRAME_WIDTH},iw)':h=-2",
        "-q:v", "3",
        "-f", "image2",
        "-y", out_path,
    ]


def probe_duration(path: str) -> float | None:
    """The video's duration in seconds per ffprobe, or None when
    ffprobe cannot say. Duration only steers the sample spacing,
    so a failed probe degrades to first-frame-only extraction
    instead of failing the video."""
    try:
        result = subprocess.run(
            [
                _ffprobe_bin(), "-v", "error",
                "-show_format", "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float(
            json.loads(result.stdout).get("format", {}).get("duration", 0.0)
        )
    except (ValueError, TypeError, AttributeError):
        return None
    return duration if duration > 0 else None


def _grab_frame(input_path: str, timestamp: float, out_path: str) -> bytes | None:
    """One frame as JPEG bytes, or None when this sample cannot
    be had (a seek past a damaged section, a quiet ffmpeg
    failure): one bad sample costs that frame, not the video."""
    try:
        result = subprocess.run(
            build_extract_command(input_path, timestamp, out_path),
            capture_output=True, text=True, timeout=_EXTRACT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not os.path.exists(out_path):
        return None
    with open(out_path, "rb") as fh:
        data = fh.read()
    # A stub or non-JPEG file is ffmpeg having failed quietly.
    if len(data) < 32 or data[:2] != b"\xff\xd8":
        return None
    return data


def extract_frames(data: bytes) -> list[bytes]:
    """Representative JPEG frames from one video's bytes, in
    stream order. Raises on total failure - empty input, or not
    one frame extractable (corrupt file, unsupported codec) - so
    the caller can fall back to metadata-only drafting."""
    if not data:
        raise ValueError("no video bytes to extract frames from")
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="freetalon-frames-") as tmp:
        input_path = os.path.join(tmp, "input")
        with open(input_path, "wb") as fh:
            fh.write(data)
        duration = probe_duration(input_path)
        for index, timestamp in enumerate(frame_timestamps(duration)):
            frame = _grab_frame(
                input_path, timestamp, os.path.join(tmp, f"frame-{index}.jpg")
            )
            if frame is not None:
                frames.append(frame)
    if not frames:
        raise RuntimeError(
            "ffmpeg extracted no frames from the video "
            "(corrupt file or unsupported codec)"
        )
    return frames
