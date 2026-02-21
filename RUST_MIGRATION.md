# TranscodeAPI → Rust Migration Plan

**Stack:** Axum + SQLx + Tokio + Stripe
**Goal:** 1:1 functional parity with the Python MVP, with better performance and type safety
**Estimated effort:** 3–5 days for a solo developer familiar with Rust

---

## Why Rust?

| Concern | Python/FastAPI | Rust/Axum |
|---|---|---|
| Cold start | ~800ms | ~5ms |
| Memory (idle) | ~80MB | ~8MB |
| CPU (FFmpeg dispatch) | GIL-limited | True parallelism |
| Type safety | Runtime errors | Compile-time guarantees |
| Binary deploy | Needs Python runtime | Single static binary |

---

## Target Stack

| Layer | Python | Rust |
|---|---|---|
| Web framework | FastAPI | **Axum** (Tokio-native, zero-cost middleware) |
| Async runtime | asyncio | **Tokio** |
| Database | SQLAlchemy (async) | **SQLx** (async, compile-time checked queries) |
| DB migrations | alembic / auto | **sqlx-cli migrate** |
| Config | pydantic-settings | **config** crate + envy |
| Auth middleware | FastAPI `Depends` | **Tower Layer** + Axum extractor |
| Stripe | stripe-python | **async-stripe** crate |
| FFmpeg | asyncio.subprocess | **tokio::process::Command** |
| Worker queue | asyncio.Queue | **tokio::sync::mpsc** |
| Concurrency cap | asyncio.Semaphore | **tokio::sync::Semaphore** |
| Serialization | Pydantic | **serde / serde_json** |
| Tests | pytest + httpx | **axum::test + tokio::test** |
| File serving | FileResponse | **axum::body::Body** + `tokio::fs` |

---

## Project Structure

```
transcode-api-rs/
├── Cargo.toml
├── .env.example
├── migrations/
│   ├── 20250101_create_users.sql
│   ├── 20250102_create_jobs.sql
│   └── 20250103_create_credit_purchases.sql
├── src/
│   ├── main.rs               # App bootstrap, lifespan
│   ├── config.rs             # Settings struct
│   ├── db.rs                 # DB pool init + migration runner
│   ├── error.rs              # AppError enum → HTTP responses
│   ├── models/
│   │   ├── mod.rs
│   │   ├── user.rs           # User struct (sqlx FromRow)
│   │   ├── job.rs            # Job struct + JobStatus enum
│   │   └── credit_purchase.rs
│   ├── middleware/
│   │   ├── mod.rs
│   │   └── auth.rs           # Tower Layer: X-API-Key → CurrentUser
│   ├── routes/
│   │   ├── mod.rs
│   │   ├── auth.rs           # POST /auth/register, GET /auth/me
│   │   ├── jobs.rs           # POST/GET /jobs, GET /jobs/:id, GET /jobs/:id/download
│   │   └── billing.rs        # POST /billing/checkout, POST /billing/webhook
│   ├── services/
│   │   ├── mod.rs
│   │   ├── transcoder.rs     # probe_video(), transcode_video(), build_ffmpeg_cmd()
│   │   └── user_service.rs   # create_user(), get_by_email(), deduct_credits()
│   └── worker/
│       └── mod.rs            # Job worker: mpsc queue + semaphore + process_job()
└── tests/
    ├── test_auth.rs
    ├── test_jobs.rs
    ├── test_health.rs
    └── test_transcoder.rs
```

---

## Phase 1: Project Setup & Config (Day 1, ~2h)

### 1.1 Cargo.toml

```toml
[package]
name = "transcode-api"
version = "0.1.0"
edition = "2021"

[dependencies]
# Web
axum = { version = "0.7", features = ["multipart", "macros"] }
tower = "0.4"
tower-http = { version = "0.5", features = ["cors", "trace", "fs"] }
tokio = { version = "1", features = ["full"] }

# DB
sqlx = { version = "0.7", features = ["sqlite", "runtime-tokio", "chrono", "uuid"] }

# Config
config = "0.14"
dotenvy = "0.15"

# Serialization
serde = { version = "1", features = ["derive"] }
serde_json = "1"

# Stripe
async-stripe = { version = "0.37", features = ["runtime-tokio-hyper"] }

# Auth
uuid = { version = "1", features = ["v4"] }
chrono = { version = "0.4", features = ["serde"] }
rand = "0.8"

# Error handling
thiserror = "1"
anyhow = "1"

# HTTP client (for Stripe webhooks)
axum-extra = { version = "0.9", features = ["typed-header"] }
bytes = "1"
```

