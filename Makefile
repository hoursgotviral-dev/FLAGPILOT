PYTHON ?= .venv/bin/python
FLAGPILOT ?= .venv/bin/flagpilot
PYTEST ?= .venv/bin/pytest
RUFF ?= .venv/bin/ruff

.PHONY: install test lint demo demo-plan demo-apply demo-run demo-rollback demo-report

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTEST) -v

lint:
	$(RUFF) check src tests

demo-plan:
	$(FLAGPILOT) plan "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%"

# HUMAN INTERACTION REQUIRED: type APPROVE when prompted
demo-apply:
	$(FLAGPILOT) apply plan.json

# HUMAN INTERACTION REQUIRED: type APPROVE for each stage (1%, 10%, 50%) and triggered rollback (10%)
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
		--interval 0

# HUMAN INTERACTION REQUIRED: type APPROVE when prompted
demo-rollback:
	$(FLAGPILOT) rollback \
		--feature checkout_v2 \
		--env Development \
		--old-percentage 10 \
		--reason "demo cleanup"

demo-report:
	$(FLAGPILOT) report --input reports/last_run.json --audit-log audit.log --out reports/rollout.html

demo:
	bash scripts/demo

