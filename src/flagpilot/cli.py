"""Command-line interface for FlagPilot."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from flagpilot.apply import ApprovalDeniedError, ApprovedFlagsmithWriter
from flagpilot.controller import RolloutController, execute_manual_rollback
from flagpilot.fsapi import FlagsmithAPIError, FlagsmithConfig, FlagsmithManagementClient
from flagpilot.planner import (
    DEFAULT_STAGES,
    PlanValidationError,
    build_plan,
    load_plan,
    parse_plain_english_request,
    save_plan_artifacts,
)
from flagpilot.report import (
    load_audit_log,
    load_run_result,
    save_html_report,
    save_run_result,
)
from flagpilot.simulator import (
    DEFAULT_BASELINE_ERROR_RATE,
    DEFAULT_USER_COUNT,
    SYNTHETIC_LABEL,
    FlagsmithSDKEvaluator,
    OfflineFakeEvaluator,
    TrafficSimulator,
    make_error_rate_fn,
)
from flagpilot.types import FlagEvaluator, Plan, StageMetrics, Step

SUBCOMMANDS = ("plan", "apply", "run", "rollback", "report")
DEFAULT_MAX_ERROR_RATE = 0.02


class _SyntheticSignalProvider:
    """Adapter connecting persistent-identity TrafficSimulator to RolloutController."""

    def __init__(self, evaluator: FlagEvaluator, simulator: TrafficSimulator) -> None:
        self._evaluator = evaluator
        self._simulator = simulator

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        if isinstance(self._evaluator, OfflineFakeEvaluator):
            self._evaluator.set_percentage(step.percentage)
        round_result = self._simulator.run_round(plan.feature_name)
        print(round_result.format_summary())
        return round_result.to_stage_metrics(step.percentage)


def _add_plan_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan", type=Path, default=None, help="Path to plan JSON")
    parser.add_argument("--feature", default=None, help="Flagsmith feature name")
    parser.add_argument("--env", default=None, help="Flagsmith environment name or key")
    parser.add_argument(
        "--stages",
        type=int,
        nargs="+",
        default=list(DEFAULT_STAGES),
        help="Rollout percentages (default: 1 10 50 100)",
    )
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument(
        "--max-error-rate",
        type=float,
        default=DEFAULT_MAX_ERROR_RATE,
        help="Rollback error-rate threshold (default: 0.02 = 2%%)",
    )
    parser.add_argument("--project-id", type=int, default=None)
    parser.add_argument("--segment-name", default=None)


def _resolve_plan(args: argparse.Namespace) -> Plan:
    if args.plan is not None:
        return load_plan(args.plan)
    if not args.feature or not args.env:
        raise ValueError("Provide either --plan <path> or both --feature and --env.")
    return build_plan(
        feature_name=args.feature,
        environment=args.env,
        stages=args.stages,
        min_samples=args.min_samples,
        max_error_rate=args.max_error_rate,
        project_id=args.project_id,
        segment_name=args.segment_name,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the FlagPilot argument parser (never exposes auto-approve flags)."""
    parser = argparse.ArgumentParser(
        prog="flagpilot",
        description="Staged feature flag rollout controller for self-hosted Flagsmith.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Generate a deterministic rollout plan")
    plan_parser.add_argument(
        "request",
        nargs="?",
        default=None,
        help="Plain-English rollout request (e.g. 'roll out checkout_v2 to 10%% in Development, pause if errors exceed 2%%')",
    )
    plan_parser.add_argument("--feature", default=None, help="Flagsmith feature name")
    plan_parser.add_argument("--env", default=None, help="Flagsmith environment name or key")
    plan_parser.add_argument(
        "--stages",
        type=int,
        nargs="+",
        default=list(DEFAULT_STAGES),
        help="Rollout percentages (default: 1 10 50 100)",
    )
    plan_parser.add_argument("--min-samples", type=int, default=20)
    plan_parser.add_argument("--max-error-rate", type=float, default=DEFAULT_MAX_ERROR_RATE)
    plan_parser.add_argument("--project-id", type=int, default=None)
    plan_parser.add_argument("--segment-name", default=None)
    plan_parser.add_argument("--out-dir", type=Path, default=Path("."))
    plan_parser.add_argument("--out", type=Path, default=None, help="Output plan JSON path")
    plan_parser.add_argument("--md-out", type=Path, default=None, help="Output plan Markdown path")

    apply_parser = subparsers.add_parser("apply", help="Apply a plan (requires APPROVE)")
    apply_parser.add_argument("plan_file", nargs="?", type=Path, default=None, help="Path to plan.json")
    apply_parser.add_argument("--plan", type=Path, default=None, help="Path to plan.json")
    apply_parser.add_argument("--percentage", type=int, default=None, help="Target percentage override")
    apply_parser.add_argument("--audit-log", type=Path, default=Path("audit.log"))

    run_parser = subparsers.add_parser("run", help="Run staged rollout (requires APPROVE)")
    _add_plan_target_args(run_parser)
    run_parser.add_argument(
        "--interval",
        type=float,
        default=0.0,
        help="Wait interval in seconds between applying a stage and reading metrics",
    )
    run_parser.add_argument("--sample-count", type=int, default=DEFAULT_USER_COUNT)
    run_parser.add_argument("--baseline-error-rate", type=float, default=DEFAULT_BASELINE_ERROR_RATE)
    run_parser.add_argument("--spike-rate", type=float, default=None)
    run_parser.add_argument("--spike-threshold-pct", type=float, default=None)
    run_parser.add_argument("--seed", type=int, default=42)
    run_parser.add_argument(
        "--offline-evaluator",
        action="store_true",
        help="Use OfflineFakeEvaluator instead of Flagsmith SDK for traffic evaluation",
    )
    run_parser.add_argument(
        "--identities-file",
        type=Path,
        default=Path("scratch/synthetic_identities.json"),
    )
    run_parser.add_argument(
        "--run-out",
        type=Path,
        default=Path("reports/last_run.json"),
        help="Path to save the run result JSON (default: reports/last_run.json)",
    )
    run_parser.add_argument(
        "--report-out",
        type=Path,
        default=Path("reports/rollout.html"),
        help="Path to save the HTML rollout report (default: reports/rollout.html)",
    )
    run_parser.add_argument("--audit-log", type=Path, default=Path("audit.log"))

    rollback_parser = subparsers.add_parser(
        "rollback", help="Roll back a plan's rollout segment to 0%% (requires APPROVE)"
    )
    _add_plan_target_args(rollback_parser)
    rollback_parser.add_argument("--old-percentage", type=int, default=0)
    rollback_parser.add_argument("--reason", default="manual rollback")
    rollback_parser.add_argument("--audit-log", type=Path, default=Path("audit.log"))

    report_parser = subparsers.add_parser(
        "report", help="Generate static reports/rollout.html from audit.log and last run"
    )
    report_parser.add_argument(
        "--input",
        type=Path,
        default=Path("reports/last_run.json"),
        help="Saved run result JSON (default: reports/last_run.json)",
    )
    report_parser.add_argument(
        "--audit-log",
        type=Path,
        default=Path("audit.log"),
        help="Audit log JSONL path (default: audit.log)",
    )
    report_parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/rollout.html"),
        help="Output HTML path (default: reports/rollout.html)",
    )

    return parser


