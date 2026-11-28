# FlagPilot

Terminal-first progressive rollout controller for self-hosted Flagsmith (`2.295.2`).
Turn plain-English rollout requests into deterministic stage plans gated by explicit human `APPROVE`.
Monitor simulated or external error-rate signals (`--signal-url` / `--signal-file`), fail closed to `0%`, and check cohort monotonicity.

![FlagPilot Rollout Report showing 1% to 10% to 50% monotonicity check, 26.32% error-spike rollback to 0%, 0/200 post-rollback verification, and Grafana Feature Health UNHEALTHY/HEALTHY webhooks](docs/assets/rollout-report.png)

## The problem

Feature flag rollouts often jump straight from disabled to 100% or rely on ad-hoc dashboard clicks with no repeatable safety net. When an error spike hits mid-rollout, operators have to scramble to diagnose which cohort is affected and manually revert state without an audit trail. Self-hosted teams need a terminal-first workflow that plans percentage stages deterministically, enforces explicit human sign-off on every change, and rolls back automatically when error rates breach a threshold.

## The approach

FlagPilot orchestrates staged **% Split** (`PERCENTAGE_SPLIT`) rollouts on self-hosted Flagsmith through four safeguards:

1. **Zero-key deterministic planning (`planner.py`)**: A rule-based parser turns a plain-English rollout request into `plan.json` and `plan.md` with no LLM, no external API keys, and zero network calls by default. An optional thin `plan_with_llm()` adapter (enabled only via `--use-llm` or `FLAGPILOT_LLM_PROVIDER` + `GEMINI_API_KEY`/`OPENAI_API_KEY`) requests strict `Plan` JSON via standard-library `urllib.request`, validates through `plan_from_dict()`, falls back cleanly to the deterministic parser on error, and passes through the identical diff review and mandatory `APPROVE` gate.
2. **Mandatory human approval (`apply.py`)**: `ApprovedFlagsmithWriter` gates every Flagsmith rollout mutation (`apply`, `run`, `rollback`), refusing non-interactive TTYs and anything other than exact `APPROVE` with no `--auto-approve` or `--yes` flag (while `bootstrap` for local dev setup and fail-safe Feature Health webhooks are intentionally exempt). On `flagpilot run`, one upfront `APPROVE` authorizes the schedule and safety policy so emergency rollback executes immediately during an incident instead of blocking on mid-incident human input (per-stage prompts remain opt-in via `--step-approve`).
3. **Stable persistent identities (`simulator.py`)**: Evaluations always supply persistent identities (`user_000`..`user_199` by default) because Flagsmith `% Split` hashing does not work on anonymous requests, with all simulated data labelled `SYNTHETIC DATA` (and observed external error rates labelled `OBSERVED`).
4. **Deterministic rollback, monotonicity check & Rich UI (`controller.py`, `report.py`)**: Advances through configurable stages (default `1% -> 10% -> 50% -> 100%`) in a live `rich` rollout table (`--plain` for plain text), tracks exact exposed identity IDs per stage (`retained_from_previous_stage` monotonicity check), automatically rolls back to the previous safe percentage or `0%` (`--rollback-to-zero`, default in demo runners) on error breach or signal failure, runs a post-rollback `0/200` verification tick, notifies Flagsmith Grafana Feature Health (`FLAGSMITH_HEALTH_WEBHOOK_URL`) fail-safely (`UNHEALTHY` on rollback -> `HEALTHY` on completion/reset), logs every decision in UTC to `audit.log` without secrets, and renders `reports/rollout.html`.

## Architecture

```mermaid
flowchart LR
  Req["Plain-English Request"] --> Plan["planner.py (offline)<br/>plan.json + plan.md"]
  Plan --> Gate{"apply.py<br/>TTY APPROVE Gate"}
  Gate -- "APPROVE" --> FS["Flagsmith % Split<br/>fsapi.py"]
  FS --> Eval["Persistent Identities<br/>user_000..user_199"]
  Eval --> Check{"Error Rate<br/>Check"}
  Check -- "<= threshold" --> Gate
  Check -- "> threshold" --> RB["Auto-Rollback<br/>+ UNHEALTHY Webhook"]
  RB --> Artifacts["audit.log +<br/>reports/rollout.html"]
  Check -- "Clean / Reset" --> Healthy["HEALTHY Webhook<br/>-> audit.log"]
```

