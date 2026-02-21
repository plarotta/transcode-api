import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.user import User


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False, index=True)
    # pending → processing → completed | failed
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    input_url: Mapped[str] = mapped_column(Text, nullable=False)
    output_format: Mapped[str] = mapped_column(String(10), nullable=False)
    output_resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    output_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    output_url: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    credits_charged: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Segment tracking for long-form video parallelism
    parent_job_id: Mapped[str | None] = mapped_column(String, ForeignKey("jobs.id"), nullable=True, index=True)
    segment_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_segments: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="jobs")

    children: Mapped[list["Job"]] = relationship(
        "Job",
        back_populates="parent",
        foreign_keys=[parent_job_id],
    )
    parent: Mapped["Job | None"] = relationship(
        "Job",
        back_populates="children",
        remote_side=[id],
        foreign_keys=[parent_job_id],
    )
