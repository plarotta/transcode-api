"""
Transcoding job service layer.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_job(
    db: AsyncSession,
    user_id: str,
    input_url: str,
    output_format: str,
    output_resolution: str | None = None,
) -> Job:
    job = Job(
        user_id=user_id,
        input_url=input_url,
        output_format=output_format,
        output_resolution=output_resolution,
        status="pending",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job(db: AsyncSession, job_id: str) -> Job | None:
    result = await db.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


async def get_jobs_for_user(
    db: AsyncSession,
    user_id: str,
    limit: int = 20,
    offset: int = 0,
) -> list[Job]:
    result = await db.execute(
        select(Job)
        .where(Job.user_id == user_id)
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_child_jobs(db: AsyncSession, parent_job_id: str) -> list[Job]:
    """Return all segment jobs for a parent job, ordered by segment_index."""
    result = await db.execute(
        select(Job)
        .where(Job.parent_job_id == parent_job_id)
        .order_by(Job.segment_index)
    )
    return list(result.scalars().all())


async def all_segments_complete(db: AsyncSession, parent_job_id: str) -> bool:
    """Return True if all child jobs are in 'completed' status."""
    children = await get_child_jobs(db, parent_job_id)
    if not children:
        return False
    return all(child.status == "completed" for child in children)


async def create_segment_job(
    db: AsyncSession,
    parent_job_id: str,
    user_id: str,
    input_url: str,
    output_format: str,
    output_resolution: str | None,
    segment_index: int,
    total_segments: int,
) -> Job:
    """Create a child segment job."""
    job = Job(
        user_id=user_id,
        input_url=input_url,
        output_format=output_format,
        output_resolution=output_resolution,
        status="pending",
        parent_job_id=parent_job_id,
        segment_index=segment_index,
        total_segments=total_segments,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def update_job_status(
    db: AsyncSession,
    job_id: str,
    status: str,
    error_message: str | None = None,
    output_filename: str | None = None,
    output_url: str | None = None,
    duration_seconds: float | None = None,
    credits_charged: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> Job | None:
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        return None

    job.status = status
    if error_message is not None:
        job.error_message = error_message
    if output_filename is not None:
        job.output_filename = output_filename
    if output_url is not None:
        job.output_url = output_url
    if duration_seconds is not None:
        job.duration_seconds = duration_seconds
    if credits_charged is not None:
        job.credits_charged = credits_charged
    if started_at is not None:
        job.started_at = started_at
    if completed_at is not None:
        job.completed_at = completed_at

    await db.commit()
    await db.refresh(job)
    return job
