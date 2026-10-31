# Two targets from one file. The API needs none of the extraction stack;
# the worker needs all of it. Building both from a shared base keeps the
# lockfile and the copy steps in one place.
#
# The API image is ~250 MB. The worker is ~1 GB, dominated by the ONNX
# runtime and the OCR models. That difference is the reason the worker is a
# separate process, not a separate codebase.

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
COPY worker ./worker
COPY alembic.ini ./
COPY alembic ./alembic

# --- API: no extraction extra, so no OCR engine and no native parsers ---
FROM base AS api

RUN uv sync --frozen --no-dev --no-install-project \
    && uv sync --frozen --no-dev \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Worker: the extraction extra and the OCR models, baked at build ---
FROM base AS worker

# LibreOffice Impress renders a .pptx into the PDF the pipeline reads: a
# slide's charts and SmartArt are stored as recipes rather than pictures, so a
# parser reading the XML would lose the figure the slide exists to show. It is
# deliberately absent from the API image, which carries no office suite.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libreoffice-impress \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN uv sync --frozen --no-dev --extra extraction --no-install-project \
    && uv sync --frozen --no-dev --extra extraction \
    && python -c "from rapidocr import RapidOCR; RapidOCR()" \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app

CMD ["python", "-m", "worker"]