```text
  Plain-English Request / CLI (cli.py)
        |
        +--> planner.py ----------> plan.json + plan.md (offline, zero-key; optional LLM adapter boundary)
        |
        +--> bootstrap.py --------> Zero-UI setup (superuser, org, project, env, keys, health webhook, .env)
        |
        +--> controller.py -------> audit.log (UTC decisions + feature_health events, no secrets)
        |       |
        |       +--> apply.py (TTY + exact "APPROVE" gate; refuses non-interactive)
        |       |       |
        |       |       v
        |       |    fsapi.py [FlagsmithClient / HealthNotifier] --(REST API + Webhook)--> Self-Hosted Flagsmith
        |       |
        |       +--> simulator.py (persistent user_000..; SYNTHETIC DATA)
        |               |
        |               +--> FlagsmithSDKEvaluator ---------(Python SDK + refresh)--> Self-Hosted Flagsmith
        |               +--> OfflineFakeEvaluator (unit tests / offline-only mode)
        |
        +--> report.py (reads audit.log + reports/last_run.json) --> reports/rollout.html (static, inline CSS+SVG)
```

### Why Flagsmith's `PERCENTAGE_SPLIT` Engine Is Load-Bearing (Deletion Test)

If you delete Flagsmith's `PERCENTAGE_SPLIT` segment rules and SDK evaluation from FlagPilot, production cohort bucketing stops working:

- FlagPilot stores the rollout rule (`operator: "PERCENTAGE_SPLIT"`, `property: null`, `value: "<pct>"`) in Flagsmith via `PUT /api/v1/projects/{project_id}/segments/{segment_id}/` and enables only the environment's segment override via `PATCH /api/__future__/environments/{env_key}/features/{feature_id}/` (leaving the global default `enabled: false`).
- On every rollout tick, `FlagsmithSDKEvaluator` calls `update_environment()` and `get_identity_flags(identifier=...)` against Flagsmith's engine so Flagsmith deterministically hashes each persistent identity (`user_000`..`user_199`), guaranteeing monotonic enrollment (`1% ⊆ 10% ⊆ 50% ⊆ 100%`) and immediate `0%` exposure on rollback. (`OfflineFakeEvaluator` exists solely for offline unit tests.)

### Targets

| Target | Command / File | Purpose |
| --- | --- | --- |
| `bootstrap` | `flagpilot bootstrap` | Zero-UI setup against `http://localhost:8000`; writes `.env` idempotently. |
| `plan` | `flagpilot plan "<request>"` | Parse request offline (`--use-llm` optional via `plan_with_llm()`), render Rich panel (`--plain` fallback), write `plan.json` + `plan.md`. |
| `apply` | `flagpilot apply plan.json` | Print plan, require exact `APPROVE`, and apply target percentage to Flagsmith. |
| `run` | `flagpilot run ...` | Require upfront policy `APPROVE` (`--step-approve` opt-in), render live table, run monotonicity check, auto-roll back (`--rollback-to-zero`) + verify `0/200`, notify Feature Health. |
| `rollback` | `flagpilot rollback ...` | Gate and apply a `0%` rollback and disable segment override, logging to `audit.log`. |
| `report` | `flagpilot report` | Read `audit.log` and `reports/last_run.json` and render static `reports/rollout.html`. |
| Task runners | `scripts/demo`, `Makefile`, `tasks.ps1` | Cross-platform `up`, `bootstrap`, `down`, `test`, `lint`, `smoke`, and `demo`. |

### Bring your own error signal

By default `flagpilot run` simulates error rates alongside persistent Flagsmith identities (`SYNTHETIC DATA`). Pass `--signal-url` or `--signal-file` to feed real observed error telemetry (`OBSERVED`):

```bash
# HTTP GET returning JSON {"error_rate": 0.012} or a plain float in [0, 1]
.venv/bin/flagpilot run --feature checkout_v2 --env Development \
  --signal-url http://localhost:9090/checkout-errors --signal-timeout 5 --rollback-to-zero

# Local file containing JSON {"error_rate": 0.012} or a plain float in [0, 1]
.venv/bin/flagpilot run --feature checkout_v2 --env Development \
  --signal-file ./metrics/error_rate.json --rollback-to-zero
```

When `--signal-url` or `--signal-file` is active, the external `OBSERVED` error rate is evaluated directly against `max_error_rate` at every stage (including `1%`, bypassing `min_samples` gating) while simulated identities are still evaluated separately; timeouts, HTTP failures, malformed payloads, or values outside `[0, 1]` fail closed and trigger rollback with the exact reason logged.

### What makes this hard to break

