# VENDORED_FROM dailyJournal@6b1f67f Makefile
# Target names are the ones CLAUDE.md lists; 06 §3.3 says what each does.
# Targets that drive a tool a later task builds (backup, restore,
# import-clients, push-test, purge, smoke) name it here already, so the
# Makefile does not change when the tool lands.
.DEFAULT_GOAL := help
COMPOSE := docker compose
# A switch on the command line (DRY=1, FORCE=1, APPLY=1, ACCEPT_FEWER=1): «1»
# is on, empty or «0» is off, and any other value stops make with a message
# — so DRY=yes never runs a real restore and APPLY=0 never applies. A plain
# $(if $(X),…) would read every non-empty value, «0» included, as on.
switch = $(if $(filter-out 0 1,$(strip $($(1)))),$(error $(1)=$($(1)): use $(1)=1 (on) or $(1)=0 (off)),$(filter 1,$(strip $($(1)))))
.PHONY: help env doctor up update down reload logs ps psql migrate revision downgrade \
        bot worker userbot userbot-login backup backup-key backup-key-show restore \
        import-clients erp-sync push-test purge install test lint fmt typecheck check db-test hooks smoke smoke-cron

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo ".env yaratildi — make up dan oldin uni toʻldir")

# --- Docker -----------------------------------------------------------------
doctor: env ## Check .env, the server and the database before make up
	$(COMPOSE) build worker
	$(COMPOSE) run --rm --no-deps -e CS_IN_UPDATE=$(IN_UPDATE) worker python -m clientscrapper.tools.doctor

up: env $(if $(filter 1,$(strip $(SKIP_DOCTOR))),,doctor) ## Build and start everything; init migrates (SKIP_DOCTOR=1 to skip the check)
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
# The key is written through init's ./secrets mount: the app services mount
# it read-only (MIYA's worker mounted it writable). As the app's uid, so the
# worker can read the 0600 file it writes; `up init` handed /secrets over.
backup-key: ## Create the backup key and print the .env line
	@test ! -e secrets/backup-key.txt || { echo "secrets/backup-key.txt allaqachon bor — yangisi yaratilmaydi (eski zaxiralar faqat eski kalit bilan ochiladi)."; exit 1; }
	$(COMPOSE) up init
	$(COMPOSE) run --rm --no-deps --user 10001:10001 init age-keygen -o /secrets/backup-key.txt
	@echo "Quyidagi qatorni .env faylidagi BACKUP_AGE_RECIPIENT= oʻrniga yoz:"
	@$(COMPOSE) run --rm --no-deps worker sh -c 'printf "BACKUP_AGE_RECIPIENT=%s\n" "$$(age-keygen -y /app/secrets/backup-key.txt)"'
	@echo "Maxfiy kalitni server tashqarisida saqla: make backup-key-show — chiqqan qatorlarni parol menejeriga koʻchir."

backup-key-show: ## Print the secret backup key (store it off the server)
	$(COMPOSE) run --rm --no-deps worker cat /app/secrets/backup-key.txt

# The nightly job itself, not create_backup alone: the `backup` heartbeat, the
# drill's counts and the Telegram copy, so a good run clears backup_failed.
backup: ## Run the encrypted database backup now (fails when the backup does, so make update stops)
	$(COMPOSE) run --rm --no-deps worker python -m clientscrapper.worker.main backup

restore: ## Restore a backup: make restore FILE=/data/backups/clientscrapper-….dump.age [DRY=1] [FORCE=1]
	@test -n "$(FILE)" || (echo "usage: make restore FILE=/data/backups/clientscrapper-….dump.age [DRY=1] [FORCE=1]" && exit 1)
	$(COMPOSE) run --rm --no-deps worker python -m clientscrapper.tools.restore "$(FILE)" \
		--identity /app/secrets/backup-key.txt $(if $(call switch,DRY),--dry-run,) $(if $(call switch,FORCE),--force,)

