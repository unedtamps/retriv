SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
HOST ?= 127.0.0.1
PORT ?= 8000
WEB_DIR ?= web
WEB_HOST ?= 127.0.0.1
WEB_PORT ?= 5173
WAIT_TIMEOUT ?= 300

UV_RUN := uv run --env-file $(ENV_FILE) --
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: help install infra worker run-worker run-build migrate bootstrap api run web status logs down test check check-env sql-role

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run       Start the API and the web dev server, reusing the worker image' \
		'  run-build Rebuild the worker image, then start the API and the web dev server' \
		'  api       Start the worker without rebuilding, then run only the API' \
		'  web       Run only the web dev server (API must already be running)' \
		'  worker    Build and start the processing worker (Docker)' \
		'  run-worker Start the worker without rebuilding it' \
		'  infra     Start and wait for PostgreSQL, Qdrant, and ClamAV' \
		'  sql-role  Create or repair the read-only SQL role and its grants' \
		'  migrate   Apply database migrations' \
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

# Everything except the worker and object storage. The worker is a Docker
# container, so it is started separately by `make run-worker` or `make worker`.
# Object storage is an external S3-compatible provider; its buckets must exist
# before the API or worker can use them.
#
# sql-role runs last and as a separate step because `up --wait` is the wrong
# tool for a one-shot service: it waits for a container to be *running*, and
# this script finishes in under a second, so compose reports the exit as a
# failure and returns 1 even when the script succeeded. `run --rm` waits for the
# job to finish and hands back its exit code, which is what a build step needs.
infra: check-env
	$(COMPOSE) up -d --wait --wait-timeout $(WAIT_TIMEOUT) \
		postgres qdrant clamav
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

# The shared front half of `api` and `run`: start the worker without
# rebuilding it, the reader role, and the migrations. Split out so the
# API-only and API+web targets do not carry two copies of the same steps.
#
# The reader role is brought up here too, and not only through `infra`, because
# neither run target depends on it: with the DSN configured the API calls
# verify_reader() during startup and refuses to start against a missing role,
# so a developer running `make api` on a fresh clone would hit a failure whose
# cause is one compose service they never named.
bootstrap: run-worker sql-role
	@$(UV_RUN) alembic upgrade head

# Migrate, then run the API in the foreground. Runs the worker but never
# rebuilds it; `make run-build` is the one that rebuilds.
api: bootstrap
	$(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT)

# Everything `api` starts, plus the Vite dev server, in one foreground recipe.
# The two servers are background jobs of this shell, so they share one process
# group: Ctrl-C reaches both at once, and if either exits on its own the trap
# stops the other instead of leaving a half-running stack behind. Ctrl-C is a
# normal way to stop a dev stack, so the interrupted path exits 0; a server
# dying on its own still fails the target with its own status.
run: bootstrap
	@api_pid=''; web_pid=''; interrupted=0; \
	cleanup() { \
		trap - EXIT INT TERM; \
		[ -n "$$web_pid" ] && kill -TERM "$$web_pid" 2>/dev/null || true; \
		[ -n "$$api_pid" ] && kill -TERM "$$api_pid" 2>/dev/null || true; \
		wait 2>/dev/null || true; \
	}; \
	trap cleanup EXIT; \
	trap 'interrupted=1' INT TERM; \
	printf 'API   http://%s:%s\n' '$(HOST)' '$(PORT)'; \
	printf 'Web   http://%s:%s\n' '$(WEB_HOST)' '$(WEB_PORT)'; \
	$(UV_RUN) uvicorn app.main:app --host $(HOST) --port $(PORT) & api_pid=$$!; \
	(cd $(WEB_DIR) && npm run dev -- --host $(WEB_HOST) --port $(WEB_PORT)) & web_pid=$$!; \
	wait -n "$$api_pid" "$$web_pid"; status=$$?; \
	[ "$$interrupted" = 1 ] && status=0; \
	exit $$status

# Only the Vite dev server, for when the API is already running elsewhere.
web: check-env
	cd $(WEB_DIR) && npm run dev -- --host $(WEB_HOST) --port $(WEB_PORT)

# Create or repair the read-only SQL role and its grants. Idempotent and fast,
# so it is also the recovery path when the password in .env has been rotated or
# the role has been dropped.
sql-role: check-env
	$(COMPOSE) run --rm postgres-init

# Rebuild the worker image, then do what `make run` does (API + web).
run-build: worker
	@$(MAKE) run

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
