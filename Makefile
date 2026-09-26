.DEFAULT_GOAL := help
COMPOSE := docker compose
.PHONY: help env up down restart logs ps health migrate revision downgrade psql \
        bot worker userbot userbot-login shell install test lint fmt check gcal-auth \
        backfill backup backup-key backup-key-show restore doctor

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "Created .env — fill it in before 'make up'")

# --- Docker -----------------------------------------------------------------
doctor: ## Check .env and the server before make up
	$(COMPOSE) build api
	$(COMPOSE) run --rm --no-deps api python -m miya.tools.doctor

up: env $(if $(SKIP_DOCTOR),,doctor) ## Build and start everything, then run migrations (SKIP_DOCTOR=1 to skip the check)
	$(COMPOSE) up -d --build db api
	$(MAKE) migrate
	$(COMPOSE) up -d --build bot worker userbot
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

userbot: ## Tail the Telegram userbot logs
	$(COMPOSE) logs -f --tail=100 userbot

userbot-login: ## One-time Telethon login; prints TELETHON_SESSION for .env
	$(COMPOSE) run --rm -it userbot python -m miya.tools.userbot_login

backfill: ## Backfill one chat's history: make backfill CHAT=@akmal DAYS=7
	$(COMPOSE) run --rm userbot python -m miya.tools.backfill "$(CHAT)" --days $${DAYS:-7}

backup-key: ## Create the backup key and print the .env line
	@test ! -e secrets/backup-key.txt || { echo "secrets/backup-key.txt allaqachon bor — yangisi yaratilmaydi (eski zaxiralar faqat eski kalit bilan ochiladi)."; exit 1; }
	$(COMPOSE) up init
	$(COMPOSE) run --rm --no-deps worker age-keygen -o /app/secrets/backup-key.txt
	@echo "Quyidagi qatorni .env faylidagi BACKUP_AGE_RECIPIENT= o'rniga yoz:"
	@$(COMPOSE) run --rm --no-deps worker sh -c 'printf "BACKUP_AGE_RECIPIENT=%s\n" "$$(age-keygen -y /app/secrets/backup-key.txt)"'
	@echo "Maxfiy kalitni server tashqarisida saqla: make backup-key-show — chiqqan qatorlarni parol menejeriga ko'chir."

backup-key-show: ## Print the secret backup key (store it off the server)
	$(COMPOSE) run --rm --no-deps worker cat /app/secrets/backup-key.txt

backup: ## Run the encrypted database backup now
	$(COMPOSE) run --rm --no-deps worker python -c \
		"import asyncio; from miya.services.backup import create_backup; \
		 print(asyncio.run(create_backup()))"

restore: ## Restore a backup: make restore FILE=/data/backups/miya-….dump.age [DRY=1] [FORCE=1]
	@test -n "$(FILE)" || (echo "usage: make restore FILE=/data/backups/miya-….dump.age [DRY=1] [FORCE=1]" && exit 1)
	$(COMPOSE) run --rm --no-deps worker python -m miya.tools.restore "$(FILE)" \
		--identity /app/secrets/backup-key.txt $(if $(DRY),--dry-run,) $(if $(FORCE),--force,)

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
