"""Staged rollout controller with error-rate monitoring, rollback, and audit log."""

from __future__ import annotations

import json
import logging
import math
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TextIO
from urllib.parse import urlsplit, urlunsplit

import requests

from flagpilot.apply import ApprovalDeniedError, ApprovedFlagsmithWriter
from flagpilot.types import (
    Decision,
    HealthNotifier,
    MonotonicityCheck,
    Plan,
    PostRollbackVerification,
    RolloutChange,
    RolloutRunResult,
    StageMetrics,
    StageOutcome,
    Step,
)

logger = logging.getLogger(__name__)

OBSERVED_LABEL = "OBSERVED"


class SignalCollectionError(RuntimeError):
    """Raised when an external error-rate signal provider fails (fail-closed rollback)."""


class StageSignalProvider(Protocol):
    """Source of per-stage evaluation metrics for the rollout controller."""

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        """Collect traffic and error metrics at the active stage."""
        ...


def _sanitize_signal_url(raw_url: str) -> str:
    """Strip embedded credentials, query strings, and fragments from a signal URL."""
    parts = urlsplit(raw_url.strip())
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path or "/", "", ""))


def _scrub_url_credentials(text: str, raw_url: str, safe_url: str) -> str:
    cleaned = text.replace(raw_url, safe_url)
    parts = urlsplit(raw_url.strip())
    for secret in (parts.password, parts.username, parts.query):
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


def _parse_error_rate_payload(raw_text: str, *, source_name: str) -> float:
    """Parse JSON ``{"error_rate": <float>}`` or a plain float strictly in [0.0, 1.0]."""
    cleaned = raw_text.strip()
    if not cleaned:
        raise SignalCollectionError(f"{source_name} returned an empty response.")

    value: Any
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            value = float(cleaned)
        except ValueError as exc:
            raise SignalCollectionError(
                f"{source_name} returned non-numeric / malformed payload."
            ) from exc
    else:
        if isinstance(decoded, dict):
            if "error_rate" not in decoded:
                raise SignalCollectionError(
                    f"{source_name} JSON payload missing required 'error_rate' field."
                )
            value = decoded["error_rate"]
        elif isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
            value = decoded
        else:
            raise SignalCollectionError(
                f"{source_name} returned unsupported JSON type {type(decoded).__name__}."
            )

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalCollectionError(
            f"{source_name} error_rate must be a numeric float, got {type(value).__name__}."
        )
    rate = float(value)
    if not math.isfinite(rate) or not (0.0 <= rate <= 1.0):
        raise SignalCollectionError(
            f"{source_name} error_rate {rate!r} is outside valid range [0.0, 1.0]."
        )
    return rate


