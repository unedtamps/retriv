SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
HOST ?= 127.0.0.1
PORT ?= 8000
WAIT_TIMEOUT ?= 300

UV_RUN := uv run --env-file $(ENV_FILE) --
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: help install infra migrate api outbox run status logs down test check check-env

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run      Start infrastructure, migrate, then run API and outbox' \
		'  api      Start infrastructure, migrate, then run only the API' \
		'  outbox   Start infrastructure, migrate, then run only the relay' \
		'  infra    Start and wait for PostgreSQL, MinIO, and ClamAV' \
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

infra: check-env
	$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT)

migrate: infra
	$(UV_RUN) alembic upgrade head

api: migrate
	$(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT)

outbox: migrate
	$(UV_RUN) python -m outbox

run: migrate
	@set -Eeuo pipefail; \
	printf 'Starting API on http://%s:%s and outbox relay. Press Ctrl+C to stop both.\n' "$(HOST)" "$(PORT)"; \
	$(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT) & \
	api_pid=$$!; \
	$(UV_RUN) python -m outbox & \
	outbox_pid=$$!; \
	cleanup() { \
		trap - EXIT INT TERM; \
		kill "$$api_pid" "$$outbox_pid" 2>/dev/null || true; \
		wait "$$api_pid" "$$outbox_pid" 2>/dev/null || true; \
	}; \
	trap cleanup EXIT INT TERM; \
	wait -n "$$api_pid" "$$outbox_pid"

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
	uv run mypy app provisioning outbox alembic tests