### 1.2 Config struct (`src/config.rs`)

```rust
use config::{Config, Environment};
use serde::Deserialize;

#[derive(Debug, Deserialize, Clone)]
pub struct Settings {
    pub app_env: String,
    pub secret_key: String,
    pub base_url: String,
    pub database_url: String,
    pub storage_dir: String,
    pub stripe_secret_key: String,
    pub stripe_webhook_secret: String,
    pub stripe_price_id: String,
    pub ffmpeg_path: String,
    pub ffprobe_path: String,
    pub max_concurrent_jobs: usize,
    pub max_video_duration_seconds: f64,
    pub credits_per_minute: i64,
    pub credit_pack_credits: i64,
    pub credit_pack_price_usd: i64,
}

impl Settings {
    pub fn load() -> anyhow::Result<Self> {
        let s = Config::builder()
            .set_default("app_env", "development")?
            .set_default("base_url", "http://localhost:8000")?
            .set_default("database_url", "sqlite://transcode.db")?
            .set_default("storage_dir", "./storage")?
            .set_default("ffmpeg_path", "ffmpeg")?
            .set_default("ffprobe_path", "ffprobe")?
            .set_default("max_concurrent_jobs", num_cpus::get() as i64)?
            .set_default("max_video_duration_seconds", 3600.0)?
            .set_default("credits_per_minute", 10)?
            .set_default("credit_pack_credits", 1000)?
            .set_default("credit_pack_price_usd", 500)?
            .add_source(Environment::default().separator("_"))
            .build()?;
        Ok(s.try_deserialize()?)
    }
}
```

---

## Phase 2: Database & Models (Day 1, ~3h)

### 2.1 Migrations

**`migrations/001_create_users.sql`**
```sql
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    api_key TEXT NOT NULL UNIQUE,
    credits INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL
);
```

**`migrations/002_create_jobs.sql`**
```sql
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending',
    input_url TEXT NOT NULL,
    output_format TEXT NOT NULL,
    output_resolution TEXT,
    output_url TEXT,
    duration_seconds REAL,
    credits_charged INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
```

### 2.2 Job model (`src/models/job.rs`)

```rust
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;

#[derive(Debug, Clone, Serialize, Deserialize, FromRow)]
pub struct Job {
    pub id: String,
    pub user_id: String,
    pub status: String,   // "pending" | "processing" | "completed" | "failed"
    pub input_url: String,
    pub output_format: String,
    pub output_resolution: Option<String>,
    pub output_url: Option<String>,
    pub duration_seconds: Option<f64>,
    pub credits_charged: Option<i64>,
    pub error_message: Option<String>,
    pub created_at: DateTime<Utc>,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
}
```

---

## Phase 3: Error Handling (Day 1, ~1h)

This is critical in Axum — define it once and everything composes cleanly.

```rust
// src/error.rs
use axum::{http::StatusCode, response::{IntoResponse, Response}, Json};
use serde_json::json;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum AppError {
    #[error("Not found")]
    NotFound,
    #[error("Unauthorized")]
    Unauthorized,
    #[error("Conflict: {0}")]
    Conflict(String),
    #[error("Bad request: {0}")]
    BadRequest(String),
    #[error("Payment required: {0}")]
    PaymentRequired(String),
    #[error("Stripe error: {0}")]
    Stripe(String),
    #[error("Internal error: {0}")]
    Internal(#[from] anyhow::Error),
    #[error("Database error: {0}")]
    Db(#[from] sqlx::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AppError::NotFound => (StatusCode::NOT_FOUND, self.to_string()),
            AppError::Unauthorized => (StatusCode::UNAUTHORIZED, self.to_string()),
            AppError::Conflict(m) => (StatusCode::CONFLICT, m.clone()),
            AppError::BadRequest(m) => (StatusCode::BAD_REQUEST, m.clone()),
            AppError::PaymentRequired(m) => (StatusCode::PAYMENT_REQUIRED, m.clone()),
            AppError::Stripe(m) => (StatusCode::BAD_GATEWAY, m.clone()),
            AppError::Internal(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
            AppError::Db(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
        };
        (status, Json(json!({"detail": message}))).into_response()
    }
}

pub type Result<T> = std::result::Result<T, AppError>;
```

---

## Phase 4: Auth Middleware (Day 2, ~2h)

The most idiomatic change from Python — Tower extractors replace FastAPI `Depends`.

