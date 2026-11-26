"""Staged rollout controller with error-rate monitoring, rollback, and audit log."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from flagpilot.apply import ApprovalDeniedError, ApprovedFlagsmithWriter
from flagpilot.types import (
    Decision,
    Plan,
    RolloutChange,
    RolloutRunResult,
    StageMetrics,
    StageOutcome,
    Step,
)


class StageSignalProvider(Protocol):
    """Source of per-stage evaluation metrics for the rollout controller."""

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        """Collect traffic and error metrics at the active stage."""
        ...


def append_audit_entry(
    audit_log_path: Path | None,
    *,
    action: str,
    feature_name: str,
    environment: str,
    old_percentage: int,
    new_percentage: int,
    reason: str,
    observed_error_rate: float | None = None,
) -> None:
    """Append a secret-free decision entry with UTC timestamp to ``audit.log``."""
    if audit_log_path is None:
        return
    entry: dict[str, object] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": action,
        "feature_name": feature_name,
        "environment": environment,
        "old_percentage": old_percentage,
        "new_percentage": new_percentage,
        "reason": reason,
    }
    if observed_error_rate is not None:
        entry["observed_error_rate"] = observed_error_rate
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def evaluate_stage(
    step: Step,
    sample_count: int,
    error_count: int,
    next_step: Step | None = None,
    *,
    observed_error_rate: float | None = None,
    rollback_target: int = 0,
) -> Decision:
    """Decide whether to hold, rollback, advance, or complete a rollout stage."""
    if sample_count < 0 or error_count < 0 or error_count > sample_count:
        raise ValueError(
            f"Invalid sample/error counts: sample_count={sample_count}, error_count={error_count}."
        )

    rate = (
        observed_error_rate
        if observed_error_rate is not None
        else (error_count / sample_count if sample_count > 0 else 0.0)
    )
    if sample_count == 0 or sample_count < step.min_samples:
        return Decision(
            action="hold",
            reason=f"Insufficient samples ({sample_count} < {step.min_samples}).",
            current_percentage=step.percentage,
            next_percentage=step.percentage,
            observed_error_rate=rate,
            sample_count=sample_count,
        )

    if rate > step.max_error_rate:
        return Decision(
            action="rollback",
            reason=(
                f"Error rate {rate:.2%} exceeded threshold "
                f"{step.max_error_rate:.2%}; rolling back to {rollback_target}%."
            ),
            current_percentage=step.percentage,
            next_percentage=rollback_target,
            observed_error_rate=rate,
            sample_count=sample_count,
        )

    if next_step is not None:
        return Decision(
            action="advance",
            reason=f"Error rate {rate:.2%} within threshold {step.max_error_rate:.2%}.",
            current_percentage=step.percentage,
            next_percentage=next_step.percentage,
            observed_error_rate=rate,
            sample_count=sample_count,
        )

    return Decision(
        action="complete",
        reason=(
            f"Final stage {step.percentage}% passed ({rate:.2%} "
            f"<= {step.max_error_rate:.2%})."
        ),
        current_percentage=step.percentage,
        next_percentage=step.percentage,
        observed_error_rate=rate,
        sample_count=sample_count,
    )


class RolloutController:
    """Executes staged Flagsmith rollouts gated by human approval and error thresholds."""

    def __init__(
        self,
        writer: ApprovedFlagsmithWriter,
        signal_provider: StageSignalProvider,
        *,
        interval_seconds: float = 0.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        audit_log_path: Path | None = Path("audit.log"),
        output_stream: TextIO | None = None,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0.")
        self._writer = writer
        self._signal_provider = signal_provider
        self._interval_seconds = interval_seconds
        self._sleep_fn = sleep_fn
        self._audit_log_path = audit_log_path
        self._output_stream = output_stream if output_stream is not None else sys.stdout

    def _log_decision(
        self,
        plan: Plan,
        *,
        action: str,
        old_percentage: int,
        new_percentage: int,
        reason: str,
        observed_error_rate: float | None = None,
    ) -> None:
        append_audit_entry(
            self._audit_log_path,
            action=action,
            feature_name=plan.feature_name,
            environment=plan.environment,
            old_percentage=old_percentage,
            new_percentage=new_percentage,
            reason=reason,
            observed_error_rate=observed_error_rate,
        )

    def run(self, plan: Plan, *, is_synthetic: bool = True) -> RolloutRunResult:
        """Walk through ``plan.steps``, waiting, evaluating, and rolling back on spike."""
        outcomes: list[StageOutcome] = []
        steps = plan.steps
        previous_safe_percentage = 0
        current_percentage = 0

        for index, step in enumerate(steps):
            try:
                self._writer.apply_stage(plan, step.percentage)
            except ApprovalDeniedError as exc:
                status = "aborted" if not outcomes else "paused"
                self._log_decision(
                    plan,
                    action=status,
                    old_percentage=current_percentage,
                    new_percentage=current_percentage,
                    reason=str(exc),
                )
                self._output_stream.write(f"Rollout {status}: {exc}\n")
                return RolloutRunResult(
                    plan=plan,
                    final_status=status,
                    outcomes=tuple(outcomes),
                    rollback_applied=False,
                    is_synthetic=is_synthetic,
                )

            current_percentage = step.percentage
            if self._interval_seconds > 0:
                self._sleep_fn(self._interval_seconds)

            metrics = self._signal_provider.collect(plan, step)
            next_step = steps[index + 1] if index + 1 < len(steps) else None
            decision = evaluate_stage(
                step=step,
                sample_count=metrics.sample_count,
                error_count=metrics.error_count,
                next_step=next_step,
                observed_error_rate=metrics.error_rate,
                rollback_target=previous_safe_percentage,
            )
            outcomes.append(
                StageOutcome(
                    step=step,
                    metrics=metrics,
                    decision=decision,
                    applied=True,
                )
            )
            self._log_decision(
                plan,
                action=decision.action,
                old_percentage=step.percentage,
                new_percentage=(
                    decision.next_percentage
                    if decision.next_percentage is not None
                    else step.percentage
                ),
                reason=decision.reason,
                observed_error_rate=decision.observed_error_rate,
            )

            if decision.action in ("hold", "pause"):
                self._output_stream.write(
                    f"Paused at {step.percentage}%: {decision.reason}\n"
                )
                return RolloutRunResult(
                    plan=plan,
                    final_status="paused",
                    outcomes=tuple(outcomes),
                    rollback_applied=False,
                    is_synthetic=is_synthetic,
                )

            if decision.action == "rollback":
                rollback_target = previous_safe_percentage
                self._output_stream.write(
                    f"Rollback triggered at {step.percentage}% -> {rollback_target}%: "
                    f"{decision.reason}\n"
                )
                try:
                    self._writer.rollback(
                        plan,
                        target_percentage=rollback_target,
                        reason=decision.reason,
                    )
                except ApprovalDeniedError as exc:
                    self._log_decision(
                        plan,
                        action="rollback_aborted",
                        old_percentage=step.percentage,
                        new_percentage=step.percentage,
                        reason=str(exc),
                        observed_error_rate=decision.observed_error_rate,
                    )
                    self._output_stream.write(f"Rollback not approved: {exc}\n")
                    return RolloutRunResult(
                        plan=plan,
                        final_status="paused",
                        outcomes=tuple(outcomes),
                        rollback_applied=False,
                        is_synthetic=is_synthetic,
                    )
                return RolloutRunResult(
                    plan=plan,
                    final_status="rolled_back",
                    outcomes=tuple(outcomes),
                    rollback_applied=True,
                    rollback_to_percentage=rollback_target,
                    is_synthetic=is_synthetic,
                )

            previous_safe_percentage = step.percentage

        return RolloutRunResult(
            plan=plan,
            final_status="completed",
            outcomes=tuple(outcomes),
            rollback_applied=False,
            is_synthetic=is_synthetic,
        )


def execute_manual_rollback(
    writer: ApprovedFlagsmithWriter,
    plan: Plan,
    *,
    old_percentage: int = 0,
    reason: str = "manual rollback",
    audit_log_path: Path | None = Path("audit.log"),
    output_stream: TextIO | None = None,
) -> RolloutChange:
    """Roll back a plan to 0% through the approval gate and log to ``audit.log``."""
    out = output_stream if output_stream is not None else sys.stdout
    change = writer.rollback(plan, target_percentage=0, reason=reason)
    append_audit_entry(
        audit_log_path,
        action="rollback",
        feature_name=plan.feature_name,
        environment=plan.environment,
        old_percentage=old_percentage,
        new_percentage=0,
        reason=reason,
    )
    out.write(
        f"Rolled back {change.feature_name} ({change.environment}) from "
        f"{old_percentage}% -> 0%: {reason}\n"
    )
    return change
