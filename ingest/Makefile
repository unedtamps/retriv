SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
HOST ?= 127.0.0.1
PORT ?= 8082
WAIT_TIMEOUT ?= 300

UV_RUN := uv run --env-file $(ENV_FILE) --
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: help install infra migrate api worker run status logs down test check check-env

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run      Start PostgreSQL, Qdrant, and worker, then run API on host' \
		'  api      Start PostgreSQL and Qdrant, migrate, then run the API' \
		'  worker   Start infrastructure, migrate, then build/start worker' \
		'  infra    Start and wait for PostgreSQL and Qdrant' \
		'  migrate  Apply database migrations' \
		'  status   Show infrastructure container status' \
		'  logs     Follow infrastructure logs' \
		'  down     Stop infrastructure containers' \
		'  install  Install host API and development dependencies' \
		'  test     Run the test suite' \
		'  check    Run tests, lint, and type checking'

check-env:
	@test -f "$(ENV_FILE)" || { \
		printf 'Missing %s. Create it from .env.example and configure its secrets.\n' "$(ENV_FILE)"; \
		exit 1; \
	}

install:
	uv sync

infra: check-env
	$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT) postgres qdrant

migrate: infra
	INTERNAL_COMPONENT=api $(UV_RUN) alembic upgrade head

api: migrate
	INTERNAL_COMPONENT=api $(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT)

worker: migrate
	$(COMPOSE) up -d --build worker

run: worker
	@printf 'Worker is running in Docker. Starting internal API on http://%s:%s.\n' "$(HOST)" "$(PORT)"
	@printf 'Press Ctrl+C to stop the API; run make down to stop Docker services.\n'
	INTERNAL_COMPONENT=api $(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT)

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
	uv run mypy app worker alembic tests
