# Architecture

## Overview

FlagPilot is a deterministic terminal controller for staged feature flag rollouts on self-hosted Flagsmith. It separates offline planning and decision logic from all Flagsmith network I/O so every component can be unit-tested with fake clients and no Flagsmith write can occur without human approval.

> **Note:** All Flagsmith HTTP and Python SDK calls in `src/flagpilot/fsapi.py` and `src/flagpilot/simulator.py` are marked **UNVERIFIED** until tested against a running local Flagsmith instance.

## Components

- **`types.py`**: Immutable frozen dataclasses (`Plan`, `Step`, `Decision`, `FlagEvaluation`, `StageMetrics`, `RolloutChange`, `StageOutcome`, `RolloutRunResult`) plus the `FlagsmithClient` and `FlagEvaluator` `Protocol` interfaces.
- **`planner.py`**: Offline rule-based parser (`parse_plain_english_request`) and artifact writer (`plan.json`, `plan.md`). Imports neither `requests` nor `flagsmith`.
- **`apply.py`**: Mandatory human-in-the-loop approval gate (`require_approval`, `ApprovedFlagsmithWriter`). Refuses non-interactive runs (`NonInteractiveError`), rejects any input other than exact `APPROVE`, and exposes no bypass flag.
- **`fsapi.py`**: The sole home for Flagsmith Management REST API HTTP calls (`FlagsmithManagementClient`). Scrubs all keys from errors before raising `FlagsmithAPIError`. (**UNVERIFIED**)
- **`simulator.py`**: The sole home for Flagsmith Python SDK calls (`FlagsmithSDKEvaluator`, **UNVERIFIED**), plus `OfflineFakeEvaluator`, persistent identity management (`user_000..`), and seeded `SYNTHETIC DATA` traffic simulation (`TrafficSimulator`).
- **`controller.py`**: Executes stages (`[1, 10, 50, 100]` by default), waits the configured interval, evaluates observed error rate against `max_error_rate` (default `2%`), rolls back to the previous safe percentage (or `0%`) on breach, and appends UTC JSON lines to `audit.log` without secrets.
- **`report.py`**: Combines `audit.log` and `reports/last_run.json` into a single static `reports/rollout.html` with inline CSS (light/dark mode), zero `<script>` tags, an inline SVG chart, `SYNTHETIC DATA` banner, and rollback reason banner.

## Rollout Data Flow

1. `flagpilot plan "<request>"` parses the request offline into `plan.json` and `plan.md`.
2. `flagpilot apply plan.json` prints the plan, prompts for exact `APPROVE`, and writes the target percentage through `FlagsmithManagementClient`.
3. `flagpilot run` loops over `plan.steps`:
   - Requests `APPROVE` via `apply.py` before writing the stage percentage.
   - Waits `interval_seconds`.
   - Runs `TrafficSimulator` over persistent `user_000..` identities.
   - Logs the decision (`advance`, `hold`, `rollback`, or `complete`) to `audit.log`.
   - On threshold breach, prints the reason and requests `APPROVE` to roll back to the previous safe percentage (or `0%`).
4. `flagpilot report` reads `audit.log` and `reports/last_run.json` and writes `reports/rollout.html`.

## Approval Gate

Every Flagsmith mutation (`apply`, `run` stage advancement, automatic rollback, and manual `rollback`) enters `ApprovedFlagsmithWriter` in `src/flagpilot/apply.py`:

- Non-TTY `stdin` raises `NonInteractiveError` immediately before any Flagsmith request is issued.
- Any response other than exact `APPROVE` cancels the write (`ApprovalDeniedError`).
- CLI argument parsers intentionally omit `--auto-approve`, `--yes`, and `-y`.

## Identity Stability

Flagsmith's `% Split` (`PERCENTAGE_SPLIT`) operator hashes the identity identifier rather than user traits. Anonymous or rotating identifiers produce inconsistent bucketing across stages. `load_or_create_identities()` generates deterministic ordered IDs (`user_000`..`user_199` by default) and persists them to `scratch/synthetic_identities.json` so every stage evaluates the exact same cohort.
