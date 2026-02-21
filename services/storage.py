"""
Cloudflare R2 storage service.

R2 is S3-compatible — boto3 works with a custom endpoint_url.
All boto3 calls are run in asyncio.to_thread() to avoid blocking the event loop.
"""

import asyncio
import os

import boto3
from botocore.client import Config as BotocoreConfig

from config import settings


def _get_r2_client():
    """Build a boto3 S3 client pointed at Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        config=BotocoreConfig(signature_version="s3v4"),
        region_name="auto",
    )


async def upload_to_r2(local_path: str, key: str) -> str:
    """Upload a local file to R2 and return a presigned URL (1-hour expiry)."""

    def _upload() -> str:
        client = _get_r2_client()

        # Upload the file
        with open(local_path, "rb") as f:
            client.put_object(
                Bucket=settings.r2_bucket,
                Key=key,
                Body=f,
            )

        # If a public domain is configured, return a plain public URL instead of presigned
        if settings.r2_public_url:
            return f"{settings.r2_public_url.rstrip('/')}/{key}"

        # Otherwise generate a presigned URL (1 hour)
        url: str = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.r2_bucket, "Key": key},
            ExpiresIn=3600,
        )
        return url

    return await asyncio.to_thread(_upload)


async def delete_from_r2(key: str) -> None:
    """Delete an object from R2 by key."""

    def _delete() -> None:
        client = _get_r2_client()
        client.delete_object(Bucket=settings.r2_bucket, Key=key)

    await asyncio.to_thread(_delete)
