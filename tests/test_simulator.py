"""Unit tests for the FlagPilot persistent-identity SYNTHETIC DATA simulator."""

from __future__ import annotations

from pathlib import Path

import pytest

from flagpilot.simulator import (
    DEFAULT_BASELINE_ERROR_RATE,
    DEFAULT_USER_COUNT,
    SYNTHETIC_LABEL,
    OfflineFakeEvaluator,
    TrafficSimulator,
    load_or_create_identities,
    make_error_rate_fn,
)
from flagpilot.types import FlagEvaluation


class _RecordingFakeEvaluator:
    """Offline fake evaluator recording every (round/call, identifier) evaluated."""

    def __init__(self, exposed_ids: set[str] | None = None) -> None:
        self.exposed_ids = exposed_ids or set()
        self.calls: list[str] = []

    def evaluate(self, identifier: str, feature_name: str) -> FlagEvaluation:
        self.calls.append(identifier)
        return FlagEvaluation(
            identifier=identifier,
            feature_name=feature_name,
            enabled=identifier in self.exposed_ids,
            value="v2" if identifier in self.exposed_ids else "v1",
            source_label=SYNTHETIC_LABEL,
        )


def test_default_identities_and_same_identities_every_round(tmp_path: Path) -> None:
    """Simulator uses 200 user_000..user_199 identities and evaluates identical IDs each round."""
    state_file = tmp_path / "identities.json"
    identities = load_or_create_identities(state_path=state_file)
    assert len(identities) == DEFAULT_USER_COUNT == 200
    assert identities[0] == "user_000"
    assert identities[-1] == "user_199"
    assert load_or_create_identities(state_path=state_file) == identities

    evaluator = _RecordingFakeEvaluator()
    sim = TrafficSimulator(evaluator, state_path=state_file, seed=7)
    round_1 = sim.run_round("checkout_v2")
    round_2 = sim.run_round("checkout_v2")

    assert round_1.identities == tuple(identities)
    assert round_2.identities == tuple(identities)
    assert evaluator.calls[:200] == identities
    assert evaluator.calls[200:] == identities
    assert round_1.label == "SYNTHETIC DATA"
    assert "SYNTHETIC DATA" in round_1.format_summary()


def test_offline_fake_evaluator_with_spike_produces_expected_error_rate() -> None:
    """OfflineFakeEvaluator + configurable spike produces expected seeded error rates."""
    evaluator = OfflineFakeEvaluator(rollout_percentage=10)
    error_fn = make_error_rate_fn(
        baseline_rate=DEFAULT_BASELINE_ERROR_RATE,
        spike_rate=0.25,
        spike_threshold_pct=25.0,
    )
    sim_a = TrafficSimulator(evaluator, user_count=200, seed=123, error_rate_fn=error_fn)
    sim_b = TrafficSimulator(
        OfflineFakeEvaluator(rollout_percentage=10),
        user_count=200,
        seed=123,
        error_rate_fn=error_fn,
    )

    # Round 1: ~10% exposure (< 25% threshold) -> baseline 0.5% target rate
    r1_a = sim_a.run_round("checkout_v2")
    r1_b = sim_b.run_round("checkout_v2")
    assert r1_a == r1_b
    assert r1_a.spike_injected is False
    assert r1_a.target_exposed_error_rate == pytest.approx(0.005)
    assert r1_a.exposed_count > 0

    # Round 2: 50% exposure (>= 25% threshold) -> injected 25% spike
    evaluator.set_percentage(50)
    r2 = sim_a.run_round("checkout_v2")
    assert r2.spike_injected is True
    assert r2.target_exposed_error_rate == pytest.approx(0.25)
    assert r2.exposure_percentage >= 25.0
    assert r2.exposed_error_rate == pytest.approx(0.25, abs=0.08)
    assert r2.label == "SYNTHETIC DATA"
    assert r2.to_stage_metrics(50).source_label == "SYNTHETIC DATA"
    assert "SYNTHETIC DATA" in r2.format_summary()


def test_exact_injected_spike_rate_with_fixed_exposed_set() -> None:
    """A 100% spike when exposure exceeds threshold hits 1.00 exposed error rate."""
    identities = load_or_create_identities(100)
    evaluator = _RecordingFakeEvaluator(exposed_ids=set(identities[:40]))  # 40% exposed
    sim = TrafficSimulator(
        evaluator,
        identities=identities,
        seed=99,
        baseline_rate=0.0,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=1.0,
            spike_threshold_pct=25.0,
        ),
    )

    result = sim.run_round("checkout_v2")
    assert result.exposed_count == 40
    assert result.exposed_identities == tuple(identities[:40])
    assert result.unexposed_identities == tuple(identities[40:])
    assert result.spike_injected is True
    assert result.exposed_error_rate == pytest.approx(1.0)
    assert result.overall_error_rate == pytest.approx(0.40)