```rust
// src/middleware/auth.rs
use axum::{
    async_trait,
    extract::{FromRequestParts, State},
    http::{request::Parts, HeaderMap},
};
use sqlx::SqlitePool;
use crate::{error::AppError, models::user::User};

pub struct CurrentUser(pub User);

#[async_trait]
impl<S> FromRequestParts<S> for CurrentUser
where
    S: Send + Sync,
    SqlitePool: axum::extract::FromRef<S>,
{
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let api_key = parts
            .headers
            .get("X-API-Key")
            .and_then(|v| v.to_str().ok())
            .ok_or(AppError::Unauthorized)?;

        let pool = SqlitePool::from_ref(state);
        let user = sqlx::query_as::<_, User>(
            "SELECT * FROM users WHERE api_key = ?"
        )
        .bind(api_key)
        .fetch_optional(&pool)
        .await?
        .ok_or(AppError::Unauthorized)?;

        Ok(CurrentUser(user))
    }
}
```

---

## Phase 5: FFmpeg Service (Day 2, ~3h)

Direct translation of `services/transcoder.py` — the logic is identical.

```rust
// src/services/transcoder.rs
use tokio::process::Command;
use anyhow::{anyhow, Result};
use serde_json::Value;

pub struct ProbeInfo {
    pub duration: f64,
    pub width: u32,
    pub height: u32,
    pub video_codec: String,
    pub audio_codec: String,
}

pub async fn probe_video(source: &str, ffprobe: &str) -> Result<ProbeInfo> {
    let output = Command::new(ffprobe)
        .args(["-v", "quiet", "-print_format", "json",
               "-show_streams", "-show_format", source])
        .output()
        .await?;

    if !output.status.success() {
        return Err(anyhow!("ffprobe failed: {}", String::from_utf8_lossy(&output.stderr)));
    }

    let data: Value = serde_json::from_slice(&output.stdout)?;
    // ... parse streams (same logic as Python version)
    parse_probe_output(data)
}

pub fn build_ffmpeg_cmd(
    source: &str,
    output_path: &str,
    output_format: &str,
    output_resolution: Option<&str>,
    probe: &ProbeInfo,
    ffmpeg: &str,
) -> Vec<String> {
    let mut cmd = vec![ffmpeg.to_string(), "-y".to_string()];

    if source.starts_with("http") {
        cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"].map(String::from));
    }

    cmd.extend(["-i".to_string(), source.to_string(),
                "-threads".to_string(), "0".to_string()]);

    if can_copy_streams(probe, output_format, output_resolution) {
        cmd.extend(["-c", "copy"].map(String::from));
        if output_format == "mp4" {
            cmd.extend(["-movflags", "+faststart"].map(String::from));
        }
    } else {
        // ... encode path (same logic as Python)
        build_encode_args(&mut cmd, probe, output_format, output_resolution);
    }

    cmd.extend(["-fs", "2147483648"].map(String::from));
    cmd.push(output_path.to_string());
    cmd
}

pub async fn transcode_video(
    source: &str,
    output_path: &str,
    output_format: &str,
    output_resolution: Option<&str>,
    probe: &ProbeInfo,
    ffmpeg: &str,
) -> Result<()> {
    let cmd = build_ffmpeg_cmd(source, output_path, output_format, output_resolution, probe, ffmpeg);
    let output = Command::new(&cmd[0])
        .args(&cmd[1..])
        .output()
        .await?;

    if !output.status.success() {
        return Err(anyhow!("FFmpeg failed:\n{}", String::from_utf8_lossy(&output.stderr)));
    }

    Ok(())
}
```

> **Note on progress streaming:** Python uses `asyncio.create_subprocess_exec` with live stderr reads. In Rust, use `tokio::io::AsyncBufReadExt` + `child.stderr.take()` to stream progress lines in real-time — same pattern, slightly more explicit.

---

## Phase 6: Job Worker (Day 3, ~3h)

The asyncio.Queue → tokio::sync::mpsc translation is clean:

