"""
Job enqueueing helper.

The actual processing logic has moved to workers/arq_worker.py (ARQ + Redis).
This module now exposes a single async helper — enqueue_job() — that pushes
a job ID into the Redis queue so the ARQ worker can pick it up immediately.

The ArqRedis pool is created once at startup and stored on the module; callers
just call `await enqueue_job(job_id)`.
"""

from __future__ import annotations

from arq import ArqRedis
from arq.connections import create_pool

from workers.arq_worker import _redis_settings

# ---------------------------------------------------------------------------
# Module-level pool — initialised by init_arq_pool() at app startup
# ---------------------------------------------------------------------------

_arq_pool: ArqRedis | None = None


async def init_arq_pool() -> None:
    """Create (or re-use) the shared ArqRedis connection pool."""
    global _arq_pool
    _arq_pool = await create_pool(_redis_settings())
    print("[job_worker] ARQ Redis pool ready")


async def close_arq_pool() -> None:
    """Close the pool gracefully on shutdown."""
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.aclose()
        _arq_pool = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def enqueue_job(job_id: str) -> None:
    """Push job_id to Redis so the ARQ worker picks it up immediately."""
    if _arq_pool is None:
        raise RuntimeError("ARQ pool not initialised — call init_arq_pool() first")
    await _arq_pool.enqueue_job("process_job", job_id)
    print(f"[job_worker] Enqueued job {job_id}")
