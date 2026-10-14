SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
HOST ?= 127.0.0.1
PORT ?= 8000
WAIT_TIMEOUT ?= 300

UV_RUN := uv run --env-file $(ENV_FILE) --
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: help install infra worker run-worker run-build migrate api run provision status logs down test check check-env

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run       Start everything, reusing the worker image already built' \
		'  run-build Rebuild the worker image, then start everything' \
		'  api       Start the worker without rebuilding, then run only the API' \
		'  worker    Build and start the processing worker (Docker)' \
		'  run-worker Start the worker without rebuilding it' \
		'  infra     Start and wait for PostgreSQL, Qdrant, MinIO, and ClamAV' \
		'  sql-role  Create or repair the read-only SQL role and its grants' \
		'  migrate   Apply database migrations' \
		'  provision Apply the provisioning manifest in MinIO (one-shot)' \
		'  status    Show infrastructure container status' \
		'  logs      Follow infrastructure logs' \
		'  down      Stop infrastructure containers' \
		'  install   Install project and development dependencies' \
		'  test      Run the test suite' \
		'  check     Run tests, lint, and type checking'

check-env:
	@test -f "$(ENV_FILE)" || { \
		printf 'Missing %s. Create it from .env.example and configure its secrets.\n' "$(ENV_FILE)"; \
		exit 1; \
	}

install:
	uv sync

# Everything except the worker. The worker is a Docker container, so it is
# started separately by `make run-worker` or `make worker`.
#
# sql-role runs last and as a separate step because `up --wait` is the wrong
# tool for a one-shot service: it waits for a container to be *running*, and
# this script finishes in under a second, so compose reports the exit as a
# failure and returns 1 even when the script succeeded. `run --rm` waits for the
# job to finish and hands back its exit code, which is what a build step needs.
infra: check-env
	$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT) \
		postgres qdrant minio minio-init clamav
	$(MAKE) sql-role

migrate: infra
	$(UV_RUN) alembic upgrade head

# Build the worker image and start it. The slow path, and the only one that
# picks up a change in the worker's source or its Dockerfile.
worker: check-env
	$(COMPOSE) up -d --build worker

# The same worker from the image already built. Compose builds it only when
# the image is missing, so a restart costs seconds instead of a Docling and
# PyTorch rebuild.
run-worker: check-env
	$(COMPOSE) up -d worker

# Migrate, then run the API in the foreground. Runs the worker but never
# rebuilds it; `make run-build` is the one that rebuilds.
#
# The reader role is brought up here too, and not only through `infra`, because
# this target does not depend on it: with the DSN configured the API calls
# verify_reader() during startup and refuses to start against a missing role,
# so a developer running `make api` on a fresh clone would hit a failure whose
# cause is one compose service they never named.
api: run-worker sql-role
	@$(UV_RUN) alembic upgrade head
	$(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT)

run: api

# Create or repair the read-only SQL role and its grants. Idempotent and fast,
# so it is also the recovery path when the password in .env has been rotated or
# the role has been dropped.
sql-role: check-env
	$(COMPOSE) run --rm postgres-init

# Rebuild the worker image, then do what `make run` does.
run-build: worker
	@$(MAKE) api

# The one-shot that creates the first users and workspaces. Not part of any
# other target: it writes to every user row and bumps auth_version, which
# signs live sessions out, so it must be something a person asks for.
#
# --env-file is required. The job's settings set env_file=None and read the
# environment only, so without it every PROVISIONER_* variable is missing and
# it fails on the first one. All the missing ones are listed at once now.
#
# The manifest has to be in MinIO already, under the prefix in
# PROVISIONER_MANIFEST_PREFIX. Uploading it is a deliberate act: the job
# trusts whatever it finds there, and migration-reader is read-only by design.
#
# Exit 2 means the operator's input was wrong: bad settings, an unreadable
# manifest, a manifest that failed validation, or a migration id already
# applied with different content. Each of those names the field or the s3
# code and prints a hint. Exit 1 is reserved for a genuine bug and prints the
# traceback.
provision: migrate
	$(UV_RUN) python -m provisioning

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
