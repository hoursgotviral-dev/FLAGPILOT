"""Core immutable data structures and interfaces for FlagPilot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

DecisionAction = Literal["advance", "hold", "pause", "rollback", "complete"]
BreachAction = Literal["pause", "rollback"]


@dataclass(frozen=True)
class Step:
    """Single stage in a staged rollout plan."""

    percentage: int
    min_samples: int = 20
    max_error_rate: float = 0.02


@dataclass(frozen=True)
class Plan:
    """Deterministic staged rollout plan for a feature flag."""

    feature_name: str
    environment: str
    steps: tuple[Step, ...]
    project_id: int | None = None
    segment_name: str | None = None
    rollback_percentage: int = 0
    on_breach: BreachAction = "rollback"
    description: str = ""
    source_request: str = ""


@dataclass(frozen=True)
class Decision:
    """Controller evaluation decision for a rollout stage."""

    action: DecisionAction
    reason: str
    current_percentage: int
    next_percentage: int | None = None
    observed_error_rate: float = 0.0
    sample_count: int = 0


@dataclass(frozen=True)
class FlagEvaluation:
    """Single identity-based feature flag evaluation result."""

    identifier: str
    feature_name: str
    enabled: bool
    value: str | int | float | bool | None = None
    source_label: str = "SYNTHETIC DATA"


@dataclass(frozen=True)
class StageMetrics:
    """Aggregated traffic metrics observed for a single rollout stage."""

    percentage: int
    sample_count: int
    error_count: int
    enabled_count: int
    source_label: str = "SYNTHETIC DATA"
    reported_error_rate: float | None = None

    @property
    def error_rate(self) -> float:
        """Return observed error rate in [0.0, 1.0]."""
        if self.reported_error_rate is not None:
            return self.reported_error_rate
        if self.sample_count <= 0:
            return 0.0
        return self.error_count / self.sample_count


@dataclass(frozen=True)
class RolloutChange:
    """Summary of a Flagsmith configuration change."""

    feature_name: str
    environment: str
    segment_name: str
    percentage: int
    enabled: bool
    details: str = ""


@dataclass(frozen=True)
class StageOutcome:
    """Recorded outcome of executing and evaluating one rollout stage."""

    step: Step
    metrics: StageMetrics
    decision: Decision
    applied: bool


@dataclass(frozen=True)
class RolloutRunResult:
    """Complete execution result for a staged rollout run."""

    plan: Plan
    final_status: Literal["completed", "paused", "rolled_back", "aborted"]
    outcomes: tuple[StageOutcome, ...]
    rollback_applied: bool = False
    rollback_to_percentage: int | None = None
    is_synthetic: bool = True


@runtime_checkable
class FlagsmithClient(Protocol):
    """Protocol for Flagsmith Management API clients (replaceable with fakes in tests)."""

    def create_feature(
        self,
        name: str,
        *,
        description: str = "",
        default_enabled: bool = False,
    ) -> dict[str, Any]:
        """Create (or retrieve) a project feature flag."""
        ...

    def create_segment_with_percentage_split(
        self,
        name: str,
        percent: int,
    ) -> dict[str, Any]:
        """Create (or update) a project segment using the PERCENTAGE_SPLIT operator."""
        ...

    def attach_segment_override(
        self,
        feature_name: str,
        segment_id: int,
        *,
        enabled: bool = True,
        priority: int = 0,
    ) -> dict[str, Any]:
        """Attach or update a segment override for a feature in the target environment."""
        ...

    def set_segment_percentage(
        self,
        segment_id: int,
        percent: int,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Update the PERCENTAGE_SPLIT condition value on an existing segment."""
        ...

    def disable_override(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        """Disable a segment override (or environment feature state if segment_id is None)."""
        ...

    def get_state(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        """Fetch current feature state and segment overrides in the target environment."""
        ...


class RolloutBackend(Protocol):
    """Minimal Flagsmith write interface consumed by apply.py and controller.py."""

    def apply_stage(self, plan: Plan, percentage: int) -> RolloutChange:
        """Set the rollout segment percentage and enable its override for the plan."""
        ...

    def rollback(self, plan: Plan) -> RolloutChange:
        """Roll back the rollout segment percentage and disable/revert its override."""
        ...


class FlagEvaluator(Protocol):
    """Minimal identity-based flag evaluation interface."""

    def evaluate(self, identifier: str, feature_name: str) -> FlagEvaluation:
        """Evaluate a feature flag for a persistent identity."""
        ...


@runtime_checkable
class HealthNotifier(Protocol):
    """Fail-safe notifier for Flagsmith Feature Health webhook events."""

    def notify_health(
        self,
        feature_name: str,
        environment: str,
        *,
        status: Literal["firing", "resolved"],
        summary: str,
        description: str,
    ) -> bool:
        """Send an Alertmanager-format health event; never raises on failure."""
        ...