# --- Data -------------------------------------------------------------------
# The file goes into the server's data/imports/ first (mounted as /data); a
# bare FILE name is looked for there, a name with a «/» is passed as given. A
# dry run by default. ACCEPT_FEWER=1 lets one export with fewer rows than the
# previous import's through (it reads like a filtered screen otherwise); only
# «1» switches a flag on (``switch``), so ACCEPT_FEWER=0 is off.
import-clients: ## Load the ERP client export as suppression: make import-clients FILE=clients.xlsx [APPLY=1] [ACCEPT_FEWER=1]
	@test -n "$(FILE)" || (echo "usage: make import-clients FILE=clients.xlsx (in data/imports/) [APPLY=1] [ACCEPT_FEWER=1]" && exit 1)
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.import_clients \
		"$(if $(findstring /,$(FILE)),$(FILE),/data/imports/$(FILE))" \
		$(if $(call switch,APPLY),--apply,) $(if $(call switch,ACCEPT_FEWER),--accept-fewer,)

# One ERP feed walk by hand (06 §4.6, 07 §5.6): the run the scheduler makes.
# Exit 3 when it was skipped (disabled, feeds not configured, already running).
# A WALK that is not exactly one of ERP_WALKS stops make with the list.
ERP_WALKS := full delta outcomes quarter year
erp_walk = $(if $(and $(filter $(ERP_WALKS),$(WALK)),$(filter 1,$(words $(WALK)))),$(strip $(WALK)),$(error WALK=$(WALK): use one of $(ERP_WALKS)))
erp-sync: ## One ERP feed walk: make erp-sync WALK=full|delta|outcomes|quarter|year [ACCEPT_SHRINK=1]
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.erp_sync --$(erp_walk) \
		$(if $(call switch,ACCEPT_SHRINK),--accept-shrink,)

push-test: ## Push one visibly synthetic lead to the ERP (05 §B.1)
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.push_test

purge: ## Forget a subject: make purge COMPANY=<uuid> | PHONE=<tail9> | TG_USER=<id> [APPLY=1]
	@test -n "$(COMPANY)$(PHONE)$(TG_USER)" || (echo "usage: make purge COMPANY=<uuid> | PHONE=<tail9> | TG_USER=<id> [APPLY=1]" && exit 1)
	$(COMPOSE) run --rm worker python -m clientscrapper.tools.purge \
		$(if $(COMPANY),--company "$(COMPANY)",) $(if $(PHONE),--phone "$(PHONE)",) \
		$(if $(TG_USER),--tg-user "$(TG_USER)",) $(if $(call switch,APPLY),--apply,)

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

# The live tests run in the compose `smoke` service (the app image plus pytest
# and tests/, 07 §1.12): the VPS has no dev venv and the app image ships
# neither. Weekly: every live test but the `quarterly` ones; FULL=1: all of
# them (the 1st of Jan/Apr/Jul/Oct). The outcomes land in collector_runs for
# the next morning's digest («Tekshiruv: ✓ N / ✗ M / oʻtkazildi K»).
SMOKE_MARK = $(if $(call switch,FULL),live,live and not quarterly)
smoke: ## Live smoke tests, one real request per collector, VPS only (FULL=1: the quarterly full run)
	$(COMPOSE) --profile smoke run --rm --build $(if $(call switch,FULL),-e CS_SMOKE_FULL=1,) smoke \
		python -m pytest -q -rs -p no:cacheprovider -m "$(SMOKE_MARK)" tests/live

# The host's crontab lines for this checkout (07 §1.12: Sunday 05:00 weekly,
# quarterly on the 1st; the full run half an hour later so a Sunday 1st does
# not run twice at once). Printed, not installed: add them with `crontab -e`.
smoke-cron: ## Print the VPS crontab lines for make smoke (weekly and quarterly)
	@echo "0 5 * * 0 cd $(CURDIR) && make smoke >> $(CURDIR)/data/smoke.log 2>&1"
	@echo "30 5 1 1,4,7,10 * cd $(CURDIR) && make smoke FULL=1 >> $(CURDIR)/data/smoke.log 2>&1"
