PYTHON ?= .venv/bin/python
FLAGPILOT ?= .venv/bin/flagpilot
PYTEST ?= .venv/bin/pytest
RUFF ?= .venv/bin/ruff

.DEFAULT_GOAL := help

.PHONY: help install test lint smoke up bootstrap down demo demo-plan demo-apply demo-run demo-rollback demo-report

help:
	@echo "Available targets: help install test lint smoke up bootstrap down demo-plan demo-apply demo-run demo-rollback demo-report demo"

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTEST) -v

lint:
	$(RUFF) check src tests

smoke:
	$(FLAGPILOT) bootstrap --wait-only
	FLAGPILOT_LIVE_TEST=1 $(PYTEST) -m live -v

up:
	docker compose up -d

bootstrap:
	$(FLAGPILOT) bootstrap

down:
	docker compose down

demo-plan:
	$(FLAGPILOT) plan "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%"

# HUMAN INTERACTION REQUIRED: type APPROVE when prompted
demo-apply:
	$(FLAGPILOT) apply plan.json

# HUMAN INTERACTION REQUIRED: type APPROVE once upfront for schedule & auto-rollback to 0%
demo-run:
	$(FLAGPILOT) run \
		--feature checkout_v2 \
		--env Development \
		--stages 1 10 50 100 \
		--max-error-rate 0.02 \
		--baseline-error-rate 0.005 \
		--spike-rate 0.25 \
		--spike-threshold-pct 25 \
		--seed 42 \
		--interval 0 \
		--rollback-to-zero

# HUMAN INTERACTION REQUIRED: type APPROVE when prompted
demo-rollback:
	$(FLAGPILOT) rollback \
		--feature checkout_v2 \
		--env Development \
		--old-percentage 0 \
		--reason "demo cleanup"

demo-report:
	$(FLAGPILOT) report --input reports/last_run.json --audit-log audit.log --out reports/rollout.html

demo:
	bash scripts/demo
