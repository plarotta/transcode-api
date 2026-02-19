"""
Background worker that processes pending jobs from the DB.

Runs as an asyncio task started in main.py's lifespan context manager.

Design:
  - Polls the DB every 5 seconds for jobs whose status == "pending".
  - Uses an in-memory set (_active_jobs) to avoid dispatching the same job twice.
  - Uses asyncio.Semaphore to hard-cap concurrent FFmpeg processes.
  - Each job runs through the full lifecycle in process_job(), which opens its
    own short-lived DB sessions so the event loop is never blocked by DB I/O
    during the download or transcode steps.
"""

import asyncio
import math
import os
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from models.job import Job
from models.user import User
from services.transcoder import cleanup_files, download_video, probe_video, transcode_video

# ---------------------------------------------------------------------------
# Module-level concurrency controls
# ---------------------------------------------------------------------------

# Semaphore is created at module level; its internal value is controlled by
# settings.max_concurrent_jobs which is read once at startup.
_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

# IDs of jobs that have been dispatched (may be waiting for the semaphore or
# actively transcoding).  Prevents re-dispatching the same job on the next
# poll cycle.
_active_jobs: set[str] = set()


# ---------------------------------------------------------------------------
# Core job processor
# ---------------------------------------------------------------------------

async def process_job(job_id: str) -> None:
    """
    Full lifecycle for a single transcoding job:

    1.  Mark job status → "processing", record started_at
    2.  Download input_url to {storage_dir}/{job_id}/input.{ext}
    3.  ffprobe the download to get duration
    4.  Verify user has enough credits (ceil(minutes * credits_per_minute))
    5.  Transcode to {storage_dir}/{job_id}/output.{output_format}
    6.  Deduct credits; mark job "completed" with output_url + metadata
    7.  On any error: mark job "failed" with error_message; clean up files
    """
    input_path: str | None = None
    output_path: str | None = None

    # ── Step 1: claim the job ───────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()

        # Guard against races: only process jobs still in "pending"
        if not job or job.status != "pending":
            return

        job.status = "processing"
        job.started_at = datetime.utcnow()
        await db.commit()
        await db.refresh(job)

        # Snapshot the values we'll need; the session will be closed below
        input_url = job.input_url
        output_format = job.output_format
        output_resolution = job.output_resolution
        user_id = job.user_id

    try:
        # ── Step 2: prepare directories & download ──────────────────────────
        job_dir = os.path.join(settings.storage_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)

        # Derive a reasonable extension from the URL path
        url_path = urlparse(input_url).path
        ext = os.path.splitext(url_path)[1].lower()
        _KNOWN_EXTS = {
            ".mp4", ".webm", ".mov", ".mkv", ".avi", ".flv",
            ".wmv", ".m4v", ".ts", ".gif", ".m2ts", ".mts",
        }
        if ext not in _KNOWN_EXTS:
            ext = ".mp4"  # safe default

        input_path = os.path.join(job_dir, f"input{ext}")
        output_path = os.path.join(job_dir, f"output.{output_format}")

        await download_video(input_url, input_path)

        # ── Step 3: probe duration ──────────────────────────────────────────
        probe = await probe_video(input_path)
        duration_seconds = probe["duration"]

        if duration_seconds <= 0:
            raise ValueError("Could not determine video duration from ffprobe")

        if duration_seconds > settings.max_video_duration_seconds:
            raise ValueError(
                f"Video duration {duration_seconds:.0f}s exceeds the maximum "
                f"allowed {settings.max_video_duration_seconds}s"
            )

        # ── Step 4: verify credits ──────────────────────────────────────────
        duration_minutes = duration_seconds / 60.0
        credits_needed = math.ceil(duration_minutes * settings.credits_per_minute)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                raise RuntimeError(f"User {user_id!r} not found in database")
            if user.credits < credits_needed:
                raise ValueError(
                    f"Insufficient credits: need {credits_needed}, "
                    f"have {user.credits}"
                )

        # ── Step 5: transcode ───────────────────────────────────────────────
        await transcode_video(input_path, output_path, output_format, output_resolution)

        # ── Steps 6–8: deduct credits and mark completed ────────────────────
        async with AsyncSessionLocal() as db:
            # Refresh user to get the latest credit balance
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                user.credits = max(0, user.credits - credits_needed)

            result = await db.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                job.status = "completed"
                job.output_url = f"/jobs/{job_id}/download"
                job.duration_seconds = duration_seconds
                job.credits_charged = credits_needed
                job.completed_at = datetime.utcnow()

            await db.commit()

        # Keep the output file; remove the (usually large) input
        await cleanup_files(input_path)
        input_path = None  # already cleaned

    except Exception as exc:
        error_msg = str(exc)
        print(f"[worker] Job {job_id} failed: {error_msg}")

        # Mark the job as failed in the DB
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Job).where(Job.id == job_id))
                job = result.scalar_one_or_none()
                if job:
                    job.status = "failed"
                    job.error_message = error_msg[:2000]  # cap length
                    job.completed_at = datetime.utcnow()
                    await db.commit()
        except Exception as db_err:
            print(f"[worker] Could not write failure status for job {job_id}: {db_err}")

        # Clean up any partial files
        files_to_clean = [p for p in (input_path, output_path) if p]
        if files_to_clean:
            await cleanup_files(*files_to_clean)


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

async def _run_job_with_semaphore(job_id: str) -> None:
    """Acquire the concurrency semaphore, run the job, then release tracking."""
    try:
        async with _semaphore:
            await process_job(job_id)
    finally:
        _active_jobs.discard(job_id)


async def _check_and_dispatch() -> None:
    """
    Query the DB for pending jobs, ignoring any already in _active_jobs.
    Spawn an asyncio.Task for each new job found.
    """
    async with AsyncSessionLocal() as db:
        query = (
            select(Job)
            .where(Job.status == "pending")
            .order_by(Job.created_at.asc())
        )
        # Exclude jobs already dispatched
        if _active_jobs:
            query = query.where(Job.id.not_in(list(_active_jobs)))

        # Fetch slightly more than max_concurrent_jobs so there's always
        # work queued up behind the semaphore
        query = query.limit(settings.max_concurrent_jobs * 2)

        result = await db.execute(query)
        pending = result.scalars().all()

    for job in pending:
        if job.id not in _active_jobs:
            _active_jobs.add(job.id)
            asyncio.create_task(_run_job_with_semaphore(job.id))


async def start_worker() -> None:
    """
    Entry point called from main.py lifespan.
    Polls the DB every 5 seconds and dispatches pending jobs.
    """
    print("[worker] Job worker started")
    while True:
        try:
            await _check_and_dispatch()
        except Exception as e:
            print(f"[worker] Worker error in dispatch loop: {e}")
        await asyncio.sleep(5)
