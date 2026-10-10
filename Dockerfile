# syntax=docker/dockerfile:1
#
# DeutschPath — single-container production image.
# Builds the Next.js frontend as a static export and serves it together with the
# FastAPI backend from one process/port, so the whole app runs as ONE service.
#
# ────────────────────────────────────────────────────────────────────────────
# Stage 1 — build the Next.js frontend (static export → /app/frontend/out)
# ────────────────────────────────────────────────────────────────────────────
FROM node:22-slim AS frontend

WORKDIR /app/frontend

# Install dependencies first so this layer is cached across source changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build the app. NEXT_PUBLIC_* values are inlined at build time; the browser will
# call the API on the same origin under /api (served by FastAPI in this image).
ENV NEXT_PUBLIC_API_URL=/api \
    NEXT_STATIC_EXPORT=1 \
    NEXT_TELEMETRY_DISABLED=1
COPY frontend/ ./
RUN npm run build

# The favicon is generated locally (git-ignored); derive it from the logo so the
# deployed app has an icon too.
RUN cp public/logo.png out/favicon.png


# ────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime: FastAPI serves the API (/api/*) and the static frontend
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app

# Backend dependencies
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Backend source
COPY backend/ ./

# Static frontend produced by stage 1 → backend/static (mounted at "/")
COPY --from=frontend /app/frontend/out ./static

# Mutable state (SQLite DB, uploaded books, usage stats) lives in DATA_DIR.
# Mount a persistent disk here to survive restarts/redeploys.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Render injects $PORT; fall back to 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
