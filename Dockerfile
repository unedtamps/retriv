# Two targets from one file. The API needs none of the extraction stack;
# the worker needs all of it. Building both from a shared base keeps the
# lockfile and the copy steps in one place.
#
# The API image is ~250 MB. The worker is ~1.7 GB, almost all of it torch.
# That difference is the reason the worker is a separate process, not a
# separate codebase.

FROM python:3.13-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY README.md ./
COPY app ./app
COPY provisioning ./provisioning
COPY worker ./worker
COPY alembic.ini ./
COPY alembic ./alembic

# --- API: no extraction extra, so no torch, no Docling, no tesseract ---
FROM base AS api

RUN uv sync --frozen --no-dev --no-install-project \
    && uv sync --frozen --no-dev \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Worker: extraction extra, OCR, and baked model weights ---
FROM base AS worker

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-ind \
    && rm -rf /var/lib/apt/lists/*

RUN uv sync --frozen --no-dev --extra extraction --no-install-project \
    && docling-tools models download layout tableformer \
        --output-dir /opt/docling-models \
    && uv sync --frozen --no-dev --extra extraction \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app

ENV RETRIV_DOCLING_ARTIFACTS_PATH=/opt/docling-models

CMD ["python", "-m", "worker"]
