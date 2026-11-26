"""Unit tests for static HTML/SVG rollout report generation from audit.log and last run."""

from __future__ import annotations

import io
from pathlib import Path

from flagpilot.apply import ApprovedFlagsmithWriter
from flagpilot.cli import main
from flagpilot.controller import RolloutController, execute_manual_rollback
from flagpilot.planner import build_plan
from flagpilot.report import load_run_result, render_markdown_report, save_run_result
from flagpilot.simulator import OfflineFakeEvaluator, TrafficSimulator, make_error_rate_fn
from flagpilot.types import Plan, RolloutChange, StageMetrics, Step


class _FakeRolloutBackend:
    def apply_stage(self, plan: Plan, percentage: int) -> RolloutChange:
        return RolloutChange(
            feature_name=plan.feature_name,
            environment=plan.environment,
            segment_name=plan.segment_name or "seg",
            percentage=percentage,
            enabled=percentage > 0,
        )

    def rollback(self, plan: Plan) -> RolloutChange:
        return RolloutChange(
            feature_name=plan.feature_name,
            environment=plan.environment,
            segment_name=plan.segment_name or "seg",
            percentage=0,
            enabled=False,
        )


class _FakeSpikeSignal:
    def collect(self, plan: Plan, step: Step) -> StageMetrics:
        if step.percentage == 50:
            return StageMetrics(
                percentage=50,
                sample_count=200,
                error_count=24,
                enabled_count=100,
                source_label="SYNTHETIC DATA",
                reported_error_rate=0.24,
            )
        return StageMetrics(
            percentage=step.percentage,
            sample_count=200,
            error_count=1,
            enabled_count=step.percentage * 2,
            source_label="SYNTHETIC DATA",
            reported_error_rate=0.005,
        )


