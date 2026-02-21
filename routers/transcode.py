"""
/jobs — Transcode job routes
"""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import get_db
from middleware.auth import get_current_user
from models.user import User
from services.job_service import create_job, get_job, get_jobs_for_user
from workers.job_worker import enqueue_job

router = APIRouter()

SUPPORTED_FORMATS = {"mp4", "webm", "gif", "mov", "mkv"}


# ── Schemas ────────────────────────────────────────────────────────────────────

class TranscodeRequest(BaseModel):
    input_url: str
    output_format: str
    output_resolution: Optional[str] = None  # e.g. "1280x720"


class JobResponse(BaseModel):
    id: str
    status: str
    input_url: str
    output_format: str
    output_resolution: Optional[str]
    output_url: Optional[str]
    duration_seconds: Optional[float]
    credits_charged: Optional[int]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    parent_job_id: Optional[str] = None
    segment_index: Optional[int] = None
    total_segments: Optional[int] = None

    class Config:
        from_attributes = True


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a video transcoding job",
)
async def submit_job(
    body: TranscodeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Submit a video for transcoding. The job is processed asynchronously.
    Poll GET /jobs/{id} to check status.

    Pricing: 10 credits / minute of video. 100 free credits on signup.
    """
    if body.output_format not in SUPPORTED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{body.output_format}'. Supported: {sorted(SUPPORTED_FORMATS)}",
        )

    if current_user.credits <= 0:
        raise HTTPException(
            status_code=402,
            detail="Insufficient credits. Purchase more at POST /billing/checkout",
        )

    # Validate resolution format if provided
    if body.output_resolution:
        parts = body.output_resolution.lower().split("x")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise HTTPException(
                status_code=400,
                detail="output_resolution must be in format WIDTHxHEIGHT, e.g. '1280x720'",
            )

    job = await create_job(
        db,
        user_id=current_user.id,
        input_url=body.input_url,
        output_format=body.output_format,
        output_resolution=body.output_resolution,
    )
    # Push to in-memory queue immediately — no polling delay
    enqueue_job(job.id)
    return job


@router.get(
    "",
    response_model=list[JobResponse],
    summary="List your transcoding jobs",
)
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the authenticated user's jobs, newest first."""
    jobs = await get_jobs_for_user(db, current_user.id)
    return jobs[offset : offset + limit]


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get job status",
)
async def get_job_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll this endpoint to check job progress."""
    job = await get_job(db, job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get(
    "/{job_id}/download",
    summary="Download the transcoded output file",
)
async def download_output(
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download the transcoded file. Only available when job status is 'completed'."""
    job = await get_job(db, job_id)
    if not job or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Job is not completed yet (status: {job.status})",
        )

    output_path = os.path.join(settings.storage_dir, job_id, f"output.{job.output_format}")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Output file not found")

    media_types = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "gif": "image/gif",
        "mov": "video/quicktime",
        "mkv": "video/x-matroska",
    }
    return FileResponse(
        path=output_path,
        media_type=media_types.get(job.output_format, "application/octet-stream"),
        filename=f"output.{job.output_format}",
    )
