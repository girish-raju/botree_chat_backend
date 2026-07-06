# syntax=docker/dockerfile:1

########################################
# Stage 1: build dependencies
########################################
FROM python:3.11-slim AS builder

ARG PRELOAD_EMBEDDINGS=false

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

# Install into an isolated prefix so stage 2 can copy just the site-packages.
RUN pip install --no-cache-dir --prefix=/install .

# Optionally warm the sentence-transformers embedding model cache at build
# time so the first request in prod doesn't pay the download cost. Off by
# default to keep everyday builds fast. The cache dir is always created so
# the later COPY of /root/.cache never fails regardless of this flag.
RUN mkdir -p /root/.cache && \
    if [ "$PRELOAD_EMBEDDINGS" = "true" ]; then \
        PYTHONPATH=/install/lib/python3.11/site-packages python -c \
        "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"; \
    fi

########################################
# Stage 2: runtime image
########################################
FROM python:3.11-slim AS runtime

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /root/.cache /root/.cache
COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
