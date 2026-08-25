# ---------------------------------------------------------------------------
# Options Alpha Agents
# ---------------------------------------------------------------------------
.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- setup ------------------------------------------------------------------
.PHONY: setup
setup: venv install env ## One-shot: venv, deps, .env, config
	@echo ""
	@echo "Next:"
	@echo "  1. fill in .env (Alpaca paper keys for BOTH accounts)"
	@echo "  2. make doctor"
	@echo "  3. make scan"

$(VENV):
	$(PY) -m venv $(VENV)

.PHONY: venv
venv: $(VENV) ## Create the virtualenv

.PHONY: install
install: $(VENV) ## Install the package and all extras
	$(BIN)/pip install --upgrade pip wheel >/dev/null
	$(BIN)/pip install -e '.[all]'

.PHONY: env
env: ## Create .env and config/local.yaml from the examples
	@[ -f .env ] || (cp .env.example .env && echo "created .env - fill it in")
	@[ -f config/local.yaml ] || cp config/local.example.yaml config/local.yaml

.PHONY: tools
tools: ## Install the Alpaca CLI, MCP server and agent skills
	./scripts/install_tools.sh

# --- checks -----------------------------------------------------------------
.PHONY: doctor
doctor: ## Check every dependency and credential
	$(BIN)/oaa doctor

.PHONY: test
test: ## Run the test suite
	$(BIN)/pytest

.PHONY: lint
lint: ## Lint and type-check
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests || true

.PHONY: fmt
fmt: ## Auto-format
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

.PHONY: check
check: lint test ## Lint + test

# --- running ----------------------------------------------------------------
.PHONY: scan
scan: ## One dry scan cycle - what would the agent do right now?
	$(BIN)/oaa scan

.PHONY: run
run: ## Start the autonomous loop (dev profile)
	$(BIN)/oaa run

.PHONY: run-judged
run-judged: ## Start the autonomous loop against the JUDGED account
	@echo "This trades the account the judges evaluate."
	@read -p "Type 'judged' to confirm: " c; [ "$$c" = "judged" ] || exit 1
	$(BIN)/oaa run --profile judged

.PHONY: manage
manage: ## Apply exit rules to open positions
	$(BIN)/oaa manage

.PHONY: flatten
flatten: ## Close every position
	$(BIN)/oaa flatten

.PHONY: report
report: ## Build the performance report (JSON + HTML)
	$(BIN)/oaa report

.PHONY: serve
serve: ## Run the public dashboard
	$(BIN)/oaa serve

.PHONY: mcp-tools
mcp-tools: ## List the tools the Alpaca MCP server exposes
	$(BIN)/oaa mcp-tools

# --- deployment ---------------------------------------------------------------
.PHONY: pm2-dev
pm2-dev: ## Run the dev loop under PM2
	pm2 start ecosystem.config.js --only oaa-dev

.PHONY: pm2-judged
pm2-judged: ## Run the JUDGED loop under PM2 (this trades the submitted account)
	@echo "This trades the account the judges evaluate."
	@read -p "Type 'judged' to confirm: " c; [ "$$c" = "judged" ] || exit 1
	pm2 start ecosystem.config.js --only oaa-judged
	pm2 save

.PHONY: pm2-status
pm2-status: ## PM2 process table plus the firewall state
	pm2 list
	$(BIN)/oaa firewall

.PHONY: pm2-logs
pm2-logs: ## Tail the PM2 logs
	pm2 logs --lines 100

.PHONY: pm2-stop
pm2-stop: ## Stop every PM2 process (positions are NOT closed - use `make flatten`)
	pm2 stop all

# --- docker -----------------------------------------------------------------
.PHONY: docker-build
docker-build: ## Build the container image
	docker build -t oaa:latest .

.PHONY: docker-run
docker-run: ## Run the loop in the container
	docker run --rm --env-file .env -v $$(pwd)/runs:/app/runs oaa:latest

.PHONY: clean
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
