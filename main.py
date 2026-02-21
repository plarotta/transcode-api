import asyncio
from contextlib import asynccontextmanager

from arq import Worker
from fastapi import FastAPI

from database import init_db
from routers import auth, billing, transcode
from workers.arq_worker import WorkerSettings
from workers.job_worker import close_arq_pool, init_arq_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()

    # Initialise ARQ Redis pool used by enqueue_job()
    await init_arq_pool()

    # Launch ARQ worker in-process (background task).
    # For multi-process deployments run: arq workers.arq_worker.WorkerSettings
    worker = Worker(
        functions=WorkerSettings.functions,
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
        redis_settings=WorkerSettings.redis_settings,
        max_jobs=WorkerSettings.max_jobs,
        job_timeout=WorkerSettings.job_timeout,
        keep_result=WorkerSettings.keep_result,
        # Don't let the ARQ worker install its own signal handlers —
        # FastAPI / uvicorn owns SIGINT/SIGTERM in-process.
        handle_signals=False,
    )
    worker_task = asyncio.create_task(worker.async_run())

    yield

    # Shutdown
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    await close_arq_pool()


app = FastAPI(
    title="TranscodeAPI",
    description="Simple, cheap video transcoding API. Pay per minute.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(transcode.router, prefix="/jobs", tags=["jobs"])
app.include_router(billing.router, prefix="/billing", tags=["billing"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@app.get("/")
async def root():
    return {
        "name": "TranscodeAPI",
        "docs": "/docs",
        "pricing": "10 credits/minute of video. 1000 credits = $5.00",
    }
