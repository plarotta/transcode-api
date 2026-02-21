"""
FFmpeg transcoding service — optimized for maximum throughput.

Key design decisions:
- FFmpeg reads from source URL directly (no download-to-disk)
- Smart remux: copy streams when codecs are already compatible (orders of magnitude faster)
- ultrafast preset + -threads 0 for pure CPU encoding
- Audio copy when codec is already compatible
"""
import asyncio
import json
import os
import re
import shutil
from typing import Awaitable, Callable

from config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB (also enforced via -fs flag)

# Match "time=HH:MM:SS.ss" in FFmpeg progress output
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")

# Codec compatibility maps for remux detection
_MP4_MOV_MKV_VIDEO = {"h264", "hevc"}
_MP4_MOV_MKV_AUDIO = {"aac", "mp3"}
_WEBM_VIDEO = {"vp8", "vp9"}
_WEBM_AUDIO = {"opus", "vorbis"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def probe_video(source: str) -> dict:
    """
    Run ffprobe on source (URL or file path).

    Returns:
        {
            "duration":     float,  # seconds
            "width":        int,
            "height":       int,
            "video_codec":  str,    # e.g. "h264", "hevc", "vp9"
            "audio_codec":  str,    # e.g. "aac", "opus" — "" if no audio
            "format":       str,    # container format name
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
        source,
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
    streams = data.get("streams", [])

    video_stream: dict | None = None
    audio_stream: dict | None = None
    for stream in streams:
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream
        elif stream.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = stream

    if video_stream is None:
        raise ValueError("No video stream found in source")

    fmt = data.get("format", {})
    raw_duration = (
        fmt.get("duration")
        or video_stream.get("duration")
        or "0"
    )

    return {
        "duration":    float(raw_duration or 0),
        "width":       int(video_stream.get("width", 0)),
        "height":      int(video_stream.get("height", 0)),
        "video_codec": video_stream.get("codec_name", "unknown"),
        "audio_codec": (audio_stream.get("codec_name", "") if audio_stream else ""),
        "format":      fmt.get("format_name", "unknown"),
    }


async def transcode_video(
    source: str,
    output_path: str,
    output_format: str,
    output_resolution: str | None,
    probe: dict | None = None,             # pass pre-fetched probe to avoid double round-trip
    progress_callback: Callable[[float], Awaitable[None]] | None = None,
) -> str:
    """
    Transcode source (URL or local file path) → output_path using FFmpeg.

    Pass `probe` (from a prior probe_video() call) to skip the internal re-probe.

    output_format must be one of: mp4, webm, gif, mov, mkv
    output_resolution is an optional "WIDTHxHEIGHT" string (e.g. "1280x720").
    progress_callback(percent: float) is called periodically if supplied.

    Raises RuntimeError (with full stderr) if FFmpeg exits non-zero.
    Returns output_path on success.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Use caller-supplied probe info to avoid a second network round-trip
    if probe is None:
        try:
            probe = await probe_video(source)
        except Exception:
            probe = {}

    duration: float | None = probe.get("duration") or None
    cmd = _build_ffmpeg_cmd(source, output_path, output_format, output_resolution, probe)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stderr_text = await _monitor_stderr(proc, duration, progress_callback)
        await proc.wait()
    except asyncio.CancelledError:
        # ARQ timeout or explicit cancellation — kill the subprocess before re-raising
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # already gone
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        raise  # re-raise CancelledError so ARQ knows the task was cancelled

    if proc.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (exit {proc.returncode}):\n{stderr_text}"
        )

    return output_path


async def cleanup_files(*paths: str) -> None:
    """Delete files (or directory trees) at each path. Errors are silently ignored."""
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


def _can_copy_streams(
    probe_info: dict,
    output_format: str,
    output_resolution: str | None,
) -> bool:
    """
    Returns True if we can remux (copy streams) without re-encoding.

    Conditions (all must hold):
    - No resolution change requested
    - Video codec is compatible with the output container:
        mp4 / mov / mkv : h264 or hevc
        webm            : vp8 or vp9
        gif             : never (always re-encode)
    - Audio codec is compatible with the output container:
        mp4 / mov / mkv : aac or mp3
        webm            : opus or vorbis
    """
    if output_format == "gif":
        return False

    if output_resolution:
        return False

    video_codec = probe_info.get("video_codec", "").lower()
    audio_codec = probe_info.get("audio_codec", "").lower()

    if output_format in ("mp4", "mov", "mkv"):
        return video_codec in _MP4_MOV_MKV_VIDEO and audio_codec in _MP4_MOV_MKV_AUDIO
    elif output_format == "webm":
        return video_codec in _WEBM_VIDEO and audio_codec in _WEBM_AUDIO

    return False


def _build_ffmpeg_cmd(
    source: str,
    output_path: str,
    output_format: str,
    output_resolution: str | None,
    probe: dict,
) -> list[str]:
    """Build the optimal FFmpeg command list for the given parameters."""
    cmd = [settings.ffmpeg_path, "-y"]

    # For HTTP(S) sources, add reconnect options for resilience / speed
    if source.startswith("http"):
        cmd += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]

    cmd += ["-i", source, "-threads", "0"]

    can_copy = _can_copy_streams(probe, output_format, output_resolution)

    if output_format == "gif":
        # GIF always needs palette-based re-encoding; no audio stream
        gif_w = int(output_resolution.split("x")[0]) if output_resolution else 480
        cmd += ["-vf", f"fps=10,scale={gif_w}:-1:flags=lanczos", "-loop", "0"]

    elif can_copy:
        # Remux only — fastest path, no quality loss
        cmd += ["-c", "copy"]
        if output_format == "mp4":
            cmd += ["-movflags", "+faststart"]

    else:
        # Re-encode with speed-optimised settings
        if output_resolution:
            w, h = output_resolution.split("x")
            cmd += ["-vf", f"scale={w}:{h}"]

        if output_format in ("mp4", "mov", "mkv"):
            cmd += [
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "28",
                "-threads", "0",
            ]
            # Copy audio when already in a compatible codec — skip re-encoding
            audio_codec = probe.get("audio_codec", "").lower()
            if audio_codec in _MP4_MOV_MKV_AUDIO:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "aac", "-b:a", "128k"]
            if output_format == "mp4":
                cmd += ["-movflags", "+faststart"]

        elif output_format == "webm":
            # -deadline realtime + -cpu-used 8 is the fastest VP9 mode
            cmd += [
                "-c:v", "libvpx-vp9",
                "-deadline", "realtime",
                "-cpu-used", "8",
                "-crf", "35",
                "-b:v", "0",
                "-threads", "0",
            ]
            audio_codec = probe.get("audio_codec", "").lower()
            if audio_codec in _WEBM_AUDIO:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-c:a", "libopus", "-b:a", "96k"]

        else:
            raise ValueError(f"Unsupported output format: {output_format!r}")

    # Hard cap: abort if output exceeds 2 GB
    cmd += ["-fs", "2147483648"]
    cmd.append(output_path)
    return cmd


async def _monitor_stderr(
    proc: asyncio.subprocess.Process,
    duration: float | None,
    progress_callback: Callable[[float], Awaitable[None]] | None,
) -> str:
    """
    Drain FFmpeg's stderr, firing progress_callback on each time= update.
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
        # Split on CR or LF; keep the last (possibly incomplete) fragment
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
