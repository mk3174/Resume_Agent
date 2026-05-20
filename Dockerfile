# syntax=docker/dockerfile:1.7
# Resume Agent — Phase 5 service container.
#
# Builds a slim image that runs the FastAPI control plane. The Streamlit UI
# and Ollama daemon are run as separate services in docker-compose so each can
# scale or be swapped independently.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# System deps: libpq for Postgres, fonts for Typst PDF rendering.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    fonts-dejavu-core \
    fontconfig \
 && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution.
RUN pip install uv==0.4.18

COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY orchestrator ./orchestrator
COPY pipeline ./pipeline
COPY ingest ./ingest
COPY filters ./filters
COPY render ./render
COPY llm ./llm
COPY retrieval ./retrieval
COPY notify ./notify
COPY store ./store
COPY compute ./compute
COPY credentials ./credentials
COPY apply ./apply
COPY config ./config
COPY config_loader.py ./

# Install the project + Postgres driver + FastAPI extras. Patchright is omitted
# from the container build by default (Phase 4 LinkedIn / Indeed need a desktop
# session). Re-enable by uncommenting the line below.
RUN uv pip install --system ".[notify]" "fastapi>=0.115" "uvicorn[standard]>=0.30" "psycopg[binary]>=3.2"
# RUN uv run patchright install chromium

EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
