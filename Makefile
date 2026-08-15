.DEFAULT_GOAL := help

VERSION := $(shell grep -m1 '^version' pyproject.toml | cut -d'"' -f2)

# Throwaway directory used by `make smoke`; override to test somewhere else.
SMOKE_DIR ?= /tmp/dai-smoke
# Target directory for `make run-here`.
DIR ?= .

## Development

.PHONY: install
install: ## Install dependencies (prod + dev) via uv
	uv sync

.PHONY: test
test: ## Run tests
	uv run pytest tests/ -v

.PHONY: test-quick
test-quick: ## Run tests, quiet
	uv run pytest -q

.PHONY: doctor
doctor: ## Check the CLIs dai drives are installed
	@command -v uv     >/dev/null && echo "  uv     $$(uv --version | cut -d' ' -f2)"     || echo "  uv     NOT FOUND"
	@command -v claude >/dev/null && echo "  claude $$(claude --version | cut -d' ' -f1)" || echo "  claude NOT FOUND — dai cannot run"
	@command -v codex  >/dev/null && echo "  codex  $$(codex --version | cut -d' ' -f2)"  || echo "  codex  NOT FOUND — dai cannot run"

.PHONY: smoke
smoke: ## End-to-end check in a throwaway sandbox — SPENDS TOKENS (override SMOKE_DIR)
	@rm -rf $(SMOKE_DIR) && mkdir -p $(SMOKE_DIR)
	@printf '# Services\n\n## auth\nPort 8080. Team platform. In production since 2024-03.\n\n## search\nPort 8082. Team platform. Beta, not yet GA.\n' > $(SMOKE_DIR)/README.md
	@printf '# Service matrix\n\n| Service | Port | Team | Status |\n|---------|------|------|--------|\n| auth    |      |      |        |\n| search  |      |      |        |\n' > $(SMOKE_DIR)/matrix.md
	@cd $(SMOKE_DIR) && git init -q . && git add -A && \
		git -c user.email=dai@localhost -c user.name=dai commit -qm "initial"
	@echo "sandbox: $(SMOKE_DIR)"
	uv run dai -C $(SMOKE_DIR) --no-tui --rounds 2 --budget 1 \
		"Fill in the empty cells of the table in matrix.md, using README.md as the source of truth."
	@echo "--- matrix.md ---"; cat $(SMOKE_DIR)/matrix.md
	@echo "--- git there: HEAD must be unmoved, only matrix.md modified ---"; \
		cd $(SMOKE_DIR) && git log --oneline && git status --short
	@echo "--- snapshots dai took ---"; \
		cd $(SMOKE_DIR) && git for-each-ref --format='  %(refname)' refs/dai/

## Run

.PHONY: run
run: ## Run in the current directory (pass ARGS, e.g. make run ARGS="'fill in docs/matrix.md'")
	uv run dai $(ARGS)

.PHONY: run-here
run-here: ## Run against another directory (make run-here DIR=~/proj ARGS="'the task'")
	uv run dai -C $(DIR) $(ARGS)

.PHONY: run-no-tui
run-no-tui: ## Run with plain streaming output, for scripts and CI
	uv run dai --no-tui $(ARGS)

.PHONY: run-dry
run-dry: ## Run with both agents read-only — nothing on disk is modified
	uv run dai --dry-run $(ARGS)

.PHONY: run-swap
run-swap: ## Run with the roles swapped (codex solves, claude critiques)
	uv run dai --solver codex --critic claude $(ARGS)

.PHONY: init
init: ## Write the default config to ~/.config/dai/config.toml
	uv run dai --init

.PHONY: runs
runs: ## List past runs recorded in the current directory
	uv run dai --runs

.PHONY: snapshots
snapshots: ## Show the git snapshots dai took in this repo
	@git for-each-ref --format='  %(refname)  %(objectname:short)' refs/dai/ || true
	@echo "  (diff two of them: git diff <ref-a> <ref-b>)"

## Build

.PHONY: build
build: ## Build wheel and sdist into dist/
	uv build

## Install

.PHONY: install-cli
install-cli: ## Install the dai command globally via uv
	uv tool install . --force
	@echo "installed dai $(VERSION) via uv"

.PHONY: uninstall-cli
uninstall-cli: ## Uninstall the dai command installed by uv
	uv tool uninstall dai

.PHONY: pipx-install
pipx-install: build ## Install dai globally via pipx (from the local wheel)
	pipx install dist/*.whl --force
	@echo "installed dai $(VERSION) via pipx"

.PHONY: pipx-uninstall
pipx-uninstall: ## Uninstall dai from pipx
	pipx uninstall dai

## Cleanup

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf dist/ build/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

.PHONY: clean-runs
clean-runs: ## Delete recorded run transcripts in .dai/ (does NOT touch git snapshots)
	rm -rf .dai/runs

.PHONY: clean-all
clean-all: clean ## Remove everything including venv
	rm -rf .venv

## Help

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
