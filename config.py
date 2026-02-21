from pydantic_settings import BaseSettings
from typing import Optional
import os


def _default_concurrency() -> int:
    """Use all CPU cores by default. Override via MAX_CONCURRENT_JOBS env var."""
    return os.cpu_count() or 2


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "changeme"
    base_url: str = "http://localhost:8000"

    database_url: str = "sqlite+aiosqlite:///./transcode.db"
    storage_dir: str = "./storage"

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""

    # Cloudflare R2 storage (S3-compatible)
    # Set USE_R2=true in production; leave false for local dev (no creds needed)
    use_r2: bool = False
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "transcode-outputs"
    r2_public_url: str = ""  # optional: custom public domain for R2 bucket

    redis_url: str = "redis://localhost:6379"

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    # Auto-detects CPU count; override with MAX_CONCURRENT_JOBS=N in .env
    max_concurrent_jobs: int = _default_concurrency()
    max_video_duration_seconds: int = 3600
    # ultrafast|superfast|veryfast|faster|fast — override with FFMPEG_PRESET=fast
    ffmpeg_preset: str = "ultrafast"

    credits_per_minute: int = 10        # credits consumed per minute of video
    credit_pack_credits: int = 1000     # credits per pack
    credit_pack_price_usd: int = 500    # in cents ($5.00)

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()

# Ensure storage dir exists
os.makedirs(settings.storage_dir, exist_ok=True)
