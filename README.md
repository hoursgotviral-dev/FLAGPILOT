# FlagPilot

## The problem

Feature flag rollouts often jump straight from disabled to 100% or rely on ad-hoc dashboard clicks with no repeatable safety net. When an error spike hits mid-rollout, operators have to scramble to diagnose which cohort is affected and manually revert state without an audit trail. Self-hosted teams need a terminal-first workflow that plans percentage stages deterministically, enforces explicit human sign-off on every change, and rolls back automatically when error rates breach a threshold.

## The approach

FlagPilot orchestrates staged **% Split** (`PERCENTAGE_SPLIT`) rollouts on self-hosted Flagsmith through four safeguards:

1. **Deterministic planning (`planner.py`)**: Rule-based parser turns a plain-English rollout request into `plan.json` and `plan.md` with no LLM and zero network calls.
2. **Mandatory human approval (`apply.py`)**: Every Flagsmith mutation requires an interactive TTY where the operator types exact `APPROVE`; non-interactive runs refuse and there is no `--auto-approve` or `--yes` flag.
3. **Stable persistent identities (`simulator.py`)**: Evaluations always supply persistent identities (`user_000`..`user_199` by default) because Flagsmith `% Split` hashing does not work on anonymous requests, with all simulated data labelled `SYNTHETIC DATA`.
4. **Deterministic rollback & audit trail (`controller.py`, `report.py`)**: Advances through configurable stages (default `1% -> 10% -> 50% -> 100%`), rolls back to the previous safe percentage (or `0%`) if errors exceed the threshold (default `2%`), logs every decision to `audit.log` in UTC without secrets, and builds static `reports/rollout.html`.

## Architecture

```text
  Plain-English Request / CLI (cli.py)
        |
        +--> planner.py ----------> plan.json + plan.md (offline, no network)
        |
        +--> controller.py -------> audit.log (UTC decisions, no secrets)
        |       |
        |       +--> apply.py (TTY + exact "APPROVE" gate; refuses non-interactive)
        |       |       |
        |       |       v
        |       |    fsapi.py [FlagsmithClient Protocol] --(UNVERIFIED REST API)--> Self-Hosted Flagsmith
        |       |
        |       +--> simulator.py (persistent user_000..; SYNTHETIC DATA)
        |               |
        |               +--> FlagsmithSDKEvaluator ---------(UNVERIFIED Python SDK)--> Self-Hosted Flagsmith
        |               +--> OfflineFakeEvaluator (pure offline % Split simulation)
        |
        +--> report.py (reads audit.log + reports/last_run.json) --> reports/rollout.html (static, inline CSS+SVG)
```

### Targets

| Target | Command / File | Purpose |
| --- | --- | --- |
| `plan` | `flagpilot plan "<request>"` | Deterministically parse request and write `plan.json` + `plan.md` offline. |
| `apply` | `flagpilot apply plan.json` | Print plan, require exact `APPROVE`, and apply target percentage to Flagsmith. |
| `run` | `flagpilot run ...` | Run staged rollout (`1 -> 10 -> 50 -> 100`), watch error rate, roll back on breach. |
| `rollback` | `flagpilot rollback ...` | Gate and apply a `0%` rollback and disable segment override, logging to `audit.log`. |
| `report` | `flagpilot report` | Read `audit.log` and `reports/last_run.json` and render static `reports/rollout.html`. |
| Demo runners | `scripts/demo`, `Makefile`, `tasks.ps1` | End-to-end demo (`plan`, `apply`, spiked `run`, `rollback`, `report`). |

### What makes this hard to break

| Failure Mode | How FlagPilot Handles It |
| --- | --- |
| **Unstable identities** | `load_or_create_identities()` generates and persists ordered IDs (`user_000..`), and `FlagsmithSDKEvaluator.evaluate()` rejects empty identifiers so `% Split` hashing stays consistent across stages. |
| **Writes without approval** | Every Flagsmith mutation routes through `ApprovedFlagsmithWriter` -> `require_approval()` in `apply.py`, which rejects non-TTY stdin and anything other than exact `APPROVE` with no bypass flag. |
| **Runaway rollout** | `RolloutController` stops advancing whenever observed error rate exceeds `max_error_rate` (default `2%`), prints the reason, and gates a rollback to the previous safe percentage (or `0%`). |
| **Unlabelled synthetic data** | `simulator.py`, `types.py`, `report.py`, and `cli.py` tag all simulated evaluations, metrics, summaries, and HTML/Markdown reports with `SYNTHETIC DATA`. |
| **Flagsmith unreachable** | `FlagsmithManagementClient` wraps `requests.RequestException` and HTTP errors into `FlagsmithAPIError` with sanitized response bodies (keys scrubbed to `[REDACTED]`); `plan`, `report`, and `OfflineFakeEvaluator` run offline. |

