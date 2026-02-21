import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.job import Job
    from models.credit_purchase import CreditPurchase


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    api_key: Mapped[str] = mapped_column(
        String,
        unique=True,
        nullable=False,
        index=True,
        default=lambda: f"tca_{secrets.token_hex(24)}",
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    jobs: Mapped[list["Job"]] = relationship("Job", back_populates="user")
    credit_purchases: Mapped[list["CreditPurchase"]] = relationship(
        "CreditPurchase", back_populates="user"
    )
