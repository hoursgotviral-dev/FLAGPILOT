"""Unit tests for RolloutController, audit logging, and CLI run/rollback subcommands."""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from flagpilot.apply import ApprovedFlagsmithWriter
from flagpilot.cli import main
from flagpilot.controller import RolloutController, execute_manual_rollback
from flagpilot.planner import build_plan
from flagpilot.simulator import OfflineFakeEvaluator, TrafficSimulator, make_error_rate_fn
from flagpilot.types import Plan, StageMetrics, Step


class _FakeFlagsmithClient:
    def __init__(self) -> None:
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
        return {"id": 1, "name": name, "default_enabled": default_enabled}

    def create_segment_with_percentage_split(self, name: str, percent: int) -> dict[str, Any]:
        self.segments[7] = {"id": 7, "name": name, "percent": percent}
        self.percentage_history.append(percent)
        return self.segments[7]

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
        self.overrides[(feature_name, segment_id)]["enabled"] = False
        return self.overrides[(feature_name, segment_id)]

    def get_state(
        self,
        feature_name: str,
        segment_id: int | None = None,
    ) -> dict[str, Any]:
        return {
            "feature_name": feature_name,
            "segment_override": self.overrides.get((feature_name, segment_id)),
        }


class _SimulatorSignalProvider:
    def __init__(self, evaluator: OfflineFakeEvaluator, sim: TrafficSimulator) -> None:
        self._evaluator = evaluator
        self._sim = sim

    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        self._evaluator.set_percentage(step.percentage)
        return self._sim.run_round(plan.feature_name).to_stage_metrics(step.percentage)


def test_controller_all_steps_pass_and_audit_log(tmp_path: Path) -> None:
    """Controller advances [1, 10, 50, 100], calls sleep_fn, and logs UTC audit entries."""
    client = _FakeFlagsmithClient()
    evaluator = OfflineFakeEvaluator()
    sim = TrafficSimulator(
        evaluator,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(baseline_rate=0.005),
    )
    slept: list[float] = []
    audit_log = tmp_path / "audit.log"
    out = io.StringIO()
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    controller = RolloutController(
        writer,
        _SimulatorSignalProvider(evaluator, sim),
        interval_seconds=0.5,
        sleep_fn=slept.append,
        audit_log_path=audit_log,
        output_stream=out,
    )

    plan = build_plan("checkout_v2", "production")
    result = controller.run(plan)

    assert result.final_status == "completed"
    assert client.percentage_history == [1, 10, 50, 100]
    assert slept == [0.5, 0.5, 0.5, 0.5]

    rows = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    for row in rows:
        parsed_ts = datetime.fromisoformat(row["timestamp"])
        assert parsed_ts.utcoffset() is not None
        assert "old_percentage" in row and "new_percentage" in row and "reason" in row


def test_controller_spike_at_50_rolls_back_to_10(tmp_path: Path) -> None:
    """Spike when exposure passes 25% (at 50% step) rolls back to previous safe 10%."""
    client = _FakeFlagsmithClient()
    evaluator = OfflineFakeEvaluator()
    sim = TrafficSimulator(
        evaluator,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=0.20,
            spike_threshold_pct=25.0,
        ),
    )
    audit_log = tmp_path / "audit.log"
    out = io.StringIO()
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    controller = RolloutController(
        writer,
        _SimulatorSignalProvider(evaluator, sim),
        interval_seconds=0.0,
        audit_log_path=audit_log,
        output_stream=out,
    )

    result = controller.run(build_plan("checkout_v2", "production"))

    assert result.final_status == "rolled_back"
    assert result.rollback_applied is True
    assert result.rollback_to_percentage == 10
    assert client.percentage_history == [1, 10, 50, 10]
    assert "50% -> 10%" in out.getvalue()

    rows = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "rollback"
    assert rows[-1]["old_percentage"] == 50
    assert rows[-1]["new_percentage"] == 10


