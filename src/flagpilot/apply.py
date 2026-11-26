"""Human approval gate and Flagsmith write boundary.

Every write to Flagsmith must pass through this module. Non-interactive runs
are always refused, and no auto-approve or default-yes mechanism exists.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from flagpilot.planner import render_plan_markdown
from flagpilot.types import FlagsmithClient, Plan, RolloutBackend, RolloutChange

APPROVAL_PHRASE = "APPROVE"


class ApprovalDeniedError(RuntimeError):
    """Raised when a Flagsmith write is not explicitly approved by a human."""


class NonInteractiveError(ApprovalDeniedError):
    """Raised when a Flagsmith write is attempted without an interactive TTY."""


def require_approval(
    change_summary: str,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    is_tty: bool | None = None,
) -> None:
    """Require an interactive human to type exact ``APPROVE`` before continuing."""
    in_stream = input_stream if input_stream is not None else sys.stdin
    out_stream = output_stream if output_stream is not None else sys.stdout
    interactive = in_stream.isatty() if is_tty is None else is_tty

    if not interactive:
        raise NonInteractiveError(
            "Refusing Flagsmith write in non-interactive mode; interactive APPROVE is required."
        )

    out_stream.write("\n=== FlagPilot Proposed Flagsmith Change ===\n")
    out_stream.write(f"{change_summary.rstrip()}\n")
    out_stream.write(f"Type {APPROVAL_PHRASE} to apply this change: ")
    out_stream.flush()

    response = in_stream.readline()
    if not response or response.rstrip("\r\n") != APPROVAL_PHRASE:
        raise ApprovalDeniedError(
            f"Flagsmith write aborted: expected exact '{APPROVAL_PHRASE}'."
        )


def _format_stage_summary(plan: Plan, percentage: int, *, action: str) -> str:
    segment_name = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
    return (
        f"Action: {action}\n"
        f"Feature: {plan.feature_name}\n"
        f"Environment: {plan.environment}\n"
        f"Segment: {segment_name}\n"
        f"Target percentage: {percentage}% (override enabled={percentage > 0})"
    )


def _format_run_policy_summary(plan: Plan, *, rollback_to_zero: bool = False) -> str:
    segment_name = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
    schedule = " -> ".join(f"{step.percentage}%" for step in plan.steps)
    min_samples = plan.steps[0].min_samples if plan.steps else 20
    max_error_rate = plan.steps[0].max_error_rate if plan.steps else 0.02
    rollback_policy = (
        f"{plan.rollback_percentage}% (direct zero-exposure rollback)"
        if rollback_to_zero
        else "previous safe stage (or 0% on initial stage)"
    )
    return (
        "Action: authorize_rollout_run\n"
        f"Feature: {plan.feature_name}\n"
        f"Environment: {plan.environment}\n"
        f"Segment: {segment_name}\n"
        f"Rollout schedule: {schedule}\n"
        f"Safety policy: min_samples={min_samples}, max_error_rate={max_error_rate:.2%} -> "
        f"auto-rollback to {rollback_policy}\n"
        "Execution mode: stages and emergency rollback run automatically after this approval."
    )


class ApprovedFlagsmithWriter:
    """Write guard ensuring every Flagsmith mutation passes require_approval."""

    def __init__(
        self,
        backend: FlagsmithClient | RolloutBackend | Callable[[], FlagsmithClient | RolloutBackend],
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        is_tty: bool | None = None,
        audit_log_path: Path | None = None,
    ) -> None:
        self._backend = backend
        self._input_stream = input_stream
        self._output_stream = output_stream
        self._is_tty = is_tty
        self._audit_log_path = audit_log_path
        self._segment_ids: dict[tuple[str, str], int] = {}
        self._authorized_plans: list[Plan] = []

    def _is_run_authorized(self, plan: Plan) -> bool:
        return any(authorized is plan for authorized in self._authorized_plans)

    def authorize_run(self, plan: Plan, *, rollback_to_zero: bool = False) -> None:
        """Require upfront APPROVE for the entire rollout schedule and rollback safety policy."""
        summary = _format_run_policy_summary(plan, rollback_to_zero=rollback_to_zero)
        require_approval(
            summary,
            input_stream=self._input_stream,
            output_stream=self._output_stream,
            is_tty=self._is_tty,
        )
        if not self._is_run_authorized(plan):
            self._authorized_plans.append(plan)
        segment_name = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
        self._record_audit(
            "approve_run",
            RolloutChange(
                feature_name=plan.feature_name,
                environment=plan.environment,
                segment_name=segment_name,
                percentage=0,
                enabled=False,
            ),
            reason="approved rollout schedule and safety policy",
        )

    def _record_audit(self, action: str, change: RolloutChange, reason: str = "") -> None:
        if self._audit_log_path is None:
            return
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "feature_name": change.feature_name,
            "environment": change.environment,
            "segment_name": change.segment_name,
            "percentage": change.percentage,
            "enabled": change.enabled,
            "reason": reason,
        }
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def _write_percentage(self, plan: Plan, percentage: int) -> RolloutChange:
        if callable(self._backend):
            self._backend = self._backend()
        if hasattr(self._backend, "apply_stage") and percentage > 0:
            return self._backend.apply_stage(plan, percentage)
        if hasattr(self._backend, "rollback") and percentage == 0:
            return self._backend.rollback(plan)

        if isinstance(self._backend, FlagsmithClient):
            segment_name = plan.segment_name or f"flagpilot-{plan.feature_name}-rollout"
            key = (plan.feature_name, segment_name)
            self._backend.create_feature(plan.feature_name)
            segment_id = self._segment_ids.get(key)
            if segment_id is None:
                seg = self._backend.create_segment_with_percentage_split(
                    segment_name,
                    percentage,
                )
                segment_id = int(seg["id"])
                self._segment_ids[key] = segment_id
            else:
                self._backend.set_segment_percentage(
                    segment_id,
                    percentage,
                    name=segment_name,
                )

            enabled = percentage > 0
            if enabled:
                self._backend.attach_segment_override(
                    plan.feature_name,
                    segment_id,
                    enabled=True,
                )
            else:
                state = self._backend.get_state(plan.feature_name, segment_id=segment_id)
                if state.get("segment_override") is not None:
                    self._backend.disable_override(plan.feature_name, segment_id=segment_id)

            return RolloutChange(
                feature_name=plan.feature_name,
                environment=plan.environment,
                segment_name=segment_name,
                percentage=percentage,
                enabled=enabled,
                details=f"segment_id={segment_id}",
            )

        raise TypeError("Backend does not implement FlagsmithClient or RolloutBackend.")

    def apply_stage(
        self,
        plan: Plan,
        percentage: int,
        *,
        use_run_approval: bool = False,
    ) -> RolloutChange:
        """Gate and apply a rollout stage percentage to Flagsmith."""
        if not (use_run_approval and self._is_run_authorized(plan)):
            summary = _format_stage_summary(plan, percentage, action="apply_stage")
            require_approval(
                summary,
                input_stream=self._input_stream,
                output_stream=self._output_stream,
                is_tty=self._is_tty,
            )
        change = self._write_percentage(plan, percentage)
        self._record_audit("apply_stage", change)
        return change

    def apply_plan(self, plan: Plan, *, percentage: int | None = None) -> RolloutChange:
        """Print the full rollout plan and apply its target percentage only after APPROVE."""
        target_pct = percentage if percentage is not None else plan.steps[-1].percentage
        out = self._output_stream if self._output_stream is not None else sys.stdout
        out.write(render_plan_markdown(plan))
        out.flush()
        return self.apply_stage(plan, target_pct)

    def rollback(
        self,
        plan: Plan,
        *,
        target_percentage: int | None = None,
        reason: str = "",
        use_run_approval: bool = False,
    ) -> RolloutChange:
        """Gate and apply a rollback to ``target_percentage`` (or ``plan.rollback_percentage``)."""
        resolved_target = (
            plan.rollback_percentage if target_percentage is None else target_percentage
        )
        if not (use_run_approval and self._is_run_authorized(plan)):
            summary = _format_stage_summary(plan, resolved_target, action="rollback")
            if reason:
                summary = f"{summary}\nReason: {reason}"
            require_approval(
                summary,
                input_stream=self._input_stream,
                output_stream=self._output_stream,
                is_tty=self._is_tty,
            )
        change = self._write_percentage(plan, resolved_target)
        self._record_audit("rollback", change, reason=reason)
        return change
