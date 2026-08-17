.DEFAULT_GOAL := help
COMPOSE := docker compose
.PHONY: help env up down restart logs ps health migrate revision downgrade psql \
        bot worker shell install test lint fmt check gcal-auth

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "Created .env — fill it in before 'make up'")

# --- Docker -----------------------------------------------------------------
up: env ## Build and start everything, then run migrations
	$(COMPOSE) up -d --build db api
	$(MAKE) migrate
	$(COMPOSE) up -d --build bot worker
	@echo "API: http://127.0.0.1:$${API_PORT:-8000}/health"

down: ## Stop everything (volumes are kept)
	$(COMPOSE) down

restart: ## Restart the api container
	$(COMPOSE) restart api

logs: ## Tail logs from all running services
	$(COMPOSE) logs -f --tail=100

ps: ## Show container status
	$(COMPOSE) ps

health: ## Curl the health endpoint
	@curl -fsS "http://127.0.0.1:$${API_PORT:-8000}/health" && echo

# --- Database ---------------------------------------------------------------
migrate: ## Apply all migrations
	$(COMPOSE) run --rm api alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	@test -n "$(m)" || (echo "usage: make revision m=\"message\"" && exit 1)
	$(COMPOSE) run --rm api alembic revision --autogenerate -m "$(m)"

downgrade: ## Roll back one migration
	$(COMPOSE) run --rm api alembic downgrade -1

psql: ## Open a psql shell
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-miya} -d $${POSTGRES_DB:-miya}

# --- Processes --------------------------------------------------------------
bot: ## Tail the assistant bot logs
	$(COMPOSE) logs -f --tail=100 bot

worker: ## Tail the scheduler logs
	$(COMPOSE) logs -f --tail=100 worker

shell: ## Shell into the api container
	$(COMPOSE) run --rm api bash

gcal-auth: ## One-time Google Calendar OAuth (use with: ssh -L 8765:127.0.0.1:8765)
	$(COMPOSE) run --rm -p 127.0.0.1:8765:8765 -v ./secrets:/app/secrets worker \
		python -m miya.tools.gcal_auth

# --- Local development ------------------------------------------------------
install: ## Install dev dependencies into the active virtualenv
	pip install -r requirements-dev.txt

test: ## Run the test suite
	pytest -q

lint: ## Lint
	ruff check .

fmt: ## Format
	ruff format .

check: lint test ## Lint + test