def test_controller_spike_at_first_step_rolls_back_to_0(tmp_path: Path) -> None:
    """Spike at the first step (no prior safe percentage) rolls back to 0%."""
    client = _FakeFlagsmithClient()

    class _ImmediateSpikeProvider:
        def collect(self, plan: Plan, step: Step) -> StageMetrics:
            return StageMetrics(
                percentage=step.percentage,
                sample_count=200,
                error_count=30,
                enabled_count=2,
            )

    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=io.StringIO("APPROVE\nAPPROVE\n"),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    result = RolloutController(
        writer,
        _ImmediateSpikeProvider(),
        audit_log_path=tmp_path / "audit.log",
        output_stream=io.StringIO(),
    ).run(build_plan("checkout_v2", "production"))

    assert result.final_status == "rolled_back"
    assert result.rollback_to_percentage == 0
    assert client.percentage_history == [1, 0]
    assert client.disabled_calls == [("checkout_v2", 7)]


def test_manual_rollback_sets_zero_and_logs_audit(tmp_path: Path) -> None:
    """Manual rollback sets segment percentage to 0, disables override, and logs audit.log."""
    client = _FakeFlagsmithClient()
    plan = build_plan("checkout_v2", "production")
    writer = ApprovedFlagsmithWriter(
        client,
        input_stream=io.StringIO("APPROVE\nAPPROVE\n"),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    writer.apply_stage(plan, 10)

    audit_log = tmp_path / "audit.log"
    out = io.StringIO()
    change = execute_manual_rollback(
        writer,
        plan,
        old_percentage=10,
        reason="operator stop",
        audit_log_path=audit_log,
        output_stream=out,
    )
    assert change.percentage == 0
    assert client.percentage_history == [10, 0]
    assert client.disabled_calls == [("checkout_v2", 7)]
    entry = json.loads(audit_log.read_text(encoding="utf-8").strip())
    assert entry["old_percentage"] == 10
    assert entry["new_percentage"] == 0
    assert entry["reason"] == "operator stop"


def test_cli_run_and_rollback_subcommands_with_fake_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI run and rollback subcommands invoke controller and require APPROVE."""
    fake_client = _FakeFlagsmithClient()
    monkeypatch.setenv("FLAGSMITH_API_URL", "http://localhost:8000/api/v1")
    monkeypatch.setenv("FLAGSMITH_ADMIN_KEY", "secret-admin-key-not-logged")

    audit_log = tmp_path / "audit.log"
    run_out = tmp_path / "last_run.json"
    report_out = tmp_path / "rollout.html"
    identities_file = tmp_path / "identities.json"

    fake_stdin = io.StringIO("APPROVE\n" * 5)
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", fake_stdin)

    with patch("flagpilot.cli.FlagsmithManagementClient", return_value=fake_client):
        run_exit = main(
            [
                "run",
                "--feature",
                "checkout_v2",
                "--env",
                "production",
                "--offline-evaluator",
                "--interval",
                "0",
                "--identities-file",
                str(identities_file),
                "--audit-log",
                str(audit_log),
                "--run-out",
                str(run_out),
                "--report-out",
                str(report_out),
            ]
        )
        assert run_exit == 0
        assert fake_client.percentage_history == [1, 10, 50, 100]

        rb_exit = main(
            [
                "rollback",
                "--feature",
                "checkout_v2",
                "--env",
                "production",
                "--old-percentage",
                "100",
                "--reason",
                "post-demo reset",
                "--audit-log",
                str(audit_log),
            ]
        )
        assert rb_exit == 0

    assert fake_client.percentage_history == [1, 10, 50, 100, 0]
    audit_text = audit_log.read_text(encoding="utf-8")
    assert "secret-admin-key-not-logged" not in audit_text
    assert "post-demo reset" in audit_text
    assert "SYNTHETIC DATA" in report_out.read_text(encoding="utf-8")