def test_report_subcommand_generates_static_html_from_fake_run_and_audit_log(
    tmp_path: Path,
) -> None:
    """report subcommand reads audit.log and last run and writes reports/rollout.html."""
    plan = build_plan("checkout_v2", "Production", max_error_rate=0.02)
    writer = ApprovedFlagsmithWriter(
        _FakeRolloutBackend(),
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    audit_log = tmp_path / "audit.log"
    result = RolloutController(
        writer,
        _FakeSpikeSignal(),
        audit_log_path=audit_log,
        output_stream=io.StringIO(),
    ).run(plan, is_synthetic=True)
    assert result.final_status == "rolled_back"

    last_run_path = tmp_path / "reports" / "last_run.json"
    out_html = tmp_path / "reports" / "rollout.html"
    save_run_result(result, last_run_path)

    exit_code = main(
        [
            "report",
            "--input",
            str(last_run_path),
            "--audit-log",
            str(audit_log),
            "--out",
            str(out_html),
        ]
    )
    assert exit_code == 0
    assert out_html.exists()

    html_doc = out_html.read_text(encoding="utf-8")
    # Static self-contained HTML with inline CSS, zero scripts, light/dark mode
    assert "<style>" in html_doc
    assert "<script" not in html_doc.lower()
    assert "prefers-color-scheme: dark" in html_doc
    assert "Toggle Light / Dark" in html_doc

    # Banners
    assert "SYNTHETIC DATA" in html_doc
    assert "ROLLBACK TRIGGERED to 10%" in html_doc
    assert "24.00% exceeded threshold 2.00%" in html_doc

    # Plan summary, steps table with UTC timestamps, and inline SVG chart
    assert "checkout_v2" in html_doc
    assert "Production" in html_doc
    assert "1% → 10% → 50% → 100%" in html_doc
    assert "100 / 200 (50.0%)" in html_doc
    assert "<svg " in html_doc
    assert "<polyline" in html_doc
    assert " UTC" in html_doc
    assert "repeat(3, 1fr)" in html_doc
    assert "white-space: nowrap" in html_doc
    assert "Feature Health" in html_doc


def test_report_surfaces_feature_health_status_and_transition(tmp_path: Path) -> None:
    """Saved run JSON, Markdown, and HTML report show Feature Health status and recovery."""
    plan = build_plan("checkout_v2", "Development", max_error_rate=0.02)
    writer = ApprovedFlagsmithWriter(
        _FakeRolloutBackend(),
        input_stream=io.StringIO("APPROVE\n" * 4),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    audit_log = tmp_path / "audit.log"

    class _FakeNotifier:
        def notify_health(
            self,
            feature_name: str,
            environment: str,
            *,
            status: str,
            summary: str,
            description: str,
        ) -> bool:
            return True

    result = RolloutController(
        writer,
        _FakeSpikeSignal(),
        audit_log_path=audit_log,
        output_stream=io.StringIO(),
        health_notifier=_FakeNotifier(),
    ).run(plan, is_synthetic=True)
    assert result.health_status == "UNHEALTHY"
    assert result.health_webhook_sent is True

    last_run_path = tmp_path / "last_run.json"
    save_run_result(result, last_run_path)
    loaded = load_run_result(last_run_path)
    assert loaded.health_status == "UNHEALTHY"
    assert loaded.health_webhook_sent is True

    md = render_markdown_report(loaded)
    assert "UNHEALTHY" in md

    out_html = tmp_path / "rollout.html"
    # Simulate follow-up manual reset entry in audit.log marking feature HEALTHY again
    execute_manual_rollback(
        ApprovedFlagsmithWriter(
            _FakeRolloutBackend(),
            input_stream=io.StringIO("APPROVE\n"),
            output_stream=io.StringIO(),
            is_tty=True,
        ),
        plan,
        old_percentage=10,
        reason="demo cleanup",
        audit_log_path=audit_log,
        output_stream=io.StringIO(),
        health_notifier=_FakeNotifier(),
    )
    main(
        [
            "report",
            "--input",
            str(last_run_path),
            "--audit-log",
            str(audit_log),
            "--out",
            str(out_html),
        ]
    )
    html_doc = out_html.read_text(encoding="utf-8")
    assert "UNHEALTHY (webhook delivered) → HEALTHY (webhook delivered)" in html_doc
    assert "Flagsmith Feature Health UNHEALTHY" in html_doc
    assert "Flagsmith Feature Health HEALTHY" in html_doc


def test_report_includes_monotonicity_and_post_rollback_verification(tmp_path: Path) -> None:
    """last_run.json and rollout.html capture monotonic enrollment proof and 0/200 verification."""
    evaluator = OfflineFakeEvaluator()
    sim = TrafficSimulator(
        evaluator,
        user_count=200,
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=0.25,
            spike_threshold_pct=25.0,
        ),
    )

    class _Provider:
        def collect(self, plan: Plan, step: Step) -> StageMetrics:
            evaluator.set_percentage(step.percentage)
            return sim.run_round(plan.feature_name).to_stage_metrics(step.percentage)

        def verify_rollback(self, plan: Plan, target_percentage: int) -> StageMetrics:
            evaluator.set_percentage(target_percentage)
            return sim.run_round(plan.feature_name).to_stage_metrics(target_percentage)

    writer = ApprovedFlagsmithWriter(
        _FakeRolloutBackend(),
        input_stream=io.StringIO("APPROVE\n"),
        output_stream=io.StringIO(),
        is_tty=True,
    )
    audit_log = tmp_path / "audit.log"
    result = RolloutController(
        writer,
        _Provider(),
        audit_log_path=audit_log,
        output_stream=io.StringIO(),
        rollback_to_zero=True,
    ).run(build_plan("checkout_v2", "Development"), is_synthetic=True)

    run_path = tmp_path / "last_run.json"
    html_path = tmp_path / "rollout.html"
    save_run_result(result, run_path)
    reloaded = load_run_result(run_path)
    assert reloaded == result

    main(
        [
            "report",
            "--input",
            str(run_path),
            "--audit-log",
            str(audit_log),
            "--out",
            str(html_path),
        ]
    )
    html_doc = html_path.read_text(encoding="utf-8")
    assert "Monotonic Enrollment" in html_doc
    assert "Verified (100% retained across 2 step-up(s))" in html_doc
    assert "Verified: 0/200 exposed identities after rollback to 0%" in html_doc
    assert "Zero-Exposure Confirmed" in html_doc

