# TranscodeAPI 🎬

Simple, cheap video transcoding API. Pay per minute. No queues to manage, no servers to provision.

## Pricing

| | |
|---|---|
| 💳 Free credits | 100 credits on signup |
| 🎬 Cost | 10 credits / minute of video |
| 💰 Top up | $5.00 = 1,000 credits = **100 minutes** |

---

## Quick Start

### 1. Register

```bash
curl -X POST https://api.transcodeapi.dev/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

```json
{ "api_key": "tca_...", "credits": 100 }
```

### 2. Transcode a video

```bash
curl -X POST https://api.transcodeapi.dev/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: tca_..." \
  -d '{
    "input_url": "https://example.com/video.mp4",
    "output_format": "mp4",
    "output_resolution": "1280x720"
  }'
```

```json
{ "id": "job-uuid", "status": "pending", "credits_used": 0 }
```

### 3. Check status

```bash
curl https://api.transcodeapi.dev/jobs/{job_id} \
  -H "X-API-Key: tca_..."
```

```json
{ "status": "completed", "output_url": "/jobs/{id}/download", "credits_used": 30 }
```

### 4. Download output

```bash
curl https://api.transcodeapi.dev/jobs/{job_id}/download \
  -H "X-API-Key: tca_..." \
  -o output.mp4
```

---

## Supported Formats

| Format | Codec | Notes |
|--------|-------|-------|
| `mp4`  | H.264 + AAC | Web-optimized, fast start |
| `webm` | VP9 + Opus  | Open format, smaller files |
| `gif`  | Palette-optimized | 10fps, looping |
| `mov`  | H.264 + AAC | Apple-compatible |
| `mkv`  | H.264 + AAC | Flexible container |

## Resolutions

Pass as a `WxH` string. Omit to keep the original resolution.

```
"1920x1080"  →  Full HD
"1280x720"   →  HD
"854x480"    →  SD
"640x360"    →  Mobile
```

---

## Buy Credits

```bash
curl -X POST https://api.transcodeapi.dev/billing/checkout \
  -H "X-API-Key: tca_..."
```

Returns a Stripe checkout URL. After payment, credits are applied instantly.

---

## Job Lifecycle

```
pending → processing → completed
                    ↘ failed
```

| Status | Meaning |
|--------|---------|
| `pending` | Job queued, waiting for worker |
| `processing` | ffmpeg running |
| `completed` | Output ready to download |
| `failed` | Something went wrong — credits refunded |

---

## Self-host

**Local dev (uv):**
```bash
git clone https://github.com/your-org/transcode-api
cd transcode-api
cp .env.example .env   # fill in Stripe keys + SECRET_KEY
uv sync
uv run uvicorn main:app --reload
```

**Docker:**
```bash
cp .env.example .env
docker-compose up
```

The API will be available at `http://localhost:8000`.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Random secret for signing tokens |
| `DATABASE_URL` | SQLAlchemy DB URL (default: SQLite) |
| `STORAGE_DIR` | Where output files are stored |
| `STRIPE_SECRET_KEY` | Stripe secret key (`sk_...`) |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `STRIPE_PRICE_ID` | Price ID for the credit pack |

### Deploy to Fly.io

```bash
fly launch --no-deploy
fly volumes create transcode_storage --size 10
fly secrets set SECRET_KEY=$(openssl rand -hex 32)
fly secrets set STRIPE_SECRET_KEY=sk_live_...
fly secrets set STRIPE_WEBHOOK_SECRET=whsec_...
fly secrets set STRIPE_PRICE_ID=price_...
fly deploy
```

---

## API Reference

Interactive docs available at:

- **Swagger UI** → [`/docs`](https://api.transcodeapi.dev/docs)
- **ReDoc** → [`/redoc`](https://api.transcodeapi.dev/redoc)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/auth/register` | Register and get API key |
| `GET` | `/auth/me` | Get current user info |
| `POST` | `/jobs` | Submit a transcode job |
| `GET` | `/jobs` | List your jobs |
| `GET` | `/jobs/{id}` | Get job status |
| `GET` | `/jobs/{id}/download` | Download output file |
| `POST` | `/billing/checkout` | Create Stripe checkout session |

---

## Smoke Test

```bash
chmod +x scripts/test_api.sh
./scripts/test_api.sh http://localhost:8000
```

Runs through: health check → register → submit job → poll status.

---

## License

MIT
