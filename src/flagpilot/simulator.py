"""SYNTHETIC DATA traffic simulator using stable persistent identities.

Flagsmith Python SDK calls live exclusively in this module (verified against
local self-hosted Flagsmith 2.295.2).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from flagsmith import Flagsmith

from flagpilot.types import FlagEvaluation, FlagEvaluator, StageMetrics

SYNTHETIC_LABEL = "SYNTHETIC DATA"
DEFAULT_USER_COUNT = 200
DEFAULT_BASELINE_ERROR_RATE = 0.005

ErrorRateFn = Callable[[float, int], float]


@dataclass(frozen=True)
class SimulationRoundResult:
    """Result of evaluating all persistent identities in one simulation round."""

    round_number: int
    feature_name: str
    identities: tuple[str, ...]
    exposed_identities: tuple[str, ...]
    unexposed_identities: tuple[str, ...]
    exposed_error_count: int
    baseline_error_count: int
    target_exposed_error_rate: float
    spike_injected: bool
    label: str = SYNTHETIC_LABEL

    @property
    def total_count(self) -> int:
        return len(self.identities)

    @property
    def exposed_count(self) -> int:
        return len(self.exposed_identities)

    @property
    def exposure_percentage(self) -> float:
        if not self.identities:
            return 0.0
        return (len(self.exposed_identities) / len(self.identities)) * 100.0

    @property
    def exposed_error_rate(self) -> float:
        if not self.exposed_identities:
            return 0.0
        return self.exposed_error_count / len(self.exposed_identities)

    @property
    def overall_error_count(self) -> int:
        return self.exposed_error_count + self.baseline_error_count

    @property
    def overall_error_rate(self) -> float:
        if not self.identities:
            return 0.0
        return self.overall_error_count / len(self.identities)

    def to_stage_metrics(self, percentage: int) -> StageMetrics:
        """Convert round results into StageMetrics for RolloutController."""
        monitored_rate = (
            self.exposed_error_rate if self.exposed_count >= 10 else self.overall_error_rate
        )
        return StageMetrics(
            percentage=percentage,
            sample_count=self.total_count,
            error_count=self.overall_error_count,
            enabled_count=self.exposed_count,
            source_label=self.label,
            reported_error_rate=monitored_rate,
        )

    def format_summary(self) -> str:
        """Return a human-readable summary explicitly tagged SYNTHETIC DATA."""
        return (
            f"[{self.label}] round={self.round_number} feature={self.feature_name} "
            f"exposed={self.exposed_count}/{self.total_count} "
            f"({self.exposure_percentage:.1f}%) "
            f"exposed_error_rate={self.exposed_error_rate:.2%} "
            f"overall_error_rate={self.overall_error_rate:.2%} "
            f"spike_injected={self.spike_injected}"
        )


class FlagsmithSDKEvaluator:
    """Flagsmith Python SDK evaluator using persistent identity evaluations.

    ``% Split`` (``PERCENTAGE_SPLIT``) requires an identity identifier on every
    evaluation call; environment-only requests cannot trigger percentage splits.
    """

    def __init__(
        self,
        environment_key: str,
        api_url: str,
        *,
        enable_local_evaluation: bool = False,
    ) -> None:
        if not environment_key:
            raise ValueError("environment_key is required for Flagsmith SDK evaluation.")
        if not api_url:
            raise ValueError("api_url is required for Flagsmith SDK evaluation.")
        normalized_url = api_url.rstrip("/")
        if not normalized_url.endswith("/api/v1"):
            normalized_url = f"{normalized_url}/api/v1/"
        else:
            normalized_url = f"{normalized_url}/"

        self._enable_local_evaluation = enable_local_evaluation
        if enable_local_evaluation:
            self._client = Flagsmith(
                environment_key=environment_key,
                api_url=normalized_url,
                enable_local_evaluation=True,
            )
        else:
            self._client = Flagsmith(
                environment_key=environment_key,
                api_url=normalized_url,
            )

    @classmethod
    def from_env(cls) -> FlagsmithSDKEvaluator:
        """Build evaluator from FLAGSMITH_SERVER_KEY (or CLIENT_KEY) and FLAGSMITH_API_URL."""
        load_dotenv()
        api_url = os.environ.get("FLAGSMITH_API_URL", "").strip()
        env_key = (
            os.environ.get("FLAGSMITH_SERVER_KEY", "").strip()
            or os.environ.get("FLAGSMITH_CLIENT_KEY", "").strip()
        )
        return cls(
            environment_key=env_key,
            api_url=api_url,
            enable_local_evaluation=env_key.startswith("ser."),
        )

    def refresh(self) -> None:
        """Refresh the cached environment document when local evaluation is enabled."""
        if self._enable_local_evaluation:
            self._client.update_environment()

    def close(self) -> None:
        """Stop any background polling threads started by the Flagsmith SDK."""
        self._client.close()

    def evaluate(self, identifier: str, feature_name: str) -> FlagEvaluation:
        """Evaluate ``feature_name`` for a stable persistent ``identifier``."""
        if not identifier:
            raise ValueError("Persistent identity identifier must not be empty.")
        flags = self._client.get_identity_flags(identifier=identifier)
        enabled = bool(flags.is_feature_enabled(feature_name))
        value = flags.get_feature_value(feature_name)
        return FlagEvaluation(
            identifier=identifier,
            feature_name=feature_name,
            enabled=enabled,
            value=value,
            source_label=SYNTHETIC_LABEL,
        )


class OfflineFakeEvaluator:
    """Pure offline deterministic evaluator simulating Flagsmith % Split bucketing."""

    def __init__(
        self,
        rollout_percentage: int = 0,
        *,
        variant_value: str | float | bool | None = "new-variant",
        control_value: str | float | bool | None = "control",
    ) -> None:
        self.set_percentage(rollout_percentage)
        self._variant_value = variant_value
        self._control_value = control_value

    def set_percentage(self, rollout_percentage: int) -> None:
        if isinstance(rollout_percentage, bool) or not isinstance(rollout_percentage, int):
            raise TypeError("rollout_percentage must be an integer.")
        if not (0 <= rollout_percentage <= 100):
            raise ValueError("rollout_percentage must be between 0 and 100.")
        self._rollout_percentage = rollout_percentage

    @staticmethod
    def _bucket(identifier: str, feature_name: str) -> float:
        digest = hashlib.sha256(f"{feature_name}:{identifier}".encode()).digest()
        return (int.from_bytes(digest[:8], "big") / float(1 << 64)) * 100.0

    def evaluate(self, identifier: str, feature_name: str) -> FlagEvaluation:
        if not identifier:
            raise ValueError("Persistent identity identifier must not be empty.")
        enabled = self._bucket(identifier, feature_name) < float(self._rollout_percentage)
        return FlagEvaluation(
            identifier=identifier,
            feature_name=feature_name,
            enabled=enabled,
            value=self._variant_value if enabled else self._control_value,
            source_label=SYNTHETIC_LABEL,
        )


def make_error_rate_fn(
    *,
    baseline_rate: float = DEFAULT_BASELINE_ERROR_RATE,
    spike_rate: float | None = None,
    spike_threshold_pct: float | None = None,
) -> ErrorRateFn:
    """Build a configurable error-rate function for exposed users.

    Returns ``baseline_rate`` (default 0.5%) unless both ``spike_rate`` and
    ``spike_threshold_pct`` are set and ``exposure_pct >= spike_threshold_pct``.
    """
    if not (0.0 <= baseline_rate <= 1.0):
        raise ValueError("baseline_rate must be between 0.0 and 1.0.")
    if spike_rate is not None and not (0.0 <= spike_rate <= 1.0):
        raise ValueError("spike_rate must be between 0.0 and 1.0.")
    if spike_threshold_pct is not None and not (0.0 <= spike_threshold_pct <= 100.0):
        raise ValueError("spike_threshold_pct must be between 0.0 and 100.0.")

    def _error_rate(exposure_pct: float, _round_number: int) -> float:
        if (
            spike_rate is not None
            and spike_threshold_pct is not None
            and exposure_pct >= spike_threshold_pct
        ):
            return spike_rate
        return baseline_rate

    return _error_rate


def load_or_create_identities(
    count: int = DEFAULT_USER_COUNT,
    *,
    state_path: Path | None = None,
    prefix: str = "user",
) -> list[str]:
    """Return ``count`` stable, persistent identities (``user_000..``) in fixed order."""
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ValueError("count must be a positive integer.")

    existing: list[str] = []
    if state_path is not None and state_path.exists():
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"Invalid identity state file: {state_path}")
        existing = list(raw)

    identities = list(existing)
    next_index = len(identities)
    seen = set(identities)
    while len(identities) < count:
        candidate = f"{prefix}_{next_index:03d}"
        next_index += 1
        if candidate not in seen:
            identities.append(candidate)
            seen.add(candidate)

    if state_path is not None and len(identities) != len(existing):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(identities, indent=2) + "\n", encoding="utf-8")

    return identities[:count]


class TrafficSimulator:
    """Evaluates persistent identities across rounds and simulates SYNTHETIC DATA errors."""

    def __init__(
        self,
        evaluator: FlagEvaluator,
        *,
        user_count: int = DEFAULT_USER_COUNT,
        identities: Sequence[str] | None = None,
        state_path: Path | None = None,
        seed: int = 42,
        baseline_rate: float = DEFAULT_BASELINE_ERROR_RATE,
        error_rate_fn: ErrorRateFn | None = None,
    ) -> None:
        if not (0.0 <= baseline_rate <= 1.0):
            raise ValueError("baseline_rate must be between 0.0 and 1.0.")
        self._evaluator = evaluator
        self._identities: tuple[str, ...] = (
            tuple(identities)
            if identities is not None
            else tuple(load_or_create_identities(user_count, state_path=state_path))
        )
        if not self._identities:
            raise ValueError("TrafficSimulator requires at least one identity.")
        self._baseline_rate = baseline_rate
        self._error_rate_fn = error_rate_fn or make_error_rate_fn(baseline_rate=baseline_rate)
        self._rng = random.Random(seed)
        self._round_number = 0

    @property
    def identities(self) -> tuple[str, ...]:
        """Return the persistent ordered identity tuple reused every round."""
        return self._identities

    def run_round(self, feature_name: str) -> SimulationRoundResult:
        """Evaluate ``feature_name`` for every persistent identity and simulate errors."""
        refresh = getattr(self._evaluator, "refresh", None)
        if callable(refresh):
            refresh()
        self._round_number += 1
        exposed: list[str] = []
        unexposed: list[str] = []
        for identifier in self._identities:
            evaluation = self._evaluator.evaluate(identifier, feature_name)
            if evaluation.enabled:
                exposed.append(identifier)
            else:
                unexposed.append(identifier)

        exposure_pct = (len(exposed) / len(self._identities)) * 100.0
        target_exposed_rate = self._error_rate_fn(exposure_pct, self._round_number)
        if not (0.0 <= target_exposed_rate <= 1.0):
            raise ValueError("error_rate_fn must return a probability in [0.0, 1.0].")

        exposed_errors = sum(1 for _ in exposed if self._rng.random() < target_exposed_rate)
        baseline_errors = sum(1 for _ in unexposed if self._rng.random() < self._baseline_rate)
        spike_injected = target_exposed_rate > self._baseline_rate

        return SimulationRoundResult(
            round_number=self._round_number,
            feature_name=feature_name,
            identities=self._identities,
            exposed_identities=tuple(exposed),
            unexposed_identities=tuple(unexposed),
            exposed_error_count=exposed_errors,
            baseline_error_count=baseline_errors,
            target_exposed_error_rate=target_exposed_rate,
            spike_injected=spike_injected,
            label=SYNTHETIC_LABEL,
        )


def simulate_stage_traffic(
    evaluator: FlagEvaluator,
    feature_name: str,
    identities: Sequence[str],
    *,
    percentage: int,
    enabled_error_rate: float = DEFAULT_BASELINE_ERROR_RATE,
    baseline_error_rate: float = DEFAULT_BASELINE_ERROR_RATE,
    seed: int = 42,
) -> StageMetrics:
    """Convenience helper running one simulation round and returning StageMetrics."""
    simulator = TrafficSimulator(
        evaluator,
        identities=identities,
        seed=seed + percentage,
        baseline_rate=baseline_error_rate,
        error_rate_fn=make_error_rate_fn(baseline_rate=enabled_error_rate),
    )
    return simulator.run_round(feature_name).to_stage_metrics(percentage)
