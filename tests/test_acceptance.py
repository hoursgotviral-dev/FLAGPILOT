"""Acceptance tests for FlagPilot features using fake clients and mocks only."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from flagpilot.apply import (
    ApprovalDeniedError,
    ApprovedFlagsmithWriter,
    NonInteractiveError,
    require_approval,
)
from flagpilot.controller import RolloutController, execute_manual_rollback
from flagpilot.planner import PlanValidationError, build_plan, load_plan, save_plan
from flagpilot.report import (
    load_audit_log,
    load_run_result,
    render_markdown_report,
    save_html_report,
    save_run_result,
)
from flagpilot.simulator import load_or_create_identities, simulate_stage_traffic
from flagpilot.types import FlagEvaluation, Plan, StageMetrics, Step


class _FakeFlagsmithClient:
    """In-memory fake implementing FlagsmithClient Protocol."""

    def __init__(self) -> None:
        self.features: dict[str, dict[str, Any]] = {}
        self.segments: dict[int, dict[str, Any]] = {}
        self.overrides: dict[tuple[str, int | None], dict[str, Any]] = {}
        self.percentage_history: list[int] = []
        self.disabled_calls: list[tuple[str, int | None]] = []

    def create_feature(
        self,
        name: str,
        *,
        description: str = "",
        default_enabled: bool = False,
    ) -> dict[str, Any]:
        feat = self.features.setdefault(
            name,
            {"id": len(self.features) + 1, "name": name, "default_enabled": default_enabled},
        )
        return feat

    def create_segment_with_percentage_split(self, name: str, percent: int) -> dict[str, Any]:
        seg_id = 1
        seg = {"id": seg_id, "name": name, "percent": percent}
        self.segments[seg_id] = seg
        self.percentage_history.append(percent)
        return seg

    def attach_segment_override(
        self,
        feature_name: str,
        segment_id: int,
        *,
        enabled: bool = True,
        priority: int = 0,
    ) -> dict[str, Any]:
        state = {"feature_name": feature_name, "segment_id": segment_id, "enabled": enabled}
        self.overrides[(feature_name, segment_id)] = state
        return state

    def set_segment_percentage(
        self,
        segment_id: int,
        percent: int,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        self.segments[segment_id]["percent"] = percent
        self.percentage_history.append(percent)
        return self.segments[segment_id]

    def disable_override(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        self.disabled_calls.append((feature_name, segment_id))
        state = self.overrides[(feature_name, segment_id)]
        state["enabled"] = False
        return state

    def get_state(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "feature_name": feature_name,
            "segment_override": self.overrides.get((feature_name, segment_id)),
        }


class _FakeSignalProvider:
    def __init__(self, metrics_by_pct: dict[int, StageMetrics]) -> None:
        self._metrics_by_pct = metrics_by_pct

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        return self._metrics_by_pct[step.percentage]


class _FakeEvaluator:
    def __init__(self, enabled_ratio: float = 0.5) -> None:
        self._enabled_ratio = enabled_ratio
        self.seen_identities: list[str] = []

    def evaluate(self, identifier: str, feature_name: str) -> FlagEvaluation:
        self.seen_identities.append(identifier)
        index = int(identifier.rsplit("_", maxsplit=1)[-1])
        enabled = (index % 100) < int(self._enabled_ratio * 100)
        return FlagEvaluation(
            identifier=identifier,
            feature_name=feature_name,
            enabled=enabled,
            value=None,
            source_label="SYNTHETIC DATA",
        )


def test_plan_parsing(tmp_path: Path) -> None:
    """Parse and validate deterministic staged rollout plans."""
    plan = build_plan("checkout_v2", "production", project_id=7)
    assert [s.percentage for s in plan.steps] == [1, 10, 50, 100]
    assert all(s.max_error_rate == pytest.approx(0.02) for s in plan.steps)

    plan_path = tmp_path / "rollout.json"
    save_plan(plan, plan_path)
    loaded = load_plan(plan_path)
    assert loaded == plan

    with pytest.raises(PlanValidationError):
        build_plan("checkout_v2", "production", stages=(10, 10, 50))
    with pytest.raises(PlanValidationError):
        build_plan("checkout_v2", "production", stages=(0, 50))


def test_approval_gate(tmp_path: Path) -> None:
    """Require interactive APPROVE before any Flagsmith write and refuse non-interactive runs."""
    out = io.StringIO()
    with pytest.raises(NonInteractiveError):
        require_approval(
            "change",
            input_stream=io.StringIO("APPROVE\n"),
            output_stream=out,
            is_tty=False,
        )

    for bad_input in ("", "yes\n", "approve\n", "\n"):
        with pytest.raises(ApprovalDeniedError):
            require_approval(
                "change",
                input_stream=io.StringIO(bad_input),
                output_stream=io.StringIO(),
                is_tty=True,
            )

    client = _FakeFlagsmithClient()
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=io.StringIO("APPROVE\n"),
        output_stream=io.StringIO(),
        is_tty=True,
        audit_log_path=tmp_path / "audit.log",
    )
    plan = build_plan("checkout_v2", "production")
    change = writer.apply_stage(plan, 1)
    assert change.percentage == 1
    assert client.percentage_history == [1]
    assert (tmp_path / "audit.log").exists()


def test_staged_rollout(tmp_path: Path) -> None:
    """Advance rollout stages (1% -> 10% -> 50% -> 100%) when error rate stays <= 2%."""
    plan = build_plan("checkout_v2", "production", min_samples=20, max_error_rate=0.02)
    client = _FakeFlagsmithClient()
    approvals = io.StringIO("APPROVE\n" * 4)
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=approvals,
        output_stream=io.StringIO(),
        is_tty=True,
    )
    signals = _FakeSignalProvider(
        {
            1: StageMetrics(percentage=1, sample_count=200, error_count=1, enabled_count=2),
            10: StageMetrics(percentage=10, sample_count=200, error_count=1, enabled_count=20),
            50: StageMetrics(percentage=50, sample_count=200, error_count=2, enabled_count=100),
            100: StageMetrics(percentage=100, sample_count=200, error_count=1, enabled_count=200),
        }
    )
    audit_path = tmp_path / "audit.log"

    result = RolloutController(
        writer,
        signals,
        interval_seconds=0.0,
        audit_log_path=audit_path,
        output_stream=io.StringIO(),
    ).run(plan)

    assert result.final_status == "completed"
    assert result.rollback_applied is False
    assert client.percentage_history == [1, 10, 50, 100]
    assert [o.decision.action for o in result.outcomes] == [
        "advance",
        "advance",
        "advance",
        "complete",
    ]
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 4
    assert entries[0]["old_percentage"] == 1
    assert entries[0]["new_percentage"] == 10
    assert entries[-1]["old_percentage"] == 100
    assert entries[-1]["new_percentage"] == 100


def test_auto_rollback(tmp_path: Path) -> None:
    """Spike at 50% rolls back to previous safe 10%, and manual rollback sets 0%."""
    plan = build_plan("checkout_v2", "production", min_samples=20, max_error_rate=0.02)
    client = _FakeFlagsmithClient()
    # Approve 1%, 10%, 50%, then approve rollback to 10%, then approve manual rollback to 0%.
    approvals = io.StringIO("APPROVE\n" * 5)
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=approvals,
        output_stream=io.StringIO(),
        is_tty=True,
    )
    signals = _FakeSignalProvider(
        {
            1: StageMetrics(percentage=1, sample_count=200, error_count=0, enabled_count=2),
            10: StageMetrics(percentage=10, sample_count=200, error_count=1, enabled_count=20),
            50: StageMetrics(percentage=50, sample_count=200, error_count=20, enabled_count=100),
            100: StageMetrics(percentage=100, sample_count=200, error_count=0, enabled_count=200),
        }
    )
    out = io.StringIO()
    audit_path = tmp_path / "audit.log"

    result = RolloutController(
        writer,
        signals,
        interval_seconds=0.0,
        audit_log_path=audit_path,
        output_stream=out,
    ).run(plan)

    assert result.final_status == "rolled_back"
    assert result.rollback_applied is True
    assert result.rollback_to_percentage == 10
    assert client.percentage_history == [1, 10, 50, 10]
    assert result.outcomes[-1].decision.action == "rollback"
    assert "Rollback triggered at 50% -> 10%" in out.getvalue()

    execute_manual_rollback(
        writer,
        plan,
        old_percentage=10,
        reason="manual halt",
        audit_log_path=audit_path,
        output_stream=out,
    )
    assert client.percentage_history[-1] == 0
    assert client.disabled_calls == [("checkout_v2", 1)]


def test_stable_identities(tmp_path: Path) -> None:
    """Reuse persistent stable identities across % Split evaluations."""
    identities_path = tmp_path / "identities.json"
    first = load_or_create_identities(10, state_path=identities_path)
    second = load_or_create_identities(10, state_path=identities_path)
    assert first == second
    assert len(set(first)) == 10

    evaluator = _FakeEvaluator(enabled_ratio=0.5)
    metrics_stage_1 = simulate_stage_traffic(
        evaluator,
        "checkout_v2",
        first,
        percentage=10,
        enabled_error_rate=0.0,
    )
    metrics_stage_2 = simulate_stage_traffic(
        evaluator,
        "checkout_v2",
        second,
        percentage=50,
        enabled_error_rate=0.0,
    )
    assert evaluator.seen_identities == first + second
    assert metrics_stage_1.source_label == "SYNTHETIC DATA"
    assert metrics_stage_2.source_label == "SYNTHETIC DATA"


def test_report(tmp_path: Path) -> None:
    """Generate and persist rollout report with SYNTHETIC DATA labelling and no secrets."""
    plan = build_plan("checkout_v2", "production")
    client = _FakeFlagsmithClient()
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    signals = _FakeSignalProvider(
        {
            pct: StageMetrics(percentage=pct, sample_count=20, error_count=0, enabled_count=1)
            for pct in (1, 10, 50, 100)
        }
    )
    audit_path = tmp_path / "audit.log"
    result = RolloutController(
        writer,
        signals,
        audit_log_path=audit_path,
        output_stream=io.StringIO(),
    ).run(plan, is_synthetic=True)

    run_json = tmp_path / "run.json"
    save_run_result(result, run_json)
    reloaded = load_run_result(run_json)
    assert reloaded == result

    html_path = save_html_report(
        reloaded,
        tmp_path / "reports" / "rollout.html",
        audit_entries=load_audit_log(audit_path),
    )
    html_text = html_path.read_text(encoding="utf-8")
    assert "<style>" in html_text
    assert "<script" not in html_text.lower()
    assert "<svg" in html_text
    assert "prefers-color-scheme: dark" in html_text
    assert "SYNTHETIC DATA" in html_text

    markdown = render_markdown_report(reloaded)
    assert "# FlagPilot Rollout Report: `checkout_v2`" in markdown
    assert "SYNTHETIC DATA" in markdown
    assert "| 1% |" in markdown
    assert "| 100% |" in markdown
