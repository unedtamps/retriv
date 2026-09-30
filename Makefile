SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
HOST ?= 127.0.0.1
PORT ?= 8000
WAIT_TIMEOUT ?= 300

UV_RUN := uv run --env-file $(ENV_FILE) --
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: help install infra worker migrate api run status logs down test check check-env

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run      Start everything and run the API in the foreground' \
		'  api      Start infrastructure and the worker, then run only the API' \
		'  worker   Build and start the processing worker (Docker)' \
		'  infra    Start and wait for PostgreSQL, Qdrant, MinIO, and ClamAV' \
		'  migrate  Apply database migrations' \
		'  status   Show infrastructure container status' \
		'  logs     Follow infrastructure logs' \
		'  down     Stop infrastructure containers' \
		'  install  Install project and development dependencies' \
		'  test     Run the test suite' \
		'  check    Run tests, lint, and type checking'

check-env:
	@test -f "$(ENV_FILE)" || { \
		printf 'Missing %s. Create it from .env.example and configure its secrets.\n' "$(ENV_FILE)"; \
		exit 1; \
	}

install:
	uv sync

# Everything except the worker. The worker is a Docker build, so it is
# started separately by `make worker`.
infra: check-env
	$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT) \
		postgres qdrant minio minio-init clamav

migrate: infra
	$(UV_RUN) alembic upgrade head

worker: check-env
	$(COMPOSE) up -d --build worker

api: worker
	@$(UV_RUN) alembic upgrade head
	$(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT)

run: api

status: check-env
	$(COMPOSE) ps

logs: check-env
	$(COMPOSE) logs -f

down: check-env
	$(COMPOSE) down

test:
	uv run pytest -q

check:
	uv run pytest -q
	uv run ruff check .
	uv run mypy app provisioning worker alembic tests
