# VENDORED_FROM dailyJournal@6b1f67f Makefile
# Target names are the ones CLAUDE.md lists; 06 §3.3 says what each does.
# Targets that drive a tool a later task builds (backup, restore,
# import-clients, push-test, purge, smoke) name it here already, so the
# Makefile does not change when the tool lands.
.DEFAULT_GOAL := help
COMPOSE := docker compose
.PHONY: help env doctor up update down reload logs ps psql migrate revision downgrade \
        bot worker userbot userbot-login backup backup-key backup-key-show restore \
        import-clients push-test purge install test lint fmt typecheck check db-test hooks smoke

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo ".env yaratildi — make up dan oldin uni toʻldir")

# --- Docker -----------------------------------------------------------------
doctor: env ## Check .env, the server and the database before make up
	$(COMPOSE) build worker
	$(COMPOSE) run --rm --no-deps -e CS_IN_UPDATE=$(IN_UPDATE) worker python -m clientscrapper.tools.doctor

up: env $(if $(SKIP_DOCTOR),,doctor) ## Build and start everything; init migrates (SKIP_DOCTOR=1 to skip the check)
	$(COMPOSE) up -d --build db init
	$(COMPOSE) up -d --build worker userbot bot

update: ## Backup (must succeed), pull, rebuild, prune
	@echo "Yangilashdan oldin zaxira nusxa olinmoqda…"
	$(MAKE) backup
	git pull --ff-only
	@# The old processes stop first: a running worker would re-write rows a
	@# migration just changed.
	$(COMPOSE) stop worker userbot bot
	$(MAKE) up IN_UPDATE=1
	docker image prune -f
	@echo "Yangilandi. Botda /holat ni tekshir."

down: ## Stop everything (volumes are kept)
	$(COMPOSE) down

reload: ## After editing .env: recreate worker, userbot and bot so they read it again
	$(COMPOSE) up -d --force-recreate worker userbot bot

logs: ## Tail logs from all running services
	$(COMPOSE) logs -f --tail=100

ps: ## Show container status
	$(COMPOSE) ps

# The role and database come from the db container's own environment (compose
# set them from .env); the host shell never loaded .env, so expanding them here
# fell back to the defaults whenever the owner had changed them.
psql: ## Open a psql shell
	$(COMPOSE) exec db sh -c 'exec psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

# --- Database ---------------------------------------------------------------
migrate: ## Apply all migrations
	$(COMPOSE) run --rm init alembic upgrade head

# 02 §5 names migrations 000N_<slug>: the id is minted here and alembic.ini's
# file_template names the file after it.
revision: ## Autogenerate a migration 000N_<slug> on this machine (needs DATABASE_URL): make revision m="add x"
	@test -n "$(m)" || (echo "usage: make revision m=\"message\"" && exit 1)
	rev_id="$$(python scripts/revision_id.py "$(m)")" && \
		alembic revision --autogenerate -m "$(m)" --rev-id "$$rev_id"

downgrade: ## Roll back one migration
	$(COMPOSE) run --rm init alembic downgrade -1

# --- Processes --------------------------------------------------------------
bot: ## Tail the owner bot logs
	$(COMPOSE) logs -f --tail=100 bot

worker: ## Tail the scheduler logs
	$(COMPOSE) logs -f --tail=100 worker

userbot: ## Tail the Telegram reader logs
	$(COMPOSE) logs -f --tail=100 userbot

userbot-login: ## One-time Telethon login; prints TELETHON_SESSION for .env
	$(COMPOSE) run --rm -it userbot python -m clientscrapper.tools.userbot_login

# --- Backups ----------------------------------------------------------------
backup-key: ## Create the backup key and print the .env line
	@test ! -e secrets/backup-key.txt || { echo "secrets/backup-key.txt allaqachon bor — yangisi yaratilmaydi (eski zaxiralar faqat eski kalit bilan ochiladi)."; exit 1; }
	$(COMPOSE) up init
	$(COMPOSE) run --rm --no-deps worker age-keygen -o /app/secrets/backup-key.txt
	@echo "Quyidagi qatorni .env faylidagi BACKUP_AGE_RECIPIENT= oʻrniga yoz:"
	@$(COMPOSE) run --rm --no-deps worker sh -c 'printf "BACKUP_AGE_RECIPIENT=%s\n" "$$(age-keygen -y /app/secrets/backup-key.txt)"'
	@echo "Maxfiy kalitni server tashqarisida saqla: make backup-key-show — chiqqan qatorlarni parol menejeriga koʻchir."

backup-key-show: ## Print the secret backup key (store it off the server)
	$(COMPOSE) run --rm --no-deps worker cat /app/secrets/backup-key.txt

