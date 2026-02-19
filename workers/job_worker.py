"""
Background worker — optimized for minimum latency.

Design:
- asyncio.Queue for instant job dispatch (no polling delay)
- Jobs are pushed to the queue the moment they're created in the router
- On startup, any "pending" jobs from a previous crash are recovered into the queue
- Semaphore caps concurrency at settings.max_concurrent_jobs
"""

import asyncio
import math
import os
from datetime import datetime

from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from models.job import Job
from models.user import User
from services.transcoder import cleanup_files, probe_video, transcode_video

# ---------------------------------------------------------------------------
# Shared queue — router pushes job IDs here, worker pops immediately
# ---------------------------------------------------------------------------

_job_queue: asyncio.Queue[str] = asyncio.Queue()
_semaphore: asyncio.Semaphore | None = None  # created in start_worker (needs running loop)
_active_jobs: set[str] = set()


def enqueue_job(job_id: str) -> None:
    """Called from the router immediately after job creation. Zero delay."""
    if job_id not in _active_jobs:
        _job_queue.put_nowait(job_id)


# ---------------------------------------------------------------------------
# Core job processor
# ---------------------------------------------------------------------------

async def process_job(job_id: str) -> None:
    output_path: str | None = None

    # Step 1: claim the job
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job or job.status != "pending":
            return

        job.status = "processing"
        job.started_at = datetime.utcnow()
        await db.commit()
        await db.refresh(job)

        input_url = job.input_url
        output_format = job.output_format
        output_resolution = job.output_resolution
        user_id = job.user_id

    try:
        # Step 2: probe source URL directly (no download)
        probe = await probe_video(input_url)
        duration_seconds = probe["duration"]

        if duration_seconds <= 0:
            raise ValueError("Could not determine video duration")
        if duration_seconds > settings.max_video_duration_seconds:
            raise ValueError(
                f"Video too long: {duration_seconds:.0f}s (max {settings.max_video_duration_seconds}s)"
            )

        # Step 3: check credits
        duration_minutes = duration_seconds / 60.0
        credits_needed = math.ceil(duration_minutes * settings.credits_per_minute)

        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                raise RuntimeError(f"User {user_id!r} not found")
            if user.credits < credits_needed:
                raise ValueError(
                    f"Insufficient credits: need {credits_needed}, have {user.credits}"
                )

        # Step 4: transcode (FFmpeg reads from URL directly, probe reused — no double fetch)
        job_dir = os.path.join(settings.storage_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        output_path = os.path.join(job_dir, f"output.{output_format}")

        await transcode_video(
            source=input_url,
            output_path=output_path,
            output_format=output_format,
            output_resolution=output_resolution,
            probe=probe,  # reuse probe result — avoids second network round-trip
        )

        # Step 5: deduct credits + mark complete
        async with AsyncSessionLocal() as db:
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

    except Exception as exc:
        error_msg = str(exc)
        print(f"[worker] Job {job_id} failed: {error_msg}")
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Job).where(Job.id == job_id))
                job = result.scalar_one_or_none()
                if job:
                    job.status = "failed"
                    job.error_message = error_msg[:2000]
                    job.completed_at = datetime.utcnow()
                    await db.commit()
        except Exception as db_err:
            print(f"[worker] Could not write failure for {job_id}: {db_err}")

        if output_path:
            await cleanup_files(output_path)

    finally:
        _active_jobs.discard(job_id)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

async def _run_with_semaphore(job_id: str) -> None:
    assert _semaphore is not None
    async with _semaphore:
        await process_job(job_id)


async def _recover_pending_jobs() -> None:
    """On startup, re-queue any jobs that were pending before a restart."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job)
            .where(Job.status.in_(["pending", "processing"]))
            .order_by(Job.created_at.asc())
        )
        jobs = result.scalars().all()

        # Reset any "processing" jobs (they were interrupted) back to pending
        for job in jobs:
            if job.status == "processing":
                job.status = "pending"
                job.started_at = None
        await db.commit()

    for job in jobs:
        enqueue_job(job.id)

    if jobs:
        print(f"[worker] Recovered {len(jobs)} pending job(s) from previous run")


async def start_worker() -> None:
    """Entry point from main.py lifespan. Runs forever."""
    global _semaphore
    _semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

    print(f"[worker] Started (concurrency={settings.max_concurrent_jobs})")

    # Recover any jobs that survived a restart
    await _recover_pending_jobs()

    # Drain the queue as fast as jobs arrive
    while True:
        job_id = await _job_queue.get()
        if job_id in _active_jobs:
            continue
        _active_jobs.add(job_id)
        asyncio.create_task(_run_with_semaphore(job_id))
