FROM python:3.13-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /uvx /bin/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-ind \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra extraction --no-install-project
RUN docling-tools models download layout tableformer \
    --output-dir /opt/docling-models

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY worker ./worker
COPY README.md ./

RUN uv sync --frozen --no-dev --extra extraction \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app
EXPOSE 8080

ENV INTERNAL_DOCLING_ARTIFACTS_PATH=/opt/docling-models

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
