from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr
from datetime import datetime

from database import get_db
from config import settings
from models import User
from middleware.auth import get_current_user
from services.user_service import get_user_by_email, create_user

router = APIRouter()


# ── Request / Response models ──────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr


class RegisterResponse(BaseModel):
    api_key: str
    email: str
    credits: int


class MeResponse(BaseModel):
    email: str
    credits: int
    api_key: str
    created_at: datetime


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account and receive an API key",
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """
    Register with an email address.  No password required — your API key
    IS your credential.  100 free starter credits are included.
    """
    existing = await get_user_by_email(db, body.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = await create_user(db, body.email)

    return RegisterResponse(
        api_key=user.api_key,
        email=user.email,
        credits=user.credits,
    )


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Return info about the authenticated user",
)
async def me(
    current_user: User = Depends(get_current_user),
) -> MeResponse:
    """
    Returns the profile of the user identified by the `X-API-Key` header.
    """
    return MeResponse(
        email=current_user.email,
        credits=current_user.credits,
        api_key=current_user.api_key,
        created_at=current_user.created_at,
    )
