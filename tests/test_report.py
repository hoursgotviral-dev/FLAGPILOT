"""Unit tests for static HTML/SVG rollout report generation from audit.log and last run."""

from __future__ import annotations

import io
from pathlib import Path

from flagpilot.apply import ApprovedFlagsmithWriter
from flagpilot.cli import main
from flagpilot.controller import RolloutController
from flagpilot.planner import build_plan
from flagpilot.report import save_run_result
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
    assert "+00:00" in html_doc

