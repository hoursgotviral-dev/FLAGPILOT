"""Live end-to-end integration test against self-hosted Flagsmith at http://localhost:8000.

Skipped by default unless ``FLAGPILOT_LIVE_TEST=1`` is set so unit CI stays offline and fast.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from flagpilot.apply import ApprovedFlagsmithWriter
from flagpilot.controller import RolloutController
from flagpilot.fsapi import FlagsmithConfig, FlagsmithManagementClient
from flagpilot.planner import build_plan
from flagpilot.report import load_audit_log, save_html_report, save_run_result
from flagpilot.simulator import FlagsmithSDKEvaluator, TrafficSimulator, make_error_rate_fn
from flagpilot.types import Plan, StageMetrics, Step


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("FLAGPILOT_LIVE_TEST") != "1",
    reason="Set FLAGPILOT_LIVE_TEST=1 to run live Flagsmith integration tests.",
)
def test_live_end_to_end_rollout_monotonicity_and_rollback(tmp_path: Path) -> None:
    """Verify 1% -> 10% -> 50% -> fault spike -> 0% rollback against live localhost:8000."""
    cfg = FlagsmithConfig.from_env()
    assert cfg.api_url.rstrip("/").startswith("http://localhost:8000")
    assert cfg.health_webhook_url, (
        "FLAGSMITH_HEALTH_WEBHOOK_URL missing from .env; re-run python -m flagpilot.bootstrap."
    )

    client = FlagsmithManagementClient(cfg)
    project_id = client.resolve_project_id(cfg.project_id)
    evaluator = FlagsmithSDKEvaluator.from_env()

    plan = build_plan(
        "checkout_v2",
        "Development",
        stages=(1, 10, 50, 100),
        min_samples=20,
        max_error_rate=0.02,
        project_id=project_id,
    )
    segment_name = plan.segment_name or "flagpilot-checkout_v2-rollout"

    simulator = TrafficSimulator(
        evaluator,
        user_count=200,
        state_path=tmp_path / "identities.json",
        seed=42,
        baseline_rate=0.005,
        error_rate_fn=make_error_rate_fn(
            baseline_rate=0.005,
            spike_rate=0.25,
            spike_threshold_pct=25.0,
        ),
    )

    exposed_by_stage: dict[int, set[str]] = {}
    persisted_percentages: list[str] = []

    def _get_live_segment_info() -> tuple[int, str]:
        segments = client._extract_results(
            client._request("GET", f"projects/{project_id}/segments/")
        )
        match = next((s for s in segments if s.get("name") == segment_name), None)
        assert match is not None
        seg_id = int(match["id"])
        detail = client._request("GET", f"projects/{project_id}/segments/{seg_id}/")
        cond = detail["rules"][0]["conditions"][0]
        assert cond["operator"] == "PERCENTAGE_SPLIT"
        return seg_id, str(cond["value"])

    class _LiveSignalProvider:
        def collect(self, active_plan: Plan, step: Step) -> StageMetrics:
            _, live_pct = _get_live_segment_info()
            assert live_pct == str(step.percentage)
            persisted_percentages.append(live_pct)

            round_result = simulator.run_round(active_plan.feature_name)
            exposed_by_stage[step.percentage] = set(round_result.exposed_identities)
            return round_result.to_stage_metrics(step.percentage)

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
        _LiveSignalProvider(),
        interval_seconds=0.0,
        audit_log_path=audit_log,
        output_stream=out,
        rollback_to_zero=True,
        health_notifier=client,
    )

    try:
        result = controller.run(plan, is_synthetic=True)

        assert persisted_percentages == ["1", "10", "50"]
        assert result.final_status == "rolled_back"
        assert result.rollback_applied is True
        assert result.rollback_to_percentage == 0

        # Monotonic enrollment across persistent identities (1% <= 10% <= 50%)
        assert exposed_by_stage[1].issubset(exposed_by_stage[10])
        assert exposed_by_stage[10].issubset(exposed_by_stage[50])
        assert len(exposed_by_stage[50]) > len(exposed_by_stage[10]) >= len(exposed_by_stage[1])

        # Verify rollback persisted in live Flagsmith API and exposure is 0%
        seg_id, live_pct_after = _get_live_segment_info()
        assert live_pct_after == "0"
        state = client.get_state("checkout_v2", segment_id=seg_id)
        assert state["default_state"]["enabled"] is False
        assert state["segment_override"]["enabled"] is False

        post_rollback_round = simulator.run_round("checkout_v2")
        assert post_rollback_round.exposed_count == 0

        # Verify Flagsmith Feature Health flipped to UNHEALTHY on rollback, then reset to HEALTHY
        feature_id = client._get_feature_id(project_id, "checkout_v2")
        health_events = [
            e for e in client.get_feature_health_events(project_id) if e.get("feature") == feature_id
        ]
        assert health_events, "Expected Flagsmith Feature Health event after rollback webhook"
        latest_unhealthy = max(health_events, key=lambda e: (str(e["created_at"]), int(e["id"])))
        assert latest_unhealthy["type"] == "UNHEALTHY"
        assert latest_unhealthy["provider_name"] == "Grafana"

        assert (
            client.notify_health(
                "checkout_v2",
                "Development",
                status="resolved",
                summary="Live smoke post-rollback cleanup",
                description="Resetting checkout_v2 health status after live smoke verification.",
            )
            is True
        )
        resolved_events = [
            e for e in client.get_feature_health_events(project_id) if e.get("feature") == feature_id
        ]
        latest_healthy = max(resolved_events, key=lambda e: (str(e["created_at"]), int(e["id"])))
        assert latest_healthy["type"] == "HEALTHY"

        # Verify audit.log and HTML report artifacts without leaking secrets
        audit_text = audit_log.read_text(encoding="utf-8")
        assert cfg.admin_key not in audit_text
        entries = [json.loads(line) for line in audit_text.splitlines() if line.strip()]
        assert [e["action"] for e in entries] == ["advance", "advance", "rollback"]
        assert entries[-1]["old_percentage"] == 50
        assert entries[-1]["new_percentage"] == 0

        last_run_path = tmp_path / "reports" / "last_run.json"
        html_path = tmp_path / "reports" / "rollout.html"
        save_run_result(result, last_run_path)
        save_html_report(result, html_path, audit_entries=load_audit_log(audit_log))

        html_text = html_path.read_text(encoding="utf-8")
        assert cfg.admin_key not in html_text
        assert "SYNTHETIC DATA" in html_text
        assert "ROLLBACK TRIGGERED to 0%" in html_text
        assert "<svg " in html_text
    finally:
        evaluator.close()