| Failure Mode | How FlagPilot Handles It |
| --- | --- |
| **Unstable identities** | `load_or_create_identities()` generates and persists ordered IDs (`user_000..`), and `FlagsmithSDKEvaluator.evaluate()` rejects empty identifiers so `% Split` hashing stays consistent across stages. |
| **Writes without approval** | Every Flagsmith mutation routes through `ApprovedFlagsmithWriter` -> `require_approval()` in `apply.py`, which rejects non-TTY stdin and anything other than exact `APPROVE` with no bypass flag (including LLM-generated plans). |
| **Runaway rollout** | `RolloutController` stops advancing whenever observed error rate exceeds `max_error_rate` (default `2%`), automatically executes rollback to `0%` (with `--rollback-to-zero`) or previous safe stage, runs a post-rollback `0/200` exposure check, and sends a fail-safe `firing` (`UNHEALTHY`) Feature Health webhook. |
| **Unlabelled synthetic data** | `simulator.py`, `types.py`, `report.py`, and `cli.py` tag all simulated evaluations, metrics, summaries, and HTML/Markdown reports with `SYNTHETIC DATA`. |
| **Flagsmith unreachable** | `FlagsmithManagementClient` wraps `requests.RequestException` and HTTP errors into `FlagsmithAPIError` with sanitized response bodies (keys scrubbed to `[REDACTED]`); `plan`, `report`, and `OfflineFakeEvaluator` run offline. |

## Quickstart

Zero-click self-hosted setup and end-to-end demo (no browser UI clicks or manual `.env` copy required):

**macOS / Linux (`bash`):**
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d
.venv/bin/flagpilot bootstrap
bash scripts/demo
```

**Windows PowerShell:**
```powershell
python -m venv .venv; .venv\Scripts\pip.exe install -e ".[dev]"
docker compose up -d
.venv\Scripts\flagpilot.exe bootstrap
.\tasks.ps1 demo
```

During `scripts/demo` / `.\tasks.ps1 demo`, type `APPROVE` interactively when prompted (`apply`, one-shot upfront policy `APPROVE` for `run` with `--rollback-to-zero`, and cleanup `rollback` reset); after completion, open `reports/rollout.html` in any browser.

> **Note:** On a fresh database, Flagsmith's initial startup (`migrate-db`) takes about 2 to 3 minutes. `flagpilot bootstrap` and the `smoke`/`demo` task runners automatically wait up to 300 seconds for `/health/liveness/` before continuing.

## Repository map

```text
.
├── .github/workflows/ci.yml         # GitHub Actions CI (ruff + offline pytest on Python 3.11 & 3.13)
├── pyproject.toml                   # Python >=3.11 packaging, deps (requests, python-dotenv, flagsmith, rich), pytest markers
├── docker-compose.yml               # Pinned Flagsmith 2.295.2 (migrate-db, serve, task-processor) + Postgres 15.5 stack
├── .env.example                     # Placeholder env vars (FLAGSMITH_API_URL, keys, project/feature/segment IDs)
├── .gitattributes                   # Enforces LF on *.sh, scripts/demo, Makefile; CRLF on *.ps1
├── .gitignore                       # Ignores .env, *.env.local, audit.log, reports/, .venv/, caches, build artifacts
├── Makefile                         # POSIX targets (help, install, test, lint, smoke, up, bootstrap, down, demo*)
├── tasks.ps1                        # Windows PowerShell tasks matching Makefile targets 1:1
├── scripts/
│   └── demo                         # Executable POSIX demo (ensures Flagsmith up, bootstraps .env if absent, runs demo)
├── docs/
│   ├── ARCHITECTURE.md              # Component design, safety boundaries, and data flow
│   ├── SELF_HOSTING.md              # Zero-click self-hosted Flagsmith setup & verification guide
│   └── assets/rollout-report.png    # Verified full-page screenshot of reports/rollout.html
├── src/flagpilot/
│   ├── types.py                     # Frozen dataclasses (Plan, Step, Decision) & FlagsmithClient Protocol
│   ├── bootstrap.py                 # Idempotent zero-UI Flagsmith bootstrap writing local .env
│   ├── planner.py                   # Offline zero-key plain-English parser, plan.json & plan.md generator
│   ├── apply.py                     # Interactive APPROVE gate & non-interactive refusal boundary
│   ├── fsapi.py                     # Flagsmith Management REST API client (verified against local Flagsmith 2.295.2)
│   ├── simulator.py                 # Persistent-identity SYNTHETIC DATA simulator & Flagsmith SDK wrapper
│   ├── controller.py                # Deterministic staged rollout state machine & UTC audit.log writer
│   ├── report.py                    # Static self-contained HTML + inline CSS/SVG report generator
│   └── cli.py                       # argparse CLI (bootstrap, plan, apply, run, rollback, report)
└── tests/
    ├── test_acceptance.py           # End-to-end feature acceptance tests (fakes/mocks)
    ├── test_bootstrap.py            # Mocked initial & idempotent rerun bootstrap tests
    ├── test_cli.py                  # CLI help, no-bypass, plan, bootstrap, and apply gate tests
    ├── test_controller.py           # Controller advancement, 50%->10% rollback, 0% rollback, audit log
    ├── test_fsapi_and_simulator.py  # Mocked Flagsmith REST & SDK tests + key-redaction tests
    ├── test_live_flagsmith.py       # @pytest.mark.live integration test (requires FLAGPILOT_LIVE_TEST=1)
    ├── test_planner.py              # Plain-English parser & planner import-isolation tests
    ├── test_report.py               # Static HTML/SVG report generation from fake run + audit.log
    └── test_simulator.py            # Persistent user_000.. identity & error-spike tests
