"""Generated video for content entries (YouTube V1).

One MP4 per entry that selected the youtube output and has at
least one PLATE-treated still: a Pillow-rendered title card (the
entry's generated YouTube title, in the house plate register),
then the treated stills as a slow Ken Burns slideshow - 5 s clear
per still, 1 s crossfade dissolves - at 1920x1080, 30 fps, H.264
with a silent AAC track muxed in (players and YouTube expect an
audio stream; V1 is deliberately silent - there is no licensed
music library on either box and the build does not lift tracks).

The render is CPU libx264 on purpose: the filter chain is
CPU-bound, NVENC measured barely faster, and a second GPU tenant
would contend with the Ollama models the rest of the pipeline
lives on. The ffmpeg invocation runs under nice -n 10 so a render
never starves the box's other work.

Failure discipline matches the rest of the pipeline: render_video
raises on any failure (bad still, ffmpeg error, probe mismatch),
and the generator treats the whole video step as advisory - a
missing video never fails an entry (see generator._submit_video).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# Output geometry and timing. Each still's segment runs
# STILL_SEGMENT_SECONDS of Ken Burns; a 1 s crossfade into the
# next segment consumes the first second of it, so each still
# holds STILL_SECONDS clear. The title card is the same shape:
# TITLE_SEGMENT_SECONDS of segment, TITLE_SECONDS fully clear.
WIDTH = 1920
HEIGHT = 1080
FPS = 30
TITLE_SEGMENT_SECONDS = 3.5
TITLE_SECONDS = 2.5
STILL_SEGMENT_SECONDS = 6.0
STILL_SECONDS = 5.0
XFADE_SECONDS = 1.0
HEAD_FADE_SECONDS = 0.5
TAIL_FADE_SECONDS = 1.0

VIDEO_FILENAME = "inertiald-video.mp4"

# The plate register (photo standard, slice 1d): near-black,
# warm-gray letterspaced type, plate red as the single accent.
_CARD_BG = (12, 12, 14)
_CARD_TYPE = (216, 210, 198)
_CARD_DIM = (140, 134, 122)
_CARD_RED = (255, 51, 0)

_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_RENDER_TIMEOUT_SECONDS = 900.0


@dataclass
class RenderedVideo:
    """One rendered entry video: the MP4 bytes plus the metadata
    the site records on the entry (and the admin tab shows)."""

    data: bytes
    filename: str = VIDEO_FILENAME
    duration_seconds: float = 0.0
    width: int = WIDTH
    height: int = HEIGHT


def expected_duration(still_count: int) -> float:
    """Total runtime for a video with this many stills: the title
    card plus 5 clear seconds per still, crossfades included."""
    if still_count < 1:
        raise ValueError("a video needs at least one still")
    return TITLE_SEGMENT_SECONDS + still_count * (
        STILL_SEGMENT_SECONDS - XFADE_SECONDS
    )


def _ffmpeg_bin() -> str:
    return os.environ.get("FREETALON_FFMPEG", "ffmpeg")


def _ffprobe_bin() -> str:
    return os.environ.get("FREETALON_FFPROBE", "ffprobe")


# ---------- Title card (Pillow; drawtext has no letterspacing) ----------


def _tracked_width(draw: ImageDraw.ImageDraw, text: str, font, tracking: int) -> float:
    if not text:
        return 0.0
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font,
    fill: tuple[int, int, int],
    tracking: int,
) -> float:
    """Draw letterspaced text char by char; returns the end x."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _draw_tracked_centered(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    y: float,
    text: str,
    font,
    fill: tuple[int, int, int],
    tracking: int,
) -> None:
    width = _tracked_width(draw, text, font, tracking)
    _draw_tracked(draw, (center_x - width / 2, y), text, font, fill, tracking)


def _wrap_title(draw: ImageDraw.ImageDraw, title: str, font, tracking: int, max_width: float) -> list[str]:
    words = title.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _tracked_width(draw, candidate, font, tracking) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def render_title_card(title: str) -> bytes:
    """The title card as PNG bytes: INERTIALD wordmark small at
    top, a thin plate-red rule, the YouTube title letterspaced
    below - the photo plate's register, as a card."""
    image = Image.new("RGB", (WIDTH, HEIGHT), _CARD_BG)
    draw = ImageDraw.Draw(image)
    wordmark_font = ImageFont.truetype(_FONT_BOLD, 44)
    title_font = ImageFont.truetype(_FONT_REGULAR, 68)

    _draw_tracked_centered(draw, WIDTH / 2, 150, "INERTIALD", wordmark_font, _CARD_DIM, 18)

    rule_y = 250
    draw.line([(WIDTH / 2 - 120, rule_y), (WIDTH / 2 + 120, rule_y)], fill=_CARD_RED, width=4)

    lines = _wrap_title(draw, title.strip().upper() or "UNTITLED", title_font, 10, WIDTH - 320)
    line_height = 104
    block_height = line_height * len(lines)
    y = (HEIGHT - block_height) / 2 + 40
    for line in lines:
        _draw_tracked_centered(draw, WIDTH / 2, y, line, title_font, _CARD_TYPE, 10)
        y += line_height

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


# ---------- Filter graph (pure, pinned by tests) ----------