## Quickstart

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env  # fill in FLAGSMITH_API_URL and keys locally
flagpilot plan "roll out checkout_v2 to 10% in Development, pause if errors exceed 2%"
flagpilot apply plan.json  # type APPROVE when prompted
flagpilot run --feature checkout_v2 --env Development --spike-rate 0.25 --spike-threshold-pct 25
flagpilot report --input reports/last_run.json --audit-log audit.log --out reports/rollout.html
```

*(On Windows PowerShell, use `.venv\Scripts\pip.exe` / `.venv\Scripts\flagpilot.exe` or run `./tasks.ps1 demo`.)*

## Repository map

```text
.
├── pyproject.toml                   # Python >=3.11 packaging, deps (requests, python-dotenv, flagsmith)
├── .env.example                     # Placeholder env vars (FLAGSMITH_API_URL, CLIENT/SERVER/ADMIN keys)
├── .gitignore                       # Ignores .env, .venv/, scratch/, reports/, audit.log, *.key
├── Makefile                         # POSIX demo, test, and lint targets
├── tasks.ps1                        # Windows PowerShell demo, test, and lint tasks
├── scripts/
│   └── demo                         # End-to-end demo script (plan -> apply -> run -> rollback -> report)
├── docs/
│   ├── ARCHITECTURE.md              # Component design, safety boundaries, UNVERIFIED call notes
│   └── SELF_HOSTING.md              # Self-hosted Flagsmith setup & verification checklist
├── src/flagpilot/
│   ├── types.py                     # Frozen dataclasses (Plan, Step, Decision) & FlagsmithClient Protocol
│   ├── planner.py                   # Offline plain-English parser, plan.json & plan.md generator
│   ├── apply.py                     # Interactive APPROVE gate & non-interactive refusal boundary
│   ├── fsapi.py                     # Flagsmith Management REST API client (UNVERIFIED endpoints)
│   ├── simulator.py                 # Persistent-identity SYNTHETIC DATA simulator & Flagsmith SDK wrapper
│   ├── controller.py                # Deterministic staged rollout state machine & UTC audit.log writer
│   ├── report.py                    # Static self-contained HTML + inline CSS/SVG report generator
│   └── cli.py                       # argparse CLI (plan, apply, run, rollback, report)
└── tests/
    ├── test_acceptance.py           # End-to-end feature acceptance tests (fakes/mocks)
    ├── test_cli.py                  # CLI help, no-bypass, plan, and apply gate tests
    ├── test_controller.py           # Controller advancement, 50%->10% rollback, 0% rollback, audit log
    ├── test_fsapi_and_simulator.py  # Mocked Flagsmith REST & SDK tests + key-redaction tests
    ├── test_planner.py              # Plain-English parser & planner import-isolation tests
    ├── test_report.py               # Static HTML/SVG report generation from fake run + audit.log
    └── test_simulator.py            # Persistent user_000.. identity & error-spike tests
```

## How I used Flagsmith

- **Self-hosted Flagsmith**: Targets a local self-hosted Flagsmith server via `FLAGSMITH_API_URL` (e.g. `http://localhost:8000/api/v1/`). **UNVERIFIED** against a live local container so far.
- **`% Split` (`PERCENTAGE_SPLIT`) segments**: Creates and updates project segments with a `PERCENTAGE_SPLIT` condition (`property: null`, `value: "<percent>"`) via `/api/v1/projects/{project_id}/segments/`. **UNVERIFIED**.
- **Segment overrides**: Attaches a segment override (`priority: 0`, `enabled: true`) in the target environment via `/api/v1/features/feature-segments/` and `/api/v1/features/featurestates/`, or disables it on rollback to `0%` via `PATCH /api/v1/environments/{env_key}/featurestates/{state_id}/`. **UNVERIFIED**.
- **Stable persistent identities**: Evaluates every rollout round with the exact same ordered identities (`user_000`..`user_199`) via `Flagsmith.get_identity_flags(identifier=...)`, because Flagsmith `% Split` requires an identity to hash deterministically. **UNVERIFIED**.
- **REST Management API**: Uses `Authorization: Api-Key <FLAGSMITH_ADMIN_KEY>` directly via `requests` in `src/flagpilot/fsapi.py` (never using an MCP server or external LLM) and surfaces sanitized response bodies on HTTP failures without ever printing keys. **UNVERIFIED**.

## Development status

- **Implemented & unit-tested offline**: Deterministic planner (`plan.json` + `plan.md`), strict `APPROVE` gate, `FlagsmithClient` protocol and REST client (`fsapi.py`), persistent-identity `SYNTHETIC DATA` simulator + `OfflineFakeEvaluator` (`simulator.py`), staged rollout controller with `audit.log` (`controller.py`), static light/dark HTML+SVG report (`report.py`), CLI subcommands, and `31` mocked/fake-client `pytest` tests + `ruff` lint checks.
- **UNVERIFIED against live Flagsmith**: Every HTTP endpoint in `src/flagpilot/fsapi.py` and Python SDK call in `src/flagpilot/simulator.py` is marked `# UNVERIFIED: check against local instance` and still needs verification against a running local Flagsmith instance.