```

## How I used Flagsmith

- **Self-hosted Flagsmith (`docker.flagsmith.com/flagsmith/flagsmith:2.295.2`)**: Runs locally via [docker-compose.yml](docker-compose.yml), which pins `2.295.2` across `migrate-db`, `flagsmith`, and `flagsmith-task-processor` because `/api/__future__/` segment-override and Feature Health APIs can change across releases. Provisioned without UI clicks by `flagpilot bootstrap` (`/health/liveness/`, `/version/`, `/api/v1/auth/users/`, `/api/v1/auth/login/`, `/api/v1/organisations/`, `/api/v1/organisations/{id}/master-api-keys/`, `/api/v1/environments/{client_key}/api-keys/`).
- **`% Split` (`PERCENTAGE_SPLIT`) segments**: Creates and updates project segments with `operator: "PERCENTAGE_SPLIT"`, `property: null`, and `value: "<percent>"` via `/api/v1/projects/{project_id}/segments/`.
- **Segment overrides (v1 & v2 compatible)**: Reads and patches segment overrides via `GET`/`PATCH /api/__future__/environments/{env_key}/features/{feature_id}/` so `environment_default.enabled` stays `false` while the rollout segment override is toggled cleanly across both Feature Versioning v1 and v2.
- **Stable persistent identities**: Evaluates every rollout round with the exact same ordered identities (`user_000`..`user_199`) via `Flagsmith.get_identity_flags(identifier=...)` with `enable_local_evaluation=True` and explicit `update_environment()` refresh per round.
- **REST Management API & Feature Health**: Uses `Authorization: Api-Key <FLAGSMITH_ADMIN_KEY>` directly via `requests` in `src/flagpilot/fsapi.py` (never using an MCP server or external LLM) and scrubs all secrets from error messages and logs. `flagpilot bootstrap` also idempotently provisions a `"Grafana"` Feature Health provider (`/api/v1/projects/{project_id}/feature-health/providers/`) as `FLAGSMITH_HEALTH_WEBHOOK_URL`; `controller.py` fail-safely POSTs Alertmanager JSON (`status: "firing"` on error-rate rollback -> Flagsmith shows `Unhealthy`; `status: "resolved"` on clean completion or manual reset -> `Healthy`).

## Development status

- **Offline CI (`.\tasks.ps1 test` / `pytest -v` & `ruff check src tests` via `.github/workflows/ci.yml`)**: Runs the mocked/offline unit suite (`@pytest.mark.live` skipped unless `FLAGPILOT_LIVE_TEST=1`) and `ruff` linter cleanly across Python `3.11` and `3.13`, with `--plain` for deterministic plain-text logs alongside Rich terminal panels/tables.
- **Live Flagsmith integration (`.\tasks.ps1 smoke` / `make smoke` -> `FLAGPILOT_LIVE_TEST=1 pytest -m live -v`)**: Verifies the live pinned Flagsmith `2.295.2` stack end-to-end, covering idempotent `bootstrap`, single upfront `APPROVE` policy gate, `1% -> 10% -> 50%` monotonicity check, automatic `0%` rollback + `0/200` post-rollback verification on fault spike, Grafana Feature Health `UNHEALTHY`/`HEALTHY` webhook delivery, `audit.log` recording, and static `reports/rollout.html`.
