FROM python:3.11-slim

# Install ffmpeg + curl (for uv install)
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copy dependency files first (cache-friendly)
COPY pyproject.toml uv.lock ./

# Install dependencies (no project, sync from lockfile)
RUN uv sync --frozen --no-install-project

# Copy source
COPY . .

# Create storage dir
RUN mkdir -p storage

EXPOSE 8000

# Run with uv's managed venv
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
