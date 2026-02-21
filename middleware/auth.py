"""
FastAPI dependency for API-key authentication.

Usage in a router:
    from middleware.auth import get_current_user

    @router.get("/protected")
    async def endpoint(user: User = Depends(get_current_user)):
        ...
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from services.user_service import get_user_by_api_key
from database import get_db
from models.user import User


async def get_current_user(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Resolve an API key from the X-API-Key header into a User.

    Raises HTTP 401 if the key is missing, unknown, or the account is inactive.
    """
    user = await get_user_by_api_key(db, x_api_key)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return user