backup: ## Run the encrypted database backup now (fails when the backup does, so make update stops)
	$(COMPOSE) run --rm --no-deps worker python -c \
		"import asyncio, sys; from clientscrapper.services.backup import create_backup; \
		 result = asyncio.run(create_backup()); print(result); \
		 sys.exit(0 if result.ok else 1)"

restore: ## Restore a backup: make restore FILE=/data/backups/clientscrapper-….dump.age [DRY=1] [FORCE=1]
	@test -n "$(FILE)" || (echo "usage: make restore FILE=/data/backups/clientscrapper-….dump.age [DRY=1] [FORCE=1]" && exit 1)
	$(COMPOSE) run --rm --no-deps worker python -m clientscrapper.tools.restore "$(FILE)" \
		--identity /app/secrets/backup-key.txt $(if $(DRY),--dry-run,) $(if $(FORCE),--force,)

# --- Data -------------------------------------------------------------------
# The file goes into the server's data/imports/ first (mounted as /data); a
# dry run by default. ACCEPT_FEWER=1 lets one export with fewer rows than the
# previous import's through (it reads like a filtered screen otherwise).
import-clients: ## Load the ERP client export as suppression: make import-clients FILE=/data/imports/clients.xlsx [APPLY=1] [ACCEPT_FEWER=1]
	@test -n "$(FILE)" || (echo "usage: make import-clients FILE=/data/imports/clients.xlsx [APPLY=1] [ACCEPT_FEWER=1]" && exit 1)
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.import_clients "$(FILE)" \
		$(if $(APPLY),--apply,) $(if $(ACCEPT_FEWER),--accept-fewer,)

push-test: ## Push one visibly synthetic lead to the ERP (05 §B.1)
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.push_test

purge: ## Forget a subject: make purge COMPANY=<uuid> | PHONE=<tail9> | TG_USER=<id> [APPLY=1]
	@test -n "$(COMPANY)$(PHONE)$(TG_USER)" || (echo "usage: make purge COMPANY=<uuid> | PHONE=<tail9> | TG_USER=<id> [APPLY=1]" && exit 1)
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.purge \
		$(if $(COMPANY),--company "$(COMPANY)",) $(if $(PHONE),--phone "$(PHONE)",) \
		$(if $(TG_USER),--tg-user "$(TG_USER)",) $(if $(APPLY),--apply,)

# --- Local development ------------------------------------------------------
install: ## Install dev dependencies into the active virtualenv
	pip install -r requirements-dev.txt

# One shell line, gates joined by &&: a piped gate would report the pipe's
# exit code instead of its own.
test: ## ruff + mypy (scoped) + pytest (needs postgres: make db-test)
	ruff check . && ruff format --check . && mypy && pytest -q

lint: ## Lint
	ruff check .

fmt: ## Format
	ruff format .

typecheck: ## mypy strict on the files listed in pyproject.toml
	mypy

check: test ## Same gates as make test

# Migrated, with the scratch database test_migrations needs, so the printed
# line is the whole recipe. TCP readiness, not the socket: during initdb the
# image runs a socket-only server that is then restarted.
DB_TEST_URL = postgresql+psycopg://cs:cs@localhost:$${DB_PORT:-5433}
db-test: ## Throwaway postgres:16 (cs_test migrated + cs_test_rt) on 127.0.0.1:DB_PORT (5433); prints the line to run
	docker run -d --rm --name clientscrapper-test-db \
		-e POSTGRES_USER=cs -e POSTGRES_PASSWORD=cs -e POSTGRES_DB=cs_test \
		-p 127.0.0.1:$${DB_PORT:-5433}:5432 postgres:16
	@for i in $$(seq 60); do \
		docker exec clientscrapper-test-db pg_isready -q -h 127.0.0.1 -U cs -d cs_test && exit 0; \
		sleep 1; \
	done; echo "postgres did not start in 60 s"; exit 1
	docker exec clientscrapper-test-db psql -q -h 127.0.0.1 -U cs -d cs_test -c 'CREATE DATABASE cs_test_rt'
	DATABASE_URL=$(DB_TEST_URL)/cs_test alembic upgrade head
	@echo "DATABASE_URL=$(DB_TEST_URL)/cs_test CS_MIGRATION_DATABASE_URL=$(DB_TEST_URL)/cs_test_rt CS_REQUIRE_DB=1 make test"

# git rev-parse finds the hooks directory even where .git is a file (a
# worktree shares the main checkout's hooks, and so its script).
hooks: ## Install the pre-push gate (scripts/pre-push.sh)
	@test -x scripts/pre-push.sh || (echo "scripts/pre-push.sh yoʻq yoki bajariladigan emas" && exit 1)
	ln -sf ../../scripts/pre-push.sh "$$(git rev-parse --git-path hooks)/pre-push"

smoke: ## Live smoke tests, one real request per collector (VPS only)
	LIVE=1 pytest -q -m live tests/live
