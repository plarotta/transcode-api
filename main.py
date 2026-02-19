from fastapi import FastAPI
from contextlib import asynccontextmanager
from database import init_db
from routers import transcode, billing, auth
import asyncio


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    # Start background job worker
    from workers.job_worker import start_worker
    worker_task = asyncio.create_task(start_worker())
    yield
    # Shutdown
    worker_task.cancel()


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
