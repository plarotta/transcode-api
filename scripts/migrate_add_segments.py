# Run once: python3 scripts/migrate_add_segments.py
# Adds segment tracking columns to existing jobs table
import asyncio
import sys
import os

# Allow running from the project root or from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from database import engine


async def migrate():
    async with engine.begin() as conn:
        for stmt in [
            "ALTER TABLE jobs ADD COLUMN parent_job_id TEXT REFERENCES jobs(id)",
            "ALTER TABLE jobs ADD COLUMN segment_index INTEGER",
            "ALTER TABLE jobs ADD COLUMN total_segments INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_jobs_parent_job_id ON jobs(parent_job_id)",
        ]:
            try:
                await conn.execute(text(stmt))
                print(f"OK: {stmt[:60]}...")
            except Exception as e:
                print(f"Skip (probably exists): {e}")


asyncio.run(migrate())