def build_filter_graph(still_count: int) -> str:
    """The deterministic filter_complex for the title card (input
    0), `still_count` stills (inputs 1..n) and a silent audio
    source (input n+1). Ken Burns alternates push-in / pull-back
    by index so a run of stills never feels mechanical."""
    if still_count < 1:
        raise ValueError("a video needs at least one still")
    total = expected_duration(still_count)
    parts: list[str] = []

    parts.append(
        f"[0:v]scale={WIDTH}:{HEIGHT},setsar=1,fps={FPS},format=yuv420p[v0]"
    )
    for i in range(still_count):
        frames = int(STILL_SEGMENT_SECONDS * FPS)
        if i % 2 == 0:
            zoom = f"1+0.15*on/{frames - 1}"
        else:
            zoom = f"1.15-0.15*on/{frames - 1}"
        parts.append(
            f"[{i + 1}:v]scale=2560:1440:force_original_aspect_ratio=increase,"
            f"crop=2560:1440,"
            f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1,format=yuv420p"
            f"[v{i + 1}]"
        )

    current = "v0"
    elapsed = TITLE_SEGMENT_SECONDS
    for i in range(still_count):
        offset = elapsed - XFADE_SECONDS
        out = f"x{i}"
        parts.append(
            f"[{current}][v{i + 1}]xfade=transition=fade:duration={XFADE_SECONDS:.1f}"
            f":offset={offset:.2f}[{out}]"
        )
        current = out
        elapsed += STILL_SEGMENT_SECONDS - XFADE_SECONDS

    parts.append(
        f"[{current}]fade=t=in:st=0:d={HEAD_FADE_SECONDS},"
        f"fade=t=out:st={total - TAIL_FADE_SECONDS:.2f}:d={TAIL_FADE_SECONDS},"
        f"format=yuv420p[vout]"
    )
    parts.append(
        f"[{still_count + 1}:a]atrim=0:{total:.2f},asetpts=PTS-STARTPTS[aout]"
    )
    return ";".join(parts)


def build_command(
    title_card_path: str,
    still_paths: list[str],
    out_path: str,
    ffmpeg: str | None = None,
) -> list[str]:
    """The full ffmpeg invocation: nice'd CPU libx264, crf 20,
    maxrate 12M (the storage ceiling from the scope), silent AAC."""
    command = [
        "nice", "-n", "10",
        ffmpeg or _ffmpeg_bin(),
        "-y",
        "-loop", "1", "-framerate", str(FPS), "-t", f"{TITLE_SEGMENT_SECONDS}",
        "-i", title_card_path,
    ]
    for path in still_paths:
        command += ["-i", path]
    command += [
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", build_filter_graph(len(still_paths)),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-maxrate", "12M", "-bufsize", "24M",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        out_path,
    ]
    return command


# ---------- Probe + render ----------


def probe_video(path: str) -> dict:
    """ffprobe facts about a rendered file: stream codecs,
    geometry, frame rate and duration. Raises on any failure."""
    result = subprocess.run(
        [
            _ffprobe_bin(), "-v", "error",
            "-show_streams", "-show_format", "-of", "json", path,
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()[:400]}")
    info = json.loads(result.stdout)
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    if video_stream is None:
        raise RuntimeError(f"ffprobe found no video stream in {path}")
    rate = str(video_stream.get("avg_frame_rate", "0/1"))
    num, _, den = rate.partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    return {
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": fps,
        "duration": float(info.get("format", {}).get("duration", 0.0)),
    }


def render_video(stills: list[bytes], title: str) -> RenderedVideo:
    """Render one entry video from its PLATE-treated stills (JPEG
    bytes, in media order) and its YouTube title. Raises on any
    failure; the generator isolates that to 'no video this pass'."""
    if not stills:
        raise ValueError("a video needs at least one still")
    title_card = render_title_card(title)
    with tempfile.TemporaryDirectory(prefix="freetalon-video-") as tmp:
        card_path = os.path.join(tmp, "title.png")
        with open(card_path, "wb") as fh:
            fh.write(title_card)
        still_paths = []
        for i, data in enumerate(stills):
            path = os.path.join(tmp, f"still-{i}.jpg")
            with open(path, "wb") as fh:
                fh.write(data)
            still_paths.append(path)
        out_path = os.path.join(tmp, VIDEO_FILENAME)
        command = build_command(card_path, still_paths, out_path)
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=_RENDER_TIMEOUT_SECONDS
        )
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-5:]
            raise RuntimeError(
                "ffmpeg render failed: " + " | ".join(tail)[:600]
            )
        probe = probe_video(out_path)
        expected = expected_duration(len(stills))
        problems = []
        if probe["video_codec"] != "h264":
            problems.append(f"video codec {probe['video_codec']!r} != 'h264'")
        if probe["audio_codec"] != "aac":
            problems.append(f"audio codec {probe['audio_codec']!r} != 'aac'")
        if (probe["width"], probe["height"]) != (WIDTH, HEIGHT):
            problems.append(
                f"geometry {probe['width']}x{probe['height']} != {WIDTH}x{HEIGHT}"
            )
        if abs(probe["fps"] - FPS) > 0.1:
            problems.append(f"fps {probe['fps']} != {FPS}")
        if abs(probe["duration"] - expected) > 1.0:
            problems.append(
                f"duration {probe['duration']:.2f}s != expected {expected:.2f}s"
            )
        if problems:
            raise RuntimeError("rendered video failed verification: " + "; ".join(problems))
        with open(out_path, "rb") as fh:
            data = fh.read()
    return RenderedVideo(
        data=data,
        duration_seconds=round(probe["duration"], 2),
        width=probe["width"],
        height=probe["height"],
    )
