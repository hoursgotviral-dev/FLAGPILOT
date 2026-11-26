# Self-Hosting

## Prerequisites

- Python `>= 3.11`
- A local self-hosted Flagsmith instance (for example via Flagsmith's Docker Compose setup, managed outside this repository)
- An Admin / Organisation API token (`FLAGSMITH_ADMIN_KEY`) and environment SDK key (`FLAGSMITH_SERVER_KEY` or `FLAGSMITH_CLIENT_KEY`)

## Flagsmith Setup

1. Start your self-hosted Flagsmith instance (for example on `http://localhost:8000`).
2. Create an organisation, project, and environment (such as `Development`) in the Flagsmith dashboard.
3. Generate an Organisation API Key (for the REST Management API) and copy the environment's Server-Side SDK Key (for `FlagsmithSDKEvaluator`).
4. Note that all Management API paths and Python SDK evaluation calls in `src/flagpilot/fsapi.py` and `src/flagpilot/simulator.py` are currently **UNVERIFIED** against a live instance; verify each endpoint against your local Flagsmith API docs (`/api/v1/docs/`) before relying on them in production.

## Environment Configuration

Copy `.env.example` to `.env` (gitignored; never commit `.env` or paste keys into terminal logs) and fill in your local values:

```text
FLAGSMITH_API_URL=http://localhost:8000/api/v1
FLAGSMITH_CLIENT_KEY=<your-client-key>
FLAGSMITH_SERVER_KEY=<your-server-key>
FLAGSMITH_ADMIN_KEY=<your-organisation-api-key>
```

Optional environment variables supported by `FlagsmithConfig.from_env()`:
- `FLAGSMITH_PROJECT_ID` (numeric project ID; if omitted, FlagPilot queries `GET /api/v1/projects/` — **UNVERIFIED**)
- `FLAGSMITH_ENVIRONMENT_KEY` (defaults to `FLAGSMITH_SERVER_KEY` or `FLAGSMITH_CLIENT_KEY`)

## Running FlagPilot

Install locally in a virtual environment:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run the demo steps interactively (typing `APPROVE` at each prompt):

- **POSIX / Git Bash**: `bash scripts/demo` or `make demo`
- **Windows PowerShell**: `.\tasks.ps1 demo`

To exercise the controller and simulator purely offline without a running Flagsmith SDK endpoint for traffic evaluation, pass `--offline-evaluator` to `flagpilot run` (writes still require an interactive `APPROVE` and a `FlagsmithClient` backend).
