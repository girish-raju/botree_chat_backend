# syntax=docker/dockerfile:1

########################################
# Stage 1: build dependencies
########################################
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

# Install into an isolated prefix so stage 2 can copy just the site-packages.
RUN pip install --no-cache-dir --prefix=/install .

########################################
# Stage 2: runtime image
########################################
FROM python:3.11-slim AS runtime

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Shell form so ${PORT} (injected by managed hosts like Railway) is expanded;
# falls back to 8000 for local/docker-compose use.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
