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

    # Subsequent evaluation tick confirms 0 enabled identities after rollback to 0%.
    eval_after = OfflineFakeEvaluator(rollout_percentage=client.percentage_history[-1])
    post_round = TrafficSimulator(eval_after, user_count=200, seed=42).run_round("checkout_v2")
    assert post_round.exposed_count == 0


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


class _RecordingHealthNotifier:
    def __init__(self, *, should_raise: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self.should_raise = should_raise

    def notify_health(
        self,
        feature_name: str,
        environment: str,
        *,
        status: str,
        summary: str,
        description: str,
    ) -> bool:
        self.calls.append(
            {
                "feature_name": feature_name,
                "environment": environment,
                "status": status,
                "summary": summary,
                "description": description,
            }
        )
        if self.should_raise:
            raise RuntimeError("webhook exploded")
        return True


def test_controller_and_manual_rollback_emit_health_notifications(tmp_path: Path) -> None:
    """Rollback emits 'firing' (fail-safe); clean completion and manual reset emit 'resolved'."""
    # 1. Error-rate spike rollback -> 'firing' (and raising notifier does not abort rollback)
    client_fail = _FakeFlagsmithClient()
    eval_fail = OfflineFakeEvaluator()
    sim_fail = TrafficSimulator(
        eval_fail,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=0.25,
            spike_threshold_pct=25.0,
        ),
    )
    firing_notifier = _RecordingHealthNotifier(should_raise=True)
    writer_fail = ApprovedFlagsmithWriter(
        client_fail,
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    ctrl_fail = RolloutController(
        writer_fail,
        _SimulatorSignalProvider(eval_fail, sim_fail),
        interval_seconds=0.0,
        audit_log_path=tmp_path / "audit1.log",
        output_stream=io.StringIO(),
        rollback_to_zero=True,
        health_notifier=firing_notifier,
    )
    res_fail = ctrl_fail.run(build_plan("checkout_v2", "Development"))
    assert res_fail.final_status == "rolled_back"
    assert res_fail.rollback_to_percentage == 0
    assert res_fail.health_status == "UNHEALTHY"
    assert res_fail.health_webhook_sent is False
    assert len(firing_notifier.calls) == 1
    assert firing_notifier.calls[0]["status"] == "firing"
    assert firing_notifier.calls[0]["feature_name"] == "checkout_v2"
    assert firing_notifier.calls[0]["environment"] == "Development"
    assert "2.00%" in firing_notifier.calls[0]["description"]
    fail_rows = [json.loads(line) for line in (tmp_path / "audit1.log").read_text().splitlines()]
    assert fail_rows[-1]["action"] == "feature_health"
    assert fail_rows[-1]["health_status"] == "UNHEALTHY"
    assert fail_rows[-1]["webhook_sent"] is False

    # 2. Clean rollout completion -> 'resolved'
    client_ok = _FakeFlagsmithClient()
    eval_ok = OfflineFakeEvaluator()
    sim_ok = TrafficSimulator(
        eval_ok,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(baseline_rate=0.005),
    )
    resolved_notifier = _RecordingHealthNotifier()
    writer_ok = ApprovedFlagsmithWriter(
        client_ok,
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    ctrl_ok = RolloutController(
        writer_ok,
        _SimulatorSignalProvider(eval_ok, sim_ok),
        interval_seconds=0.0,
        audit_log_path=tmp_path / "audit2.log",
        output_stream=io.StringIO(),
        health_notifier=resolved_notifier,
    )
    res_ok = ctrl_ok.run(build_plan("checkout_v2", "Development"))
    assert res_ok.final_status == "completed"
    assert res_ok.health_status == "HEALTHY"
    assert res_ok.health_webhook_sent is True
    assert [c["status"] for c in resolved_notifier.calls] == ["resolved"]
    ok_rows = [json.loads(line) for line in (tmp_path / "audit2.log").read_text().splitlines()]
    assert ok_rows[-1]["action"] == "feature_health"
    assert ok_rows[-1]["health_status"] == "HEALTHY"
    assert ok_rows[-1]["webhook_sent"] is True

    # 3. Manual rollback/reset -> 'resolved'
    manual_notifier = _RecordingHealthNotifier()
    manual_out = io.StringIO()
    writer_manual = ApprovedFlagsmithWriter(
        client_ok,
        input_stream=io.StringIO("APPROVE\n"),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    execute_manual_rollback(
        writer_manual,
        build_plan("checkout_v2", "Development"),
        old_percentage=100,
        reason="manual reset",
        audit_log_path=tmp_path / "audit3.log",
        output_stream=manual_out,
        health_notifier=manual_notifier,
    )
    assert [c["status"] for c in manual_notifier.calls] == ["resolved"]
    assert "Feature Health: HEALTHY (webhook delivered)" in manual_out.getvalue()
    manual_rows = [json.loads(line) for line in (tmp_path / "audit3.log").read_text().splitlines()]
    assert manual_rows[-1]["action"] == "feature_health"
    assert manual_rows[-1]["health_status"] == "HEALTHY"


def test_controller_upfront_approve_non_interactive_abort_and_step_approve_opt_in(
    tmp_path: Path,
) -> None:
    """Default run requires 1 upfront APPROVE (no mid-incident prompts); --step-approve opts into per-step prompts."""
    # 1. Non-interactive TTY=False aborts before touching Flagsmith
    client_abort = _FakeFlagsmithClient()
    eval_abort = OfflineFakeEvaluator()
    sim_abort = TrafficSimulator(eval_abort, user_count=200, seed=42)
    out_abort = io.StringIO()
    res_abort = RolloutController(
        ApprovedFlagsmithWriter(
            client_abort,
            input_stream=io.StringIO("APPROVE\n"),
            output_stream=out_abort,
            is_tty=False,
        ),
        _SimulatorSignalProvider(eval_abort, sim_abort),
        audit_log_path=tmp_path / "abort.log",
        output_stream=out_abort,
        rollback_to_zero=True,
    ).run(build_plan("checkout_v2", "Development"))
    assert res_abort.final_status == "aborted"
    assert client_abort.percentage_history == []

    # 2. Single upfront APPROVE runs 1->10->50 + automatic 0% rollback without pausing mid-incident
    client_upfront = _FakeFlagsmithClient()
    eval_upfront = OfflineFakeEvaluator()
    sim_upfront = TrafficSimulator(
        eval_upfront,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=0.25,
            spike_threshold_pct=25.0,
        ),
    )

    class _VerifyingSignalProvider(_SimulatorSignalProvider):
        def verify_rollback(self, plan: Plan, target_percentage: int) -> StageMetrics:
            self._evaluator.set_percentage(target_percentage)
            return self._sim.run_round(plan.feature_name).to_stage_metrics(target_percentage)

    gate_out = io.StringIO()
    ctrl_out = io.StringIO()
    audit_log = tmp_path / "upfront.log"
    res_upfront = RolloutController(
        ApprovedFlagsmithWriter(
            client_upfront,
            input_stream=io.StringIO("APPROVE\n"),
            output_stream=gate_out,
            is_tty=True,
        ),
        _VerifyingSignalProvider(eval_upfront, sim_upfront),
        audit_log_path=audit_log,
        output_stream=ctrl_out,
        rollback_to_zero=True,
    ).run(build_plan("checkout_v2", "Development"))

    assert res_upfront.final_status == "rolled_back"
    assert res_upfront.rollback_to_percentage == 0
    assert client_upfront.percentage_history == [1, 10, 50, 0]
    assert "Rollout schedule: 1% -> 10% -> 50% -> 100%" in gate_out.getvalue()
    assert "auto-rollback to 0%" in gate_out.getvalue()

    # Monotonic enrollment proof recorded across step-ups (1%->10%, 10%->50%)
    assert res_upfront.outcomes[0].retained_from_previous_stage is None
    m_1_10 = res_upfront.outcomes[1].retained_from_previous_stage
    m_10_50 = res_upfront.outcomes[2].retained_from_previous_stage
    assert m_1_10 is not None and m_1_10.monotonic is True
    assert m_1_10.retained_count == m_1_10.previous_exposed_count
    assert m_10_50 is not None and m_10_50.monotonic is True
    assert m_10_50.retained_count == m_10_50.previous_exposed_count
    assert "10% users stayed enrolled at 50%" in m_10_50.summary
    assert f"Monotonicity check: {m_10_50.summary}" in ctrl_out.getvalue()

    # Post-rollback 0/200 verification tick recorded
    verif = res_upfront.post_rollback_verification
    assert verif is not None
    assert verif.target_percentage == 0
    assert verif.exposed_count == 0
    assert verif.sample_count == 200
    assert verif.verified is True
    assert verif.summary == "0/200 exposed identities after rollback to 0%"
    ctrl_text = ctrl_out.getvalue()
    assert "\x1b[" not in ctrl_text
    assert "ROLLBACK EXECUTED → 0%" in ctrl_text
    assert "VERIFIED 0/200 EXPOSED" in ctrl_text

    # Simulated TTY stream exercises Rich Live table rendering cleanly
    client_tty = _FakeFlagsmithClient()
    eval_tty = OfflineFakeEvaluator()
    sim_tty = TrafficSimulator(
        eval_tty,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=0.25,
            spike_threshold_pct=25.0,
        ),
    )
    tty_out = io.StringIO()
    tty_out.isatty = lambda: True  # type: ignore[method-assign]
    res_tty = RolloutController(
        ApprovedFlagsmithWriter(
            client_tty,
            input_stream=io.StringIO("APPROVE\n"),
            output_stream=io.StringIO(),
            is_tty=True,
        ),
        _VerifyingSignalProvider(eval_tty, sim_tty),
        audit_log_path=tmp_path / "tty.log",
        output_stream=tty_out,
        rollback_to_zero=True,
    ).run(build_plan("checkout_v2", "Development"))
    assert res_tty.final_status == "rolled_back"
    assert "ROLLBACK EXECUTED" in tty_out.getvalue()
    assert "VERIFIED 0/200 EXPOSED" in tty_out.getvalue()

    rows = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert rows[1]["retained_from_previous_stage"]["monotonic"] is True
    assert rows[-1]["action"] == "post_rollback_verification"
    assert rows[-1]["verified"] is True

    # 3. Opt-in step_approve=True prompts before every stage (1 APPROVE pauses at stage 2)
    client_step = _FakeFlagsmithClient()
    eval_step = OfflineFakeEvaluator()
    sim_step = TrafficSimulator(eval_step, user_count=200, seed=42)
    res_step = RolloutController(
        ApprovedFlagsmithWriter(
            client_step,
            input_stream=io.StringIO("APPROVE\n"),
            output_stream=io.StringIO(),
            is_tty=True,
        ),
        _SimulatorSignalProvider(eval_step, sim_step),
        audit_log_path=tmp_path / "step.log",
        output_stream=io.StringIO(),
        step_approve=True,
    ).run(build_plan("checkout_v2", "Development"))
    assert res_step.final_status == "paused"
    assert client_step.percentage_history == [1]
