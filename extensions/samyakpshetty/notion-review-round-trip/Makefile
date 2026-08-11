# Every target runs in Docker (Python 3.12) so local == CI, byte for byte.
# The lint/type/test targets use --no-deps: the keyless suite needs no Postgres.

.DEFAULT_GOAL := help
COMPOSE := docker compose
RUN := $(COMPOSE) run --rm --no-deps app

.PHONY: help build lint fmt typecheck test check demo demo-live up down clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

build: ## Build the app image
	$(COMPOSE) build

lint: build ## Ruff lint
	$(RUN) ruff check .

fmt: build ## Ruff format (writes)
	$(RUN) ruff format .

typecheck: build ## mypy (strict, a real gate)
	$(RUN) mypy src

test: build ## Run the keyless test suite
	$(RUN) pytest

check: build ## The full CI gate: lint + typecheck + tests
	$(RUN) sh -c "ruff check . && ruff format --check . && mypy src && pytest"

demo: build ## Full fake round-trip over the real Postgres substrate (no keys, no ops)
	$(COMPOSE) run --rm app python -m notion_review.cli demo

demo-live: build ## Real Notion + SuperDocs round-trip (needs SUPERDOCS_API_KEY + NOTION_TOKEN)
	PROVIDER=live $(COMPOSE) run --rm app python -m notion_review.cli demo --live

up: ## Start db + app in the background
	$(COMPOSE) up -d

down: ## Stop and remove containers
	$(COMPOSE) down

clean: ## Remove containers, images, volumes for this project
	$(COMPOSE) down --rmi local --volumes --remove-orphans
