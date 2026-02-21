"""
ARQ worker — Redis-backed job queue.

Each task function receives `ctx` as the first argument (ARQ convention).
The actual job processing logic lives here; job_worker.py now only handles
enqueueing via ArqRedis.

Run standalone:
    arq workers.arq_worker.WorkerSettings

Or launched in-process from main.py lifespan (current default).
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy import update as sql_update

from config import settings
from database import AsyncSessionLocal
from models.job import Job
from models.user import User
from services.transcoder import cleanup_files, probe_video, transcode_video


# ---------------------------------------------------------------------------
# Task: process_job
# ---------------------------------------------------------------------------

async def process_job(ctx: dict, job_id: str) -> None:  # noqa: ARG001
    """Process a single transcode job. Called by ARQ when a job is dequeued."""
    output_path: str | None = None

    # Step 1: claim the job
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job or job.status != "pending":
            return

        job.status = "processing"
        job.started_at = datetime.now(timezone.utc)
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
                f"Video too long: {duration_seconds:.0f}s "
                f"(max {settings.max_video_duration_seconds}s)"
            )

        # Step 3: compute credits needed
        duration_minutes = duration_seconds / 60.0
        credits_needed = math.ceil(duration_minutes * settings.credits_per_minute)

        # Step 4: transcode
        job_dir = os.path.join(settings.storage_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        output_path = os.path.join(job_dir, f"output.{output_format}")

        await transcode_video(
            source=input_url,
            output_path=output_path,
            output_format=output_format,
            output_resolution=output_resolution,
            probe=probe,
        )

        # Step 5: atomically deduct credits + mark complete
        async with AsyncSessionLocal() as db:
            # Atomic credit deduction: only succeeds if credits >= credits_needed
            result = await db.execute(
                sql_update(User)
                .where(User.id == user_id, User.credits >= credits_needed)
                .values(credits=User.credits - credits_needed)
                .returning(User.id)
            )
            row = result.fetchone()
            if row is None:
                raise ValueError(
                    f"Insufficient credits: need {credits_needed}"
                )

            result = await db.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if job:
                job.status = "completed"
                job.output_url = f"/jobs/{job_id}/download"
                job.duration_seconds = duration_seconds
                job.credits_charged = credits_needed
                job.completed_at = datetime.now(timezone.utc)

            await db.commit()

    except Exception as exc:
        error_msg = str(exc)
        print(f"[arq] Job {job_id} failed: {error_msg}")
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Job).where(Job.id == job_id))
                job = result.scalar_one_or_none()
                if job:
                    job.status = "failed"
                    job.error_message = error_msg[:2000]
                    job.completed_at = datetime.now(timezone.utc)
                    await db.commit()
        except Exception as db_err:
            print(f"[arq] Could not write failure for {job_id}: {db_err}")

        if output_path:
            await cleanup_files(output_path)


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

async def on_startup(ctx: dict) -> None:  # noqa: ARG001
    """Re-queue any jobs left in pending/processing state from a prior crash."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job)
            .where(Job.status.in_(["pending", "processing"]))
            .order_by(Job.created_at.asc())
        )
        jobs = result.scalars().all()

        # Reset interrupted "processing" jobs back to pending so ARQ can retry
        for job in jobs:
            if job.status == "processing":
                job.status = "pending"
                job.started_at = None
        await db.commit()

    if jobs:
        # Import here to avoid circular imports at module level
        from arq.connections import create_pool
        redis = await create_pool(_redis_settings())
        for job in jobs:
            await redis.enqueue_job("process_job", job.id)
        await redis.aclose()
        print(f"[arq] Recovered {len(jobs)} pending job(s) from previous run")


async def on_shutdown(ctx: dict) -> None:  # noqa: ARG001
    print("[arq] Worker shutting down")


# ---------------------------------------------------------------------------
# Redis settings helper
# ---------------------------------------------------------------------------

def _redis_settings() -> RedisSettings:
    """Parse REDIS_URL into arq RedisSettings."""
    url = settings.redis_url  # e.g. redis://localhost:6379 or redis://:pass@host:port/db
    # RedisSettings.from_dsn available in arq >= 0.25
    return RedisSettings.from_dsn(url)


# ---------------------------------------------------------------------------
# Worker configuration
# ---------------------------------------------------------------------------

class WorkerSettings:
    functions = [process_job]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = _redis_settings()
    max_jobs = settings.max_concurrent_jobs
    job_timeout = settings.max_video_duration_seconds + 120  # a bit over max video len
    keep_result = 3600  # keep result in Redis for 1 hour