def _handle_plan(args: argparse.Namespace) -> int:
    if args.request is not None:
        plan = parse_plain_english_request(
            args.request,
            min_samples=args.min_samples,
            project_id=args.project_id,
            segment_name=args.segment_name,
        )
    elif args.feature and args.env:
        plan = build_plan(
            feature_name=args.feature,
            environment=args.env,
            stages=args.stages,
            min_samples=args.min_samples,
            max_error_rate=args.max_error_rate,
            project_id=args.project_id,
            segment_name=args.segment_name,
        )
    else:
        raise ValueError(
            "Provide a plain-English rollout request or both --feature and --env."
        )

    json_path = args.out if args.out is not None else args.out_dir / "plan.json"
    md_path = args.md_out if args.md_out is not None else json_path.with_suffix(".md")
    save_plan_artifacts(plan, json_path=json_path, markdown_path=md_path)
    print(f"Wrote {json_path} and {md_path} (no Flagsmith API calls made).")
    return 0


def _handle_apply(args: argparse.Namespace) -> int:
    plan_path = args.plan_file or args.plan
    if plan_path is None:
        raise ValueError("Provide plan.json path (e.g. 'flagpilot apply plan.json').")
    plan = load_plan(plan_path)
    writer = ApprovedFlagsmithWriter(
        lambda: FlagsmithManagementClient(FlagsmithConfig.from_env()),
        audit_log_path=args.audit_log,
    )
    change = writer.apply_plan(plan, percentage=args.percentage)
    print(f"Applied {change.percentage}% to {change.feature_name} ({change.environment}).")
    return 0


def _handle_run(args: argparse.Namespace) -> int:
    plan = _resolve_plan(args)
    backend = FlagsmithManagementClient(FlagsmithConfig.from_env())
    writer = ApprovedFlagsmithWriter(backend)
    evaluator: FlagEvaluator = (
        OfflineFakeEvaluator() if args.offline_evaluator else FlagsmithSDKEvaluator.from_env()
    )
    error_rate_fn = make_error_rate_fn(
        baseline_rate=args.baseline_error_rate,
        spike_rate=args.spike_rate,
        spike_threshold_pct=args.spike_threshold_pct,
    )
    simulator = TrafficSimulator(
        evaluator,
        user_count=args.sample_count,
        state_path=args.identities_file,
        seed=args.seed,
        baseline_rate=args.baseline_error_rate,
        error_rate_fn=error_rate_fn,
    )
    provider = _SyntheticSignalProvider(evaluator, simulator)
    controller = RolloutController(
        writer=writer,
        signal_provider=provider,
        interval_seconds=args.interval,
        audit_log_path=args.audit_log,
    )
    result = controller.run(plan, is_synthetic=True)

    print(f"[{SYNTHETIC_LABEL}] Rollout finished with status: {result.final_status}")
    save_run_result(result, args.run_out)
    audit_entries = load_audit_log(args.audit_log)
    save_html_report(result, args.report_out, audit_entries=audit_entries)
    print(f"Saved rollout report to {args.report_out}")
    return 0 if result.final_status == "completed" else 1


def _handle_rollback(args: argparse.Namespace) -> int:
    plan = _resolve_plan(args)
    backend = FlagsmithManagementClient(FlagsmithConfig.from_env())
    writer = ApprovedFlagsmithWriter(backend)
    execute_manual_rollback(
        writer,
        plan,
        old_percentage=args.old_percentage,
        reason=args.reason,
        audit_log_path=args.audit_log,
    )
    return 0


def _handle_report(args: argparse.Namespace) -> int:
    result = load_run_result(args.input)
    audit_entries = load_audit_log(args.audit_log)
    out_path = save_html_report(result, args.out, audit_entries=audit_entries)
    print(f"Saved HTML report to {out_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the flagpilot CLI."""
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handlers = {
        "plan": _handle_plan,
        "apply": _handle_apply,
        "run": _handle_run,
        "rollback": _handle_rollback,
        "report": _handle_report,
    }
    try:
        return handlers[args.command](args)
    except (ApprovalDeniedError, FlagsmithAPIError, PlanValidationError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
