"""
Core FFmpeg/ffprobe wrapper.

All FFmpeg/ffprobe invocations use asyncio.create_subprocess_exec so they
never block the event loop.  Downloads use httpx async streaming.
"""
import asyncio
import json
import os
import re
import shutil
from typing import Awaitable, Callable

import httpx

from config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

# Match "time=HH:MM:SS.ss" in FFmpeg progress output
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def probe_video(input_path: str) -> dict:
    """
    Run ffprobe on *input_path* and return a metadata dict:
        {
            "duration": float,   # seconds
            "width":    int,
            "height":   int,
            "codec":    str,
            "format":   str,
        }
    Raises RuntimeError if ffprobe exits non-zero.
    Raises ValueError  if no video stream is found.
    """
    proc = await asyncio.create_subprocess_exec(
        settings.ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        input_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (code {proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace')}"
        )

    data = json.loads(stdout.decode("utf-8"))

    # Find first video stream
    video_stream: dict | None = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            video_stream = stream
            break

    if video_stream is None:
        raise ValueError("No video stream found in file")

    fmt = data.get("format", {})

    # Duration may live on the format container or the stream itself
    raw_duration = (
        fmt.get("duration")
        or video_stream.get("duration")
        or "0"
    )
    duration = float(raw_duration or 0)

    return {
        "duration": duration,
        "width":    int(video_stream.get("width", 0)),
        "height":   int(video_stream.get("height", 0)),
        "codec":    video_stream.get("codec_name", "unknown"),
        "format":   fmt.get("format_name", "unknown"),
    }


async def download_video(url: str, dest_path: str) -> str:
    """
    Download the video at *url* to *dest_path* using httpx async streaming.

    Raises:
        ValueError – content-type is clearly not a video, or the file > 2 GB.
        httpx.HTTPStatusError – on non-2xx responses.

    Returns dest_path on success.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            base_type = content_type.split(";")[0].strip().lower()

            # Reject obviously non-video content types
            _REJECTED = ("text/", "application/json", "application/xml", "image/")
            if base_type and not base_type.startswith("video/") and base_type not in (
                "application/octet-stream",
                "binary/octet-stream",
                "",
            ):
                if any(base_type.startswith(r) for r in _REJECTED):
                    raise ValueError(
                        f"URL does not appear to be a video "
                        f"(content-type: {content_type!r})"
                    )

            # Reject if Content-Length already exceeds the limit
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_FILE_SIZE:
                raise ValueError(
                    f"File too large ({content_length} bytes); maximum is 2 GB"
                )

            downloaded = 0
            with open(dest_path, "wb") as fh:
                async for chunk in response.aiter_bytes(chunk_size=65_536):
                    downloaded += len(chunk)
                    if downloaded > MAX_FILE_SIZE:
                        raise ValueError(
                            "Download exceeded 2 GB limit; aborting"
                        )
                    fh.write(chunk)

    return dest_path


async def transcode_video(
    input_path: str,
    output_path: str,
    output_format: str,
    output_resolution: str | None,
    progress_callback: Callable[[float], Awaitable[None]] | None = None,
) -> str:
    """
    Transcode *input_path* → *output_path* using FFmpeg.

    output_format must be one of: mp4, webm, gif, mov, mkv
    output_resolution is optional "WIDTHxHEIGHT" string (e.g. "1280x720").

    progress_callback(percent: float) is called periodically if supplied.

    Raises RuntimeError (with full stderr) if FFmpeg exits non-zero.
    Returns output_path on success.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Parse resolution
    out_w: int | None = None
    out_h: int | None = None
    if output_resolution:
        parts = output_resolution.lower().split("x")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            out_w, out_h = int(parts[0]), int(parts[1])

    # Probe duration up-front so we can report meaningful progress percentages
    duration: float | None = None
    try:
        info = await probe_video(input_path)
        duration = info["duration"] or None
    except Exception:
        pass  # progress will just not fire — that's fine

    # Build the FFmpeg command
    cmd = [settings.ffmpeg_path, "-y", "-i", input_path]

    if output_format == "gif":
        # GIF: special palette filter, no audio
        gif_w = out_w or 480
        vf = f"fps=10,scale={gif_w}:-1:flags=lanczos"
        cmd += ["-vf", vf, "-loop", "0"]

    else:
        # Resolution filter (non-GIF)
        if out_w and out_h:
            cmd += ["-vf", f"scale={out_w}:{out_h}"]

        # Codec / container settings
        if output_format == "mp4":
            cmd += [
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-movflags", "+faststart",
            ]
        elif output_format == "webm":
            cmd += ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-c:a", "libopus"]
        elif output_format == "mov":
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac"]
        elif output_format == "mkv":
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac"]
        else:
            raise ValueError(f"Unsupported output format: {output_format!r}")

    cmd.append(output_path)

    # Launch FFmpeg
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_text = await _monitor_stderr(proc, duration, progress_callback)
    await proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (exit {proc.returncode}):\n{stderr_text}"
        )

    return output_path


async def cleanup_files(*paths: str) -> None:
    """
    Delete files (or directory trees) at each path.
    Errors are silently ignored.
    """
    for path in paths:
        if not path:
            continue
        try:
            if os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _monitor_stderr(
    proc: asyncio.subprocess.Process,
    duration: float | None,
    progress_callback: Callable[[float], Awaitable[None]] | None,
) -> str:
    """
    Drain FFmpeg's stderr, firing *progress_callback* on each time= update.
    Returns the full stderr as a single string (for error messages).

    FFmpeg uses \\r (not \\n) for in-place progress lines, so we split on both.
    """
    lines: list[str] = []
    buf = b""

    while True:
        chunk = await proc.stderr.read(4096)
        if not chunk:
            break
        buf += chunk
        # Split on either CR or LF; keep the last (possibly incomplete) fragment
        parts = re.split(rb"[\r\n]", buf)
        buf = parts[-1]

        for raw in parts[:-1]:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            lines.append(line)

            if progress_callback and duration and duration > 0:
                m = _TIME_RE.search(line)
                if m:
                    h, mins, secs = m.groups()
                    elapsed = int(h) * 3600 + int(mins) * 60 + float(secs)
                    pct = min(100.0, elapsed / duration * 100)
                    try:
                        await progress_callback(pct)
                    except Exception:
                        pass

    # Flush any trailing bytes
    if buf:
        lines.append(buf.decode("utf-8", errors="replace").strip())

    return "\n".join(lines)
