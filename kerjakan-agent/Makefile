SHELL := /bin/bash
.DEFAULT_GOAL := help

ENV_FILE ?= .env
COMPOSE := docker compose --env-file $(ENV_FILE)

.PHONY: help check-env run up build restart status logs down

help:
	@printf '%s\n' \
		'Usage: make <target>' \
		'' \
		'  run      Build and start the Kerjakan agent in the background' \
		'  up       Alias for run' \
		'  build    Build the agent image' \
		'  restart  Restart the running agent' \
		'  status   Show the agent container status' \
		'  logs     Follow the agent logs' \
		'  down     Stop the agent container'

check-env:
	@test -f "$(ENV_FILE)" || { \
		printf 'Missing %s. Create it from .env.example and configure its secrets.\n' "$(ENV_FILE)"; \
		exit 1; \
	}

run: check-env
	$(COMPOSE) up -d --build

up: run

build: check-env
	$(COMPOSE) build

restart: check-env
	$(COMPOSE) restart agent

status: check-env
	$(COMPOSE) ps

logs: check-env
	$(COMPOSE) logs -f agent

down: check-env
	$(COMPOSE) down
