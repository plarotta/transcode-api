import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from config import settings
from database import get_db
from models import User
from middleware.auth import get_current_user
from services.user_service import (
    create_credit_purchase,
    complete_credit_purchase,
)

stripe.api_key = settings.stripe_secret_key

router = APIRouter()


# ── Response models ────────────────────────────────────────────────────────────

class CheckoutResponse(BaseModel):
    checkout_url: str


class SuccessResponse(BaseModel):
    message: str
    session_id: str


class CancelResponse(BaseModel):
    message: str


class CreditsResponse(BaseModel):
    credits: int
    cost_per_minute: int
    cost_per_credit: str


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    summary="Create a Stripe Checkout session to purchase a credit pack",
)
async def create_checkout(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CheckoutResponse:
    """
    Initiates a Stripe Checkout flow for one credit pack
    ({credits} credits for ${price}).  Returns a URL to redirect the user to.
    """.format(
        credits=settings.credit_pack_credits,
        price=settings.credit_pack_price_usd / 100,
    )
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price": settings.stripe_price_id,
                    "quantity": 1,
                }
            ],
            success_url=(
                f"{settings.base_url}/billing/success"
                "?session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=f"{settings.base_url}/billing/cancel",
            metadata={
                "user_id": current_user.id,
                "credits": str(settings.credit_pack_credits),
            },
            customer_email=current_user.email,
        )
    except stripe.StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Stripe error: {exc.user_message or str(exc)}",
        )

    # Persist a pending purchase record so we can reconcile the webhook.
    await create_credit_purchase(
        db=db,
        user_id=current_user.id,
        stripe_session_id=session.id,
        credits=settings.credit_pack_credits,
        amount_cents=settings.credit_pack_price_usd,
    )

    return CheckoutResponse(checkout_url=session.url)


@router.post(
    "/webhook",
    summary="Stripe webhook endpoint — do not call manually",
    include_in_schema=False,
)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receives Stripe webhook events.  Only ``checkout.session.completed`` is
    handled; all other event types are acknowledged and ignored.

    Auth middleware is intentionally bypassed — Stripe signs the payload
    with the webhook secret instead.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe webhook signature.",
        )

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        stripe_session_id: str = session_obj["id"]

        # complete_credit_purchase is idempotent — it skips processing if the
        # purchase is already marked "completed".
        await complete_credit_purchase(db, stripe_session_id)

    # Always return 200 so Stripe doesn't retry unnecessarily.
    return {"received": True}


@router.get(
    "/success",
    response_model=SuccessResponse,
    summary="Landing page after a successful Stripe Checkout",
)
async def payment_success(session_id: str) -> SuccessResponse:
    """
    Stripe redirects the user here after a successful payment.
    Credits are added asynchronously via the webhook; this endpoint is
    purely informational.
    """
    return SuccessResponse(
        message="Payment successful! Credits added to your account.",
        session_id=session_id,
    )


@router.get(
    "/cancel",
    response_model=CancelResponse,
    summary="Landing page when the user cancels Stripe Checkout",
)
async def payment_cancel() -> CancelResponse:
    return CancelResponse(message="Payment cancelled.")


@router.get(
    "/credits",
    response_model=CreditsResponse,
    summary="Return the authenticated user's credit balance and pricing info",
)
async def get_credits(
    current_user: User = Depends(get_current_user),
) -> CreditsResponse:
    cost_per_credit = (
        f"${settings.credit_pack_price_usd / 100:.2f}"
        f" / {settings.credit_pack_credits} credits"
    )
    return CreditsResponse(
        credits=current_user.credits,
        cost_per_minute=settings.credits_per_minute,
        cost_per_credit=cost_per_credit,
    )
