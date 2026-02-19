from pydantic_settings import BaseSettings
from typing import Optional
import os


class Settings(BaseSettings):
    app_env: str = "development"
    secret_key: str = "changeme"
    base_url: str = "http://localhost:8000"

    database_url: str = "sqlite+aiosqlite:///./transcode.db"
    storage_dir: str = "./storage"

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    max_concurrent_jobs: int = 2
    max_video_duration_seconds: int = 3600

    credits_per_minute: int = 10        # credits consumed per minute of video
    credit_pack_credits: int = 1000     # credits per pack
    credit_pack_price_usd: int = 500    # in cents ($5.00)

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()

# Ensure storage dir exists
os.makedirs(settings.storage_dir, exist_ok=True)