```rust
// src/worker/mod.rs
use tokio::sync::{mpsc, Semaphore};
use std::sync::Arc;
use sqlx::SqlitePool;

pub struct WorkerHandle {
    pub tx: mpsc::UnboundedSender<String>,  // job_id sender
}

pub fn start_worker(pool: SqlitePool, settings: Arc<Settings>) -> WorkerHandle {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let sem = Arc::new(Semaphore::new(settings.max_concurrent_jobs));

    tokio::spawn(async move {
        // Recover pending jobs on startup
        recover_pending_jobs(&pool, &tx).await;

        while let Some(job_id) = rx.recv().await {
            let pool = pool.clone();
            let settings = settings.clone();
            let permit = sem.clone().acquire_owned().await.unwrap();

            tokio::spawn(async move {
                process_job(&job_id, &pool, &settings).await;
                drop(permit);  // release semaphore slot
            });
        }
    });

    WorkerHandle { tx }
}

async fn process_job(job_id: &str, pool: &SqlitePool, settings: &Settings) {
    // 1. Claim job (UPDATE status = 'processing')
    // 2. probe_video()
    // 3. Check + atomically deduct credits:
    //    UPDATE users SET credits = credits - ?
    //    WHERE id = ? AND credits >= ?
    //    — check rows_affected == 1, else fail
    // 4. transcode_video()
    // 5. Mark completed, set output_url
    // On any error: set status = 'failed', refund credits if deducted
}
```

**Key improvement over Python:** Credit deduction is atomic via a single `UPDATE ... WHERE credits >= ?` — this eliminates the race condition from the current Python version.

---

## Phase 7: Routes (Day 3, ~3h)

```rust
// src/main.rs
use axum::{Router, routing::{get, post}};

pub fn app(state: AppState) -> Router {
    Router::new()
        .route("/health", get(routes::health))
        .route("/auth/register", post(routes::auth::register))
        .route("/auth/me", get(routes::auth::me))
        .route("/jobs", post(routes::jobs::submit).get(routes::jobs::list))
        .route("/jobs/:id", get(routes::jobs::get_job))
        .route("/jobs/:id/download", get(routes::jobs::download))
        .route("/billing/checkout", post(routes::billing::checkout))
        .route("/billing/webhook", post(routes::billing::webhook))
        .route("/billing/credits", get(routes::billing::credits))
        .with_state(state)
}
```

Route handlers are thin — auth extractor `CurrentUser` plugs in as a parameter, same as FastAPI's `Depends(get_current_user)`:

```rust
async fn submit_job(
    State(state): State<AppState>,
    CurrentUser(user): CurrentUser,          // ← Tower extractor, replaces Depends()
    Json(body): Json<TranscodeRequest>,
) -> crate::error::Result<impl IntoResponse> {
    // validate, enqueue, return 201
}
```

---

## Phase 8: Testing (Day 4, ~2h)

Axum has first-class test support — no test server needed:

```rust
#[tokio::test]
async fn test_register_success() {
    let app = test_app().await;  // in-memory SQLite, no Stripe
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/auth/register")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"email":"test@test.com"}"#))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::CREATED);
    let body: serde_json::Value = parse_body(response).await;
    assert!(body["api_key"].as_str().unwrap().starts_with("tca_"));
}
```

Test coverage targets: match the existing 29 Python tests 1:1, then add:
- Concurrent credit deduction test (verify no race condition)
- Worker recovery test (inject "processing" job, verify it's reset)

---

## Phase 9: Deployment (Day 4–5, ~2h)

### Dockerfile (multi-stage, ~15MB final image)

```dockerfile
FROM rust:1.76 AS builder
WORKDIR /app
COPY . .
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/transcode-api /usr/local/bin/
EXPOSE 8000
CMD ["transcode-api"]
```

### fly.toml — minimal changes needed:
- Update the build section to use the new Dockerfile
- Remove Python-specific build args

---

## Migration Checklist

- [ ] Phase 1: Project setup, Cargo.toml, config
- [ ] Phase 2: DB migrations, User/Job/CreditPurchase models
- [ ] Phase 3: Error type + IntoResponse impl
- [ ] Phase 4: Auth Tower extractor
- [ ] Phase 5: FFmpeg service (probe + transcode + command builder)
- [ ] Phase 6: Job worker (mpsc queue, semaphore, atomic credit deduction)
- [ ] Phase 7: All 8 route handlers
- [ ] Phase 8: 29+ tests (1:1 parity with Python suite)
- [ ] Phase 9: Dockerfile + Fly.io deploy
- [ ] Smoke test against production Stripe + real video URL

---

## What Gets Better (Not Just Equivalent)

1. **Atomic credit deduction** — eliminates the race condition in the Python worker (see Phase 6)
2. **Input URL validation** — `url::Url::parse()` at the type level, not a string check
3. **No `datetime.utcnow()` deprecation** — `chrono::Utc::now()` is the only way
4. **Single binary deploy** — no Python runtime, no venv, no dependency hell
5. **Memory** — expect ~8MB idle vs ~80MB in Python (matters on the $6/mo VPS)
6. **Pydantic warnings** — gone; serde has no deprecated config patterns

---

*Estimated total: 3–5 days for the full implementation + test parity.*
