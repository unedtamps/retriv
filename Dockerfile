# Two targets from one file. The API needs none of the extraction stack;
# the worker needs all of it. Building both from a shared base keeps the
# lockfile and the copy steps in one place.
#
# Layer order is deliberate: manifests and dependencies come before the
# source copy, so editing app/ or worker/ rebuilds only the project install
# -- not LibreOffice, the extraction dependencies or the OCR models.
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

# The runtime user owns /app because LibreOffice, which the worker runs for
# .pptx rendering, writes its profile to $HOME/.config.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown app:app /app

COPY pyproject.toml uv.lock README.md ./

# Dependencies only. The project wheel is installed after the source copy,
# so a code change never invalidates this layer.
RUN uv sync --frozen --no-dev --no-install-project \
    && chown -R app:app /app/.venv

# --- API: no extraction extra, so no OCR engine and no native parsers ---
FROM base AS api

COPY app ./app
COPY worker ./worker
COPY alembic.ini ./
COPY alembic ./alembic

RUN uv sync --frozen --no-dev \
    && chown -R app:app /app/app /app/worker /app/alembic /app/alembic.ini

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

# The extraction dependencies and the OCR models, before the source copy: a
# code change rebuilds the last two layers, not this one.
RUN uv sync --frozen --no-dev --extra extraction --no-install-project \
    && python -c "from rapidocr import RapidOCR; RapidOCR()" \
    && chown -R app:app /app/.venv

COPY app ./app
COPY worker ./worker
COPY alembic.ini ./
COPY alembic ./alembic

RUN uv sync --frozen --no-dev --extra extraction \
    && chown -R app:app /app/app /app/worker /app/alembic /app/alembic.ini

USER app

CMD ["python", "-m", "worker"]
