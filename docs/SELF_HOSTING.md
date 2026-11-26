# Self-Hosting

## Prerequisites

- Python `>= 3.11`
- Docker & Docker Compose (with ports `8000` and `8001` free)

## Flagsmith Setup (Zero-Click Bootstrap)

1. Start the official self-hosted Flagsmith stack from the repository root:

   ```bash
   docker compose up -d
   ```

2. Run FlagPilot's idempotent bootstrapper (polls `http://localhost:8000/health/liveness/`, checks `GET /version/`, creates the initial superuser or logs in, creates/reuses organisation/project/environment, mints or preserves the Master API key and server SDK key, creates the `checkout_v2` demo feature with global default `false` and a `0%` `PERCENTAGE_SPLIT` segment override, and writes local `.env` without printing secrets):

   - **macOS / Linux**: `.venv/bin/flagpilot bootstrap` (or `make up && make bootstrap`)
   - **Windows PowerShell**: `.venv\Scripts\flagpilot.exe bootstrap` (or `.\tasks.ps1 up; .\tasks.ps1 bootstrap`)

3. Re-running `flagpilot bootstrap` is safe and idempotent: existing valid Master API keys and server SDK keys in `.env` are preserved.

## Environment Configuration

`flagpilot bootstrap` creates and updates `.env` (gitignored) automatically. See `.env.example` for the full schema:

```text
FLAGSMITH_API_URL=http://localhost:8000
FLAGSMITH_ADMIN_KEY=<master-api-key>
FLAGSMITH_CLIENT_KEY=<environment-client-key>
FLAGSMITH_SERVER_KEY=<ser.server-sdk-key>
FLAGSMITH_ENVIRONMENT_KEY=<environment-client-key>
FLAGSMITH_PROJECT_ID=<project-id>
FLAGSMITH_FEATURE_ID=<feature-id>
FLAGSMITH_SEGMENT_ID=<segment-id>
```

Never commit `.env` or print keys into terminal logs or reports.

## Running FlagPilot

Install locally in a virtual environment:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip.exe install -e ".[dev]"
```

Run the end-to-end interactive demo (type `APPROVE` at each prompt):

- **macOS / Linux (`bash`)**: `bash scripts/demo` or `make demo`
- **Windows PowerShell**: `.\tasks.ps1 demo`

Run the live integration smoke test against `http://localhost:8000`:

- **macOS / Linux**: `make smoke` (`FLAGPILOT_LIVE_TEST=1 .venv/bin/pytest -m live -v`)
- **Windows PowerShell**: `.\tasks.ps1 smoke`

Shut down containers when finished (`docker compose down`, or `docker compose down -v` to wipe Flagsmith database state).

## Planner Modes (Zero-Key Default vs. Optional LLM Adapter)

- **Default zero-key planner (`src/flagpilot/planner.py`)**: Deterministic regex/rule-based parser (`parse_plain_english_request`) that converts plain-English requests into `plan.json` and `plan.md` with zero external LLM dependencies or API keys.
- **Optional LLM adapter boundary**: Because downstream stages consume the validated `Plan` schema (`build_plan` / `plan_from_dict` in `src/flagpilot/planner.py`), you can optionally place an external LLM translator upstream to emit `plan.json`. All Flagsmith writes still pass through `src/flagpilot/apply.py`'s mandatory interactive `APPROVE` gate.