class HttpSignalProvider:
    """Reads observed error rate in [0, 1] from an HTTP GET endpoint."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 5.0,
        session: requests.Session | None = None,
    ) -> None:
        cleaned = url.strip()
        parts = urlsplit(cleaned)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("signal URL must be a valid http:// or https:// URL.")
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("signal timeout_seconds must be > 0.")
        self._url = cleaned
        self._safe_url = _sanitize_signal_url(cleaned)
        self._timeout_seconds = float(timeout_seconds)
        self._session = session

    @property
    def safe_url(self) -> str:
        """Return credential-free URL suitable for logging."""
        return self._safe_url

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        """Fetch and validate an observed error rate via HTTP GET."""
        del plan
        try:
            if self._session is not None:
                resp = self._session.get(self._url, timeout=self._timeout_seconds)
            else:
                resp = requests.get(self._url, timeout=self._timeout_seconds)
        except requests.RequestException as exc:
            sanitized = _scrub_url_credentials(str(exc), self._url, self._safe_url)
            logger.warning("HttpSignalProvider failed (%s): %s", self._safe_url, sanitized)
            raise SignalCollectionError(
                f"HTTP signal request failed for {self._safe_url}: {sanitized}"
            ) from exc

        if not resp.ok:
            logger.warning(
                "HttpSignalProvider returned HTTP %s from %s",
                resp.status_code,
                self._safe_url,
            )
            raise SignalCollectionError(
                f"HTTP signal {self._safe_url} returned HTTP {resp.status_code}."
            )

        rate = _parse_error_rate_payload(
            resp.text,
            source_name=f"HTTP signal ({self._safe_url})",
        )
        return StageMetrics(
            percentage=step.percentage,
            sample_count=max(step.min_samples, 1),
            error_count=0,
            enabled_count=0,
            source_label=OBSERVED_LABEL,
            reported_error_rate=rate,
        )


class FileSignalProvider:
    """Reads observed error rate in [0, 1] from a local file path."""

    def __init__(self, file_path: str | Path) -> None:
        raw_str = str(file_path).strip()
        if not raw_str:
            raise ValueError("signal file path must not be empty.")
        self._path = Path(raw_str)

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        """Read and validate an observed error rate from a local file."""
        del plan
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("FileSignalProvider failed (%s): %s", self._path, exc)
            raise SignalCollectionError(
                f"File signal failed reading {self._path}: {exc}"
            ) from exc

        rate = _parse_error_rate_payload(
            raw_text,
            source_name=f"File signal ({self._path})",
        )
        return StageMetrics(
            percentage=step.percentage,
            sample_count=max(step.min_samples, 1),
            error_count=0,
            enabled_count=0,
            source_label=OBSERVED_LABEL,
            reported_error_rate=rate,
        )


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
    exposed_count: int | None = None,
    sample_count: int | None = None,
    health_status: str | None = None,
    webhook_sent: bool | None = None,
    retained_from_previous_stage: dict[str, object] | None = None,
    verified: bool | None = None,
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
    if exposed_count is not None:
        entry["exposed_count"] = exposed_count
    if sample_count is not None:
        entry["sample_count"] = sample_count
    if health_status is not None:
        entry["health_status"] = health_status
    if webhook_sent is not None:
        entry["webhook_sent"] = webhook_sent
    if retained_from_previous_stage is not None:
        entry["retained_from_previous_stage"] = retained_from_previous_stage
    if verified is not None:
        entry["verified"] = verified
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _compute_monotonicity_check(
    prev_percentage: int | None,
    prev_exposed: frozenset[str] | None,
    curr_percentage: int,
    curr_exposed: tuple[str, ...] | None,
) -> MonotonicityCheck | None:
    """Verify that every identity exposed at the prior stage stays exposed on step-up."""
    if prev_percentage is None or prev_exposed is None or curr_exposed is None:
        return None
    curr_set = frozenset(curr_exposed)
    retained = len(prev_exposed & curr_set)
    prev_count = len(prev_exposed)
    curr_count = len(curr_set)
    monotonic = prev_exposed.issubset(curr_set)
    pct = (retained / prev_count * 100.0) if prev_count > 0 else 100.0
    pct_str = f"{int(pct)}%" if pct.is_integer() else f"{pct:.1f}%"
    summary = (
        f"{retained}/{prev_count} ({pct_str}) of {prev_percentage}% users stayed enrolled "
        f"at {curr_percentage}%"
    )
    return MonotonicityCheck(
        from_percentage=prev_percentage,
        to_percentage=curr_percentage,
        retained_count=retained,
        previous_exposed_count=prev_count,
        current_exposed_count=curr_count,
        monotonic=monotonic,
        summary=summary,
    )


def monotonicity_check_to_dict(check: MonotonicityCheck) -> dict[str, object]:
    """Serialize a MonotonicityCheck to a plain dictionary."""
    return {
        "from_percentage": check.from_percentage,
        "to_percentage": check.to_percentage,
        "retained_count": check.retained_count,
        "previous_exposed_count": check.previous_exposed_count,
        "current_exposed_count": check.current_exposed_count,
        "retention_percentage": round(check.retention_percentage, 2),
        "monotonic": check.monotonic,
        "summary": check.summary,
    }


def evaluate_stage(
    step: Step,
    sample_count: int,
    error_count: int,
    next_step: Step | None = None,
    *,
    observed_error_rate: float | None = None,
    rollback_target: int = 0,
    enforce_min_samples: bool = True,
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
    if enforce_min_samples and (sample_count == 0 or sample_count < step.min_samples):
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


def _notify_and_record_health(
    notifier: HealthNotifier | None,
    plan: Plan,
    *,
    status: Literal["firing", "resolved"],
    summary: str,
    description: str,
    audit_log_path: Path | None,
    old_percentage: int,
    new_percentage: int,
) -> tuple[str | None, bool | None]:
    """Dispatch Feature Health webhook fail-safely and append an audit entry when configured."""
    if notifier is None or not isinstance(notifier, HealthNotifier):
        return None, None
    health_label = "UNHEALTHY" if status == "firing" else "HEALTHY"
    try:
        delivered = bool(
            notifier.notify_health(
                plan.feature_name,
                plan.environment,
                status=status,
                summary=summary,
                description=description,
            )
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.warning("Feature Health notification (%s) failed: %s", status, exc)
        delivered = False

    delivery = "webhook delivered" if delivered else "webhook skipped or failed"
    append_audit_entry(
        audit_log_path,
        action="feature_health",
        feature_name=plan.feature_name,
        environment=plan.environment,
        old_percentage=old_percentage,
        new_percentage=new_percentage,
        reason=f"Flagsmith Feature Health {health_label} ({delivery}).",
        health_status=health_label,
        webhook_sent=delivered,
    )
    return health_label, delivered


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
        rollback_to_zero: bool = False,
        health_notifier: HealthNotifier | None = None,
        step_approve: bool = False,
        plain: bool = False,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be >= 0.")
        self._writer = writer
        self._signal_provider = signal_provider
        self._interval_seconds = interval_seconds
        self._sleep_fn = sleep_fn
        self._audit_log_path = audit_log_path
        self._output_stream = output_stream if output_stream is not None else sys.stdout
        self._rollback_to_zero = rollback_to_zero
        self._health_notifier = health_notifier
        self._step_approve = step_approve
        self._plain = plain

    def _log_decision(
        self,
        plan: Plan,
        *,
        action: str,
        old_percentage: int,
        new_percentage: int,
        reason: str,
        observed_error_rate: float | None = None,
        exposed_count: int | None = None,
        sample_count: int | None = None,
        health_status: str | None = None,
        webhook_sent: bool | None = None,
        retained_from_previous_stage: dict[str, object] | None = None,
        verified: bool | None = None,
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
            exposed_count=exposed_count,
            sample_count=sample_count,
            health_status=health_status,
            webhook_sent=webhook_sent,
            retained_from_previous_stage=retained_from_previous_stage,
            verified=verified,
        )

    def _is_tty_stream(self) -> bool:
        return bool(getattr(self._output_stream, "isatty", lambda: False)())

    def _rich_available(self) -> bool:
        if self._plain:
            return False
        try:
            import rich  # noqa: F401
        except ImportError:
            return False
        return True

    def _build_rollout_table(
        self,
        plan: Plan,
        outcomes: list[StageOutcome],
        *,
        is_synthetic: bool,
    ) -> Any:
        from rich.table import Table

        caption = (
            f"SYNTHETIC DATA — {plan.feature_name} ({plan.environment})"
            if is_synthetic
            else f"OBSERVED ERROR SIGNAL (Simulated Identities) — {plan.feature_name} ({plan.environment})"
        )
        table = Table(
            title=f"FlagPilot Live Rollout: {plan.feature_name} ({plan.environment})",
            caption=caption,
            header_style="bold cyan",
        )
        table.add_column("Stage %", justify="right", style="bold")
        table.add_column("Exposed Identities", justify="right")
        table.add_column("Retention Proof")
        table.add_column("Error Rate vs. Threshold", justify="right")
        table.add_column("Status", justify="center")

        for outcome in outcomes:
            m = outcome.metrics
            d = outcome.decision
            ret = outcome.retained_from_previous_stage
            if ret is None:
                retention_cell = "[dim]— (baseline)[/dim]"
            else:
                pct = ret.retention_percentage
                pct_str = f"{int(pct)}%" if pct.is_integer() else f"{pct:.1f}%"
                ret_style = "green" if ret.monotonic else "red"
                ret_word = "monotonic" if ret.monotonic else "NON-MONOTONIC"
                retention_cell = (
                    f"[{ret_style}]{ret.retained_count}/{ret.previous_exposed_count} "
                    f"({pct_str} {ret_word})[/{ret_style}]"
                )

            threshold = outcome.step.max_error_rate
            cmp_op = ">" if m.error_rate > threshold else "<="
            rate_style = "bold red" if d.action == "rollback" else "green"
            rate_cell = f"[{rate_style}]{m.error_rate:.2%} {cmp_op} {threshold:.2%}[/{rate_style}]"

            if d.action == "rollback":
                badge = "[bold white on red] ROLLBACK [/bold white on red]"
            elif d.action in ("hold", "pause"):
                badge = "[bold black on yellow] HOLD [/bold black on yellow]"
            elif d.action == "complete":
                badge = "[bold white on green] COMPLETE [/bold white on green]"
            else:
                badge = "[bold white on green] PASS [/bold white on green]"

            table.add_row(
                f"{outcome.step.percentage}%",
                f"{m.enabled_count}/{m.sample_count}",
                retention_cell,
                rate_cell,
                badge,
            )
        return table

    def _render_rollout_table(
        self,
        plan: Plan,
        outcomes: list[StageOutcome],
        *,
        is_synthetic: bool,
    ) -> None:
        if not outcomes or not self._rich_available():
            return
        from rich.console import Console

        is_tty = self._is_tty_stream()
        console = Console(
            file=self._output_stream,
            no_color=not is_tty,
            safe_box=True,
            highlight=False,
            width=None if is_tty else 140,
        )
        console.print(self._build_rollout_table(plan, outcomes, is_synthetic=is_synthetic))

    def _render_rollback_panel(
        self,
        *,
        from_percentage: int,
        rollback_target: int,
        reason: str,
        verification: PostRollbackVerification | None,
    ) -> None:
        if not self._rich_available():
            if verification is not None:
                badge = "VERIFIED 0/200 EXPOSED" if verification.verified else "EXPOSURE REMAINS"
                self._output_stream.write(f"{badge}: {verification.summary}\n")
            return

        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text

        is_tty = self._is_tty_stream()
        encoding = (getattr(self._output_stream, "encoding", None) or "utf-8").lower()
        arrow = "→" if "utf" in encoding else "->"
        check = "✔" if "utf" in encoding else "OK"
        console = Console(
            file=self._output_stream,
            no_color=not is_tty,
            safe_box=True,
            highlight=False,
            width=None if is_tty else 140,
        )
        panel = Panel(
            Text(
                f"Rollback triggered at {from_percentage}% -> {rollback_target}%: {reason}",
                style="bold",
            ),
            title=f"ROLLBACK EXECUTED {arrow} {rollback_target}%",
            border_style="bold red",
            padding=(0, 2),
        )
        console.print(panel)
        if verification is not None:
            if verification.verified:
                console.print(
                    f"[bold green]{check} VERIFIED {verification.exposed_count}/{verification.sample_count} EXPOSED[/bold green] "
                    f"— {verification.summary}"
                )
            else:
                console.print(
                    f"[bold red]ROLLBACK CHECK FAILED: {verification.exposed_count}/{verification.sample_count} EXPOSED[/bold red] "
                    f"— {verification.summary}"
                )

    def _verify_post_rollback(
        self,
        plan: Plan,
        target_percentage: int,
    ) -> PostRollbackVerification | None:
        """Run one verification tick after rolling back to confirm exposure reset."""
        if target_percentage != 0:
            return None
        verify_fn = getattr(self._signal_provider, "verify_rollback", None)
        if not callable(verify_fn):
            return None
        metrics = verify_fn(plan, target_percentage)
        verified = metrics.enabled_count == 0
        summary = (
            f"{metrics.enabled_count}/{metrics.sample_count} exposed identities "
            f"after rollback to {target_percentage}%"
        )
        self._output_stream.write(
            f"Post-rollback verification: {summary} (verified={verified})\n"
        )
        self._log_decision(
            plan,
            action="post_rollback_verification",
            old_percentage=target_percentage,
            new_percentage=target_percentage,
            reason=summary,
            exposed_count=metrics.enabled_count,
            sample_count=metrics.sample_count,
            verified=verified,
        )
        return PostRollbackVerification(
            target_percentage=target_percentage,
            exposed_count=metrics.enabled_count,
            sample_count=metrics.sample_count,
            verified=verified,
            summary=summary,
        )

    def run(self, plan: Plan, *, is_synthetic: bool = True) -> RolloutRunResult:
        """Walk through ``plan.steps``, waiting, evaluating, and rolling back on spike."""
        outcomes: list[StageOutcome] = []
        steps = plan.steps
        previous_safe_percentage = 0
        current_percentage = 0
        prev_stage_pct: int | None = None
        prev_exposed_ids: frozenset[str] | None = None

        if not self._step_approve:
            try:
                self._writer.authorize_run(plan, rollback_to_zero=self._rollback_to_zero)
            except ApprovalDeniedError as exc:
                self._log_decision(
                    plan,
                    action="aborted",
                    old_percentage=0,
                    new_percentage=0,
                    reason=str(exc),
                )
                self._output_stream.write(f"Rollout aborted: {exc}\n")
                return RolloutRunResult(
                    plan=plan,
                    final_status="aborted",
                    outcomes=(),
                    rollback_applied=False,
                    is_synthetic=is_synthetic,
                )

        use_live_table = self._rich_available() and self._is_tty_stream() and not self._step_approve
        live_ctx: Any = None
        if use_live_table:
            from rich.console import Console
            from rich.live import Live

            live_console = Console(file=self._output_stream, safe_box=True, highlight=False)
            live_ctx = Live(
                self._build_rollout_table(plan, outcomes, is_synthetic=is_synthetic),
                console=live_console,
                refresh_per_second=8,
            )
            live_ctx.start()

        try:
            for index, step in enumerate(steps):
                try:
                    self._writer.apply_stage(
                        plan,
                        step.percentage,
                        use_run_approval=not self._step_approve,
                    )
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

                next_step = steps[index + 1] if index + 1 < len(steps) else None
                rollback_target = (
                    plan.rollback_percentage if self._rollback_to_zero else previous_safe_percentage
                )
                try:
                    metrics = self._signal_provider.collect(plan, step)
                except SignalCollectionError as exc:
                    metrics = StageMetrics(
                        percentage=step.percentage,
                        sample_count=0,
                        error_count=0,
                        enabled_count=0,
                        source_label=OBSERVED_LABEL,
                        reported_error_rate=1.0,
                    )
                    monotonic_check = None
                    decision = Decision(
                        action="rollback",
                        reason=(
                            f"Signal collection failed (fail-closed): {exc}; "
                            f"rolling back to {rollback_target}%."
                        ),
                        current_percentage=step.percentage,
                        next_percentage=rollback_target,
                        observed_error_rate=1.0,
                        sample_count=0,
                    )
                else:
                    monotonic_check = _compute_monotonicity_check(
                        prev_stage_pct,
                        prev_exposed_ids,
                        step.percentage,
                        metrics.exposed_identities,
                    )
                    if monotonic_check is not None and not use_live_table:
                        self._output_stream.write(
                            f"Monotonicity check: {monotonic_check.summary}\n"
                        )
                    prev_stage_pct = step.percentage
                    if metrics.exposed_identities is not None:
                        prev_exposed_ids = frozenset(metrics.exposed_identities)

                    decision = evaluate_stage(
                        step=step,
                        sample_count=metrics.sample_count,
                        error_count=metrics.error_count,
                        next_step=next_step,
                        observed_error_rate=metrics.error_rate,
                        rollback_target=rollback_target,
                        enforce_min_samples=(metrics.source_label != OBSERVED_LABEL),
                    )
                outcomes.append(
                    StageOutcome(
                        step=step,
                        metrics=metrics,
                        decision=decision,
                        applied=True,
                        retained_from_previous_stage=monotonic_check,
                    )
                )
                if live_ctx is not None:
                    live_ctx.update(
                        self._build_rollout_table(plan, outcomes, is_synthetic=is_synthetic)
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
                    exposed_count=metrics.enabled_count,
                    sample_count=metrics.sample_count,
                    retained_from_previous_stage=(
                        monotonicity_check_to_dict(monotonic_check)
                        if monotonic_check is not None
                        else None
                    ),
                )

                if decision.action in ("hold", "pause"):
                    if live_ctx is not None:
                        live_ctx.stop()
                        live_ctx = None
                    else:
                        self._render_rollout_table(plan, outcomes, is_synthetic=is_synthetic)
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
                    if live_ctx is not None:
                        live_ctx.stop()
                        live_ctx = None
                    else:
                        self._render_rollout_table(plan, outcomes, is_synthetic=is_synthetic)
                    self._output_stream.write(
                        f"Rollback triggered at {step.percentage}% -> {rollback_target}%: "
                        f"{decision.reason}\n"
                    )
                    try:
                        self._writer.rollback(
                            plan,
                            target_percentage=rollback_target,
                            reason=decision.reason,
                            use_run_approval=not self._step_approve,
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
                    verification = self._verify_post_rollback(plan, rollback_target)
                    self._render_rollback_panel(
                        from_percentage=step.percentage,
                        rollback_target=rollback_target,
                        reason=decision.reason,
                        verification=verification,
                    )
                    health_status, webhook_sent = _notify_and_record_health(
                        self._health_notifier,
                        plan,
                        status="firing",
                        summary=f"FlagPilot rollback triggered at {step.percentage}% -> {rollback_target}%",
                        description=decision.reason,
                        audit_log_path=self._audit_log_path,
                        old_percentage=step.percentage,
                        new_percentage=rollback_target,
                    )
                    return RolloutRunResult(
                        plan=plan,
                        final_status="rolled_back",
                        outcomes=tuple(outcomes),
                        rollback_applied=True,
                        rollback_to_percentage=rollback_target,
                        is_synthetic=is_synthetic,
                        health_status=health_status,
                        health_webhook_sent=webhook_sent,
                        post_rollback_verification=verification,
                    )

                previous_safe_percentage = step.percentage
        finally:
            if live_ctx is not None:
                live_ctx.stop()

        if not use_live_table:
            self._render_rollout_table(plan, outcomes, is_synthetic=is_synthetic)

        health_status, webhook_sent = _notify_and_record_health(
            self._health_notifier,
            plan,
            status="resolved",
            summary=f"FlagPilot rollout completed for {plan.feature_name}",
            description=f"All {len(steps)} rollout stage(s) passed within threshold.",
            audit_log_path=self._audit_log_path,
            old_percentage=current_percentage,
            new_percentage=current_percentage,
        )
        return RolloutRunResult(
            plan=plan,
            final_status="completed",
            outcomes=tuple(outcomes),
            rollback_applied=False,
            is_synthetic=is_synthetic,
            health_status=health_status,
            health_webhook_sent=webhook_sent,
        )


def execute_manual_rollback(
    writer: ApprovedFlagsmithWriter,
    plan: Plan,
    *,
    old_percentage: int = 0,
    reason: str = "manual rollback",
    audit_log_path: Path | None = Path("audit.log"),
    output_stream: TextIO | None = None,
    health_notifier: HealthNotifier | None = None,
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
    health_status, webhook_sent = _notify_and_record_health(
        health_notifier,
        plan,
        status="resolved",
        summary=f"FlagPilot manual rollback/reset for {plan.feature_name}",
        description=reason,
        audit_log_path=audit_log_path,
        old_percentage=old_percentage,
        new_percentage=0,
    )
    if health_status is not None:
        delivery = "webhook delivered" if webhook_sent else "webhook skipped or failed"
        out.write(f"Feature Health: {health_status} ({delivery})\n")
    return change
